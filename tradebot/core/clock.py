"""Time. UTC-aware everywhere; naive datetimes are rejected at the model boundary.

The clock is injected, never read from the ambient environment, because `freezegun` does not
affect `loop.time()` — a scheduler that reads the wall clock directly cannot be tested
deterministically (REVIEW C9). Every component that needs time takes a `Clock`.

Failure semantics: `SystemClock` cannot fail. Clock *skew* against a venue is a separate
startup check — signature rejection from a skewed clock is itself a ban vector (PLAN §3.1).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from tradebot.core.errors import ConfigError


def ensure_utc(value: datetime) -> datetime:
    """Return `value` as a UTC-aware datetime, rejecting naive input.

    A naive datetime is ambiguous, and candle alignment and auth signatures both break on it.
    Timezone-aware non-UTC input is converted rather than rejected.
    """
    if value.tzinfo is None:
        raise ConfigError(f"naive datetime rejected, timezone required: {value!r}")
    return value.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    """Source of time for everything that schedules, ages, or timestamps."""

    def now(self) -> datetime:
        """Current UTC-aware wall-clock time. Used for timestamps and staleness."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds. Used for durations and timeouts; immune to wall-clock jumps."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for `seconds`."""
        ...


class SystemClock:
    """The real clock. The only implementation used outside tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """Test clock whose time only moves when a test moves it.

    `sleep` advances time and yields to the event loop instead of waiting, so scenario tests
    covering hours of cycles run in milliseconds and never depend on real timing.
    """

    def __init__(self, start: datetime, *, monotonic_start: float = 0.0) -> None:
        self._now = ensure_utc(start)
        self._monotonic = monotonic_start

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def set(self, moment: datetime) -> None:
        """Jump to an absolute instant, for replaying a specific point in history."""
        self._now = ensure_utc(moment)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)
