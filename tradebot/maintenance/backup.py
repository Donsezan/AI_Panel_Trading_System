"""Consistent database copies, taken while the bot runs.

`VACUUM INTO` produces one defragmented file that already contains everything in the WAL, holding
only a read transaction — a plain file copy would miss the WAL and a `.backup` loop would need its
own retry policy. The output is an ordinary SQLite database: recovery is copying it back over
`data/<mode>.db` with the process stopped (spec §4.1).

The copy is written to a `.tmp` sibling of its final name and only renamed onto that name once
`VACUUM INTO` has finished, so a process killed mid-copy never leaves a truncated file under the
name a restore trusts — the final name is either absent or a complete backup, never partial
(spec §2 D2/D4).

Failure semantics: anything that prevents a complete copy raises `BackupError`, and no caller
swallows it — including a plain `OSError` from the filesystem itself (a missing volume, a
permissions problem, a destination whose parent path is not a directory). The pre-migration hook
refuses to upgrade; the daily tick reports a maintenance failure and does not go on to compact
anything.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Engine

from tradebot.core.clock import Clock
from tradebot.core.errors import FailClosedError
from tradebot.core.logging import get_logger

logger = get_logger(__name__)

#: Spare room demanded beyond the copy itself. A full volume stops the event log accepting an
#: order intent (PLAN §1.4), so the backup must never be the write that fills it. Integer bytes:
#: no float ever enters this arithmetic.
HEADROOM_BYTES: Final[int] = 200 * 1024 * 1024


class BackupError(FailClosedError):
    """A backup that could not be taken. Refused upward, never logged and forgotten."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Where the copy landed and how big it is — what a report and a notification quote."""

    path: Path
    size_bytes: int


def backup_name(mode: str, at: datetime, *, revision: str = "") -> str:
    """`sim-20260820T040000Z.db`, or `sim-pre-<revision>-...` when a migration is about to run."""
    marker = f"-pre-{revision}" if revision else ""
    return f"{mode}{marker}-{at.strftime('%Y%m%dT%H%M%SZ')}.db"


def free_bytes(directory: Path) -> int:
    """Free space on the volume holding `directory`. A seam, so a test can starve it."""
    return shutil.disk_usage(directory).free


def source_path(engine: Engine) -> Path:
    """The file behind an engine. Raises for the in-memory database the test suite runs on."""
    database = engine.url.database
    if not database or database == ":memory:":
        raise BackupError("an in-memory database has nothing to back up")
    return Path(database)


def take_backup(
    engine: Engine,
    destination: Path,
    *,
    mode: str,
    clock: Clock,
    revision: str = "",
    # Not bound to `free_bytes` at definition time: a later test that monkeypatches the module
    # attribute must reach the stub, not a reference captured when this file was imported.
    probe: Callable[[Path], int] | None = None,
) -> BackupResult:
    """Copy the database into `destination`, without ever leaving a partial file at the final name.

    `mkdir` runs before the space check, because `shutil.disk_usage` needs a directory that
    already exists to probe it — so a refusal on a brand-new destination can still leave an
    empty directory behind. What is guaranteed is narrower and matters more: the backup's final
    name is either absent or a complete copy, never truncated and never overwritten.
    """
    probe = probe or free_bytes
    source = source_path(engine)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"could not create {destination}: {exc}") from exc

    required = required_bytes(source)
    try:
        available = probe(destination)
    except OSError as exc:
        raise BackupError(f"could not read free space on {destination}: {exc}") from exc
    if available < required:
        raise BackupError(
            f"{destination} has {available} bytes free; a backup of {source.name} needs "
            f"{required}. Nothing was written."
        )

    target = destination / backup_name(mode, clock.now(), revision=revision)
    if target.exists():
        raise BackupError(f"{target} already exists; refusing to overwrite a backup")

    # Written to a `.tmp` sibling and renamed onto the final name only once the copy is whole —
    # see the module docstring. A `.tmp` left by an interrupted prior run is not a backup, so it
    # is cleared rather than fought over.
    tmp_target = target.with_name(target.name + ".tmp")
    try:
        if tmp_target.exists():
            tmp_target.unlink()
    except OSError as exc:
        raise BackupError(f"could not clear stale {tmp_target}: {exc}") from exc

    # VACUUM cannot run inside a transaction, and SQLAlchemy opens one implicitly. The literal is
    # escaped rather than bound because SQLite takes no parameter in this position.
    literal = str(tmp_target).replace("'", "''")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        try:
            connection.exec_driver_sql(f"VACUUM INTO '{literal}'")
        except Exception as exc:  # re-raised classified below, never swallowed
            raise BackupError(f"could not write {target} (via {tmp_target}): {exc}") from exc

    try:
        tmp_target.replace(target)
    except OSError as exc:
        raise BackupError(f"could not finalize {target}: {exc}") from exc

    result = BackupResult(path=target, size_bytes=target.stat().st_size)
    logger.info(
        "database backed up",
        extra={"path": str(target), "bytes": result.size_bytes, "revision": revision},
    )
    return result


def required_bytes(source: Path) -> int:
    """The copy, a fifth again, and the headroom — all integer arithmetic (spec §4.4).

    Public because it is the one piece of this module worth asserting on directly: whether the
    WAL is counted cannot be observed from the outside without corrupting a live database.

    Raises `BackupError`, not a raw `OSError`, so a caller never has to catch both.
    """
    try:
        live = source.stat().st_size
        wal = source.with_name(source.name + "-wal")
        if wal.exists():
            live += wal.stat().st_size
    except OSError as exc:
        raise BackupError(f"could not read size of {source}: {exc}") from exc
    return live + live // 5 + HEADROOM_BYTES
