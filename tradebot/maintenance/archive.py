"""One gzip file per day, written once and verified by hash.

A day is archived only after it is entirely past the retention horizon, so the set of events in it
can never change afterwards. That immutability is what lets a whole-file hash mean something, and
it is why there is no partial-rewrite path here to get wrong: a file that already exists is
verified and left alone, never appended to.

The file is one JSON object per line — `seq`, `event_id`, `ts`, `type`, `payload` — with `payload`
the original payload parsed back from the row's canonical JSON. A round trip through this file
therefore reproduces exactly what the row held, which is what makes it a *copy* rather than a
summary, and what makes it the only thing standing behind compaction until it is deleted.

Failure semantics: anything that leaves the file absent, unreadable, or not matching what was just
written raises `ArchiveError`, and the caller compacts nothing for that day. The database still
holds every payload at that point, so a failed archive costs a retry, never data. `delete_aged` is
the one irreversible act in this module and is deliberately the narrowest thing in it.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select

from tradebot.core.errors import FailClosedError
from tradebot.core.logging import get_logger
from tradebot.persistence.schema import events

logger = get_logger(__name__)

#: The suffix a completed day file carries. A `.tmp` sibling is an interrupted write and is never
#: a day file — `delete_aged` cannot match one, and `archive_day` clears it rather than trusting it.
ARCHIVE_SUFFIX = ".jsonl.gz"


class ArchiveError(FailClosedError):
    """An archive that was not written, or not verifiable once it was."""


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """What a day's archive turned out to be. `rows == 0` means no file was written."""

    path: Path
    rows: int
    sha256: str

    @property
    def written(self) -> bool:
        """Whether there is a verified file behind this day. Nothing is compacted without one."""
        return self.rows > 0


def archive_path(root: Path, mode: str, day: date) -> Path:
    """`<root>/<mode>/2026-07/2026-07-19.jsonl.gz` — grouped by month so a year is browsable."""
    return root / mode / f"{day:%Y-%m}" / f"{day:%Y-%m-%d}{ARCHIVE_SUFFIX}"


def read_archive(path: Path) -> list[dict[str, Any]]:
    """Every line of a day file, parsed. The recovery path, and what verification re-reads."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def archive_day(engine: Engine, root: Path, *, mode: str, day: date) -> ArchiveResult:
    """Write one day's events to an immutable file, verify it, and report what it holds.

    A file that already exists is **verified, not rewritten**. The day is past the horizon and so
    cannot have gained events, and rewriting it would destroy the only copy of a payload the
    database has already compacted away.
    """
    target = archive_path(root, mode, day)
    rows = _rows_for(engine, day)
    if not rows:
        return ArchiveResult(path=target, rows=0, sha256="")

    if target.exists():
        return ArchiveResult(path=target, rows=len(rows), sha256=_verify(target, len(rows)))

    digest = _write(target, rows)
    logger.info(
        "day archived",
        extra={"path": str(target), "rows": len(rows), "sha256": digest, "mode": mode},
    )
    return ArchiveResult(path=target, rows=len(rows), sha256=digest)


def delete_aged(root: Path, mode: str, *, before: date) -> tuple[list[Path], list[str]]:
    """Remove this mode's day files older than `before`. Irreversible, and deliberately narrow.

    Matches only `<root>/<mode>/YYYY-MM/YYYY-MM-DD.jsonl.gz`, and decides by **parsing the name**
    rather than by reading a stat time: a file copied between machines keeps its meaning, and a
    `.tmp` from an interrupted write can never be mistaken for a completed day. Nothing outside
    this mode's archive directory is ever considered — not the database, not a backup.

    A file that cannot be removed is reported and skipped rather than raising: one locked file must
    not stop the rest of a pass, and the next pass tries again (spec §6.4).
    """
    directory = root / mode
    if not directory.exists():
        return [], []

    removed: list[Path] = []
    failures: list[str] = []
    for path in sorted(directory.glob(f"*/*{ARCHIVE_SUFFIX}")):
        day = _day_of(path)
        if day is None or day >= before:
            continue
        try:
            path.unlink()
        except OSError as exc:
            failures.append(f"{path}: {exc}")
            continue
        removed.append(path)

    if removed:
        # Warning rather than info: this is the only step in the whole piece that destroys
        # something, and D1a makes it unrecoverable.
        logger.warning(
            "archives deleted",
            extra={"count": len(removed), "before": before.isoformat(), "mode": mode},
        )
    return removed, failures


def _day_of(path: Path) -> date | None:
    """The day a file name claims, or `None` if it does not name one."""
    try:
        return date.fromisoformat(path.name.removesuffix(ARCHIVE_SUFFIX))
    except ValueError:
        return None


def _write(target: Path, rows: list[dict[str, Any]]) -> str:
    """Write, fsync, publish atomically, then verify what actually landed.

    fsync inside the open block, on the handle that did the writing: the bytes have to be on the
    device before the rename publishes the name, or a power cut leaves a complete-looking file
    holding nothing.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        with temporary.open("wb") as raw, gzip.open(raw, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            handle.flush()
            raw.flush()
            os.fsync(raw.fileno())
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ArchiveError(f"could not write {target}: {exc}") from exc
    return _verify(target, len(rows))


def _rows_for(engine: Engine, day: date) -> list[dict[str, Any]]:
    """The day's events as plain dicts, oldest first.

    `payload` is parsed back from the stored canonical JSON rather than re-derived, so the archive
    holds what the row holds. The window is half-open — `[midnight, next midnight)` — and compares
    against `ts` directly: instants are stored as ISO-8601 UTC text, which sorts lexicographically
    in the same order it sorts chronologically.
    """
    start = datetime.combine(day, time.min, tzinfo=UTC)
    query = (
        select(events)
        .where(events.c.ts >= start, events.c.ts < start + timedelta(days=1))
        .order_by(events.c.seq)
    )
    with engine.connect() as connection:
        return [
            {
                "seq": row.seq,
                "event_id": row.event_id,
                "ts": row.ts.isoformat(),
                "type": row.type,
                "payload": json.loads(row.payload_json),
            }
            for row in connection.execute(query)
        ]


def _verify(path: Path, expected_rows: int) -> str:
    """Re-read what is on disk. A file that cannot be read back is not an archive.

    The row count is checked as well as readability: gzip's own CRC catches corruption but not
    truncation at a record boundary, and a short file would silently license compacting events it
    does not contain.
    """
    try:
        lines = read_archive(path)
    except (gzip.BadGzipFile, EOFError) as exc:
        # BadGzipFile subclasses OSError, so this arm must precede the broader one below.
        raise ArchiveError(f"{path} is not a readable archive: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ArchiveError(f"{path} is not a readable archive: {exc}") from exc
    except OSError as exc:
        raise ArchiveError(f"{path} could not be re-read: {exc}") from exc
    if len(lines) != expected_rows:
        raise ArchiveError(f"{path} holds {len(lines)} rows; the day has {expected_rows}")
    return hashlib.sha256(path.read_bytes()).hexdigest()
