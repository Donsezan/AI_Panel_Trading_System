"""Rewriting the two heavy payloads, and nothing else in the log.

A registry rather than a branch, and a type absent from it is never touched — so this can never
grow to eat an event nobody reasoned about. What survives is chosen by what *reads* it: a
projector, a report, or a cost total (spec §3.2, §3.3).

**No event row is ever deleted here.** This module only ever `UPDATE`s `events.payload_json`,
which makes it the one exception to that table's otherwise append-only contract — see ADR 0028 and
the note on `persistence.schema.events`. The invariant that licenses it is asserted directly rather
than argued: a projection rebuild after compaction is identical to one before it
(`test_maintenance_compaction.py::TestTheInvariant`).

Failure semantics: this module is only ever called with a **verified** archive in hand, so the
payload it drops always exists in a file first. It rewrites through the single writer in bounded
chunks, so a crash mid-pass leaves a database that is internally consistent and a pass that is
safe to repeat — compaction is idempotent, because a payload with no `raw_text` (or no `snapshot`)
is already compacted and is skipped.
"""

from __future__ import annotations

import json
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

logger = get_logger(__name__)

#: The key a compacted payload gains, naming the file its detail moved to. Its presence is also
#: what the dashboard reads to say "archived" rather than "never existed" (spec §3.6).
MARKER_KEY = "compacted"

#: Rows rewritten per transaction. Bounded so a cycle's `append` never queues behind a
#: multi-second write: the writer is shared with the money path (PLAN §2.6).
DEFAULT_CHUNK = 200

Compactor = Callable[[dict[str, Any]], dict[str, Any] | None]


def _drop_raw_text(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The seat's literal completion. Its vote, cost, model, tokens and reason all stay.

    `None` when there is nothing to drop, which covers both an already-compacted payload and a
    seat that **abstained** — an abstention never had a completion to begin with.
    """
    response = payload.get("response")
    if not isinstance(response, dict) or "raw_text" not in response:
        return None
    trimmed = {key: value for key, value in response.items() if key != "raw_text"}
    return {**payload, "response": trimmed}


def _drop_snapshot(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The frozen input packet. `snapshot_id` and `digest` stay.

    `_project_snapshot_frozen` reads exactly those two fields and nothing else, which is *why* a
    rebuild after compaction is identical to one before it — and the digest is what keeps the
    archived copy verifiably the packet this cycle deliberated on.
    """
    if "snapshot" not in payload:
        return None
    return {key: value for key, value in payload.items() if key != "snapshot"}


#: The whole containment decision, as data. Two entries, and both were chosen by measuring: they
#: are 88% of the log's payload bytes (spec §1.2).
COMPACTORS: dict[EventType, Compactor] = {
    EventType.SEAT_RESPONDED: _drop_raw_text,
    EventType.SNAPSHOT_FROZEN: _drop_snapshot,
}


def compact_payload(
    type_: EventType, payload: dict[str, Any], marker: dict[str, Any]
) -> dict[str, Any] | None:
    """The compacted form, or `None` when there is nothing to do — unregistered, or already done."""
    compactor = COMPACTORS.get(type_)
    if compactor is None:
        return None
    trimmed = compactor(payload)
    if trimmed is None:
        return None
    return {**trimmed, MARKER_KEY: marker}


async def compact_day(
    writer: SingleWriter,
    *,
    day: date,
    archive: ArchiveResult,
    at: datetime,
    chunk: int = DEFAULT_CHUNK,
) -> int:
    """Rewrite one archived day's heavy payloads. Returns how many rows changed.

    Batches advance by **`seq`**, not by how many rows a batch happened to rewrite. That is not a
    style choice: a batch can legitimately rewrite nothing — a chunk of seats that abstained carry
    no `raw_text` and so never gain a marker — and a loop that stopped on a zero rewrite count
    would leave those rows at the head of every batch and permanently stop compacting everything
    behind them, silently and with no failed pass.
    """
    marker = {"at": at.isoformat(), "archive": archive.path.name, "sha256": archive.sha256}
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    types = [type_.value for type_ in COMPACTORS]

    rewritten = 0
    cursor = 0
    while True:
        seen, changed, cursor = await writer.run(
            lambda connection, after=cursor: _compact_batch(  # type: ignore[misc]
                connection,
                start=start,
                end=end,
                types=types,
                marker=marker,
                chunk=chunk,
                after=after,
            )
        )
        rewritten += changed
        if seen == 0:
            break

    if rewritten:
        logger.info(
            "day compacted",
            extra={"day": day.isoformat(), "rows": rewritten, "archive": archive.path.name},
        )
    return rewritten


def _compact_batch(
    connection: Connection,
    *,
    start: datetime,
    end: datetime,
    types: list[str],
    marker: dict[str, Any],
    chunk: int,
    after: int,
) -> tuple[int, int, int]:
    """One transaction's worth. Returns `(rows seen, rows rewritten, new cursor)`.

    `rows seen` is what terminates the loop, so a batch of already-compacted or uncompactable rows
    advances the cursor past them instead of blocking on them.
    """
    query = (
        select(events.c.seq, events.c.type, events.c.payload_json)
        .where(
            events.c.seq > after,
            events.c.ts >= start,
            events.c.ts < end,
            events.c.type.in_(types),
        )
        .order_by(events.c.seq)
        .limit(chunk)
    )
    rows = connection.execute(query).all()

    changed = 0
    cursor = after
    for row in rows:
        cursor = row.seq
        compacted = compact_payload(EventType(row.type), json.loads(row.payload_json), marker)
        if compacted is None:
            continue
        connection.execute(
            update(events)
            .where(events.c.seq == row.seq)
            .values(payload_json=canonical_json(compacted))
        )
        changed += 1
    return len(rows), changed, cursor
