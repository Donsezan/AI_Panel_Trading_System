"""The log tail: what it delivers, what it remembers, and what a restart does to it.

The cursor is the whole design. It advances only after delivery, which makes the guarantee
at-least-once — a repeated kill-switch alert is an annoyance, a missed one is the failure this
exists to prevent (ADR 0019).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from tradebot.core.clock import ManualClock
from tradebot.core.enums import BasketStatus, CycleOutcome, KillSwitchState
from tradebot.core.errors import VenueError
from tradebot.core.events import Event, EventFactory, EventType
from tradebot.interfaces.alerts import Alert, AlertKind, Severity
from tradebot.ops.cursor import AlertCursorStore
from tradebot.ops.dispatcher import AlertDispatcher
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore


class RecordingSink:
    """A sink that keeps what it was sent, and can be made to fail on demand."""

    def __init__(self, sink_id: str = "recorder", *, failing: bool = False) -> None:
        self.sink_id = sink_id
        self.sent: list[Alert] = []
        self.failing = failing

    async def send(self, alert: Alert) -> None:
        if self.failing:
            raise VenueError(f"{self.sink_id} is down")
        self.sent.append(alert)


@pytest.fixture
def wired(clock: ManualClock) -> Iterator[tuple[EventStore, AlertCursorStore, Engine]]:
    """One database shared by the log and the cursor, as a real process has."""
    engine = create_database(None)
    writer = SingleWriter(engine)
    yield EventStore(engine, writer), AlertCursorStore(engine, writer, clock), engine
    writer.close()


def dispatcher_for(
    wired: tuple[EventStore, AlertCursorStore, Engine],
    clock: ManualClock,
    *sinks: RecordingSink,
    degraded_streak: int = 3,
) -> AlertDispatcher:
    store, cursor, _ = wired
    return AlertDispatcher(store, cursor, sinks, clock, degraded_streak=degraded_streak)


def events_for(clock: ManualClock, cycle_id: str = "c1") -> EventFactory:
    return EventFactory(clock=clock, basket_id="demo", cycle_id=cycle_id)


async def a_trip(store: EventStore, clock: ManualClock, reason: str = "drawdown 12%") -> None:
    await store.append(
        events_for(clock).kill_switch_changed(KillSwitchState.TRIPPED, reason, actor="watchdog")
    )


class TestEnablement:
    """`enabled` gates **delivery**, and only delivery (spec 5.1).

    It used to gate the tail itself, which meant that on a machine with no webhook -- the sim and
    paper case -- the rules never evaluated at all, and anything fed by them was permanently empty.
    """

    async def test_no_sink_means_nothing_is_delivered(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, cursor, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()
        await a_trip(store, clock)

        assert not dispatcher.enabled
        assert await dispatcher.poll() == ()
        # The delivery cursor never moves, because nothing was ever delivered.
        assert cursor.load().last_seq == 0


class TestFirstPoll:
    async def test_a_fresh_database_starts_at_the_end_of_the_log(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """Switching alerting on after three weeks must not replay three weeks of incidents."""
        store, cursor, _ = wired
        await a_trip(store, clock, "an incident from last month")
        sink = RecordingSink()

        delivered = await dispatcher_for(wired, clock, sink).poll()

        assert delivered == ()
        assert sink.sent == []
        assert cursor.load().last_seq == store.last_seq()

    async def test_what_happens_after_that_is_delivered(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()

        await a_trip(store, clock, "the one that matters")
        delivered = await dispatcher.poll()

        assert [alert.kind for alert in delivered] == [AlertKind.KILL_SWITCH]
        assert "the one that matters" in sink.sent[0].body


class TestCursor:
    async def test_an_alert_is_delivered_once_across_polls(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()

        await a_trip(store, clock)
        await dispatcher.poll()
        await dispatcher.poll()

        assert len(sink.sent) == 1

    async def test_a_restart_resumes_where_delivery_stopped(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        first = RecordingSink()
        await dispatcher_for(wired, clock, first).poll()
        await a_trip(store, clock)
        await dispatcher_for(wired, clock, first).poll()

        # A second process against the same database: the cursor is the only shared state.
        second = RecordingSink()
        await dispatcher_for(wired, clock, second).poll()

        assert len(first.sent) == 1
        assert second.sent == []

    async def test_a_failed_delivery_leaves_the_cursor_and_retries(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """Nothing behind an undelivered alert may be skipped past it."""
        store, cursor, _ = wired
        sink = RecordingSink(failing=True)
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()
        before = cursor.load().last_seq

        await a_trip(store, clock)
        await dispatcher.poll()
        assert cursor.load().last_seq == before

        sink.failing = False
        await dispatcher.poll()

        assert [alert.kind for alert in sink.sent] == [AlertKind.KILL_SWITCH]

    async def test_one_broken_destination_does_not_silence_a_working_one(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, cursor, _ = wired
        working, broken = RecordingSink("ok"), RecordingSink("broken", failing=True)
        dispatcher = dispatcher_for(wired, clock, working, broken)
        await dispatcher.poll()

        await a_trip(store, clock)
        await dispatcher.poll()
        await dispatcher.poll()

        assert len(working.sent) == 1
        assert cursor.load().last_seq == store.last_seq()

    async def test_events_after_an_alert_are_still_read(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()

        await a_trip(store, clock)
        await store.append(
            events_for(clock).basket_status_changed("demo", BasketStatus.HALTED, "cause")
        )
        delivered = await dispatcher.poll()

        assert [alert.kind for alert in delivered] == [
            AlertKind.KILL_SWITCH,
            AlertKind.BASKET_HALTED,
        ]


class TestDegradedStreak:
    async def _degrade(self, store: EventStore, clock: ManualClock, times: int) -> None:
        for index in range(times):
            await store.append(
                events_for(clock, f"c{index}").cycle_completed(
                    CycleOutcome.PANEL_DEGRADED, Decimal(0)
                )
            )

    async def test_the_streak_survives_a_restart(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """A streak counted in memory is a streak a restart forgives."""
        store, _, _ = wired
        first = RecordingSink()
        await dispatcher_for(wired, clock, first, degraded_streak=3).poll()

        await self._degrade(store, clock, 2)
        await dispatcher_for(wired, clock, first, degraded_streak=3).poll()
        assert first.sent == []

        await self._degrade(store, clock, 1)
        second = RecordingSink()
        await dispatcher_for(wired, clock, second, degraded_streak=3).poll()

        assert [alert.kind for alert in second.sent] == [AlertKind.PROVIDER_FAILURE]


class TestStaleStreak:
    """The market-data streak is persisted beside the degraded one, for the same reason."""

    async def _starve(self, store: EventStore, clock: ManualClock, times: int) -> None:
        for index in range(times):
            await store.append(
                events_for(clock, f"s{index}").cycle_completed(CycleOutcome.DATA_STALE, Decimal(0))
            )

    async def test_the_streak_survives_a_restart(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, cursor, _ = wired
        await dispatcher_for(wired, clock, RecordingSink(), degraded_streak=3).poll()

        await self._starve(store, clock, 2)
        await dispatcher_for(wired, clock, RecordingSink(), degraded_streak=3).poll()
        assert cursor.load().stale_streak == 2

        await self._starve(store, clock, 1)
        resumed = RecordingSink()
        await dispatcher_for(wired, clock, resumed, degraded_streak=3).poll()

        assert [alert.kind for alert in resumed.sent] == [AlertKind.DATA_STALE]


class TestDailySummary:
    async def test_the_first_poll_does_not_summarise_a_day_it_only_saw_the_end_of(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        sink = RecordingSink()

        await dispatcher_for(wired, clock, sink).poll()

        assert sink.sent == []

    async def test_a_day_rolling_over_sends_exactly_one_summary(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()

        events = events_for(clock)
        await store.append(events.cycle_started((), "sim"))
        await store.append(events.cycle_completed(CycleOutcome.ORDERS_PLACED, Decimal("0.03")))

        clock.advance(timedelta(days=1).total_seconds())
        await dispatcher.poll()
        await dispatcher.poll()

        summaries = [alert for alert in sink.sent if alert.kind is AlertKind.DAILY_SUMMARY]
        assert len(summaries) == 1
        assert "1 cycles" in summaries[0].title
        assert "orders_placed=1" in summaries[0].body
        assert summaries[0].kind.severity is Severity.LOW

    async def test_a_failed_summary_is_retried_rather_than_skipped(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        sink = RecordingSink(failing=True)
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()

        clock.advance(timedelta(days=1).total_seconds())
        await dispatcher.poll()
        sink.failing = False
        await dispatcher.poll()

        assert [alert.kind for alert in sink.sent] == [AlertKind.DAILY_SUMMARY]


class TestResilience:
    async def test_a_poll_that_raises_does_not_end_the_tail(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """A dispatcher that died in week one alerts on nothing in weeks two to six."""
        store, _, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()
        await a_trip(store, clock)

        exploded = False

        async def once_broken() -> tuple[Alert, ...]:
            nonlocal exploded
            if not exploded:
                exploded = True
                raise RuntimeError("transient nonsense")
            return await AlertDispatcher.poll(dispatcher)

        dispatcher.poll = once_broken  # type: ignore[method-assign]
        task = asyncio.create_task(dispatcher.run(poll_seconds=0))
        # A real yield, not `sleep(0)`: every append and cursor save is a hop through the single
        # writer's thread, and a tight loop of bare yields can starve it of a scheduling slot.
        for _ in range(100):
            await asyncio.sleep(0.005)
            if sink.sent:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert [alert.kind for alert in sink.sent] == [AlertKind.KILL_SWITCH]

    async def test_a_tail_with_no_destination_keeps_recording(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """It used to return immediately, so `poll` was never reached on a sim or paper box.

        The loop now runs whatever is configured; only delivery is gated (spec 5.1).

        The log is read **after** the task is cancelled, never while it runs: on the in-memory
        engine every connection is one shared connection, so a reader returning it to the pool
        would roll back the writer thread's open transaction and this would pass or fail by
        timing (CLAUDE.md, Testing).
        """
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()
        await a_trip(store, clock)

        task = asyncio.create_task(dispatcher.run(poll_seconds=60))
        await asyncio.sleep(0.05)
        still_tailing = not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert still_tailing
        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1


class TestRecordingWithoutSinks:
    """The blocking defect this piece exists to fix: with no webhook, nothing ran the rules.

    Recording is unconditional; delivery is not. The dashboard is the only destination a sim or
    paper run has, and it reads what recording writes (spec 5.1).
    """

    async def test_a_dispatcher_with_no_sinks_still_records(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()
        await a_trip(store, clock)

        await dispatcher.poll()

        (raised,) = store.read_types(EventType.NOTIFICATION_RAISED)
        assert raised.payload["kind"] == AlertKind.KILL_SWITCH.value
        assert raised.payload["severity"] == Severity.HIGH.value
        assert "drawdown 12%" in raised.payload["body"]

    async def test_the_identity_is_deterministic_from_the_event_that_caused_it(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """What makes a re-record idempotent, and what the projection keys on (spec 5.5)."""
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()
        await a_trip(store, clock)
        source = store.last_seq()

        await dispatcher.poll()

        (raised,) = store.read_types(EventType.NOTIFICATION_RAISED)
        assert raised.payload["alert_id"] == f"{source}:{AlertKind.KILL_SWITCH.value}"
        assert raised.payload["event_seq"] == source

    async def test_the_dispatcher_never_reads_its_own_writes(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """`NOTIFICATION_RAISED` is deliberately not an alert type; otherwise this is a loop."""
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()
        await a_trip(store, clock)

        await dispatcher.poll()
        await dispatcher.poll()
        await dispatcher.poll()

        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1

    async def test_recording_advances_its_own_cursor_while_delivery_stalls(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """A dead webhook must not withhold what the operator could already see on screen."""
        store, cursor, _ = wired
        sink = RecordingSink(failing=True)
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()
        anchored = cursor.load().last_seq
        await a_trip(store, clock)

        await dispatcher.poll()
        await dispatcher.poll()

        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1
        assert cursor.load().recorded_seq > anchored
        assert cursor.load().last_seq == anchored

    async def test_the_streaks_are_counted_once_by_the_recorder(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """One evaluation, one persisted RuleState -- delivery must not re-count on top of it.

        With the two cursors at different positions, a second evaluation inside `_drain` would
        raise PROVIDER_FAILURE at a different count on screen than in the webhook.
        """
        store, cursor, _ = wired
        sink = RecordingSink(failing=True)
        dispatcher = dispatcher_for(wired, clock, sink, degraded_streak=3)
        await dispatcher.poll()

        for index in range(2):
            await store.append(
                events_for(clock, f"d{index}").cycle_completed(
                    CycleOutcome.PANEL_DEGRADED, Decimal(0)
                )
            )
        await dispatcher.poll()
        await dispatcher.poll()

        assert cursor.load().degraded_streak == 2


class TestFirstPollAnchorsBothCursors:
    async def test_a_database_alerting_never_ran_against_records_nothing_historic(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """ADR 0019's rule, applied to the new cursor: three weeks of history is not news.

        Without this the bell fills, on first boot after the upgrade, with every incident the log
        has ever held -- and an operator who scrolls past a hundred stale rows has learned to
        scroll past the one that matters.
        """
        store, cursor, _ = wired
        await a_trip(store, clock, "an incident from last month")

        await dispatcher_for(wired, clock).poll()

        assert store.read_types(EventType.NOTIFICATION_RAISED) == ()
        assert cursor.load().recorded_seq == store.last_seq()


class TestRecordingTheSummary:
    """Section 5.3 gives the summary a severity and 5.8 lists every undismissed notification.

    A summary produced only when a sink is configured would never reach the one destination a
    sim or paper operator has.
    """

    async def test_a_summary_is_recorded_without_any_sink(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()

        clock.advance(timedelta(days=1).total_seconds())
        await dispatcher.poll()

        (raised,) = store.read_types(EventType.NOTIFICATION_RAISED)
        assert raised.payload["kind"] == AlertKind.DAILY_SUMMARY.value
        assert raised.payload["severity"] == Severity.LOW.value

    async def test_one_summary_a_day_however_often_it_is_polled(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()

        clock.advance(timedelta(days=1).total_seconds())
        await dispatcher.poll()
        await dispatcher.poll()

        assert len(store.read_types(EventType.NOTIFICATION_RAISED)) == 1

    async def test_the_summary_identity_names_its_day(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """It has no source event to key on, so the day is what makes it idempotent."""
        store, _, _ = wired
        dispatcher = dispatcher_for(wired, clock)
        await dispatcher.poll()

        clock.advance(timedelta(days=1).total_seconds())
        await dispatcher.poll()

        (raised,) = store.read_types(EventType.NOTIFICATION_RAISED)
        assert raised.payload["alert_id"].startswith("summary:")
        assert raised.payload["event_seq"] == 0


class TestDeliveringWhatWasRecorded:
    async def test_the_delivered_alert_is_the_recorded_one_rebuilt(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """Delivery evaluates nothing: it reads back what recording already decided."""
        store, _, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()
        await a_trip(store, clock, "drawdown 12% below the mark")

        await dispatcher.poll()

        (sent,) = sink.sent
        (raised,) = store.read_types(EventType.NOTIFICATION_RAISED)
        assert sent.kind is AlertKind.KILL_SWITCH
        assert sent.title == raised.payload["title"]
        assert sent.body == raised.payload["body"]
        assert sent.scope == raised.payload["scope"]
        assert sent.at.isoformat() == raised.payload["at"]

    async def test_a_kind_this_version_cannot_read_is_skipped_not_blocked_on(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        """A rollback past a version that added an `AlertKind` is the realistic way this happens.

        The projection stores `kind` as text and is unbothered; delivery has to build the enum
        and cannot. It must cost that one notice, never the delivery of everything behind it.
        """
        store, cursor, _ = wired
        sink = RecordingSink()
        dispatcher = dispatcher_for(wired, clock, sink)
        await dispatcher.poll()
        await store.append(
            Event(
                ts=clock.now(),
                type=EventType.NOTIFICATION_RAISED,
                aggregate_id="notifications",
                payload={
                    "alert_id": "1:from_the_future",
                    "kind": "from_the_future",
                    "severity": "high",
                    "at": clock.now().isoformat(),
                    "scope": "portfolio",
                    "title": "a kind this version has never heard of",
                    "body": "",
                    "event_seq": 1,
                },
            )
        )
        await a_trip(store, clock)

        await dispatcher.poll()

        assert [alert.kind for alert in sink.sent] == [AlertKind.KILL_SWITCH]
        assert cursor.load().last_seq == store.last_seq()
