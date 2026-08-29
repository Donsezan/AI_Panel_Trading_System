"""§9.5's metrics, and the reporting rule that no metric is shown without its split (§8.3).

`precision on action` is the figure that matters most: a WAIT-heavy panel scores well on accuracy
while never trading, and accuracy alone would recommend it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import scoring as sc
from decision_lab.calibration_days import Pool
from tradebot.core.enums import Action

AT = datetime(2024, 1, 1, tzinfo=UTC)


def scored(
    verdict: sc.Verdict,
    action: Action = Action.BUY,
    *,
    regime: Pool = Pool.NORMAL,
    conviction: str = "0.8",
    window: str = "",
    regret: str = "0",
    cost: str = "0.01",
    degraded: bool = False,
) -> sc.ScoredDecision:
    return sc.ScoredDecision(
        cycle_id="c",
        as_of=AT,
        instrument_key="binance:BTC/USDT",
        regime=regime,
        window_name=window,
        action=action,
        conviction=Decimal(conviction),
        asked_for_an_order=action.is_tradable,
        holding=False,
        degraded=degraded,
        verdict=verdict,
        regret=Decimal(regret),
        cost_usd=Decimal(cost),
    )


def test_accuracy_counts_only_scored_decisions() -> None:
    metrics = sc.summarise(
        [
            scored(sc.Verdict.CORRECT),
            scored(sc.Verdict.WRONG),
            scored(sc.Verdict.UNSCORED_GAP),
            scored(sc.Verdict.UNSCORED_HORIZON),
        ],
        regime="NORMAL",
    )
    assert metrics.scored == 2
    assert metrics.accuracy == Decimal("0.5")


def test_unscored_counts_carry_their_reasons() -> None:
    """A run that dropped them would report accuracy over a subset it chose after the fact."""
    metrics = sc.summarise(
        [scored(sc.Verdict.UNSCORED_GAP), scored(sc.Verdict.UNSCORED_NO_ATR)], regime="NORMAL"
    )
    assert metrics.unscored == {"UNSCORED (gap)": 1, "UNSCORED (no ATR)": 1}


def test_precision_on_action_ignores_the_wait_heavy_panel() -> None:
    """Two correct WAITs and one wrong BUY: accuracy 2/3, precision on action 0/1."""
    metrics = sc.summarise(
        [
            scored(sc.Verdict.CORRECT, Action.WAIT),
            scored(sc.Verdict.CORRECT, Action.WAIT),
            scored(sc.Verdict.WRONG, Action.BUY),
        ],
        regime="NORMAL",
    )
    assert metrics.accuracy > Decimal("0.6")
    assert metrics.precision_on_action == Decimal(0)
    assert metrics.action_rate < Decimal("0.4")


def test_the_conviction_gap_is_correct_minus_wrong() -> None:
    """A panel whose conviction carries information is worth more than one right as often by
    accident, because conviction feeds the Tier-1 floor and sizing."""
    metrics = sc.summarise(
        [
            scored(sc.Verdict.CORRECT, conviction="0.9"),
            scored(sc.Verdict.WRONG, conviction="0.4"),
        ],
        regime="NORMAL",
    )
    assert metrics.mean_conviction_gap == Decimal("0.5")


def test_a_panel_with_no_wrong_calls_has_no_conviction_gap() -> None:
    """No denominator. Reporting the correct-side mean as the gap would flatter it."""
    metrics = sc.summarise([scored(sc.Verdict.CORRECT)], regime="NORMAL")
    assert metrics.mean_conviction_gap == Decimal(0)


def test_the_degradation_rate_is_over_every_decision_not_the_scored_ones() -> None:
    """A candidate that scores well on the cycles it answered while failing a third of them is
    not a better panel (§9.5)."""
    metrics = sc.summarise(
        [scored(sc.Verdict.CORRECT), scored(sc.Verdict.UNSCORED_GAP, degraded=True)],
        regime="NORMAL",
    )
    assert metrics.degradation_rate == Decimal("0.5")


def test_cost_per_scored_decision_divides_by_the_scored_ones() -> None:
    metrics = sc.summarise(
        [scored(sc.Verdict.CORRECT, cost="0.10"), scored(sc.Verdict.UNSCORED_GAP, cost="0.10")],
        regime="NORMAL",
    )
    assert metrics.cost_usd == Decimal("0.20")
    assert metrics.cost_per_scored == Decimal("0.20")


def test_an_empty_regime_reports_zeroes_rather_than_dividing_by_zero() -> None:
    metrics = sc.summarise([], regime="SHOCK_UP")
    assert metrics.scored == 0
    assert metrics.accuracy == Decimal(0)


def test_the_split_always_carries_all_three_regimes() -> None:
    """§8.3: no metric is ever shown without its regime split, including an empty one — a missing
    `SHOCK_DOWN` row reads as 'not measured', which is the opposite of 'never happened'."""
    rows = sc.by_regime([scored(sc.Verdict.CORRECT, regime=Pool.NORMAL)])
    assert [row.regime for row in rows[:3]] == ["NORMAL", "SHOCK_UP", "SHOCK_DOWN"]


def test_shock_up_and_shock_down_are_never_pooled() -> None:
    """§10.3. They ask opposite questions of a long-only system."""
    rows = sc.by_regime(
        [
            scored(sc.Verdict.CORRECT, regime=Pool.SHOCK_UP),
            scored(sc.Verdict.WRONG, regime=Pool.SHOCK_DOWN),
        ]
    )
    assert {row.regime for row in rows} >= {"SHOCK_UP", "SHOCK_DOWN"}
    assert "SHOCK" not in {row.regime for row in rows}
    up = next(r for r in rows if r.regime == "SHOCK_UP")
    down = next(r for r in rows if r.regime == "SHOCK_DOWN")
    assert up.accuracy == Decimal(1)
    assert down.accuracy == Decimal(0)


def test_a_named_window_appears_on_its_own_row_and_in_its_aggregate() -> None:
    """§8.2: both, so an episode can be read by name without vanishing from the totals."""
    rows = sc.by_regime(
        [scored(sc.Verdict.CORRECT, regime=Pool.SHOCK_UP, window="spot ETF approval")]
    )
    by_name = {row.regime: row for row in rows}
    assert by_name["SHOCK_UP"].scored == 1
    assert by_name["spot ETF approval"].scored == 1
