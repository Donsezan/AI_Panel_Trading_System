"""One cycle, folded from the four event types it wrote (spec §5.1, §9.7).

Read from the log rather than from a projection, for the same reason `validation/evidence.py`
does: the facts a score turns on — every seat's vote, including the abstentions, and the round it
was cast in — have no projector at all, and the log is the audit artifact.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import records as rc
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.enums import Action
from tradebot.marketdata.recorder import ReplayDataset


@pytest.fixture
async def built(tmp_path: Path) -> tuple[Path, str]:
    """A real corpus, over the deterministic `stub` panel that slice A's own tests build with.

    Not `sim`: its `varied-*` seats draw from an **unseeded** `random.Random`, so the pass differs
    every run — sometimes reaching a state that trips KNOWN_GAPS §5 mid-build and errors this
    fixture rather than the code under test. A flaky suite proves nothing, and nothing asserted
    here is about which votes were cast; §9.7's round-0 split is exercised on constructed
    responses where the rounds can be stated rather than hoped for.
    """
    clock = ManualClock(f.EPOCH)
    data = tmp_path / "history"
    workspace = tmp_path / "ws"
    f.write_dataset(
        data, {(f.instrument(), "1h"): f.shocked_walk(days=20, shock_up=(4,), shock_down=(9,))}
    )
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    corpus = await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel="stub",
        cadence_seconds=4 * 3600,
        start_equity=Decimal(10_000),
    )
    return workspace, corpus.meta.corpus_id


async def test_every_cycle_carries_its_snapshot_and_its_decisions(built: tuple[Path, str]) -> None:
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    assert cycles
    assert all(cycle.snapshot.instruments for cycle in cycles)
    assert any(cycle.decisions for cycle in cycles)


async def test_every_seat_response_is_kept_including_abstentions(built: tuple[Path, str]) -> None:
    """An abstention is a fact about a seat, and §9.7's abstention rate is made of them."""
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    responded = [r for cycle in cycles for r in cycle.responses]
    assert responded
    assert all(r.seat_id for r in responded)


async def test_round_zero_and_the_final_round_are_separable(built: tuple[Path, str]) -> None:
    """§9.7: under `blind_then_debate` a later vote is contaminated by peers *by design*, so
    'which seat reasons well' and 'which seat is easily talked round' are different questions."""
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)
    cycle = next(c for c in cycles if c.responses)
    key = cycle.snapshot.instruments[0].instrument.key

    assert all(r.round_index == 0 for r in cycle.round_zero_for(key))
    finals = cycle.final_round_for(key)
    assert finals
    assert len({r.round_index for r in finals}) == 1
    # `STUB_PANEL` is `single_round`, so the blind round *is* the final one and the two views
    # coincide. That is §9.7's behaviour for a non-debating panel and is worth pinning: the
    # split must be a property of the protocol, never of how the record was folded.
    assert cycle.round_zero_for(key) == finals


async def test_the_outcome_and_cost_come_off_cycle_completed(built: tuple[Path, str]) -> None:
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    assert all(cycle.outcome for cycle in cycles)
    assert all(cycle.cost_usd >= Decimal(0) for cycle in cycles)


async def test_a_degraded_cycle_is_identifiable(built: tuple[Path, str]) -> None:
    """§9.5: a candidate that scores well on the cycles it answered while failing a third of them
    is not a better panel, so the degradation rate has to be countable."""
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    assert all(isinstance(cycle.degraded, bool) for cycle in cycles)


async def test_decisions_are_addressable_by_instrument(built: tuple[Path, str]) -> None:
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)
    cycle = next(c for c in cycles if c.decisions)
    key = cycle.decisions[0].instrument_key

    found = cycle.decision_for(key)
    assert found is not None
    assert found.action in set(Action)
    assert cycle.decision_for("binance:NOPE/USDT") is None


async def test_the_meta_travels_with_the_records(built: tuple[Path, str]) -> None:
    """Scoring needs the reference `PanelConfig` for §9.7's swing rate, and the dataset directory
    for the forward prices. Both are on the meta slice A wrote."""
    workspace, corpus_id = built
    meta, _ = rc.load(corpus_id, workspace=workspace)

    assert meta.reference_basket.panel.seats
    assert meta.dataset_directory
