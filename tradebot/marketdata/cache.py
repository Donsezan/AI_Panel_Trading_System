"""A caching decorator over any `MarketDataProvider`. Its job is to not get us banned.

Three baskets on a 10-minute cadence asking for three timeframes each is nine identical
`klines` calls per cycle for the same bars, which is how a rate budget gets spent on nothing
(PLAN §3.1). Two mechanisms fix that:

* **Bar-boundary expiry.** A closed bar cannot change, so a series stays valid until the moment
  the *next* bar could have closed. The cache therefore holds each series for at most one bar
  interval, derived from the timeframe rather than a guessed TTL.
* **Single flight.** Concurrent requests for the same key wait on one in-progress fetch instead
  of each issuing their own.

**The cache cannot hide stale data.** A cached series keeps its original `observed_at`, so
`CandleSeries.require_fresh` ages it exactly as if it had just been fetched — a cache that
stopped refreshing produces `DATA_STALE`, never a fresh-looking stale price (DESIGN §6.2).

Failure semantics: a failed fetch is not cached, so the next caller retries rather than
inheriting an error. The cache never serves an entry past its expiry, and never serves one at
all for a point-in-time (`end=`) request from a different cutoff.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, TypeVar

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.instrument import Instrument
from tradebot.core.market import CandleSeries, Quote, timeframe_interval
from tradebot.interfaces.market_data import DataCapabilities, MarketDataProvider

#: Quotes move continuously, so their TTL is a latency budget rather than a correctness one.
DEFAULT_QUOTE_TTL = timedelta(seconds=5)
DEFAULT_MAX_ENTRIES = 256

CandleKey = tuple[str, str, int, str]

K = TypeVar("K")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class _Entry(Generic[T]):
    value: T
    expires_at: datetime

    def live_at(self, now: datetime) -> bool:
        return now < self.expires_at


class CachingMarketData:
    """Wraps a provider, serving repeat reads from memory until the next bar could exist."""

    def __init__(
        self,
        inner: MarketDataProvider,
        clock: Clock,
        *,
        quote_ttl: timedelta = DEFAULT_QUOTE_TTL,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._quote_ttl = quote_ttl
        self._max_entries = max_entries
        self._candles: OrderedDict[CandleKey, _Entry[CandleSeries]] = OrderedDict()
        self._quotes: OrderedDict[str, _Entry[Quote]] = OrderedDict()
        self._flights: dict[object, asyncio.Lock] = {}
        self.provider_id = inner.provider_id
        self.hits = 0
        self.misses = 0

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        key: CandleKey = (
            instrument.key,
            timeframe,
            limit,
            ensure_utc(end).isoformat() if end else "",
        )
        async with self._flight(key):
            cached = self._read(self._candles, key)
            if cached is not None:
                return cached
            series = await self._inner.get_candles(instrument, timeframe, limit, end)
            self._write(self._candles, key, series, self._series_expiry(series, timeframe))
            return series

    async def get_quote(self, instrument: Instrument) -> Quote:
        async with self._flight(("quote", instrument.key)):
            cached = self._read(self._quotes, instrument.key)
            if cached is not None:
                return cached
            quote = await self._inner.get_quote(instrument)
            self._write(self._quotes, instrument.key, quote, self._clock.now() + self._quote_ttl)
            return quote

    def capabilities(self) -> DataCapabilities:
        return self._inner.capabilities()

    def invalidate(self) -> None:
        """Drop everything. Used after a connectivity gap, when nothing cached is trustworthy."""
        self._candles.clear()
        self._quotes.clear()

    def _series_expiry(self, series: CandleSeries, timeframe: str) -> datetime:
        """Valid until the next bar could have closed — a closed bar never changes."""
        return series.latest.close_time + timeframe_interval(timeframe)

    def _flight(self, key: object) -> asyncio.Lock:
        """One in-flight fetch per key, so a burst of callers costs one venue call."""
        self._prune_flights()
        return self._flights.setdefault(key, asyncio.Lock())

    def _prune_flights(self) -> None:
        """Drop idle locks. Without this the map grows once per point-in-time cutoff, forever."""
        if len(self._flights) <= self._max_entries:
            return
        for key in [key for key, lock in self._flights.items() if not lock.locked()]:
            del self._flights[key]

    def _read(self, store: OrderedDict[K, _Entry[T]], key: K) -> T | None:
        entry = store.get(key)
        if entry is None or not entry.live_at(self._clock.now()):
            self.misses += 1
            store.pop(key, None)
            return None
        self.hits += 1
        store.move_to_end(key)
        return entry.value

    def _write(
        self, store: OrderedDict[K, _Entry[T]], key: K, value: T, expires_at: datetime
    ) -> None:
        store[key] = _Entry(value=value, expires_at=expires_at)
        store.move_to_end(key)
        while len(store) > self._max_entries:
            store.popitem(last=False)
