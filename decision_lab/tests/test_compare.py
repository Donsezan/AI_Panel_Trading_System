"""§9.6: two candidates agreeing 98% of the time are one experiment run twice.

The agreement matrix is pairwise *per regime*, because two panels can agree completely in quiet
markets and diverge entirely in a crash — which is exactly the case an operator is choosing
between them for. Tradable divergence is the disagreement that moves money: where exactly one of
them asked for an order. It reuses `asked_for_an_order`, which `scoring.py` sets from
`Action.is_tradable` — the same enum property `validation/comparison.py` pairs on — rather than a
second definition of "would have traded".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import compare
from decision_lab.calibration_days import Pool
from decision_lab.scoring import ScoredDecision, Verdict
from tradebot.core.enums import Action

AS_OF = datetime(2024, 3, 1, tzinfo=UTC)


def decision(
    cycle: str, action: Action, *, regime: Pool = Pool.NORMAL, verdict: Verdict = Verdict.CORRECT
) -> ScoredDecision:
    return ScoredDecision(
        cycle_id=cycle,
        as_of=AS_OF,
        instrument_key="binance:BTC/USDT",
        regime=regime,
        action=action,
        conviction=Decimal("0.6"),
        asked_for_an_order=action.is_tradable,
        holding=False,
        verdict=verdict,
    )


def test_two_identical_candidates_agree_completely() -> None:
    rows = [decision("c1", Action.BUY), decision("c2", Action.WAIT)]

    normal = [r for r in compare.agreement({"a": rows, "b": list(rows)}) if r.regime == "NORMAL"]

    assert len(normal) == 1
    assert normal[0].rate == Decimal(1)
    assert normal[0].tradable_divergences == 0


def test_a_disagreement_that_moves_money_is_counted_apart() -> None:
    """Not every disagreement is one. `_TRADABLE_ACTIONS` holds BUY and SELL; HOLD and WAIT are
    two different ways of not acting, and a pair that differs only between those moves no money."""
    left = [decision("c1", Action.BUY), decision("c2", Action.HOLD)]
    right = [decision("c1", Action.WAIT), decision("c2", Action.WAIT)]

    normal = next(r for r in compare.agreement({"a": left, "b": right}) if r.regime == "NORMAL")

    assert normal.agreed == 0, "both cycles disagree on the action"
    assert normal.tradable_divergences == 1, "but only c1 has one side asking for an order"


def test_two_ways_of_not_acting_are_not_a_tradable_divergence() -> None:
    left = [decision("c1", Action.HOLD)]
    right = [decision("c1", Action.WAIT)]

    normal = next(r for r in compare.agreement({"a": left, "b": right}) if r.regime == "NORMAL")

    assert normal.agreed == 0
    assert normal.tradable_divergences == 0


def test_agreement_is_reported_per_regime_and_never_pooled() -> None:
    left = [decision("c1", Action.BUY), decision("c2", Action.BUY, regime=Pool.SHOCK_DOWN)]
    right = [decision("c1", Action.BUY), decision("c2", Action.WAIT, regime=Pool.SHOCK_DOWN)]

    found = {row.regime: row for row in compare.agreement({"a": left, "b": right})}

    assert found["NORMAL"].rate == Decimal(1)
    assert found["SHOCK_DOWN"].rate == Decimal(0)


def test_only_cycles_both_candidates_answered_are_compared() -> None:
    left = [decision("c1", Action.BUY), decision("c2", Action.BUY)]
    right = [decision("c1", Action.BUY)]

    normal = next(r for r in compare.agreement({"a": left, "b": right}) if r.regime == "NORMAL")

    assert normal.compared == 1, "a cycle one candidate never answered is not a disagreement"


def test_each_pair_is_reported_once() -> None:
    rows = [decision("c1", Action.BUY)]

    pairs = {
        (row.left, row.right)
        for row in compare.agreement({"a": rows, "b": list(rows), "c": list(rows)})
    }

    assert pairs == {("a", "b"), ("a", "c"), ("b", "c")}


def test_the_ranking_orders_by_accuracy_within_a_regime() -> None:
    good = [decision(f"c{i}", Action.BUY) for i in range(4)]
    bad = [decision(f"c{i}", Action.BUY, verdict=Verdict.WRONG) for i in range(4)]

    ranked = [r for r in compare.ranking({"weak": bad, "strong": good}) if r.regime == "NORMAL"]

    assert [row.candidate_id for row in ranked] == ["strong", "weak"]
    assert ranked[0].accuracy == Decimal(1)
    assert ranked[1].accuracy == Decimal(0)


def test_the_ranking_always_renders_every_regime_even_when_empty() -> None:
    rows = [decision("c1", Action.BUY)]

    regimes = {row.regime for row in compare.ranking({"a": rows})}

    assert {"NORMAL", "SHOCK_UP", "SHOCK_DOWN"} <= regimes, (
        "an absent SHOCK_DOWN row reads as *not measured*, the opposite of *never happened*"
    )
