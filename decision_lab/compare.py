"""Candidate against candidate (spec §9.6).

§9.5 answers "how good is this panel". This answers the question a sweep exists for: *are these
two panels actually different, and which is better where?*

Two tables:

* **the ranking** — every candidate's §9.5 metrics, per regime, ordered by accuracy. Every regime
  is always rendered and `SHOCK_UP`/`SHOCK_DOWN` are never pooled (§8.3): an absent `SHOCK_DOWN`
  row reads as *not measured*, which is the opposite of *never happened*.
* **the agreement matrix** — pairwise, per regime. Two candidates agreeing 98% of the time are one
  experiment run twice, and an operator paying for both should be told so. Beside it, **tradable
  divergence**: the cycles where exactly one asked for an order, which is the disagreement that
  moves money.

Nothing here re-scores anything. `by_regime` is slice B's own fold, called once per candidate, and
`asked_for_an_order` is what `scoring.py` already stored from `Action.is_tradable` — the same enum
property the bot's `validation/comparison.py` pairs on (§2.4).

Failure semantics: only cycles *both* candidates answered are compared — a cycle one of them never
reached is not a disagreement. Nothing here performs I/O.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence

from decision_lab.scoring import RegimeMetrics, ScoredDecision, by_regime, ratio
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money


class Ranked(DomainModel):
    """One candidate's standing in one regime."""

    regime: str
    candidate_id: str
    scored: int = 0
    accuracy: Money = ZERO
    action_rate: Money = ZERO
    precision_on_action: Money = ZERO
    mean_conviction_gap: Money = ZERO
    regret_per_decision: Money = ZERO
    degradation_rate: Money = ZERO
    cost_usd: Money = ZERO
    cost_per_scored: Money = ZERO


class Agreement(DomainModel):
    """How often two candidates said the same thing, and how often it mattered."""

    regime: str
    left: str
    right: str
    compared: int = 0
    agreed: int = 0
    rate: Money = ZERO
    #: Cycles where exactly one asked for an order — the disagreement that moves money (§9.6).
    tradable_divergences: int = 0


def metrics_by_candidate(
    by_candidate: Mapping[str, Sequence[ScoredDecision]],
) -> dict[str, tuple[RegimeMetrics, ...]]:
    """Slice B's own per-regime fold, once per candidate. No second scorer (§2.4)."""
    return {name: by_regime(rows) for name, rows in by_candidate.items()}


def ranking(by_candidate: Mapping[str, Sequence[ScoredDecision]]) -> tuple[Ranked, ...]:
    """Every candidate, every regime, ordered by accuracy within each regime."""
    folded = metrics_by_candidate(by_candidate)
    regimes: list[str] = []
    for regime_rows in folded.values():
        regimes += [row.regime for row in regime_rows if row.regime not in regimes]

    ranked: list[Ranked] = []
    for regime in regimes:
        candidates_here = [
            Ranked(
                regime=regime,
                candidate_id=name,
                scored=metrics.scored,
                accuracy=metrics.accuracy,
                action_rate=metrics.action_rate,
                precision_on_action=metrics.precision_on_action,
                mean_conviction_gap=metrics.mean_conviction_gap,
                regret_per_decision=metrics.regret_per_decision,
                degradation_rate=metrics.degradation_rate,
                cost_usd=metrics.cost_usd,
                cost_per_scored=metrics.cost_per_scored,
            )
            for name, metrics_rows in folded.items()
            for metrics in metrics_rows
            if metrics.regime == regime
        ]
        ranked += sorted(candidates_here, key=lambda row: (-row.accuracy, row.candidate_id))
    return tuple(ranked)


def agreement(by_candidate: Mapping[str, Sequence[ScoredDecision]]) -> tuple[Agreement, ...]:
    """Pairwise agreement per regime, each pair reported once."""
    indexed = {
        name: {(row.cycle_id, row.instrument_key): row for row in rows}
        for name, rows in by_candidate.items()
    }
    regimes: list[str] = []
    for rows in by_candidate.values():
        for row in rows:
            if row.regime.value not in regimes:
                regimes.append(row.regime.value)
            if row.window_name and row.window_name not in regimes:
                regimes.append(row.window_name)

    found: list[Agreement] = []
    for regime in regimes:
        for left, right in itertools.combinations(sorted(indexed), 2):
            shared = [
                (indexed[left][key], indexed[right][key])
                for key in indexed[left]
                if key in indexed[right] and _in_regime(indexed[left][key], regime)
            ]
            agreed = sum(1 for a, b in shared if a.action is b.action)
            divergent = sum(
                1 for a, b in shared if a.asked_for_an_order is not b.asked_for_an_order
            )
            found.append(
                Agreement(
                    regime=regime,
                    left=left,
                    right=right,
                    compared=len(shared),
                    agreed=agreed,
                    rate=ratio(agreed, len(shared)),
                    tradable_divergences=divergent,
                )
            )
    return tuple(found)


def _in_regime(row: ScoredDecision, regime: str) -> bool:
    """A named window is its own row *and* keeps its automatic label's row (§8.2, §8.3)."""
    return row.regime.value == regime or row.window_name == regime
