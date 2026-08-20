"""Consistent database copies, taken while the bot runs.

`VACUUM INTO` produces one defragmented file that already contains everything in the WAL, holding
only a read transaction — a plain file copy would miss the WAL and a `.backup` loop would need its
own retry policy. The output is an ordinary SQLite database: recovery is copying it back over
`data/<mode>.db` with the process stopped (spec §4.1).

The copy is written to a `.tmp` sibling of its final name and published onto that name only once
`VACUUM INTO` has finished, so a process killed mid-copy never leaves a truncated file under the
name a restore trusts — the final name is either absent or a complete backup, never partial
(spec §2 D2/D4). Publishing is a hard link, not `Path.replace` (POSIX `rename(2)`), because
`replace` overwrites an existing file silently — a second call for the same mode, instant and
revision finishing during this one's copy must be refused, not let destroy the first (D4: nothing
this module writes is ever deleted or overwritten by it). Every failure past the point the `.tmp`
is created also unlinks it, so a persistent failure does not accumulate files that eat the very
headroom `HEADROOM_BYTES` reserves.

Failure semantics: anything that prevents a complete copy raises `BackupError`, and no caller
swallows it — including a plain `OSError` from the filesystem itself (a missing volume, a
permissions problem, a destination whose parent path is not a directory). The pre-migration hook
refuses to upgrade; the daily tick reports a maintenance failure and does not go on to compact
anything.
"""

from __future__ import annotations

import os
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


def _publish(tmp_target: Path, target: Path) -> None:
    """Move the finished copy onto its final name without a window where it could be overwritten.

    `Path.replace` (POSIX `rename(2)`) succeeds silently over an existing file, so a second call
    for the same mode, instant and revision finishing during *this* one's copy would destroy a
    completed backup — the early `target.exists()` check in `take_backup` cannot close that
    window, since it runs before `VACUUM INTO`, not after (spec D4). `os.link` is the OS's own
    fail-if-the-name-is-taken primitive on both platforms (POSIX `link(2)`, Win32
    `CreateHardLink`), so the existence check and the publish are one atomic syscall rather than
    the check-then-act pair `exists()` + `replace()` would be.
    """
    os.link(tmp_target, target)
    tmp_target.unlink()


def _discard(tmp_target: Path) -> None:
    """Remove a `.tmp` left by a failed attempt, on every failure path past its creation.

    Before this, the only cleanup was the *next* call's stale-`.tmp` clear, which only fires for
    an identical mode+instant+revision — so a persistent failure (a full volume, say) would
    accumulate orphaned `.tmp` files that eat exactly the headroom `HEADROOM_BYTES` exists to
    protect. Best-effort: the failure already being raised by the caller is the one that matters,
    so a second failure while cleaning up is logged, not raised — it must never mask the first,
    which is what a bare `except: pass` would risk turning this into if it re-raised.
    """
    try:
        tmp_target.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "could not remove temp file after a failed backup",
            extra={"path": str(tmp_target), "error": str(exc)},
        )


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

    # Written to a `.tmp` sibling and published onto the final name only once the copy is whole —
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
            _discard(tmp_target)
            raise BackupError(f"could not write {target} (via {tmp_target}): {exc}") from exc

    try:
        _publish(tmp_target, target)
    except FileExistsError as exc:
        _discard(tmp_target)
        raise BackupError(f"{target} already exists; refusing to overwrite a backup") from exc
    except OSError as exc:
        _discard(tmp_target)
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
