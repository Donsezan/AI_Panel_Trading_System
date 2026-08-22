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
from tradebot.core.events import EventFactory
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
    async def test_no_sink_means_the_log_is_never_read(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        store, cursor, _ = wired
        await a_trip(store, clock)
        dispatcher = dispatcher_for(wired, clock)

        assert not dispatcher.enabled
        assert await dispatcher.poll() == ()
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
        for _ in range(50):
            await asyncio.sleep(0)
            if sink.sent:
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        assert [alert.kind for alert in sink.sent] == [AlertKind.KILL_SWITCH]

    async def test_a_tail_with_no_destination_returns_instead_of_spinning(
        self, wired: tuple[EventStore, AlertCursorStore, Engine], clock: ManualClock
    ) -> None:
        await dispatcher_for(wired, clock).run(poll_seconds=0)
