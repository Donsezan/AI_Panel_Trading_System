# Maintenance Piece B — Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Age the two heavy event payloads out of the hot database — archived to disk at 30 days, deleted at 90 — under windows an operator can change, without ever deleting an event row or altering what a projection rebuild produces.

**Architecture:** Three new modules in the `tradebot/maintenance/` package created by Piece A. `archive.py` writes one immutable gzip file per day and verifies it by hash; `compaction.py` rewrites payloads through a two-entry registry, but only for a day already archived and verified; `service.py` orders the passes, deletes aged archives last, and records one `MAINTENANCE_RAN` event that also answers "is a run due". The windows come from a new versioned `maintenance` config document, read fresh at every pass.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 Core, Alembic, pydantic v2, gzip/jsonlines, pytest, ruff, mypy.

**Spec:** [docs/superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md](../specs/2026-08-20-retention-backup-and-notifications-design.md) — §3 in full, §6, and §2 D1, D1a, D9.

**Depends on:** Piece A (`tradebot/maintenance/backup.py`, `take_backup`). Do not start this piece until Piece A is merged — compaction is destructive and the backup is what makes it recoverable.

## Global Constraints

- **No event row is ever deleted.** This piece only ever `UPDATE`s `events.payload_json` and unlinks files under the archive directory. A `DELETE FROM events` anywhere in this piece is a defect, not an optimisation.
- **Nothing is compacted that is not already in a verified archive.** The ordering in `service.py` is the safety property; do not reorder it for speed.
- **The invariant:** a projection rebuild after compaction must be identical to one before it. Task 4 asserts it; if it fails, stop and re-read spec §3.3 rather than adjusting the assertion.
- **Money is `Decimal`; sizes and counts are `int`.** No float anywhere in this piece.
- **Time is UTC-aware `datetime` from an injected `Clock`.** The daily tick paces on `Clock.sleep`, never `asyncio.sleep` — this is domain time (spec §6.2).
- **Errors are classified.** Archive and compaction failures raise `FailClosedError` subclasses and are caught only at the service boundary, where they become a recorded failure.
- **Line length 100**, ruff format, full annotations, `from __future__ import annotations`.
- Verification: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_*.py -q`, then `.\check.ps1` before the final commit.

---

### Task 1: The `maintenance` config document

**Files:**
- Modify: `tradebot/core/enums.py:170-184` (`ConfigKind`)
- Modify: `tradebot/core/config.py` (add `MaintenancePolicy` beside the other documents)
- Test: `tests/unit/test_maintenance_policy.py`

**Interfaces:**
- Consumes: `tradebot.core.schema.DomainModel`, `tradebot.control.config_store.ConfigStore`.
- Produces:
  - `ConfigKind.MAINTENANCE = "maintenance"`, with `is_singleton` true for it
  - `class MaintenancePolicy(DomainModel)` with `compact_after_days: int = 30`, `archive_keep_days: int = 90`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_maintenance_policy.py`:

```python
"""The retention windows are configuration, not constants (spec §3.7).

The cross-field rule is the one that matters: inverted windows would make a day deletable before
it was ever archived, and every pass would rewrite and re-delete the same file forever.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradebot.core.config import MaintenancePolicy
from tradebot.core.enums import ConfigKind


class TestDefaults:
    def test_the_defaults_are_the_designed_policy(self) -> None:
        policy = MaintenancePolicy()

        assert policy.compact_after_days == 30
        assert policy.archive_keep_days == 90


class TestValidation:
    def test_archives_must_outlive_the_hot_window(self) -> None:
        with pytest.raises(ValidationError, match="archive_keep_days"):
            MaintenancePolicy(compact_after_days=30, archive_keep_days=30)

    def test_a_zero_hot_window_is_refused(self) -> None:
        """Zero would compact the transcripts of cycles that are still running."""
        with pytest.raises(ValidationError):
            MaintenancePolicy(compact_after_days=0, archive_keep_days=90)

    def test_a_negative_window_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            MaintenancePolicy(compact_after_days=-1, archive_keep_days=90)


class TestKind:
    def test_maintenance_is_a_singleton_kind_like_global_risk(self) -> None:
        assert ConfigKind.MAINTENANCE.is_singleton
        assert ConfigKind.GLOBAL_RISK.is_singleton
        assert not ConfigKind.BASKET.is_singleton
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_policy.py -q`
Expected: FAIL — `ImportError: cannot import name 'MaintenancePolicy'`

- [ ] **Step 3: Write the implementation**

In `tradebot/core/enums.py`, add the member and widen the property:

```python
    BASKET = "basket"
    GLOBAL_RISK = "global_risk"
    #: How long payloads are kept hot and how long archives are kept at all. Versioned like every
    #: other limit, so "who shortened retention, and when" is answerable (spec §3.7).
    MAINTENANCE = "maintenance"

    @property
    def is_singleton(self) -> bool:
        """Whether exactly one document of this kind exists, under `SINGLETON_ID`."""
        return self in _SINGLETON_KINDS
```

and below the class:

```python
_SINGLETON_KINDS = frozenset({ConfigKind.GLOBAL_RISK, ConfigKind.MAINTENANCE})
```

In `tradebot/core/config.py`, beside the other documents:

```python
class MaintenancePolicy(DomainModel):
    """How long the log's heavy payloads are kept, hot and then archived (spec §3.7).

    Both windows are days and both are operator-set. The defaults are DESIGN §6.9's stated policy;
    an absent document means these defaults rather than a refusal, because maintenance shares its
    tick with the daily backup and refusing to back anything up for want of a published policy
    would be fail-*useless*.
    """

    #: Payloads older than this are archived to disk and compacted out of the database.
    compact_after_days: int = Field(default=30, ge=1)
    #: Archive files older than this are deleted. Irreversible: after this, the literal model
    #: completion and the frozen snapshot body exist nowhere.
    archive_keep_days: int = Field(default=90, ge=2)

    @model_validator(mode="after")
    def _archives_outlive_the_hot_window(self) -> MaintenancePolicy:
        if self.archive_keep_days <= self.compact_after_days:
            raise ValueError(
                f"archive_keep_days ({self.archive_keep_days}) must exceed compact_after_days "
                f"({self.compact_after_days}); otherwise a day becomes deletable before it is "
                "ever archived"
            )
        return self
```

Match the import style already at the top of `config.py` (`Field`, `model_validator` from
pydantic).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_policy.py -q`
Expected: PASS — 6 passed

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config_store.py -q`
Expected: PASS — the new kind must not disturb existing versioning.

- [ ] **Step 5: Commit**

```bash
git add tradebot/core/enums.py tradebot/core/config.py tests/unit/test_maintenance_policy.py
git commit -m "feat(config): a versioned maintenance policy with retention windows"
```

---

### Task 2: The archive — one immutable day file, verified by hash

**Files:**
- Create: `tradebot/maintenance/archive.py`
- Test: `tests/unit/test_maintenance_archive.py`

**Interfaces:**
- Consumes: `tradebot.persistence.schema.events`, `tradebot.core.errors.FailClosedError`.
- Produces:
  - `class ArchiveError(FailClosedError)`
  - `@dataclass(frozen=True, slots=True) class ArchiveResult: path: Path; rows: int; sha256: str`
  - `def archive_path(root: Path, mode: str, day: date) -> Path`
  - `def archive_day(engine: Engine, root: Path, *, mode: str, day: date) -> ArchiveResult`
  - `def read_archive(path: Path) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_maintenance_archive.py`:

```python
"""The archive is the copy compaction relies on, so it is verified before anything is dropped.

A day file is written once and never rewritten: a day only becomes eligible when it is entirely
past the horizon, which is what makes a whole-file hash a meaningful check (spec §3.4).
"""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tradebot.core.events import Event, EventType
from tradebot.maintenance.archive import (
    ArchiveError,
    archive_day,
    archive_path,
    read_archive,
)
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore

DAY = date(2026, 7, 19)
NOON = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> EventStore:
    engine = create_database(None)
    return EventStore(engine, SingleWriter(engine))


def seat_event(at: datetime, text: str = "raw completion text") -> Event:
    return Event(
        ts=at,
        type=EventType.SEAT_RESPONDED,
        aggregate_id="cycle-1",
        cycle_id="cycle-1",
        payload={"response": {"seat_id": "technical", "raw_text": text, "cost_usd": "0"}},
    )


class TestPath:
    def test_a_day_file_is_grouped_by_month_under_its_mode(self, tmp_path: Path) -> None:
        assert archive_path(tmp_path, "sim", DAY) == (
            tmp_path / "sim" / "2026-07" / "2026-07-19.jsonl.gz"
        )


class TestArchiveDay:
    async def test_it_writes_every_event_of_that_day(self, store: EventStore, tmp_path: Path) -> None:
        await store.append(seat_event(NOON), seat_event(NOON + timedelta(hours=1)))

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert result.rows == 2
        assert result.path.exists()

    async def test_the_archived_payload_is_byte_identical_to_the_stored_one(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Byte-identical, because that is what makes verification meaningful."""
        (stored,) = await store.append(seat_event(NOON))

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        (line,) = read_archive(result.path)
        assert line["payload"] == stored.payload
        assert line["seq"] == stored.seq
        assert line["type"] == "SEAT_RESPONDED"

    async def test_a_neighbouring_day_is_not_swept_in(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON), seat_event(NOON + timedelta(days=1)))

        assert archive_day(store.engine, tmp_path, mode="sim", day=DAY).rows == 1

    async def test_a_day_with_nothing_in_it_writes_no_file(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert result.rows == 0
        assert not result.path.exists()

    async def test_no_temporary_file_survives_a_completed_write(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON))

        archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert list(tmp_path.rglob("*.tmp")) == []

    async def test_an_existing_verified_file_is_left_alone(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON))
        first = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        second = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert second.sha256 == first.sha256

    async def test_a_corrupted_archive_is_refused_rather_than_trusted(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON))
        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)
        result.path.write_bytes(b"not gzip at all")

        with pytest.raises(ArchiveError):
            archive_day(store.engine, tmp_path, mode="sim", day=DAY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_archive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.maintenance.archive'`

- [ ] **Step 3: Write the implementation**

Create `tradebot/maintenance/archive.py`:

```python
"""One gzip file per day, written once and verified by hash.

A day is archived only after it is entirely past the retention horizon, so the set of events in it
can never change afterwards. That immutability is what lets a whole-file hash mean something, and
it is why there is no partial-rewrite path here to get wrong.

Failure semantics: anything that leaves the file absent, unreadable, or not matching what was just
written raises `ArchiveError`, and the caller compacts nothing for that day. The database still
holds every payload, so a failed archive costs a retry, never data.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select

from tradebot.core.errors import FailClosedError
from tradebot.core.logging import get_logger
from tradebot.persistence.schema import events

logger = get_logger(__name__)


class ArchiveError(FailClosedError):
    """An archive that was not written, or not verifiable once it was."""


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """What a day's archive turned out to be. `rows == 0` means no file was written."""

    path: Path
    rows: int
    sha256: str


def archive_path(root: Path, mode: str, day: date) -> Path:
    """`<root>/<mode>/2026-07/2026-07-19.jsonl.gz` — grouped by month so a year is browsable."""
    return root / mode / f"{day:%Y-%m}" / f"{day:%Y-%m-%d}.jsonl.gz"


def read_archive(path: Path) -> list[dict[str, Any]]:
    """Every line of a day file, parsed. The recovery path, and what verification re-reads."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def archive_day(engine: Engine, root: Path, *, mode: str, day: date) -> ArchiveResult:
    """Write one day's events to an immutable file, verify it, and report what it holds."""
    target = archive_path(root, mode, day)
    rows = _rows_for(engine, day)
    if not rows:
        return ArchiveResult(path=target, rows=0, sha256="")

    if target.exists():
        return ArchiveResult(path=target, rows=len(rows), sha256=_verify(target, len(rows)))

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
    with open(temporary, "rb") as raw:
        os.fsync(raw.fileno())
    temporary.replace(target)

    digest = _verify(target, len(rows))
    logger.info(
        "day archived", extra={"path": str(target), "rows": len(rows), "sha256": digest}
    )
    return ArchiveResult(path=target, rows=len(rows), sha256=digest)


def _rows_for(engine: Engine, day: date) -> list[dict[str, Any]]:
    """The day's events as plain dicts. `payload` is parsed from the stored canonical JSON, so a
    round trip through this file reproduces exactly what the row held."""
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
    """Re-read what was just written. A file that cannot be read back is not an archive."""
    try:
        lines = read_archive(path)
    except OSError as exc:
        raise ArchiveError(f"{path} could not be re-read: {exc}") from exc
    except (gzip.BadGzipFile, json.JSONDecodeError) as exc:
        raise ArchiveError(f"{path} is not a readable archive: {exc}") from exc
    if len(lines) != expected_rows:
        raise ArchiveError(f"{path} holds {len(lines)} rows; the day has {expected_rows}")
    return hashlib.sha256(path.read_bytes()).hexdigest()
```

Add `from datetime import UTC` to the imports (it is used by `_rows_for`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_archive.py -q`
Expected: PASS — 8 passed

- [ ] **Step 5: Commit**

```bash
git add tradebot/maintenance/archive.py tests/unit/test_maintenance_archive.py
git commit -m "feat(maintenance): immutable per-day event archives, verified by hash"
```

---

### Task 3: Compaction — a two-entry registry, and nothing else

**Files:**
- Create: `tradebot/maintenance/compaction.py`
- Test: `tests/unit/test_maintenance_compaction.py`

**Interfaces:**
- Consumes: `ArchiveResult` from Task 2, `tradebot.persistence.database.SingleWriter`.
- Produces:
  - `COMPACTORS: dict[EventType, Compactor]`
  - `def compact_payload(type_: EventType, payload: dict[str, Any], marker: dict[str, Any]) -> dict[str, Any] | None` — `None` means "already compacted or not compactable"
  - `async def compact_day(store: EventStore, writer: SingleWriter, *, day: date, archive: ArchiveResult, at: datetime, chunk: int = 200) -> int` — returns rows rewritten

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_maintenance_compaction.py`:

```python
"""What compaction drops, what it must keep, and what it must never touch.

The registry has exactly two entries and a type absent from it is never rewritten — that is the
whole containment story for a module that edits the audit log (spec §3.2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from tradebot.core.events import Event, EventType
from tradebot.maintenance.archive import ArchiveResult
from tradebot.maintenance.compaction import COMPACTORS, compact_day, compact_payload
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore

DAY = date(2026, 7, 19)
NOON = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
MARKER: dict[str, Any] = {"at": "2026-08-20T04:00:00Z", "archive": "x.jsonl.gz", "sha256": "abc"}
ARCHIVE = ArchiveResult(path=__import__("pathlib").Path("x.jsonl.gz"), rows=1, sha256="abc")


@pytest.fixture
def store() -> EventStore:
    engine = create_database(None)
    return EventStore(engine, SingleWriter(engine))


def seat_payload() -> dict[str, Any]:
    return {
        "response": {
            "seat_id": "technical",
            "raw_text": '{"action": "BUY"}',
            "cost_usd": "0.0012",
            "call_id": "c-1",
            "vote": {"action": "BUY", "conviction": "4", "thesis": "momentum"},
        }
    }


def snapshot_payload() -> dict[str, Any]:
    return {
        "snapshot_id": "s-1",
        "digest": "d-1",
        "snapshot": {"instruments": [{"indicators": [1, 2, 3]}], "news": ["a"]},
    }


class TestRegistry:
    def test_exactly_two_types_are_compactable(self) -> None:
        assert set(COMPACTORS) == {EventType.SEAT_RESPONDED, EventType.SNAPSHOT_FROZEN}

    def test_an_unregistered_type_is_never_rewritten(self) -> None:
        assert compact_payload(EventType.ORDER_SUBMITTED, {"anything": 1}, MARKER) is None


class TestSeatResponded:
    def test_the_literal_completion_is_dropped(self) -> None:
        compacted = compact_payload(EventType.SEAT_RESPONDED, seat_payload(), MARKER)

        assert compacted is not None
        assert "raw_text" not in compacted["response"]

    def test_everything_the_research_record_needs_survives(self) -> None:
        compacted = compact_payload(EventType.SEAT_RESPONDED, seat_payload(), MARKER)

        assert compacted is not None
        response = compacted["response"]
        assert response["cost_usd"] == "0.0012"
        assert response["call_id"] == "c-1"
        assert response["vote"]["action"] == "BUY"
        assert response["seat_id"] == "technical"

    def test_it_says_where_the_text_went(self) -> None:
        compacted = compact_payload(EventType.SEAT_RESPONDED, seat_payload(), MARKER)

        assert compacted is not None
        assert compacted["compacted"] == MARKER

    def test_a_second_pass_is_a_no_op(self) -> None:
        once = compact_payload(EventType.SEAT_RESPONDED, seat_payload(), MARKER)
        assert once is not None

        assert compact_payload(EventType.SEAT_RESPONDED, once, MARKER) is None


class TestSnapshotFrozen:
    def test_the_body_goes_and_the_two_projected_fields_stay(self) -> None:
        compacted = compact_payload(EventType.SNAPSHOT_FROZEN, snapshot_payload(), MARKER)

        assert compacted is not None
        assert "snapshot" not in compacted
        assert compacted["snapshot_id"] == "s-1"
        assert compacted["digest"] == "d-1"


class TestCompactDay:
    async def test_it_rewrites_only_that_day(self, store: EventStore) -> None:
        await store.append(
            Event(ts=NOON, type=EventType.SEAT_RESPONDED, aggregate_id="c", payload=seat_payload()),
            Event(
                ts=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                type=EventType.SEAT_RESPONDED,
                aggregate_id="c",
                payload=seat_payload(),
            ),
        )

        rewritten = await compact_day(
            store, store_writer(store), day=DAY, archive=ARCHIVE, at=NOON
        )

        assert rewritten == 1
        remaining = [e for e in store.read_all() if "raw_text" in str(e.payload)]
        assert len(remaining) == 1

    async def test_running_it_twice_rewrites_nothing_the_second_time(
        self, store: EventStore
    ) -> None:
        await store.append(
            Event(ts=NOON, type=EventType.SEAT_RESPONDED, aggregate_id="c", payload=seat_payload())
        )
        writer = store_writer(store)
        await compact_day(store, writer, day=DAY, archive=ARCHIVE, at=NOON)

        assert await compact_day(store, writer, day=DAY, archive=ARCHIVE, at=NOON) == 0
```

Add this helper at the top of the test file, below the fixtures — the store owns its writer, and
the test needs the same one so both go through a single writer thread:

```python
def store_writer(store: EventStore) -> SingleWriter:
    """The writer the store was built with. Two writers against one engine is the harness trap
    CLAUDE.md warns about, so the test reuses this one rather than making another."""
    return store._writer  # noqa: SLF001 — deliberate, and asserted nowhere else
```

If reaching into `_writer` is unacceptable to review, change the `store` fixture to build the
`SingleWriter` first and return a `(store, writer)` tuple; do not construct a second writer.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_compaction.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.maintenance.compaction'`

- [ ] **Step 3: Write the implementation**

Create `tradebot/maintenance/compaction.py`:

```python
"""Rewriting the two heavy payloads, and nothing else in the log.

A registry rather than a branch, and a type absent from it is never touched — so this can never
grow to eat an event nobody reasoned about. What survives is chosen by what *reads* it: a
projector, a report, or a cost total (spec §3.2, §3.3).

Failure semantics: this module is only ever called with a verified archive in hand. It rewrites
payloads in chunked transactions through the single writer, so a crash mid-pass leaves a database
that is internally consistent and a pass that is safe to repeat.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Connection, select, update

from tradebot.core.events import EventType
from tradebot.core.logging import get_logger
from tradebot.core.schema import canonical_json
from tradebot.maintenance.archive import ArchiveResult
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import events
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

#: The key a compacted payload carries, naming the file its detail moved to.
MARKER_KEY = "compacted"

Compactor = Callable[[dict[str, Any]], dict[str, Any] | None]


def _drop_raw_text(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The seat's literal completion. Its vote, cost, model and tokens all stay."""
    response = payload.get("response")
    if not isinstance(response, dict) or "raw_text" not in response:
        return None
    trimmed = {key: value for key, value in response.items() if key != "raw_text"}
    return {**payload, "response": trimmed}


def _drop_snapshot(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The frozen input packet. `snapshot_id` and `digest` stay — the projector reads exactly
    those two, so a rebuild after this is identical to one before it."""
    if "snapshot" not in payload:
        return None
    return {key: value for key, value in payload.items() if key != "snapshot"}


#: The whole containment decision, as data.
COMPACTORS: dict[EventType, Compactor] = {
    EventType.SEAT_RESPONDED: _drop_raw_text,
    EventType.SNAPSHOT_FROZEN: _drop_snapshot,
}


def compact_payload(
    type_: EventType, payload: dict[str, Any], marker: dict[str, Any]
) -> dict[str, Any] | None:
    """The compacted form, or `None` when there is nothing to do — unregistered or already done."""
    compactor = COMPACTORS.get(type_)
    if compactor is None:
        return None
    trimmed = compactor(payload)
    if trimmed is None:
        return None
    return {**trimmed, MARKER_KEY: marker}


async def compact_day(
    store: EventStore,
    writer: SingleWriter,
    *,
    day: date,
    archive: ArchiveResult,
    at: datetime,
    chunk: int = 200,
) -> int:
    """Rewrite one archived day's heavy payloads. Returns how many rows changed.

    Chunked so a cycle's `append` never queues behind a multi-second transaction: the writer is
    shared with the money path, and a maintenance pass must not be what delays an order intent.
    """
    marker = {
        "at": at.isoformat(),
        "archive": str(archive.path.name),
        "sha256": archive.sha256,
    }
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    types = [type_.value for type_ in COMPACTORS]
    rewritten = 0

    while True:
        batch = await writer.run(
            lambda connection: _compact_batch(connection, start, end, types, marker, chunk)
        )
        rewritten += batch
        if batch == 0:
            break

    logger.info("day compacted", extra={"day": day.isoformat(), "rows": rewritten})
    return rewritten


def _compact_batch(
    connection: Connection,
    start: datetime,
    end: datetime,
    types: list[str],
    marker: dict[str, Any],
    chunk: int,
) -> int:
    query = (
        select(events.c.seq, events.c.type, events.c.payload_json)
        .where(
            events.c.ts >= start,
            events.c.ts < end,
            events.c.type.in_(types),
            events.c.payload_json.notlike(f'%"{MARKER_KEY}"%'),
        )
        .order_by(events.c.seq)
        .limit(chunk)
    )
    import json

    changed = 0
    for row in connection.execute(query).all():
        compacted = compact_payload(EventType(row.type), json.loads(row.payload_json), marker)
        if compacted is None:
            continue
        connection.execute(
            update(events)
            .where(events.c.seq == row.seq)
            .values(payload_json=canonical_json(compacted))
        )
        changed += 1
    return changed
```

Move `import json` to the module's import block rather than leaving it inside the function.

Note the `notlike` filter: it is what makes the loop terminate. Without it an already-compacted row
would be re-selected forever, because `compact_payload` returns `None` and the batch count would
stay above zero while nothing changed.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_compaction.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
git add tradebot/maintenance/compaction.py tests/unit/test_maintenance_compaction.py
git commit -m "feat(maintenance): compact seat transcripts and snapshot bodies"
```

---

### Task 4: The invariant — a rebuild after compaction is the rebuild before it

**Files:**
- Modify: `tests/unit/test_maintenance_compaction.py` (add the class)

**Interfaces:**
- Consumes: `compact_day` from Task 3, `rebuild_projections` from `tradebot.persistence.projections`.
- Produces: nothing. This is the test the whole piece exists to satisfy.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_maintenance_compaction.py`:

```python
class TestTheInvariant:
    """The property everything else rests on (spec §3.3).

    If this fails, the compactor is dropping a field a projector reads. Fix the compactor; never
    the assertion.
    """

    async def test_a_rebuild_after_compaction_is_identical_to_one_before(
        self, store: EventStore
    ) -> None:
        await store.append(
            Event(
                ts=NOON,
                type=EventType.CYCLE_STARTED,
                aggregate_id="c-1",
                cycle_id="c-1",
                basket_id="demo",
                payload={"basket_id": "demo", "venue": "sim", "started_at": NOON.isoformat()},
            ),
            Event(
                ts=NOON,
                type=EventType.SNAPSHOT_FROZEN,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload=snapshot_payload(),
            ),
            Event(
                ts=NOON,
                type=EventType.SEAT_RESPONDED,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload=seat_payload(),
            ),
        )
        await store.rebuild()
        before = _projection_snapshot(store)

        await compact_day(store, store_writer(store), day=DAY, archive=ARCHIVE, at=NOON)
        await store.rebuild()

        assert _projection_snapshot(store) == before

    async def test_the_snapshot_digest_still_reaches_the_cycle_row(
        self, store: EventStore
    ) -> None:
        """Named separately because it is *why* the invariant holds, not merely that it does."""
        await store.append(
            Event(
                ts=NOON,
                type=EventType.CYCLE_STARTED,
                aggregate_id="c-1",
                cycle_id="c-1",
                basket_id="demo",
                payload={"basket_id": "demo", "venue": "sim", "started_at": NOON.isoformat()},
            ),
            Event(
                ts=NOON,
                type=EventType.SNAPSHOT_FROZEN,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload=snapshot_payload(),
            ),
        )
        await compact_day(store, store_writer(store), day=DAY, archive=ARCHIVE, at=NOON)
        await store.rebuild()

        with store.engine.connect() as connection:
            row = connection.execute(select(cycles)).one()
        assert row.snapshot_digest == "d-1"


def _projection_snapshot(store: EventStore) -> dict[str, list[tuple[Any, ...]]]:
    """Every projection table, as comparable tuples."""
    with store.engine.connect() as connection:
        return {
            table.name: [tuple(row) for row in connection.execute(select(table)).all()]
            for table in PROJECTION_TABLES
        }
```

Add to that file's imports:

```python
from sqlalchemy import select

from tradebot.persistence.projections import PROJECTION_TABLES
from tradebot.persistence.schema import cycles
```

If `PROJECTION_TABLES` is not exported under that name, read `tradebot/persistence/projections.py`
and use the actual list it truncates in `rebuild_projections`. Do **not** hand-list the tables in
the test — the point is that the invariant covers every projection, including ones added later.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_compaction.py::TestTheInvariant -q`
Expected: PASS on a correct Task 3 — **that is the desired outcome.** This task's value is the
regression it locks in, not a red bar. If it *fails*, Task 3's compactor is dropping a projected
field: fix `compaction.py`, never this test.

- [ ] **Step 3: Run the whole persistence suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_compaction.py tests/unit -k "projection or rebuild" -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_maintenance_compaction.py
git commit -m "test(maintenance): a rebuild after compaction is identical to one before"
```

---

### Task 5: Deleting aged archives

**Files:**
- Modify: `tradebot/maintenance/archive.py`
- Modify: `tests/unit/test_maintenance_archive.py`

**Interfaces:**
- Consumes: `archive_path` from Task 2.
- Produces: `def delete_aged(root: Path, mode: str, *, before: date) -> tuple[list[Path], list[str]]` — the files removed, and one message per file that could not be.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_maintenance_archive.py`:

```python
class TestDeleteAged:
    """The only irreversible act in this piece (spec D1a), so its aim is asserted narrowly."""

    def _day_file(self, root: Path, day: date) -> Path:
        path = archive_path(root, "sim", day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_it_deletes_only_files_older_than_the_cutoff(self, tmp_path: Path) -> None:
        old = self._day_file(tmp_path, date(2026, 4, 1))
        recent = self._day_file(tmp_path, date(2026, 7, 19))

        removed, failures = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == [old]
        assert failures == []
        assert recent.exists()

    def test_a_partial_write_is_never_matched(self, tmp_path: Path) -> None:
        """A `.tmp` is not a day file, and deletion must not guess."""
        partial = archive_path(tmp_path, "sim", date(2026, 4, 1))
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial = partial.with_suffix(partial.suffix + ".tmp")
        partial.write_bytes(b"x")

        removed, _ = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == []
        assert partial.exists()

    def test_another_mode_is_never_touched(self, tmp_path: Path) -> None:
        other = archive_path(tmp_path, "live", date(2026, 4, 1))
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"x")

        delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert other.exists()

    def test_an_absent_archive_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert delete_aged(tmp_path / "nothing", "sim", before=date(2026, 5, 1)) == ([], [])
```

Extend the imports with `delete_aged` and `date`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_archive.py::TestDeleteAged -q`
Expected: FAIL — `ImportError: cannot import name 'delete_aged'`

- [ ] **Step 3: Write the implementation**

Add to `tradebot/maintenance/archive.py`:

```python
def delete_aged(root: Path, mode: str, *, before: date) -> tuple[list[Path], list[str]]:
    """Remove this mode's day files older than `before`. Irreversible, and deliberately narrow.

    Matches only `<root>/<mode>/YYYY-MM/YYYY-MM-DD.jsonl.gz`, by parsing the name rather than by
    reading a stat time — a file copied between machines keeps its meaning, and a `.tmp` from an
    interrupted write can never be mistaken for a completed day. Nothing outside this directory is
    ever considered: not the database, not a backup.
    """
    directory = root / mode
    if not directory.exists():
        return [], []

    removed: list[Path] = []
    failures: list[str] = []
    for path in sorted(directory.glob("*/*.jsonl.gz")):
        try:
            day = date.fromisoformat(path.name.removesuffix(".jsonl.gz"))
        except ValueError:
            continue
        if day >= before:
            continue
        try:
            path.unlink()
        except OSError as exc:
            failures.append(f"{path}: {exc}")
            continue
        removed.append(path)

    if removed:
        logger.warning(
            "archives deleted", extra={"count": len(removed), "before": before.isoformat()}
        )
    return removed, failures
```

`glob("*/*.jsonl.gz")` cannot match a `.tmp`, and `date.fromisoformat` rejects anything that is not
a day — two independent reasons the wrong file cannot be chosen.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_archive.py -q`
Expected: PASS — 12 passed

- [ ] **Step 5: Commit**

```bash
git add tradebot/maintenance/archive.py tests/unit/test_maintenance_archive.py
git commit -m "feat(maintenance): delete archives past the retention window"
```

---

### Task 6: `MaintenanceService` — the pass, in order, once a day

**Files:**
- Create: `tradebot/maintenance/service.py`
- Modify: `tradebot/core/events.py` (add `MAINTENANCE_RAN` to `EventType`)
- Test: `tests/unit/test_maintenance_service.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5, plus `take_backup` from Piece A.
- Produces:
  - `EventType.MAINTENANCE_RAN = "MAINTENANCE_RAN"`
  - `@dataclass(frozen=True, slots=True) class MaintenanceReport` with `backup: Path | None`, `archived_days: int`, `compacted_rows: int`, `deleted_archives: int`, `failure: str`
  - `class MaintenanceService` with `async def run_once() -> MaintenanceReport | None` (`None` when not due) and `async def run(poll_seconds: float = 300.0) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_maintenance_service.py`:

```python
"""One pass a day, in one order, recorded as one event.

The order is the safety property: back up, then archive, then compact only what was archived, then
delete what has aged out. A failure anywhere stops the destructive steps that would follow it
(spec §3.5, §6.4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import MaintenancePolicy
from tradebot.core.events import Event, EventType
from tradebot.maintenance.backup import BackupError
from tradebot.maintenance.service import MaintenanceService
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=40)
ANCIENT = NOW - timedelta(days=120)


def seat_event(at: datetime) -> Event:
    return Event(
        ts=at,
        type=EventType.SEAT_RESPONDED,
        aggregate_id="c-1",
        cycle_id="c-1",
        payload={"response": {"seat_id": "s", "raw_text": "text", "cost_usd": "0"}},
    )


@pytest.fixture
def service(tmp_path: Path) -> MaintenanceService:
    engine = create_database(None)
    writer = SingleWriter(engine)
    return MaintenanceService(
        store=EventStore(engine, writer),
        writer=writer,
        clock=ManualClock(NOW),
        mode="sim",
        archive_root=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        policy=lambda: MaintenancePolicy(),
        take=lambda *_, **__: None,
    )


class TestDueness:
    async def test_the_first_pass_of_the_day_runs(self, service: MaintenanceService) -> None:
        assert await service.run_once() is not None

    async def test_a_second_pass_the_same_day_does_nothing(
        self, service: MaintenanceService
    ) -> None:
        await service.run_once()

        assert await service.run_once() is None

    async def test_dueness_survives_a_restart_because_it_is_read_from_the_log(
        self, service: MaintenanceService, tmp_path: Path
    ) -> None:
        await service.run_once()
        restarted = MaintenanceService(
            store=service.store,
            writer=service.writer,
            clock=ManualClock(NOW + timedelta(hours=1)),
            mode="sim",
            archive_root=tmp_path / "archive",
            backup_dir=tmp_path / "backups",
            policy=lambda: MaintenancePolicy(),
            take=lambda *_, **__: None,
        )

        assert await restarted.run_once() is None

    async def test_the_next_day_is_due_again(
        self, service: MaintenanceService, tmp_path: Path
    ) -> None:
        await service.run_once()
        tomorrow = MaintenanceService(
            store=service.store,
            writer=service.writer,
            clock=ManualClock(NOW + timedelta(days=1)),
            mode="sim",
            archive_root=tmp_path / "archive",
            backup_dir=tmp_path / "backups",
            policy=lambda: MaintenancePolicy(),
            take=lambda *_, **__: None,
        )

        assert await tomorrow.run_once() is not None


class TestThePass:
    async def test_an_old_day_is_archived_and_compacted(
        self, service: MaintenanceService
    ) -> None:
        await service.store.append(seat_event(LONG_AGO))

        report = await service.run_once()

        assert report is not None
        assert report.archived_days == 1
        assert report.compacted_rows == 1
        assert all("raw_text" not in str(e.payload) for e in service.store.read_all())

    async def test_a_recent_day_is_left_alone(self, service: MaintenanceService) -> None:
        await service.store.append(seat_event(NOW - timedelta(days=2)))

        report = await service.run_once()

        assert report is not None
        assert report.compacted_rows == 0
        assert any("raw_text" in str(e.payload) for e in service.store.read_all())

    async def test_the_pass_records_one_event_naming_the_windows_it_ran_under(
        self, service: MaintenanceService
    ) -> None:
        await service.run_once()

        (recorded,) = service.store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["compact_after_days"] == 30
        assert recorded.payload["archive_keep_days"] == 90
        assert recorded.payload["outcome"] == "ok"


class TestFailure:
    async def test_a_failed_backup_stops_the_pass_before_anything_is_compacted(
        self, tmp_path: Path
    ) -> None:
        engine = create_database(None)
        writer = SingleWriter(engine)
        store = EventStore(engine, writer)

        def refuse(*_: object, **__: object) -> None:
            raise BackupError("no room")

        service = MaintenanceService(
            store=store,
            writer=writer,
            clock=ManualClock(NOW),
            mode="sim",
            archive_root=tmp_path / "archive",
            backup_dir=tmp_path / "backups",
            policy=lambda: MaintenancePolicy(),
            take=refuse,
        )
        await store.append(seat_event(LONG_AGO))

        report = await service.run_once()

        assert report is not None
        assert "no room" in report.failure
        assert report.compacted_rows == 0
        assert any("raw_text" in str(e.payload) for e in store.read_all())

    async def test_a_failure_is_recorded_as_one(self, tmp_path: Path) -> None:
        engine = create_database(None)
        writer = SingleWriter(engine)
        store = EventStore(engine, writer)

        def refuse(*_: object, **__: object) -> None:
            raise BackupError("no room")

        service = MaintenanceService(
            store=store,
            writer=writer,
            clock=ManualClock(NOW),
            mode="sim",
            archive_root=tmp_path / "archive",
            backup_dir=tmp_path / "backups",
            policy=lambda: MaintenancePolicy(),
            take=refuse,
        )

        await service.run_once()

        (recorded,) = store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["outcome"] == "failed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.maintenance.service'`

- [ ] **Step 3: Write the implementation**

Add to `EventType` in `tradebot/core/events.py`:

```python
    #: One housekeeping pass: what it did, under which windows, and whether it failed. Also what
    #: answers "is a run due" — derived from the log, never counted in memory (spec §6.2).
    MAINTENANCE_RAN = "MAINTENANCE_RAN"
```

Create `tradebot/maintenance/service.py`:

```python
"""One housekeeping pass a day: back up, archive, compact, then delete what has aged out.

The order is the design. A backup that fails stops everything destructive behind it, and nothing is
compacted for a day whose archive did not verify. A pass that fails is still *recorded*, because a
maintenance run nobody can audit is worse than one that did not happen.

Failure semantics: `run_once` never raises. It returns a report whose `failure` is empty on
success, and appends one `MAINTENANCE_RAN` event either way. `run` wraps it in a loop that survives
any exception, because a maintenance defect must never stop the bot trading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from tradebot.core.clock import Clock
from tradebot.core.config import MaintenancePolicy
from tradebot.core.events import Event, EventType
from tradebot.core.logging import get_logger
from tradebot.maintenance.archive import archive_day, delete_aged
from tradebot.maintenance.backup import take_backup
from tradebot.maintenance.compaction import COMPACTORS, compact_day
from tradebot.persistence.schema import events
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

#: How often the loop asks whether a pass is due. Far finer than the daily boundary it watches, so
#: a process started at any hour begins its first pass promptly.
DEFAULT_POLL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """What one pass did. Rendered into the event, and later into a notification."""

    backup: Path | None = None
    archived_days: int = 0
    compacted_rows: int = 0
    deleted_archives: int = 0
    failure: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure


class MaintenanceService:
    """The daily pass, and the loop that notices it is due."""

    def __init__(
        self,
        *,
        store: EventStore,
        writer: SingleWriter,
        clock: Clock,
        mode: str,
        archive_root: Path,
        backup_dir: Path,
        policy: Callable[[], MaintenancePolicy],
        take: Callable[..., object] = take_backup,
    ) -> None:
        self.store = store
        self.writer = writer
        self._clock = clock
        self._mode = mode
        self._archive_root = archive_root
        self._backup_dir = backup_dir
        #: Read fresh at every pass, never captured — an edit takes effect at the next tick
        #: without a restart (ADR 0021's rule, applied here).
        self._policy = policy
        self._take = take

    async def run(self, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        """Poll until cancelled. Started alongside the supervisor by `run` and `serve`."""
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            # A maintenance defect must not end the loop or stop trading.
            except Exception:
                logger.exception("maintenance pass failed; the loop continues")
            await self._clock.sleep(poll_seconds)

    async def run_once(self) -> MaintenanceReport | None:
        """One pass, or `None` when one has already run today."""
        now = self._clock.now()
        if not self._is_due(now):
            return None

        policy = self._policy()
        report = await self._pass(now, policy)
        await self._record(now, policy, report)
        return report

    async def _pass(self, now: datetime, policy: MaintenancePolicy) -> MaintenanceReport:
        try:
            backup = self._take(
                self.store.engine,
                self._backup_dir,
                mode=self._mode,
                clock=self._clock,
            )
        except Exception as exc:  # classified below; the pass stops here either way
            return MaintenanceReport(failure=f"backup: {exc}")

        archived = 0
        compacted = 0
        horizon = now.date() - timedelta(days=policy.compact_after_days)
        try:
            for day in self._days_before(horizon):
                result = archive_day(
                    self.store.engine, self._archive_root, mode=self._mode, day=day
                )
                if result.rows == 0:
                    continue
                archived += 1
                compacted += await compact_day(
                    self.store, self.writer, day=day, archive=result, at=now
                )
        except Exception as exc:
            return MaintenanceReport(
                backup=getattr(backup, "path", None),
                archived_days=archived,
                compacted_rows=compacted,
                failure=f"archive: {exc}",
            )

        removed, failures = delete_aged(
            self._archive_root,
            self._mode,
            before=now.date() - timedelta(days=policy.archive_keep_days),
        )
        return MaintenanceReport(
            backup=getattr(backup, "path", None),
            archived_days=archived,
            compacted_rows=compacted,
            deleted_archives=len(removed),
            failure="; ".join(failures),
        )

    def _days_before(self, horizon: date) -> list[date]:
        """Every day the log holds that is entirely past the horizon, oldest first.

        Deliberately SQL over the timestamp column rather than `read_types`: the two compactable
        types *are* the heavy payloads, and folding them in Python would load every transcript in
        the log into memory on every pass — the exact cost this module exists to remove.
        """
        query = (
            select(func.substr(events.c.ts, 1, 10))
            .where(events.c.type.in_([type_.value for type_ in COMPACTORS]))
            .distinct()
        )
        with self.store.engine.connect() as connection:
            stamps = [row[0] for row in connection.execute(query)]
        return sorted(day for stamp in stamps if (day := date.fromisoformat(stamp)) < horizon)

    def _is_due(self, now: datetime) -> bool:
        """Due when the newest recorded pass is not today. Derived from the log, so a restart can
        neither skip the day's backup nor take a second (spec §6.2)."""
        recorded = self.store.read_types(EventType.MAINTENANCE_RAN)
        if not recorded:
            return True
        return recorded[-1].ts.date() != now.date()

    async def _record(
        self, now: datetime, policy: MaintenancePolicy, report: MaintenanceReport
    ) -> None:
        await self.store.append(
            Event(
                ts=now,
                type=EventType.MAINTENANCE_RAN,
                aggregate_id="maintenance",
                payload={
                    "mode": self._mode,
                    "outcome": "ok" if report.ok else "failed",
                    "detail": report.failure,
                    "backup": str(report.backup) if report.backup else "",
                    "archived_days": report.archived_days,
                    "compacted_rows": report.compacted_rows,
                    "deleted_archives": report.deleted_archives,
                    "compact_after_days": policy.compact_after_days,
                    "archive_keep_days": policy.archive_keep_days,
                },
            )
        )


```

Delete the trailing `_COMPACTABLE` tuple — `COMPACTORS` is the single definition of what is
compactable, and `_days_before` reads it directly.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_service.py -q`
Expected: PASS — 10 passed

- [ ] **Step 5: Commit**

```bash
git add tradebot/maintenance/service.py tradebot/core/events.py tests/unit/test_maintenance_service.py
git commit -m "feat(maintenance): the daily pass, ordered and recorded"
```

---

### Task 7: Wire the tick, the config form, and the CLI

**Files:**
- Modify: `tradebot/app.py` (build the service beside `AlertDispatcher`, around :1016)
- Modify: `tradebot/__main__.py` (start it alongside the supervisor in `run`/`serve`; extend `maintenance compact`)
- Modify: `tradebot/dashboard/routes/configure.py` and the risk template (the `maintenance` document's two fields)
- Modify: `tradebot/dashboard/templates/monitor/cycle.html:197-205` (the archived-snapshot message)
- Test: `tests/unit/test_dashboard_configure.py`, `tests/unit/test_dashboard_monitor.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `MaintenanceService` from Task 6.
- Produces: `Application.maintenance: MaintenanceService`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_dashboard_monitor.py`:

```python
class TestArchivedSnapshot:
    """A compacted cycle did freeze a snapshot. Saying otherwise is the lie spec §3.6 names."""

    async def test_a_compacted_cycle_names_the_archive_rather_than_denying_the_snapshot(
        self, client: AsyncClient, store: EventStore
    ) -> None:
        await store.append(
            Event(
                ts=NOW,
                type=EventType.SNAPSHOT_FROZEN,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload={
                    "snapshot_id": "s-1",
                    "digest": "d-1",
                    "compacted": {
                        "at": "2026-08-20T04:00:00Z",
                        "archive": "2026-07-19.jsonl.gz",
                        "sha256": "abc",
                    },
                },
            )
        )

        page = (await client.get("/cycles/c-1")).text

        assert "2026-07-19.jsonl.gz" in page
        assert "No snapshot was frozen" not in page
```

Match the existing fixtures and cycle-page URL in that file rather than these placeholders if they
differ; the assertion is the part that matters.

Add to `tests/unit/test_dashboard_configure.py`, following the file's existing publish helper:

```python
class TestMaintenanceForm:
    async def test_the_windows_can_be_published(self, client: AsyncClient) -> None:
        response = await client.post(
            "/configure/maintenance",
            data={"compact_after_days": "45", "archive_keep_days": "120"},
        )

        assert response.status_code in (200, 303)

    async def test_inverted_windows_are_refused_with_the_reason(
        self, client: AsyncClient
    ) -> None:
        response = await client.post(
            "/configure/maintenance",
            data={"compact_after_days": "90", "archive_keep_days": "30"},
        )

        assert "must exceed" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_monitor.py -k Archived tests/unit/test_dashboard_configure.py -k Maintenance -q`
Expected: FAIL — 404 on the configure route, and the monitor page still saying "No snapshot was frozen".

- [ ] **Step 3: Implement the three wirings**

**a. The template** (`monitor/cycle.html`), replacing the `{% else %}` arm:

```jinja
{% elif detail.snapshot_archive %}
  <p class="empty">
    The frozen snapshot was archived on {{ detail.snapshot_archive.at }} and compacted out of the
    database — it is in <code>{{ detail.snapshot_archive.archive }}</code>. Its digest,
    <code>{{ cycle.snapshot_digest }}</code>, is unchanged, so the archived copy is verifiably the
    one this cycle deliberated on.
  </p>
{% else %}
  <p class="empty">No snapshot was frozen — the cycle was blocked before one was built.</p>
{% endif %}
```

with the accessor beside `Queries.snapshot` in `dashboard/queries.py`:

```python
    @property
    def snapshot_archive(self) -> dict[str, Any] | None:
        """Where a compacted snapshot went, or `None` if it is still here (spec §3.6)."""
        frozen = self.events_of(EventType.SNAPSHOT_FROZEN)
        return frozen[-1].payload.get("compacted") if frozen else None
```

**The seat transcripts need the same treatment**, and are easy to forget because nothing renders
`raw_text` today: the seat panel shows each vote and thesis, which compaction keeps, so a compacted
cycle looks *complete* rather than archived. Add one line to the seat section of the same template
naming the archive when the event carries the marker — silence there would leave an operator
believing they are reading the whole record:

```jinja
{% if seat.compacted %}
  <p class="muted small">
    The verbatim completion is in <code>{{ seat.compacted.archive }}</code>; the vote, cost and
    model below are unchanged.
  </p>
{% endif %}
```

with the matching test:

```python
    async def test_a_compacted_seat_says_where_its_completion_went(
        self, client: AsyncClient, store: EventStore
    ) -> None:
        """The vote survives compaction, so without this line the page looks whole."""
        await store.append(
            Event(
                ts=NOW,
                type=EventType.SEAT_RESPONDED,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload={
                    "response": {"seat_id": "technical", "vote": {"action": "BUY"}},
                    "compacted": {"archive": "2026-07-19.jsonl.gz", "sha256": "abc"},
                },
            )
        )

        assert "2026-07-19.jsonl.gz" in (await client.get("/cycles/c-1")).text
```

**b. The configure route**, following the shape of the existing global-risk route exactly — form →
`MaintenancePolicy.model_validate` → `configs.put(SINGLETON_ID, policy, actor=ACTOR)`. Render the
two fields on the Parameters page with the warning spec §3.7 requires beside `archive_keep_days`:
*"Shortening this deletes archives on the next pass. Irreversible."*

**c. `app.py`**, beside the `alerts=AlertDispatcher(...)` argument:

```python
        maintenance=MaintenanceService(
            store=store,
            writer=writer,
            clock=clock,
            mode=mode.value,
            archive_root=data_dir / "archive",
            backup_dir=backup_destination(database_path(mode, root=data_dir)),
            policy=lambda: _maintenance_policy(configs),
        ),
```

where `_maintenance_policy(configs)` reads the latest `MAINTENANCE` document and returns
`MaintenancePolicy()` when none is published — the defaults-not-refusal rule of spec §3.7.

**d. `__main__.py`**: start `application.maintenance.run()` as a task wherever
`application.alerts.run()` is started, and extend the `maintenance` subparser's `compact` action to
call `run_once` with `--older-than` / `--keep-days` overrides.

- [ ] **Step 4: Run the full suite**

Run: `.\check.ps1`
Expected: all gates pass.

- [ ] **Step 5: Commit**

```bash
git add tradebot/app.py tradebot/__main__.py tradebot/dashboard/ tests/unit/
git commit -m "feat(maintenance): daily tick, editable windows, and an honest archived-snapshot page"
```

---

### Task 8: ADR 0028 and the documentation

**Files:**
- Create: `docs/adr/0028-retention-is-archive-then-compact.md`
- Modify: `docs/adr/0003-event-log-as-source-of-truth.md:47-49`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write ADR 0028**

Follow the house format of ADR 0027 exactly (context, decision, consequences, and the "easy to get
backwards" rules). It must state: no event row is ever deleted; the registry has two entries; the
invariant and where it is asserted; that deletion at 90 days is irreversible and why that is the
policy rather than an accident.

- [ ] **Step 2: Correct ADR 0003**

Replace "DESIGN §6.9's retention policy … is not yet implemented" with what now exists, keeping the
sentence about the hash being what makes replay verifiable after compaction — that sentence is now
load-bearing rather than aspirational.

- [ ] **Step 3: Add the CLAUDE.md section**

A "Phase 13 — retention and backup" section in the house style, with the rules that are easy to get
backwards: the registry is the containment; nothing is compacted without a verified archive; the
windows are versioned configuration; deletion is irreversible.

- [ ] **Step 4: Commit**

```bash
git add docs/ CLAUDE.md
git commit -m "docs: ADR 0028, retention is archive-then-compact"
```

---

## What this piece deliberately does not do

- **No notification.** Failures are recorded on `MAINTENANCE_RAN` and logged; turning that event
  into something visible is Piece C.
- **No dashboard surface for `maintenance status`** (spec §7).
- **No compaction of `news_items` / `news_vectors`** — 79 KB, spec §3.8.
