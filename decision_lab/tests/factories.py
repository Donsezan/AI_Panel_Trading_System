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

from decision_lab.corpus import Corpus, CorpusEntry, CorpusMeta
from decision_lab.dataset import csv_path, write_series
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.enums import AssetClass, MarketSession
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, Quote, timeframe_interval
from tradebot.core.snapshot import ContextSnapshot, IndicatorReading, InstrumentContext
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


def snapshot_at(
    as_of: datetime, *, timeframe: str = "1h", price: str = "100", atr: str = "1.0"
) -> ContextSnapshot:
    """A minimal but real snapshot: one instrument, one quote, one ATR reading.

    Real rather than a stub because scoring reads `context.indicator("ATR", …)` off it, and the
    band is derived from exactly the evidence the panel had (§9.2).

    `timeframe` must match the corpus's own bar grid: `scoring.band_for` looks the ATR reading up
    *by timeframe*, so a snapshot tagged with the wrong one is not a wrong number but an absent
    one — `context.indicator("ATR", …)` returns `None` and the decision reads as unscorable
    rather than as an error. `corpus_with_entries` passes its own `timeframe` through here so the
    two agree by construction rather than by coincidence.

    The field names are verified against `tradebot/core/snapshot.py` and `core/market.py`, not
    guessed: `Quote` carries `bid`/`ask`/`last` and no `price`, `IndicatorReading` carries `text`
    and no `computed_at`, and `ContextSnapshot` requires `snapshot_id` and `basket_id`.
    """
    inst = instrument()
    return ContextSnapshot(
        snapshot_id=f"snap-{as_of.isoformat()}",
        basket_id="reference",
        as_of=as_of,
        instruments=(
            InstrumentContext(
                instrument=inst,
                quote=Quote(
                    instrument_key=inst.key,
                    bid=Decimal(price),
                    ask=Decimal(price),
                    last=Decimal(price),
                    observed_at=as_of,
                ),
                indicators=(
                    IndicatorReading(
                        name="ATR", timeframe=timeframe, value=Decimal(atr), text=f"ATR is {atr}"
                    ),
                ),
            ),
        ),
    )


def corpus_with_entries(
    *, count: int, as_of: datetime, corpus_id: str = "corpus-test", timeframe: str = "1h"
) -> Corpus:
    """A `Corpus` of `count` entries on the venue's bar grid, with a real snapshot on each."""
    interval = timeframe_interval(timeframe)
    entries = tuple(
        CorpusEntry(
            seq=index,
            cycle_id=f"c{index}",
            basket_id="reference",
            as_of=as_of + interval * index,
            snapshot=snapshot_at(as_of + interval * index, timeframe=timeframe),
        )
        for index in range(count)
    )
    meta = CorpusMeta(
        corpus_id=corpus_id,
        built_at=as_of,
        dataset_directory="data/history",
        dataset_digest="d1",
        reference_panel_id="stub",
        reference_basket=_reference_basket(),
        reference_config_digest="r1",
        cadence_seconds=int(interval.total_seconds()),
        start_equity=Decimal(10_000),
        requested_start=as_of,
        window_start=as_of,
        window_end=as_of + interval * count,
        warmup_seconds=0,
        planned_cycles=count,
        ran_cycles=count,
    )
    return Corpus(meta=meta, entries=entries)


def _reference_basket() -> Basket:
    return Basket(
        basket_id="reference",
        name="reference",
        instruments=(instrument(),),
        panel=PanelConfig(
            panel_id="reference",
            seats=(SeatConfig(seat_id="a", role="a", provider_id="stub", model="stub-technical"),),
        ),
    )
