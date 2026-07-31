"""When each basket's next cycle is due (DESIGN §6.1).

A fire time is the **earliest valid candidate**, and there are only ever two of them:

1. the next **grid tick** — `Schedule.next_tick`, computed from the interval and its offset
   against the UTC epoch rather than from when the process started, so a restart cannot shift a
   basket's cadence and a past cycle's timing is reproducible from the log; and
2. the next **session's first cycle** — the venue's next open plus `open_delay_seconds`, offered
   only when the venue is currently shut.

Whichever comes first *and* falls inside an open session wins. That one rule covers both asset
classes: a crypto venue is never shut, so only the grid is ever a candidate; an equities basket
does not cycle overnight, and `market_open+15m` is simply a daily interval whose session
candidate wins — no second schedule kind, no branch on asset class, one set of tests.

Overlap is prevented structurally rather than by a check: the supervisor gives each basket one
task that computes its next fire from the instant the previous cycle *finished*, so a cycle that
overruns its interval skips ticks instead of racing itself.

Failure semantics: fail closed. A schedule the calendar cannot satisfy — a venue that publishes
no next open, or an `open_delay_seconds` longer than the session it delays into — raises
`ConfigError`, which halts that basket for a human rather than guessing a time to trade at.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.config import Schedule
from tradebot.core.errors import ConfigError
from tradebot.interfaces.broker import TradingCalendar

#: How many closed candidates may be skipped before the schedule is judged unsatisfiable. A
#: weekend plus a holiday runs to three; beyond that it is a misconfiguration, not a quiet market.
MAX_DEFERRALS = 8


class Scheduler:
    """Computes fire times and waits for them, on an injected clock."""

    def __init__(self, clock: Clock, *, max_deferrals: int = MAX_DEFERRALS) -> None:
        self._clock = clock
        self._max_deferrals = max_deferrals

    async def next_fire(
        self, schedule: Schedule, calendar: TradingCalendar, *, after: datetime
    ) -> datetime:
        """The instant this basket should next cycle, strictly after `after`."""
        moment = ensure_utc(after)
        for _ in range(self._max_deferrals):
            candidates = await self._candidates(schedule, calendar, moment)
            for candidate in candidates:
                if candidate > moment and await calendar.is_open(candidate):
                    return candidate
            moment = candidates[-1]
        raise ConfigError(
            f"a schedule of every {schedule.every_seconds}s never lands in an open session on "
            f"{calendar.venue_id} within {self._max_deferrals} attempts after {after.isoformat()}"
        )

    async def _candidates(
        self, schedule: Schedule, calendar: TradingCalendar, moment: datetime
    ) -> list[datetime]:
        """The grid tick, plus the next session's first cycle when the venue is shut."""
        candidates = [schedule.next_tick(moment)]
        if not await calendar.is_open(moment):
            opens = await calendar.next_open(moment)
            if opens is None:
                raise ConfigError(
                    f"{calendar.venue_id} is closed at {moment.isoformat()} and publishes no next "
                    "open; refusing to guess when trading resumes"
                )
            candidates.append(ensure_utc(opens) + timedelta(seconds=schedule.open_delay_seconds))
        return sorted(candidates)

    async def wait_until(self, when: datetime) -> None:
        """Sleep until `when`. Returns immediately if it has already passed."""
        delay = (ensure_utc(when) - self._clock.now()).total_seconds()
        if delay > 0:
            await self._clock.sleep(delay)
