"""Normalized market data. Every provider returns these shapes, whatever its wire format.

Every series carries `observed_at`, so staleness is a property of the data rather than a guess
by the consumer, and so a replayed cycle can prove it only saw what existed at the time.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from pydantic import model_validator

from tradebot.core.errors import DataStaleError
from tradebot.core.schema import DomainModel, Money, UtcDatetime

#: Bar durations. Shared by the staleness policy, the replay provider and the scheduler, so a
#: timeframe means exactly one thing everywhere.
TIMEFRAME_INTERVALS: Mapping[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


def timeframe_interval(timeframe: str) -> timedelta:
    """Bar duration for a timeframe. Unknown timeframes fail closed rather than defaulting."""
    if timeframe not in TIMEFRAME_INTERVALS:
        raise DataStaleError(f"unsupported timeframe {timeframe!r}")
    return TIMEFRAME_INTERVALS[timeframe]


class Candle(DomainModel):
    """One OHLCV bar. `close_time` is exclusive of the next bar's open."""

    open_time: UtcDatetime
    close_time: UtcDatetime
    open: Money
    high: Money
    low: Money
    close: Money
    volume: Money

    @model_validator(mode="after")
    def _check_bounds(self) -> Candle:
        if self.high < self.low:
            raise ValueError(f"candle high {self.high} below low {self.low}")
        if self.close_time <= self.open_time:
            raise ValueError("candle close_time must follow open_time")
        return self


class CandleSeries(DomainModel):
    """Candles for one instrument and timeframe, oldest first.

    Gaps are left explicit rather than interpolated: a fabricated bar would feed a fabricated
    indicator value into a real order.
    """

    instrument_key: str
    timeframe: str
    candles: tuple[Candle, ...]
    observed_at: UtcDatetime

    @model_validator(mode="after")
    def _check_ordering(self) -> CandleSeries:
        times = [candle.open_time for candle in self.candles]
        if times != sorted(times):
            raise ValueError("candles must be ordered oldest first")
        return self

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def latest(self) -> Candle:
        if not self.candles:
            raise DataStaleError(f"no candles for {self.instrument_key} {self.timeframe}")
        return self.candles[-1]

    def age(self, now: UtcDatetime) -> timedelta:
        """How far behind the market this series is — measured from the last bar's *close*.

        Not from `observed_at`: a provider that keeps returning a cached or lagging series has
        a fresh fetch timestamp and stale content, which is exactly the failure to catch.
        """
        return now - self.latest.close_time

    def fetch_age(self, now: UtcDatetime) -> timedelta:
        """How long ago we fetched. Catches a cache that stopped refreshing."""
        return now - self.observed_at

    def require_fresh(self, now: UtcDatetime, max_age: timedelta) -> None:
        """Raise `DataStaleError` unless both the content and the fetch are recent enough.

        Fail closed: a stale series aborts the cycle rather than producing a decision from data
        the market has already moved past (DESIGN §6.2). `max_age` must account for the bar
        interval — a freshly closed 1d bar is legitimately almost a day old.
        """
        for label, age in (("content", self.age(now)), ("fetch", self.fetch_age(now))):
            if age > max_age:
                raise DataStaleError(
                    f"{self.instrument_key} {self.timeframe} {label} is {age} old, limit {max_age}"
                )


class Quote(DomainModel):
    """Top of book at a point in time."""

    instrument_key: str
    bid: Money
    ask: Money
    last: Money
    observed_at: UtcDatetime

    @model_validator(mode="after")
    def _check_spread(self) -> Quote:
        if self.ask < self.bid:
            raise ValueError(f"crossed quote: ask {self.ask} below bid {self.bid}")
        return self

    @property
    def spread(self) -> Money:
        return self.ask - self.bid
