"""What compaction drops, what it must keep, and what it must never touch.

The registry has exactly two entries and a type absent from it is never rewritten — that is the
whole containment story for a module that edits the audit log (spec §3.2). What survives is chosen
by what *reads* it: a projector, a report, or a cost total.

This is the only module in the system that `UPDATE`s a row in `events`. Every other reader treats
that table as append-only, so the guarantees here are load-bearing: no row is ever deleted, no
field a projection reads is ever dropped, and a second pass over the same day changes nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from tradebot.app import Application
from tradebot.core.events import Event, EventType
from tradebot.maintenance.archive import ArchiveResult
from tradebot.maintenance.compaction import (
    COMPACTORS,
    MARKER_KEY,
    compact_day,
    compact_payload,
    pending_days,
)
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import PROJECTION_TABLES, cycles
from tradebot.persistence.store import EventStore

DAY = date(2026, 7, 19)
NOON = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
NEXT_DAY = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
MARKER: dict[str, Any] = {
    "at": "2026-08-20T04:00:00+00:00",
    "archive": "2026-07-19.jsonl.gz",
    "sha256": "abc",
}
ARCHIVE = ArchiveResult(path=Path("2026-07/2026-07-19.jsonl.gz"), rows=1, sha256="abc")


def writer_of(store: EventStore) -> SingleWriter:
    """The writer the store was built with.

    Reused rather than rebuilt: two writers against one engine is the harness trap CLAUDE.md
    warns about, and compaction has to go through the same single writer a cycle's append does.
    """
    return store._writer


def seat_payload(text: str = '{"action": "BUY"}') -> dict[str, Any]:
    return {
        "response": {
            "seat_id": "technical",
            "raw_text": text,
            "cost_usd": "0.0012",
            "call_id": "c-1",
            "model": "varied-1",
            "provider_id": "stub",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "vote": {"action": "BUY", "conviction": "4", "thesis": "momentum"},
        }
    }


def abstained_payload() -> dict[str, Any]:
    """A seat whose whole chain was unwired. It has no `raw_text` and never gains a marker."""
    return {
        "response": {
            "seat_id": "macro",
            "cost_usd": "0",
            "call_id": "c-2",
            "abstain_reason": "no provider reachable",
        }
    }


def snapshot_payload() -> dict[str, Any]:
    return {
        "snapshot_id": "s-1",
        "digest": "d-1",
        "snapshot": {"instruments": [{"indicators": [1, 2, 3]}], "news": ["a"]},
    }


def seat_event(at: datetime, payload: dict[str, Any] | None = None) -> Event:
    return Event(
        ts=at,
        type=EventType.SEAT_RESPONDED,
        aggregate_id="c-1",
        cycle_id="c-1",
        payload=payload if payload is not None else seat_payload(),
    )


class TestRegistry:
    def test_exactly_two_types_are_compactable(self) -> None:
        """The containment decision, as data. Growing this table is a deliberate act."""
        assert set(COMPACTORS) == {EventType.SEAT_RESPONDED, EventType.SNAPSHOT_FROZEN}

    def test_an_unregistered_type_is_never_rewritten(self) -> None:
        assert compact_payload(EventType.ORDER_SUBMITTED, {"anything": 1}, MARKER) is None

    def test_no_money_bearing_type_is_compactable(self) -> None:
        """A fill or an order is the tax artifact; nothing may ever trim one."""
        for type_ in (
            EventType.ORDER_SUBMITTED,
            EventType.FILL_RECEIVED,
            EventType.ROUND_TRIP_CLOSED,
            EventType.POSITION_UPDATED,
        ):
            assert type_ not in COMPACTORS


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
        assert response["model"] == "varied-1"
        assert response["prompt_tokens"] == 100
        assert response["vote"]["action"] == "BUY"
        assert response["vote"]["thesis"] == "momentum"
        assert response["seat_id"] == "technical"

    def test_it_says_where_the_text_went(self) -> None:
        compacted = compact_payload(EventType.SEAT_RESPONDED, seat_payload(), MARKER)

        assert compacted is not None
        assert compacted["compacted"] == MARKER

    def test_a_second_pass_is_a_no_op(self) -> None:
        once = compact_payload(EventType.SEAT_RESPONDED, seat_payload(), MARKER)
        assert once is not None

        assert compact_payload(EventType.SEAT_RESPONDED, once, MARKER) is None

    def test_a_seat_that_abstained_has_nothing_to_drop(self) -> None:
        """No `raw_text`, so no rewrite and no marker — the row stays exactly as it was."""
        assert compact_payload(EventType.SEAT_RESPONDED, abstained_payload(), MARKER) is None


class TestSnapshotFrozen:
    def test_the_body_goes_and_the_two_projected_fields_stay(self) -> None:
        """`_project_snapshot_frozen` reads exactly these two, which is why the invariant holds."""
        compacted = compact_payload(EventType.SNAPSHOT_FROZEN, snapshot_payload(), MARKER)

        assert compacted is not None
        assert "snapshot" not in compacted
        assert compacted["snapshot_id"] == "s-1"
        assert compacted["digest"] == "d-1"

    def test_a_second_pass_is_a_no_op(self) -> None:
        once = compact_payload(EventType.SNAPSHOT_FROZEN, snapshot_payload(), MARKER)
        assert once is not None

        assert compact_payload(EventType.SNAPSHOT_FROZEN, once, MARKER) is None


class TestCompactDay:
    async def test_it_rewrites_only_that_day(self, store: EventStore) -> None:
        await store.append(seat_event(NOON), seat_event(NEXT_DAY))

        rewritten = await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON)

        assert rewritten == 1
        remaining = [e for e in store.read_all() if "raw_text" in str(e.payload)]
        assert len(remaining) == 1
        assert remaining[0].ts == NEXT_DAY

    async def test_no_event_row_is_ever_deleted(self, store: EventStore) -> None:
        """The hard rule of the whole piece. Compaction rewrites payloads; it removes nothing."""
        await store.append(seat_event(NOON), seat_event(NOON), seat_event(NEXT_DAY))
        before = [event.seq for event in store.read_all()]

        await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON)

        assert [event.seq for event in store.read_all()] == before

    async def test_running_it_twice_rewrites_nothing_the_second_time(
        self, store: EventStore
    ) -> None:
        await store.append(seat_event(NOON))
        writer = writer_of(store)
        await compact_day(writer, day=DAY, archive=ARCHIVE, at=NOON)

        assert await compact_day(writer, day=DAY, archive=ARCHIVE, at=NOON) == 0

    async def test_the_marker_names_the_archive_the_payload_moved_to(
        self, store: EventStore
    ) -> None:
        await store.append(seat_event(NOON))

        await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON)

        (event,) = store.read_types(EventType.SEAT_RESPONDED)
        assert event.payload["compacted"]["archive"] == "2026-07-19.jsonl.gz"
        assert event.payload["compacted"]["sha256"] == "abc"

    async def test_a_chunk_of_uncompactable_rows_does_not_stall_the_day(
        self, store: EventStore
    ) -> None:
        """The defect found reviewing the plan, and the reason batching is by `seq`.

        A seat that abstained carries no `raw_text`, so the compactor returns `None` and the row
        never gains a marker. Batching that stopped on a zero *changed* count would leave those
        rows at the head of every batch and permanently stop compaction of everything behind
        them — silently, with no error and no failed pass. Three seats debating blind-then-debate
        makes a chunk's worth of them an ordinary day, not a corner case.
        """
        await store.append(
            seat_event(NOON, abstained_payload()),
            seat_event(NOON, abstained_payload()),
            seat_event(NOON, abstained_payload()),
            seat_event(NOON),
        )

        rewritten = await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON, chunk=3)

        assert rewritten == 1
        assert not any("raw_text" in str(e.payload) for e in store.read_all())

    async def test_it_spans_more_batches_than_one(self, store: EventStore) -> None:
        """Chunked so a cycle's append never queues behind a multi-second transaction."""
        await store.append(*[seat_event(NOON) for _ in range(7)])

        rewritten = await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON, chunk=2)

        assert rewritten == 7
        assert not any("raw_text" in str(e.payload) for e in store.read_all())

    async def test_a_day_with_nothing_compactable_is_zero_not_an_error(
        self, store: EventStore
    ) -> None:
        await store.append(
            Event(
                ts=NOON,
                type=EventType.CYCLE_COMPLETED,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload={"outcome": "no_trade", "cost_usd": "0"},
            )
        )

        assert await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON) == 0


def projection_snapshot(store: EventStore) -> dict[str, list[tuple[Any, ...]]]:
    """Every projection table, as comparable tuples.

    Read from `PROJECTION_TABLES` rather than hand-listed, so a projection added later is covered
    by the invariant without anyone remembering to add it here.
    """
    with store.engine.connect() as connection:
        return {
            table.name: [tuple(row) for row in connection.execute(select(table)).all()]
            for table in PROJECTION_TABLES
        }


class TestTheInvariant:
    """The property everything else rests on (spec §3.3).

    If one of these fails, the compactor is dropping a field a projector reads. Fix the compactor;
    never the assertion.
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
                payload={"basket_id": "demo", "venue": "sim"},
            ),
            Event(
                ts=NOON,
                type=EventType.SNAPSHOT_FROZEN,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload=snapshot_payload(),
            ),
            seat_event(NOON),
        )
        await store.rebuild()
        before = projection_snapshot(store)

        await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON)
        await store.rebuild()

        assert projection_snapshot(store) == before

    async def test_the_snapshot_digest_still_reaches_the_cycle_row(self, store: EventStore) -> None:
        """Named separately because it is *why* the invariant holds, not merely that it does."""
        await store.append(
            Event(
                ts=NOON,
                type=EventType.CYCLE_STARTED,
                aggregate_id="c-1",
                cycle_id="c-1",
                basket_id="demo",
                payload={"basket_id": "demo", "venue": "sim"},
            ),
            Event(
                ts=NOON,
                type=EventType.SNAPSHOT_FROZEN,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload=snapshot_payload(),
            ),
        )
        await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON)
        await store.rebuild()

        with store.engine.connect() as connection:
            row = connection.execute(select(cycles)).one()
        assert row.snapshot_digest == "d-1"
        assert row.snapshot_id == "s-1"

    async def test_it_holds_for_a_real_cycle_not_only_a_handmade_one(
        self, sim_application: Application
    ) -> None:
        """The strongest form: real payloads, written by the real loop, over every projection.

        A handmade event carries the fields the test author remembered. A cycle that actually ran
        carries the fields the system writes — orders, fills, positions, risk checks and round
        trips included — which is the population compaction has to be safe against.
        """
        await sim_application.recover()
        results = await sim_application.supervisor.run_once()
        assert results

        store = sim_application.store
        await store.rebuild()
        before = projection_snapshot(store)
        day = store.read_all()[0].ts.date()

        rewritten = await compact_day(
            writer_of(store),
            day=day,
            archive=ARCHIVE,
            at=sim_application.clock.now(),
        )
        await store.rebuild()

        assert rewritten > 0, "the cycle wrote nothing compactable; the test proves nothing"
        assert projection_snapshot(store) == before

    async def test_a_real_cycle_keeps_its_votes_costs_and_digest(
        self, sim_application: Application
    ) -> None:
        """What the drill-down and the cost report read must survive a real compaction."""
        await sim_application.recover()
        await sim_application.supervisor.run_once()
        store = sim_application.store
        day = store.read_all()[0].ts.date()

        await compact_day(
            writer_of(store), day=day, archive=ARCHIVE, at=sim_application.clock.now()
        )

        seats = store.read_types(EventType.SEAT_RESPONDED)
        assert seats
        for event in seats:
            response = event.payload["response"]
            assert "raw_text" not in response
            assert response["vote"]["action"]
            assert "cost_usd" in response
            assert event.payload[MARKER_KEY]["sha256"] == "abc"
        for event in store.read_types(EventType.SNAPSHOT_FROZEN):
            assert "snapshot" not in event.payload
            assert event.payload["digest"]


class TestPendingDays:
    """Which days a pass still has work for. The answer that stops the pass growing forever."""

    async def test_it_names_a_day_that_still_holds_a_transcript(self, store: EventStore) -> None:
        await store.append(seat_event(NOON))

        assert pending_days(store.engine, before=date(2026, 8, 1)) == [DAY]

    async def test_a_day_inside_the_hot_window_is_not_named(self, store: EventStore) -> None:
        await store.append(seat_event(NOON))

        assert pending_days(store.engine, before=DAY) == []

    async def test_a_fully_compacted_day_drops_out(self, store: EventStore) -> None:
        """The whole point. Selecting on the event *type* alone would keep it forever.

        And "forever" is not merely wasteful: once this day's archive is deleted at
        `archive_keep_days`, the next pass would find the file absent and recreate it from the
        already-compacted rows — a hollow archive, reappearing daily, contradicting D1a.
        """
        await store.append(seat_event(NOON))
        await compact_day(writer_of(store), day=DAY, archive=ARCHIVE, at=NOON)

        assert pending_days(store.engine, before=date(2026, 8, 1)) == []

    async def test_a_day_of_nothing_but_abstentions_never_keeps_itself_alive(
        self, store: EventStore
    ) -> None:
        """An abstention has no completion to drop, so it is not work and never becomes work."""
        await store.append(seat_event(NOON, abstained_payload()))

        assert pending_days(store.engine, before=date(2026, 8, 1)) == []

    async def test_an_uncompactable_type_never_names_a_day(self, store: EventStore) -> None:
        await store.append(
            Event(
                ts=NOON,
                type=EventType.CYCLE_COMPLETED,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload={"outcome": "no_trade", "cost_usd": "0"},
            )
        )

        assert pending_days(store.engine, before=date(2026, 8, 1)) == []

    async def test_days_come_back_oldest_first(self, store: EventStore) -> None:
        """The pass archives in order, so a crash mid-run leaves the oldest days done."""
        await store.append(
            seat_event(NEXT_DAY),
            seat_event(NOON),
            seat_event(datetime(2026, 7, 1, 9, 0, tzinfo=UTC)),
        )

        assert pending_days(store.engine, before=date(2026, 8, 1)) == [
            date(2026, 7, 1),
            DAY,
            date(2026, 7, 20),
        ]

    async def test_a_snapshot_body_also_names_its_day(self, store: EventStore) -> None:
        await store.append(
            Event(
                ts=NOON,
                type=EventType.SNAPSHOT_FROZEN,
                aggregate_id="c-1",
                cycle_id="c-1",
                payload=snapshot_payload(),
            )
        )

        assert pending_days(store.engine, before=date(2026, 8, 1)) == [DAY]


class TestTheDrillDownStaysHonest:
    """A compacted cycle *did* freeze a snapshot and its seats *did* answer (spec §3.6).

    The page must say where the detail went, not that it never existed. The seat case is the one
    easy to forget: compaction keeps the vote, the thesis and the cost, so a compacted transcript
    renders as *complete* unless something says otherwise.
    """

    def test_the_snapshot_marker_is_readable_from_the_payload(self) -> None:
        compacted = compact_payload(EventType.SNAPSHOT_FROZEN, snapshot_payload(), MARKER)

        assert compacted is not None
        assert compacted[MARKER_KEY]["archive"] == "2026-07-19.jsonl.gz"

    def test_a_compacted_seat_keeps_everything_the_page_renders_except_the_text(self) -> None:
        """Every field `monitor/cycle.html` reads off `payload.response` must survive."""
        payload = seat_payload()
        payload["response"].update(
            {
                "role": "Technical Analyst",
                "round_index": 0,
                "instrument_key": "sim:BTC/USDT",
                "latency_ms": 12,
            }
        )
        payload["response"]["vote"].update(
            {"size_hint": "half", "key_risks": [], "invalidation": "x"}
        )

        compacted = compact_payload(EventType.SEAT_RESPONDED, payload, MARKER)

        assert compacted is not None
        rendered = compacted["response"]
        for field in (
            "role",
            "seat_id",
            "round_index",
            "instrument_key",
            "provider_id",
            "model",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "call_id",
            "cost_usd",
        ):
            assert field in rendered, field
        for field in ("action", "conviction", "size_hint", "thesis", "key_risks", "invalidation"):
            assert field in rendered["vote"], field
