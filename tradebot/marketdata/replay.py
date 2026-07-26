"""Replay provider: serves recorded series for simulation and backtest.

Point-in-time correctness is the whole job. A candle is visible only once it has **closed** at
or before the cutoff — showing a bar that is still forming leaks the future into a decision,
which is the single easiest way to produce a backtest that is quietly meaningless (DESIGN [L12]).

The cutoff is `end` when given, otherwise the injected clock's `now`. In replay the clock is a
`ManualClock` the harness advances, so point-in-time discipline is automatic rather than
something each caller has to remember.

Failure semantics: an unknown instrument/timeframe raises `DataStaleError` rather than
returning an empty series — silently deciding on no data is exactly the fail-open behaviour the
design forbids.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.errors import DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries, Quote, timeframe_interval
from tradebot.core.money import ZERO, divide, multiply, to_decimal
from tradebot.interfaces.market_data import DataCapabilities

SeriesKey = tuple[str, str]


class ReplayMarketData:
    """Serves pre-recorded candles with a hard point-in-time cutoff."""

    provider_id = "replay"

    def __init__(
        self,
        series: Mapping[SeriesKey, tuple[Candle, ...]],
        clock: Clock,
        *,
        spread_pct: Decimal = Decimal("0.02"),
    ) -> None:
        self._series = dict(series)
        self._clock = clock
        self._spread_pct = spread_pct

    @classmethod
    def from_directory(cls, directory: Path, clock: Clock) -> ReplayMarketData:
        """Load every `{venue}__{symbol}__{timeframe}.csv` in a directory.

        Columns: `open_time,close_time,open,high,low,close,volume`. Both times are stored rather
        than inferred from the timeframe, so a session gap or an irregular bar stays truthful.
        """
        series: dict[SeriesKey, tuple[Candle, ...]] = {}
        for path in sorted(directory.glob("*.csv")):
            venue, symbol, timeframe = path.stem.split("__")
            key = (f"{venue}:{symbol.replace('_', '/')}", timeframe)
            series[key] = tuple(_read_csv(path))
        return cls(series, clock)

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        cutoff = ensure_utc(end) if end is not None else self._clock.now()
        candles = self._lookup(instrument.key, timeframe)
        visible = tuple(candle for candle in candles if candle.close_time <= cutoff)
        if not visible:
            raise DataStaleError(
                f"no {timeframe} candles closed on or before {cutoff.isoformat()} "
                f"for {instrument.key}"
            )
        return CandleSeries(
            instrument_key=instrument.key,
            timeframe=timeframe,
            candles=visible[-limit:],
            observed_at=cutoff,
        )

    async def get_quote(self, instrument: Instrument) -> Quote:
        """Derive a quote from the most recent *closed* bar.

        A synthetic spread keeps the sim honest about crossing costs; SimBroker adds slippage
        and fees on top.
        """
        timeframe = self._shortest_timeframe(instrument.key)
        series = await self.get_candles(instrument, timeframe, limit=1)
        close = series.latest.close
        half_spread = divide(multiply(close, self._spread_pct), Decimal(200))
        return Quote(
            instrument_key=instrument.key,
            bid=close - half_spread,
            ask=close + half_spread,
            last=close,
            observed_at=series.observed_at,
        )

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            timeframes=tuple(sorted({timeframe for _, timeframe in self._series})),
            max_history=max((len(c) for c in self._series.values()), default=0),
            supports_point_in_time=True,
        )

    def _lookup(self, instrument_key: str, timeframe: str) -> tuple[Candle, ...]:
        candles = self._series.get((instrument_key, timeframe))
        if candles is None:
            raise DataStaleError(f"no replay series for {instrument_key} {timeframe}")
        return candles

    def _shortest_timeframe(self, instrument_key: str) -> str:
        available = [tf for key, tf in self._series if key == instrument_key]
        if not available:
            raise DataStaleError(f"no replay series for {instrument_key}")
        return min(available, key=timeframe_interval)


def _read_csv(path: Path) -> Iterable[Candle]:
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            yield Candle(
                open_time=datetime.fromisoformat(row["open_time"]),
                close_time=datetime.fromisoformat(row["close_time"]),
                open=to_decimal(row["open"]),
                high=to_decimal(row["high"]),
                low=to_decimal(row["low"]),
                close=to_decimal(row["close"]),
                volume=to_decimal(row["volume"]),
            )


def synthetic_candles(
    *,
    start: datetime,
    timeframe: str,
    count: int,
    open_price: Decimal,
    step: Decimal = Decimal("0.5"),
    seed: int = 7,
) -> tuple[Candle, ...]:
    """A deterministic price series for simulation and tests.

    Not a market model and not meant to be one — it exists so the loop has something to chew on.
    The walk is integer-driven so prices stay exact decimals with no float anywhere.
    """
    duration = timeframe_interval(timeframe)
    opening = ensure_utc(start)
    price = open_price
    state = seed
    candles: list[Candle] = []
    for index in range(count):
        state = (state * 1103515245 + 12345) % 2147483648
        drift = multiply(step, Decimal((state % 7) - 3))
        close = max(price + drift, step)
        high = max(price, close) + step
        low = max(min(price, close) - step, ZERO + step)
        candles.append(
            Candle(
                open_time=opening + duration * index,
                close_time=opening + duration * (index + 1),
                open=price,
                high=high,
                low=low,
                close=close,
                volume=Decimal(100 + (state % 50)),
            )
        )
        price = close
    return tuple(candles)
