"""The one read assembly for a period run (period spec §5.6, §7).

A real run through the real wiring, read back by `analyse_period`; the cases that need a state a
run does not naturally produce (a substituted model, a frozen portfolio) hand doctored values to
`fold_period`, the pure half of the same assembly.
"""

# ruff: noqa: F811 - the imported fixtures are used by name as test parameters
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import analysis, render
from decision_lab import period as pd
from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab.calibration_days import Pool
from decision_lab.longrun import Profit
from decision_lab.records import CycleRecord
from decision_lab.regimes import RegimeLabel
from decision_lab.spikes import SpikeEvent
from decision_lab.tests.test_period_pause import (  # noqa: F401 - pytest fixtures
    Ran,
    Scenarios,
    candidate_over,
    dropping_data,
    flat_data,
    scenarios,
)
from tradebot.core.clock import SystemClock
from tradebot.core.enums import CycleOutcome
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore


async def scoring_for(data: Path) -> analysis.Scoring:
    return await analysis.build_scoring(data, clock=SystemClock())


def records_of(ran: Ran) -> tuple[CycleRecord, ...]:
    from decision_lab.records import records_from_store

    engine = open_database(ran.database)
    writer = SingleWriter(engine)
    try:
        return records_from_store(EventStore(engine, writer))
    finally:
        writer.close()
        engine.dispose()


async def analysed(scenarios: Scenarios, flat_data: Path) -> tuple[Ran, analysis.PeriodAnalysis]:
    ran = await scenarios.paused_in_january()
    result = analysis.analyse_period(
        ran.meta.run_id,
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
        workspace=ran.database.parent.parent,
    )
    return ran, result


def stretch_ids(records: tuple[CycleRecord, ...]) -> set[str]:
    return {r.cycle_id for r in records if r.outcome == CycleOutcome.QUARANTINED.value}


async def test_all_three_regime_rows_exist_with_zero_decisions(
    scenarios: Scenarios, flat_data: Path
) -> None:
    ran = await scenarios.paused_in_january()

    empty = analysis.fold_period(
        ran.meta,
        (),
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
        outcomes={},
    )

    assert [row.regime for row in empty.regimes] == [p.value for p in Pool]
    assert all(row.decisions == 0 for row in empty.regimes)
    assert empty.scored == () and empty.seats == ()


async def test_the_quarantined_stretch_is_unscored_by_a_real_score_call(
    scenarios: Scenarios, flat_data: Path
) -> None:
    _ran, result = await analysed(scenarios, flat_data)
    scoring = await scoring_for(flat_data)
    stretch = stretch_ids(result.records)
    assert stretch, (
        "the January pause has to leave a quarantined stretch for this to prove anything"
    )

    # Proven by calling the scorer on those cycles alone, not by assuming they hold no decision.
    only_stretch = tuple(r for r in result.records if r.cycle_id in stretch)
    assert (
        sc.score_records(
            only_stretch, index=scoring.index, regimes=scoring.regimes, params=scoring.params
        )
        == ()
    )
    assert {d.cycle_id for d in result.scored}.isdisjoint(stretch)
    assert {d.cycle_id for months in result.by_month.values() for d in months}.isdisjoint(stretch)
    without = tuple(r for r in result.records if r.cycle_id not in stretch)
    assert result.seats == st.score_seats(
        without, result.scored, panel=candidate_over(flat_data, 2).panel
    )
    assert any(r.cycle_id in stretch for r in result.records), "records keep the stretch"


async def test_a_contaminated_cycle_is_unscored_and_counted(
    scenarios: Scenarios, flat_data: Path
) -> None:
    ran, result = await analysed(scenarios, flat_data)
    assert result.contaminated == 0, "the run's own seats answered on their primary bindings"
    panel = candidate_over(flat_data, 2).panel
    target = next(r for r in result.records if r.responses and r.decisions)
    doctored = tuple(
        r.model_copy(
            update={
                "responses": tuple(
                    x.model_copy(update={"model": "substitute"}) for x in r.responses
                )
            }
        )
        if r.cycle_id == target.cycle_id
        else r
        for r in result.records
    )

    folded = analysis.fold_period(
        ran.meta, doctored, await scoring_for(flat_data), panel=panel, archive=None, outcomes={}
    )

    assert folded.contaminated == 1
    assert target.cycle_id not in {d.cycle_id for d in folded.scored}
    assert target.cycle_id in {d.cycle_id for d in result.scored}, "it was scoring before"


async def test_seat_trend_buckets_by_month_for_a_quarter_and_by_quarter_for_a_year(
    scenarios: Scenarios, flat_data: Path
) -> None:
    ran, result = await analysed(scenarios, flat_data)

    seat_ids = {seat.seat_id for seat in candidate_over(flat_data, 2).panel.seats}
    assert set(result.seat_trend) == seat_ids
    assert all(list(row) == ["2024-01", "2024-02", "2024-03"] for row in result.seat_trend.values())

    year = ran.meta.model_copy(update={"period": pd.parse_period("2024")})
    folded = analysis.fold_period(
        year,
        result.records,
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
        outcomes={},
    )
    assert all(
        list(row) == ["2024-Q1", "2024-Q2", "2024-Q3", "2024-Q4"]
        for row in folded.seat_trend.values()
    )
    # A bucket in which a seat was never graded is unknown, not zero percent.
    assert all(row["2024-Q2"] is None for row in folded.seat_trend.values())
    assert any(isinstance(row["2024-Q1"], Decimal) for row in folded.seat_trend.values())


async def test_months_never_reached_get_no_row(scenarios: Scenarios, flat_data: Path) -> None:
    ran, result = await analysed(scenarios, flat_data)
    january_only = ran.meta.model_copy(update={"months": ran.meta.months[:1]})
    before_february = tuple(r for r in result.records if r.as_of < datetime(2024, 2, 1, tzinfo=UTC))

    folded = analysis.fold_period(
        january_only,
        before_february,
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
        outcomes={},
    )

    assert folded.not_run == ("2024-02", "2024-03")
    assert list(folded.by_month) == ["2024-01"]


async def test_events_are_largest_move_first_whatever_the_sign(
    scenarios: Scenarios, flat_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran, _ = await analysed(scenarios, flat_data)

    def event(day: int, move: str) -> SpikeEvent:
        return SpikeEvent(
            start=datetime(2024, 1, day, tzinfo=UTC),
            end=datetime(2024, 1, day, 6, tzinfo=UTC),
            # A merged event can retrace, so the direction is not the sign of the move.
            direction=RegimeLabel.SHOCK_UP,
            move_pct=Decimal(move),
            peak_volatility=Decimal(1),
        )

    outside = SpikeEvent(
        start=datetime(2023, 6, 1, tzinfo=UTC),
        end=datetime(2023, 6, 2, tzinfo=UTC),
        direction=RegimeLabel.SHOCK_UP,
        move_pct=Decimal(90),
        peak_volatility=Decimal(1),
    )
    monkeypatch.setattr(
        analysis,
        "find_events",
        lambda *_a, **_k: (event(3, "2"), event(5, "-7"), event(9, "4"), outside),
    )

    folded = analysis.fold_period(
        ran.meta,
        (),
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
        outcomes={},
    )

    assert [e.move_pct for e in folded.events] == [Decimal(-7), Decimal(4), Decimal(2)]
    assert len(folded.event_news) == len(folded.events)


async def test_running_follows_the_meta(scenarios: Scenarios, flat_data: Path) -> None:
    ran, result = await analysed(scenarios, flat_data)
    assert not result.running

    live = ran.meta.model_copy(update={"ending": pd.RunEnding.RUNNING})
    folded = analysis.fold_period(
        live,
        (),
        await scoring_for(flat_data),
        panel=candidate_over(flat_data, 2).panel,
        archive=None,
        outcomes={},
    )
    assert folded.running


async def test_the_page_is_written_from_the_analysis(
    scenarios: Scenarios, flat_data: Path, tmp_path: Path
) -> None:
    ran, result = await analysed(scenarios, flat_data)
    changed = _with(
        result,
        contaminated=4,
        meta=ran.meta.model_copy(update={"abandoned_cost_usd": Decimal("0.5")}),
    )

    out = render.write_period(
        changed,
        tmp_path / "page.md",
        generated_at=datetime(2026, 11, 1, tzinfo=UTC),
        panel_models=("stub:x",),
    )
    page = out.read_text(encoding="utf-8")

    assert ran.meta.run_id in page
    assert "4 cycle(s)" in page and "substitute" in page
    # The month rows leave the abandoned money out and the profit includes it: say so.
    assert "0.5" in page and "abandoned" in page
    assert "NEWS-BLIND" in page and "PAUSE LIFTED" in page


async def test_a_frozen_run_shows_spend_and_no_valuation(
    scenarios: Scenarios, flat_data: Path, tmp_path: Path
) -> None:
    ran, result = await analysed(scenarios, flat_data)
    months = tuple(m.model_copy(update={"cost_usd": Decimal("0.25")}) for m in ran.meta.months)
    frozen = ran.meta.model_copy(
        update={"profit": Profit(unvaluable=True, freeze_reason="no mark"), "months": months}
    )

    page = render.write_period(
        _with(result, meta=frozen),
        tmp_path / "frozen.md",
        generated_at=datetime(2026, 11, 1, tzinfo=UTC),
        panel_models=(),
    ).read_text(encoding="utf-8")

    assert "UNVALUABLE" in page
    assert "net profit" not in page and "total profit" not in page
    assert "spent" in page and "0.25" in page


def _with(
    result: analysis.PeriodAnalysis,
    *,
    meta: pd.PeriodMeta | None = None,
    contaminated: int | None = None,
) -> analysis.PeriodAnalysis:
    return replace(
        result,
        meta=meta or result.meta,
        contaminated=result.contaminated if contaminated is None else contaminated,
    )


async def test_each_cycle_says_what_came_of_its_decision(
    scenarios: Scenarios, flat_data: Path
) -> None:
    """Spec §4 item 7, "the result": whether an order followed, and what happened to it."""
    _ran, result = await analysed(scenarios, flat_data)
    bought = next(r for r in result.records if r.as_of == datetime(2024, 1, 10, tzinfo=UTC))
    vetoed = next(r for r in result.records if r.as_of == datetime(2024, 1, 22, tzinfo=UTC))

    entry = next(o for o in result.outcomes[bought.cycle_id].orders if o.role == "entry")
    assert entry.side == "buy" and entry.filled_qty > 0 and entry.avg_fill_price is not None
    assert result.outcomes[vetoed.cycle_id].vetoes, "the paused BUY names the rule that refused it"
    assert not result.outcomes[vetoed.cycle_id].orders


def test_fold_period_requires_the_cycle_results() -> None:
    """Fix round 1, item 5: omitted, every cycle would render "No order followed"."""
    import inspect

    parameter = inspect.signature(analysis.fold_period).parameters["outcomes"]
    assert parameter.default is inspect.Parameter.empty
