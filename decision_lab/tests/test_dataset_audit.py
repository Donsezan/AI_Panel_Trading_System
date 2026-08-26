"""Every series is audited for holes before anything is built on it (spec §4.1–§4.4).

`marketdata/recorder.record` writes whatever paging returned and never checks completeness, while
`CandleSeries.gaps` has always existed and was never consulted at dataset level. A hole matters
more here than in a backtest: ATR is both the panel's volatility evidence and the denominator of
the §9.2 scoring band, so a band computed across a hole is wrong while looking right.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab import dataset as ds
from decision_lab.params import COVERAGE_FILE
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import MANIFEST, DatasetManifest, ReplayDataset

NOW = datetime(2026, 8, 23, 9, 14, 2, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


def load(directory: Path, clock: ManualClock) -> ReplayDataset:
    return ReplayDataset.load(directory, clock)


async def test_a_complete_series_audits_clean(tmp_path: Path, clock: ManualClock) -> None:
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})

    audit = await ds.audit(load(tmp_path, clock), clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.expected == 48
    assert coverage.present == 48
    assert coverage.known_holes == ()
    assert audit.is_clean


async def test_a_dropped_page_is_found_and_measured(tmp_path: Path, clock: ManualClock) -> None:
    """Six missing bars in the middle: the exact shape a dropped page leaves.

    `CandleSeries.gaps` yields `(earlier.close_time, later.open_time)` and `close_time` is the
    exclusive boundary, so dropping bars 20–25 of a series opening at midnight is exactly
    20:00 → 02:00 the next day.
    """
    inst = f.instrument()
    holed = f.drop_bars(f.walk([str(100 + i) for i in range(48)]), at=20, count=6)
    f.write_dataset(tmp_path, {(inst, "1h"): holed})

    audit = await ds.audit(load(tmp_path, clock), clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.present == 42
    assert coverage.expected == 48, "expected counts the grid over the series' own covered window"
    assert len(coverage.known_holes) == 1
    hole = coverage.known_holes[0]
    assert (hole.from_, hole.to) == (
        datetime(2024, 1, 1, 20, tzinfo=UTC),
        datetime(2024, 1, 2, 2, tzinfo=UTC),
    )
    assert "--repair" in hole.reason, "an audit measures; only a repair may classify a hole"
    assert not audit.is_clean


async def test_expected_is_measured_over_the_covered_window_not_the_request(
    tmp_path: Path, clock: ManualClock
) -> None:
    """A manifest may legitimately request more than the venue had (§4.3 step 5).

    Counting against the request would report a permanent, unrepairable shortfall on every
    dataset whose window opened before the instrument was listed.
    """
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 10)})
    path = tmp_path / MANIFEST
    manifest = DatasetManifest.model_validate_json(path.read_text(encoding="utf-8"))
    widened = manifest.model_copy(update={"requested_start": datetime(2023, 1, 1, tzinfo=UTC)})
    path.write_text(widened.model_dump_json(indent=2), encoding="utf-8")

    audit = await ds.audit(load(tmp_path, clock), clock)

    assert audit.series["binance:BTC/USDT|1h"].expected == 10


async def test_every_instrument_and_timeframe_is_audited(
    tmp_path: Path, clock: ManualClock
) -> None:
    btc, eth = f.instrument("BTC/USDT"), f.instrument("ETH/USDT")
    bars = f.walk(["100"] * 24)
    f.write_dataset(tmp_path, {(btc, "1h"): bars, (eth, "1h"): bars})

    audit = await ds.audit(load(tmp_path, clock), clock)

    assert set(audit.series) == {"binance:BTC/USDT|1h", "binance:ETH/USDT|1h"}


async def test_the_audit_round_trips_through_the_sidecar(
    tmp_path: Path, clock: ManualClock
) -> None:
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 24), at=5, count=2)})
    written = await ds.audit(load(tmp_path, clock), clock)

    path = ds.write_audit(tmp_path, written)
    assert path.name == COVERAGE_FILE
    assert ds.read_audit(tmp_path) == written


def test_the_sidecar_uses_the_spec_field_names(tmp_path: Path) -> None:
    """`from` and `to` on disk, so the file is the one §4.3 documents.

    Asserted on what `write_audit` actually writes rather than on a bare `model_dump_json`: the
    alias only reaches the file because the writer asks for it, and that is the thing that must
    not regress.
    """
    audit = ds.CoverageAudit(
        audited_at=NOW,
        dataset_digest="abc",
        series={
            "binance:ETH/USDT|1h": ds.SeriesCoverage(
                expected=4380,
                present=4374,
                known_holes=(
                    ds.KnownHole(
                        **{
                            "from": datetime(2024, 3, 11, 4, tzinfo=UTC),
                            "to": datetime(2024, 3, 11, 10, tzinfo=UTC),
                            "reason": "venue served no bars on re-request",
                        }
                    ),
                ),
            )
        },
    )

    rendered = ds.write_audit(tmp_path, audit).read_text(encoding="utf-8")

    assert '"from"' in rendered
    assert '"from_"' not in rendered
    assert ds.read_audit(tmp_path) == audit


async def test_the_digest_moves_when_a_bar_changes(tmp_path: Path, clock: ManualClock) -> None:
    """The digest is what makes a pinned day set and a corpus detectably stale (§15)."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})
    before = ds.dataset_digest(tmp_path)

    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 25)})
    assert ds.dataset_digest(tmp_path) != before


async def test_the_sidecar_is_not_part_of_the_digest(tmp_path: Path, clock: ManualClock) -> None:
    """Writing the audit must not invalidate the audit it just recorded."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})
    before = ds.dataset_digest(tmp_path)

    ds.write_audit(tmp_path, await ds.audit(load(tmp_path, clock), clock))

    assert ds.dataset_digest(tmp_path) == before
    assert ds.require_verified(tmp_path).dataset_digest == before


def test_require_verified_refuses_an_unaudited_dataset(tmp_path: Path) -> None:
    """Fail closed: a corpus is the basis of every number downstream (§4.4, §15)."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})

    with pytest.raises(ConfigError, match="dataset verify"):
        ds.require_verified(tmp_path)


async def test_require_verified_refuses_a_stale_audit(tmp_path: Path, clock: ManualClock) -> None:
    """An audit taken before the data changed describes a dataset that no longer exists."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})
    ds.write_audit(tmp_path, await ds.audit(load(tmp_path, clock), clock))

    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 30)})
    with pytest.raises(ConfigError, match="has changed since"):
        ds.require_verified(tmp_path)
