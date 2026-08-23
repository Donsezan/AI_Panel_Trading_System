"""Dataset integrity: find the holes, measure them, and record what was found (spec §4).

`marketdata/recorder.record` writes whatever `_page` returned and never audits completeness.
`_page` walks backwards a page at a time, so a dropped page leaves a silent hole — and
`CandleSeries.gaps` has existed since Phase 3 without ever being consulted at dataset level.

A hole matters more here than in a backtest. ATR is both the panel's volatility evidence and the
denominator of the §9.2 scoring band, so a band computed across a hole is a wrong band and every
verdict it produces is wrong while looking right.

Two kinds of hole, and only one is repairable (§4.2). A **fetch gap** is bars the venue has and
our paging missed; a **venue gap** is bars never published — a halt, an outage, maintenance.
Interpolating the second is forbidden (DESIGN §6.2): a fabricated bar feeds a fabricated ATR.

The audit is written to a sidecar rather than into `dataset.json`, because `DatasetManifest` is a
`tradebot` model and editing it would be a bot change (§2). Repair, by contrast, is **in place**
on the CSVs — a strict correction in the same format, so `ReplayDataset.load` reads it unchanged
and the bot's own backtests benefit too.

Failure semantics: reading a dataset that is not one raises `ConfigError` from
`ReplayDataset.load`. `require_verified` refuses an unaudited or stale dataset, naming the
command that fixes it. Nothing here reaches a venue except `repair`, and only when it is handed
a provider.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import ConfigDict, Field

from decision_lab.params import COVERAGE_FILE
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries, timeframe_interval
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.marketdata.recorder import CSV_COLUMNS, MANIFEST, ReplayDataset

#: A cutoff no recorded bar can close after, so `point_in_time` returns the whole series.
FAR_FUTURE: Final = datetime(9999, 12, 31, tzinfo=UTC)
#: `point_in_time` slices `visible[-limit:]`, so this asks for everything.
FULL_HISTORY: Final = 10**9

#: What a hole is called before the venue has been asked about it. An audit measures; only a
#: repair can tell a fetch gap from a hole the venue never published, so an un-asked hole says
#: so rather than borrowing either verdict.
NOT_REQUESTED: Final = "not re-requested; run `dataset verify --repair` to ask the venue"


def series_key(instrument_key: str, timeframe: str) -> str:
    """`binance:BTC/USDT` + `1h` gives `binance:BTC/USDT|1h`, the sidecar's key."""
    return f"{instrument_key}|{timeframe}"


def csv_path(directory: Path, instrument: Instrument, timeframe: str) -> Path:
    """The layout `ReplayMarketData.from_directory` reads.

    Reconstructed here rather than imported: `recorder._path_for` is private, and reaching into a
    bot private for a filename is a worse dependency than restating a documented convention that
    the round-trip tests pin.
    """
    symbol = instrument.symbol.replace("/", "_")
    return directory / f"{instrument.venue}__{symbol}__{timeframe}.csv"


def write_series(path: Path, candles: Sequence[Candle]) -> None:
    """Write one series in the recorder's own format, oldest first.

    Written here rather than through `recorder._write_csv` for the same reason as `csv_path`: it
    is private. `CSV_COLUMNS` is public and *is* imported, so the column contract has one owner
    even though the writer has two — and `test_the_patched_csv_is_read_back_by_the_bot_unchanged`
    pins that the bot's own reader accepts what this writes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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


class KnownHole(DomainModel):
    """Bars the venue never published, on re-request. Recorded, never filled in."""

    model_config = ConfigDict(populate_by_name=True)

    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime
    reason: str


class SeriesCoverage(DomainModel):
    """What one `(instrument, timeframe)` series holds against what its own window implies."""

    expected: int
    present: int
    repaired: int = 0
    known_holes: tuple[KnownHole, ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.present == self.expected and not self.known_holes


class CoverageAudit(DomainModel):
    """`decision_lab-coverage.json`: what was audited, when, and against which bytes."""

    audited_at: UtcDatetime
    dataset_digest: str
    series: dict[str, SeriesCoverage] = Field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return all(coverage.is_clean for coverage in self.series.values())

    def holes_for(self, key: str) -> tuple[KnownHole, ...]:
        coverage = self.series.get(key)
        return coverage.known_holes if coverage else ()


def dataset_digest(directory: Path) -> str:
    """Content identity of a dataset: the manifest plus every CSV, by name and by bytes.

    What makes a pinned day set (§4.5) and a corpus (§5.4) detectably stale after a repair. Names
    are hashed alongside the bytes so that renaming a series is a different dataset, not the same
    one with the same total content. The sidecars are deliberately outside it — they *describe*
    the dataset, and folding them in would make writing the audit invalidate the audit.
    """
    digest = hashlib.blake2s(digest_size=16)
    for path in sorted([directory / MANIFEST, *directory.glob("*.csv")]):
        if not path.is_file():
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def read_series(
    dataset: ReplayDataset, instrument: Instrument, timeframe: str
) -> CandleSeries:
    """The whole recorded series, through the provider's own point-in-time construction.

    Through `get_candles` rather than by re-reading the CSV, so decimal parsing, session labelling
    and ordering are the replay provider's and cannot drift from what a cycle would see.
    """
    return await dataset.market_data.get_candles(
        instrument, timeframe, FULL_HISTORY, end=FAR_FUTURE
    )


def expected_bars(series: CandleSeries) -> int:
    """Bars the venue's epoch-aligned grid implies over the series' *own* covered window.

    Never over the manifest's requested window, which may legitimately be wider than what the
    venue had — an instrument listed after the requested start would otherwise report a permanent
    shortfall no repair can close (§4.3 step 5).
    """
    if not series.candles:
        return 0
    span = series.candles[-1].close_time - series.candles[0].open_time
    return int(span // timeframe_interval(series.timeframe))


def holes_of(series: CandleSeries, previous: SeriesCoverage | None = None) -> tuple[KnownHole, ...]:
    """Every gap in the series, carrying whatever reason an earlier pass established for it.

    Derived from the series as it stands rather than copied from `previous`, so a repair that
    wrote a file the reader parses differently shows up as an unexplained hole instead of
    inheriting a reason for a hole that is no longer the same shape.
    """
    known = {
        (hole.from_, hole.to): hole.reason for hole in (previous.known_holes if previous else ())
    }
    return tuple(
        KnownHole(**{"from": start, "to": end, "reason": known.get((start, end), NOT_REQUESTED)})
        for start, end in series.gaps
    )


async def audit(
    dataset: ReplayDataset, clock: Clock, *, carry: CoverageAudit | None = None
) -> CoverageAudit:
    """Audit every series in the dataset. Pure measurement — nothing is fetched or written.

    `carry` supplies the `repaired` counts and the classified reasons an earlier repair pass
    established, so re-verifying after a repair does not forget that the venue was already asked.
    That second pass is not ceremony: it re-reads the patched CSVs from disk through the bot's
    own reader, so a repair that wrote a file the replay provider cannot parse shows up as a
    shortfall here rather than as a corpus quietly built on a truncated series.
    """
    series: dict[str, SeriesCoverage] = {}
    for instrument in dataset.instruments:
        for timeframe in dataset.timeframes:
            key = series_key(instrument.key, timeframe)
            loaded = await read_series(dataset, instrument, timeframe)
            previous = carry.series.get(key) if carry else None
            series[key] = SeriesCoverage(
                expected=expected_bars(loaded),
                present=len(loaded),
                repaired=previous.repaired if previous else 0,
                known_holes=holes_of(loaded, previous),
            )
    return CoverageAudit(
        audited_at=clock.now(),
        dataset_digest=dataset_digest(dataset.directory),
        series=series,
    )


def write_audit(directory: Path, audit_: CoverageAudit) -> Path:
    """Write the sidecar. `by_alias`, so the file on disk is the one §4.3 documents."""
    path = directory / COVERAGE_FILE
    path.write_text(audit_.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    return path


def read_audit(directory: Path) -> CoverageAudit:
    path = directory / COVERAGE_FILE
    if not path.is_file():
        raise ConfigError(
            f"{directory} has no {COVERAGE_FILE}: run `python -m decision_lab dataset verify "
            f"--data {directory}` first. A corpus built on an unaudited dataset is a corpus whose "
            "ATR band may have been computed across a hole (§4.4)"
        )
    return CoverageAudit.model_validate_json(path.read_text(encoding="utf-8"))


def require_verified(directory: Path) -> CoverageAudit:
    """The audit for this dataset *as it stands now*, or a refusal naming what to run.

    Fail closed on staleness as well as absence: an audit taken before a repair describes a
    dataset that no longer exists, and its known holes are the ones the repair may have closed.
    """
    audit_ = read_audit(directory)
    current = dataset_digest(directory)
    if audit_.dataset_digest != current:
        raise ConfigError(
            f"{directory} has changed since it was audited at "
            f"{audit_.audited_at.isoformat()} ({audit_.dataset_digest} to {current}). Re-run "
            f"`python -m decision_lab dataset verify --data {directory}`"
        )
    return audit_
