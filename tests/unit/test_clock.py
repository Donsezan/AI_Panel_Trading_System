"""Clock: UTC discipline and a deterministic test double."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tradebot.core.clock import Clock, ManualClock, SystemClock, ensure_utc
from tradebot.core.errors import ConfigError


class TestEnsureUtc:
    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ConfigError, match="naive datetime rejected"):
            ensure_utc(datetime(2026, 7, 26, 12, 0))

    def test_converts_other_zones_to_utc(self) -> None:
        moscow = datetime(2026, 7, 26, 15, 0, tzinfo=timezone(timedelta(hours=3)))
        assert ensure_utc(moscow) == datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    def test_passes_utc_through(self) -> None:
        value = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        assert ensure_utc(value) == value


class TestSystemClock:
    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(SystemClock(), Clock)

    def test_now_is_utc_aware(self) -> None:
        assert SystemClock().now().tzinfo is UTC

    def test_monotonic_never_goes_backwards(self) -> None:
        clock = SystemClock()
        assert clock.monotonic() <= clock.monotonic()

    async def test_sleep_awaits(self) -> None:
        await SystemClock().sleep(0)


class TestManualClock:
    def test_time_only_moves_when_moved(self) -> None:
        clock = ManualClock(datetime(2026, 7, 26, tzinfo=UTC))
        start = clock.now()
        clock.advance(90)
        assert clock.now() == start + timedelta(seconds=90)
        assert clock.monotonic() == 90.0

    async def test_sleep_advances_instead_of_waiting(self) -> None:
        """Scenario tests cover hours of cycles without waiting hours."""
        clock = ManualClock(datetime(2026, 7, 26, tzinfo=UTC))
        await clock.sleep(3600)
        assert clock.now() == datetime(2026, 7, 26, 1, 0, tzinfo=UTC)

    def test_rejects_a_naive_start(self) -> None:
        with pytest.raises(ConfigError):
            ManualClock(datetime(2026, 7, 26))
