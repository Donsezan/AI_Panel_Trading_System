"""A fetch gap is repaired; a venue gap is recorded and never filled in (spec §4.2, §4.3).

No network: the venue is a fake provider that answers for a declared set of bars and nothing
else, which is exactly the distinction repair has to draw.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab import dataset as ds
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import VenueError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


class FakeVenue:
    """Answers from a fixed book. Records what it was asked, so silence can be asserted."""

    def __init__(self, book: Sequence[Candle], *, fails: bool = False) -> None:
        self._book = tuple(book)
        self._fails = fails
        self.calls: list[tuple[str, str, datetime | None]] = []

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        self.calls.append((instrument.key, timeframe, end))
        if self._fails:
            raise VenueError("binance returned 503")
        cutoff = end or NOW
        visible = [c for c in self._book if c.close_time <= cutoff]
        return CandleSeries(
            instrument_key=instrument.key,
            timeframe=timeframe,
            candles=tuple(visible[-limit:]),
            observed_at=cutoff,
        )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


async def test_a_fetch_gap_is_patched_in_place(tmp_path: Path, clock: ManualClock) -> None:
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})
    venue = FakeVenue(whole)

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.repaired == 6
    assert coverage.present == 48
    assert coverage.known_holes == ()
    assert audit.is_clean


async def test_the_patched_csv_is_read_back_by_the_bot_unchanged(
    tmp_path: Path, clock: ManualClock
) -> None:
    """Repair is a strict correction in the same format — the whole point of patching in place."""
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})
    await ds.repair(ReplayDataset.load(tmp_path, clock), FakeVenue(whole), clock)

    reloaded = await ds.read_series(ReplayDataset.load(tmp_path, clock), inst, "1h")

    assert reloaded.candles == whole
    assert reloaded.gaps == ()


async def test_a_venue_gap_is_recorded_and_never_filled(tmp_path: Path, clock: ManualClock) -> None:
    """The venue has nothing to give. Interpolating would feed a fabricated ATR (§4.2)."""
    inst = f.instrument()
    holed = f.drop_bars(f.walk([str(100 + i) for i in range(48)]), at=20, count=6)
    f.write_dataset(tmp_path, {(inst, "1h"): holed})
    venue = FakeVenue(holed)

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.repaired == 0
    assert coverage.present == 42
    assert len(coverage.known_holes) == 1
    assert "venue served no bars" in coverage.known_holes[0].reason
    assert not audit.is_clean


async def test_a_partial_repair_records_what_is_left(tmp_path: Path, clock: ManualClock) -> None:
    """The venue has some of the hole. Repair what exists, record the rest."""
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})
    venue = FakeVenue(f.drop_bars(whole, at=22, count=2))

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.repaired == 4
    assert coverage.present == 46
    assert len(coverage.known_holes) == 1, "the hole left is narrower than the one asked about"
    assert (coverage.known_holes[0].from_, coverage.known_holes[0].to) == (
        datetime(2024, 1, 1, 22, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
    )


async def test_a_clean_series_is_never_refetched(tmp_path: Path, clock: ManualClock) -> None:
    """No hole, no venue call. A repair pass over a good dataset must cost nothing."""
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): whole})
    venue = FakeVenue(whole)

    await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    assert venue.calls == []


async def test_a_venue_failure_is_a_recorded_hole_not_a_crash(
    tmp_path: Path, clock: ManualClock
) -> None:
    """One series' outage must not lose the audit of every other series.

    Same containment rule as the maintenance pass's per-day scope: a failure that stops the whole
    loop turns one venue hiccup into a dataset nobody can characterise.
    """
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 48), at=20, count=6)})

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), FakeVenue((), fails=True), clock)

    hole = audit.series["binance:BTC/USDT|1h"].known_holes[0]
    assert "503" in hole.reason
    assert not audit.is_clean


async def test_a_non_binance_venue_is_recorded_rather_than_guessed(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The provider answers for one venue. Asking it about another would invent history."""
    inst = f.instrument("AAPL", venue="alpaca")
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 48), at=20, count=6)})
    venue = FakeVenue(f.walk(["100"] * 48))

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock, venue_id="binance")

    hole = audit.series["alpaca:AAPL|1h"].known_holes[0]
    assert "alpaca" in hole.reason
    assert venue.calls == []


async def test_repair_leaves_a_dataset_the_pinning_step_can_read(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The digest on the returned audit describes the files *after* the correction (§15).

    A digest taken before the rewrite would make `require_verified` refuse the dataset repair had
    just fixed, on the strength of the repair itself.
    """
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), FakeVenue(whole), clock)
    ds.write_audit(tmp_path, audit)

    assert ds.require_verified(tmp_path).dataset_digest == audit.dataset_digest
