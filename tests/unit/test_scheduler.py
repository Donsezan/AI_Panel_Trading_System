"""Scheduling: when a basket cycles, and when it deliberately does not.

The two rules under test are the ones an equities basket depends on and a crypto basket must not
be disturbed by: ticks are epoch-aligned (so a restart cannot shift a cadence), and a tick landing
in a closed market is deferred to the session open plus the schedule's delay — which is how
`market_open+15m` is expressed without a second schedule kind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradebot.control.scheduler import Scheduler
from tradebot.core.clock import ManualClock
from tradebot.core.config import Schedule
from tradebot.core.errors import ConfigError
from tradebot.execution.brokers.calendars import ContinuousCalendar

NOW = datetime(2026, 7, 30, 12, 3, 17, tzinfo=UTC)
OPEN = datetime(2026, 7, 30, 13, 30, tzinfo=UTC)


class SessionCalendar:
    """A calendar with one session a day, so deferral has somewhere to defer to."""

    venue_id = "equities"

    def __init__(self, opens: datetime = OPEN, hours: float = 6.5) -> None:
        self._opens = opens
        self._length = timedelta(hours=hours)

    def _session(self, day_offset: int) -> tuple[datetime, datetime]:
        start = self._opens + timedelta(days=day_offset)
        return start, start + self._length

    async def is_open(self, at: datetime) -> bool:
        return any(
            start <= at < end for start, end in (self._session(d) for d in range(-2, 400, 1))
        )

    async def session_day(self, at: datetime) -> str:
        return at.date().isoformat()

    async def next_open(self, after: datetime) -> datetime | None:
        if await self.is_open(after):
            return None
        return next(
            (start for start, _ in (self._session(d) for d in range(-2, 400)) if start > after),
            None,
        )


class ShutForever:
    """A venue that is closed and publishes no next open — the fail-closed case."""

    venue_id = "shut"

    async def is_open(self, at: datetime) -> bool:
        return False

    async def session_day(self, at: datetime) -> str:
        return at.date().isoformat()

    async def next_open(self, after: datetime) -> datetime | None:
        return None


@pytest.fixture
def scheduler(clock: ManualClock) -> Scheduler:
    return Scheduler(clock)


class TestTickAlignment:
    def test_ticks_are_aligned_to_the_epoch_not_to_process_start(self) -> None:
        """A restart must not shift a cadence: 12:03:17 on a 10-minute schedule is 12:10."""
        assert Schedule(every_seconds=600).next_tick(NOW) == NOW.replace(
            minute=10, second=0, microsecond=0
        )

    def test_an_offset_phases_the_interval(self) -> None:
        """`every 1h at :05` is every_seconds=3600, offset_seconds=300."""
        schedule = Schedule(every_seconds=3600, offset_seconds=300)
        assert schedule.next_tick(NOW) == NOW.replace(minute=5, second=0, microsecond=0)

    def test_a_tick_is_strictly_after_the_instant_asked_about(self) -> None:
        """The next fire is computed from the end of a cycle; it must not return that instant."""
        on_the_tick = NOW.replace(minute=10, second=0, microsecond=0)
        assert Schedule(every_seconds=600).next_tick(on_the_tick) == on_the_tick + timedelta(
            minutes=10
        )

    def test_an_offset_outside_the_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="inside one interval"):
            Schedule(every_seconds=600, offset_seconds=600)

    def test_two_processes_reading_one_schedule_agree(self) -> None:
        """Reproducible timing is what makes a past cycle's instant explainable from the log."""
        schedule = Schedule(every_seconds=900)
        assert schedule.next_tick(NOW) == schedule.next_tick(NOW + timedelta(seconds=1))


class TestCalendarGating:
    async def test_a_continuous_venue_fires_on_every_tick(self, scheduler: Scheduler) -> None:
        due = await scheduler.next_fire(
            Schedule(every_seconds=600), ContinuousCalendar("binance"), after=NOW
        )
        assert due == NOW.replace(minute=10, second=0, microsecond=0)

    async def test_a_tick_in_a_closed_market_defers_to_the_open(self, scheduler: Scheduler) -> None:
        """An equities basket simply does not cycle while the market is shut (DESIGN §6.1)."""
        due = await scheduler.next_fire(
            Schedule(every_seconds=600), SessionCalendar(), after=NOW.replace(hour=3)
        )
        assert due == OPEN

    async def test_market_open_plus_a_delay_is_an_ordinary_daily_schedule(
        self, scheduler: Scheduler
    ) -> None:
        """`market_open+15m`: one rule covers it, so equities and crypto share a code path."""
        schedule = Schedule(every_seconds=86_400, open_delay_seconds=900)

        due = await scheduler.next_fire(schedule, SessionCalendar(), after=NOW.replace(hour=3))

        assert due == OPEN + timedelta(minutes=15)

    async def test_a_daily_schedule_fires_once_per_session(self, scheduler: Scheduler) -> None:
        schedule = Schedule(every_seconds=86_400, open_delay_seconds=900)
        calendar = SessionCalendar()

        first = await scheduler.next_fire(schedule, calendar, after=NOW.replace(hour=3))
        second = await scheduler.next_fire(schedule, calendar, after=first)

        assert second == first + timedelta(days=1)

    async def test_the_session_open_beats_a_later_grid_tick(self, scheduler: Scheduler) -> None:
        """The first cycle of a session happens at the open, not at the next round hour."""
        due = await scheduler.next_fire(
            Schedule(every_seconds=3600), SessionCalendar(), after=NOW.replace(hour=13, minute=0)
        )
        assert due == OPEN

    async def test_a_daily_tick_does_not_skip_todays_session(self, scheduler: Scheduler) -> None:
        """The grid tick lands at tomorrow's midnight; today's open must still win."""
        schedule = Schedule(every_seconds=86_400)

        due = await scheduler.next_fire(schedule, SessionCalendar(), after=NOW.replace(hour=3))

        assert due == OPEN

    async def test_a_tick_inside_the_session_is_left_alone(self, scheduler: Scheduler) -> None:
        due = await scheduler.next_fire(
            Schedule(every_seconds=600), SessionCalendar(), after=OPEN + timedelta(minutes=1)
        )
        assert due == OPEN + timedelta(minutes=10)


class TestFailClosed:
    async def test_a_venue_that_never_opens_is_refused_rather_than_guessed_at(
        self, scheduler: Scheduler
    ) -> None:
        with pytest.raises(ConfigError, match="publishes no next open"):
            await scheduler.next_fire(Schedule(), ShutForever(), after=NOW)

    async def test_a_delay_longer_than_the_session_is_refused(self, clock: ManualClock) -> None:
        """A delay that always lands after the close would defer forever; it is a config defect."""
        scheduler = Scheduler(clock, max_deferrals=3)
        schedule = Schedule(every_seconds=86_400, open_delay_seconds=60 * 60 * 12)

        with pytest.raises(ConfigError, match="never lands in an open session"):
            await scheduler.next_fire(schedule, SessionCalendar(), after=NOW.replace(hour=3))


class TestWaiting:
    async def test_waiting_sleeps_exactly_until_the_due_instant(self, clock: ManualClock) -> None:
        scheduler = Scheduler(clock)
        due = clock.now() + timedelta(minutes=7)

        await scheduler.wait_until(due)

        assert clock.now() == due

    async def test_a_time_already_past_does_not_sleep(self, clock: ManualClock) -> None:
        scheduler = Scheduler(clock)
        started = clock.now()

        await scheduler.wait_until(started - timedelta(minutes=1))

        assert clock.now() == started
