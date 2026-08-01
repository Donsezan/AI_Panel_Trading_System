"""Recording venue history into the dataset a backtest replays (PLAN Phase 7).

A recorded dataset is **self-describing**: alongside the CSVs it carries the venue's own trading
rules for every instrument in it, as they stood when it was recorded. That matters more than it
looks. Lot size, tick size and minimum notional are what risk quantizes against, so a backtest
run against today's rules over last year's prices is silently testing a market that never
existed — and the failure shows up as orders that would have been vetoed, not as an error.

Point-in-time discipline is inherited rather than reimplemented: every page is fetched through
the same `MarketDataProvider` a live cycle uses, with an explicit `end`, so a bar still forming
is never written. The venue's rate budget is the transport's, so a long recording spends the same
weight allowance as trading would and cannot get the key banned (PLAN §3.1).

Failure semantics: a page that comes back empty ends the recording at that point rather than
raising — history simply does not go back further — and the manifest records the span actually
obtained. A recording that reaches no bars at all raises `ConfigError`, because an empty dataset
would otherwise produce a backtest that aborts every cycle as `DATA_STALE` and reads as a fault
of the system rather than of the data.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.marketdata.replay import ReplayMarketData

logger = get_logger(__name__)

#: What a dataset's instruments and provenance are written to, beside the CSVs.
MANIFEST = "dataset.json"

CSV_COLUMNS = ("open_time", "close_time", "open", "high", "low", "close", "volume", "session")


class DatasetManifest(DomainModel):
    """The provenance of one recorded dataset.

    Kept beside the data rather than in the database on purpose: a dataset is a file you copy
    between machines, and one that has lost the rules its prices were quantized against is not
    recoverable from anywhere else.
    """

    source: str
    recorded_at: UtcDatetime
    instruments: tuple[Instrument, ...]
    timeframes: tuple[str, ...]
    requested_start: UtcDatetime
    requested_end: UtcDatetime


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    """A directory of recorded series, loaded and ready to serve a backtest."""

    directory: Path
    manifest: DatasetManifest
    market_data: ReplayMarketData

    @classmethod
    def load(cls, directory: Path, clock: Clock) -> ReplayDataset:
        """Read a recorded dataset. Raises `ConfigError` if it is not one."""
        path = directory / MANIFEST
        if not path.is_file():
            raise ConfigError(
                f"{directory} holds no {MANIFEST}: it was not produced by `tradebot backtest "
                "fetch`, so the venue trading rules its prices were recorded under are unknown"
            )
        manifest = DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
        market_data = ReplayMarketData.from_directory(directory, clock)
        if not market_data.keys:
            raise ConfigError(f"{directory} holds a manifest but no series")
        return cls(directory=directory, manifest=manifest, market_data=market_data)

    @property
    def instruments(self) -> tuple[Instrument, ...]:
        return self.manifest.instruments

    @property
    def timeframes(self) -> tuple[str, ...]:
        """Timeframes every instrument has, shortest first — what a basket may be built on."""
        per_instrument = [
            {timeframe for key, timeframe in self.market_data.keys if key == instrument.key}
            for instrument in self.instruments
        ]
        shared = set.intersection(*per_instrument) if per_instrument else set()
        return tuple(sorted(shared, key=timeframe_interval))

    @property
    def coverage(self) -> tuple[datetime, datetime]:
        """The period every series covers. Raises when the dataset is empty."""
        span = self.market_data.coverage()
        if span is None:
            raise ConfigError(f"{self.directory} holds no bars")
        return span

    def window(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> tuple[datetime, datetime]:
        """The requested window clamped into what the data actually covers.

        Clamping rather than refusing, because a window one bar wider than the dataset is the
        normal way an operator writes "the whole thing" — but a window entirely outside it is a
        mistake worth refusing, since every cycle in it would abort as `DATA_STALE`.
        """
        covered_start, covered_end = self.coverage
        resolved_start = max(ensure_utc(start), covered_start) if start else covered_start
        resolved_end = min(ensure_utc(end), covered_end) if end else covered_end
        if resolved_start >= resolved_end:
            raise ConfigError(
                f"the requested window has no overlap with the dataset, which covers "
                f"{covered_start.isoformat()} to {covered_end.isoformat()}"
            )
        return resolved_start, resolved_end


async def record(
    provider: MarketDataProvider,
    instruments: Sequence[Instrument],
    timeframes: Sequence[str],
    *,
    start: datetime,
    end: datetime,
    directory: Path,
    clock: Clock,
    source: str = "",
) -> ReplayDataset:
    """Page a venue's history into `directory` and write the manifest beside it."""
    window_start, window_end = ensure_utc(start), ensure_utc(end)
    if window_start >= window_end:
        raise ConfigError(f"empty window: {window_start.isoformat()} is not before {window_end}")

    _prepare(directory)
    recorded = 0
    for instrument in instruments:
        for timeframe in timeframes:
            candles = await _page(
                provider, instrument, timeframe, start=window_start, end=window_end
            )
            if not candles:
                logger.warning(
                    "no history recorded",
                    extra={"instrument": instrument.key, "timeframe": timeframe},
                )
                continue
            _write_csv(_path_for(directory, instrument, timeframe), candles)
            recorded += len(candles)
            logger.info(
                "series recorded",
                extra={
                    "instrument": instrument.key,
                    "timeframe": timeframe,
                    "bars": len(candles),
                    "from": candles[0].open_time.isoformat(),
                    "to": candles[-1].close_time.isoformat(),
                },
            )

    if not recorded:
        raise ConfigError(
            f"the venue served no bars in {window_start.isoformat()}–{window_end.isoformat()}; "
            "a dataset with no prices would abort every backtest cycle as DATA_STALE"
        )
    manifest = DatasetManifest(
        source=source or provider.provider_id,
        recorded_at=clock.now(),
        instruments=tuple(instruments),
        timeframes=tuple(timeframes),
        requested_start=window_start,
        requested_end=window_end,
    )
    _write_manifest(directory, manifest)
    return ReplayDataset.load(directory, clock)


async def _page(
    provider: MarketDataProvider,
    instrument: Instrument,
    timeframe: str,
    *,
    start: datetime,
    end: datetime,
) -> tuple[Candle, ...]:
    """Walk backwards from `end` a page at a time until the venue runs out or `start` is reached.

    Backwards because that is the only direction every venue's kline endpoint agrees on: `end` is
    the cutoff our own point-in-time rule already uses, so each page is a plain historical read
    with no forming bar in it.
    """
    page_size = max(provider.capabilities().max_history, 1)
    collected: dict[datetime, Candle] = {}
    cursor = end
    while cursor > start:
        try:
            series = await provider.get_candles(instrument, timeframe, page_size, end=cursor)
        except DataStaleError:
            break
        if not series.candles:
            break
        collected.update({candle.open_time: candle for candle in series.candles})
        earliest = series.candles[0].open_time
        if earliest <= start or earliest >= cursor:
            break
        cursor = earliest
    return tuple(candle for _, candle in sorted(collected.items()) if candle.open_time >= start)


def _prepare(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def _write_manifest(directory: Path, manifest: DatasetManifest) -> None:
    (directory / MANIFEST).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def _path_for(directory: Path, instrument: Instrument, timeframe: str) -> Path:
    """`binance:BTC/USDT` + `1h` → `binance__BTC_USDT__1h.csv`, the layout replay reads."""
    return directory / f"{instrument.venue}__{instrument.symbol.replace('/', '_')}__{timeframe}.csv"


def _write_csv(path: Path, candles: Iterable[Candle]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "open_time": candle.open_time.isoformat(),
                    "close_time": candle.close_time.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                    "session": candle.session.value,
                }
            )
