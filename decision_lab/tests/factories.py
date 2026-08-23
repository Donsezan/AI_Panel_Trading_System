"""Datasets written to disk for the tool's own tests. Offline, deterministic, tiny.

A real `ReplayDataset` rather than a stub, because everything under test reads the manifest, the
CSV layout and `CandleSeries.gaps` — three things a stub would get right by construction and the
real recorder might not.

The CSV writer is `dataset.write_series`, the one repair uses. Two byte-identical writers, one
for fixtures and one for production, is the copy §2.4 forbids; what keeps the format honest is
that every test reads back through `ReplayMarketData._read_csv`, which is the bot's own reader.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from decision_lab.dataset import csv_path, write_series
from tradebot.core.enums import AssetClass, MarketSession
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, timeframe_interval
from tradebot.marketdata.recorder import MANIFEST, DatasetManifest

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def instrument(symbol: str = "BTC/USDT", venue: str = "binance") -> Instrument:
    return Instrument(
        symbol=symbol,
        venue=venue,
        asset_class=AssetClass.CRYPTO,
        base_currency=symbol.split("/")[0],
        quote_currency=symbol.split("/")[1] if "/" in symbol else "USD",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"),
    )


def walk(
    closes: Sequence[str], *, timeframe: str = "1h", start: datetime = EPOCH
) -> tuple[Candle, ...]:
    """One candle per close, on the venue's epoch-aligned grid, contiguous by construction."""
    interval = timeframe_interval(timeframe)
    bars = []
    for index, close in enumerate(closes):
        price = Decimal(close)
        open_time = start + interval * index
        bars.append(
            Candle(
                open_time=open_time,
                close_time=open_time + interval,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(1),
                session=MarketSession.CONTINUOUS,
            )
        )
    return tuple(bars)


def shocked_walk(
    *,
    days: int,
    shock_up: Sequence[int] = (),
    shock_down: Sequence[int] = (),
    timeframe: str = "1h",
    start: datetime = EPOCH,
    base: str = "100",
) -> tuple[Candle, ...]:
    """A daily series with deliberate shock days, so pool selection has something to select.

    A plain walk gives every day the same volatility, which makes the 90th percentile a set of
    three days split arbitrarily by sign — and the day-set refusal would then fire on every test
    rather than on the case it exists for.
    """
    per_day = int(timedelta(days=1) // timeframe_interval(timeframe))
    closes: list[str] = []
    price = Decimal(base)
    for day in range(days):
        if day in shock_up:
            step = Decimal("0.02")
        elif day in shock_down:
            step = Decimal("-0.02")
        else:
            step = Decimal("0.0005")
        for bar in range(per_day):
            price = price * (Decimal(1) + (step if bar % 2 == 0 else -step / 2))
            closes.append(str(price.quantize(Decimal("0.00000001"))))
    return walk(closes, timeframe=timeframe, start=start)


def drop_bars(candles: Sequence[Candle], *, at: int, count: int) -> tuple[Candle, ...]:
    """Punch a hole. What a dropped page in `marketdata/recorder.py` leaves behind (§4.1)."""
    return tuple(candles[:at]) + tuple(candles[at + count :])


def write_dataset(
    directory: Path,
    series: dict[tuple[Instrument, str], Sequence[Candle]],
    *,
    source: str = "test",
) -> Path:
    """Write CSVs plus the manifest `ReplayDataset.load` demands."""
    directory.mkdir(parents=True, exist_ok=True)
    instruments = tuple(dict.fromkeys(i for i, _ in series))
    timeframes = tuple(dict.fromkeys(tf for _, tf in series))
    for (inst, timeframe), candles in series.items():
        write_series(csv_path(directory, inst, timeframe), candles)
    spans = [(c[0].open_time, c[-1].close_time) for c in series.values() if c]
    assert spans, "a dataset with no bars is not one this factory can describe"
    manifest = DatasetManifest(
        source=source,
        recorded_at=EPOCH,
        instruments=instruments,
        timeframes=timeframes,
        requested_start=min(s for s, _ in spans),
        requested_end=max(e for _, e in spans),
    )
    (directory / MANIFEST).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return directory
