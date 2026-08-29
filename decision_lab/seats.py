"""Per-seat scoring: which seat is carrying the result (spec §9.7).

§9.5 scores what the *panel* decided. A seat is not a panel, and an operator tuning seats needs
the level below. Everything this needs is already recorded on `SeatResponse` — `seat_id`, `vote`,
`abstain_reason`, `round_index`, `latency_ms`, the tokens, `cost_usd`, and `fingerprint`, the
binding that actually answered after any fallback — so it costs no new data and no new provider
calls.

Two metrics carry most of the weight and are the ones a reader will trust without checking:

* **Swing rate** — how often replaying `decision.consensus.reach_consensus` over the recorded
  votes *minus this seat* changes the panel's decision. Deterministic, free, and the number that
  separates a seat carrying weight from one padding a majority. The counterfactual removes the
  seat from the `PanelConfig` too, not only from the votes: `required_votes` and the abstention
  fraction are both computed from the seat count, and leaving it at four while three seats voted
  would ask "what if this seat had abstained", which is a different question.
* **Marginal contribution** — dissents that were right against a wrong panel, minus dissents that
  were wrong against a right one. "Does this seat earn its slot", in one signed figure.

`reach_consensus` is **imported**, never reimplemented: a second consensus rule here would make
the swing rate a measurement of the copy.

Failure semantics: nothing here fetches or writes. A one-seat panel has no swing rate and reports
none rather than zero — removing the only seat leaves no panel to reach consensus with, and zero
would read as "this seat does not matter".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from decision_lab.calibration_days import Pool
from decision_lab.records import CycleRecord
from decision_lab.scoring import CORRECT_ACTIONS, ScoredDecision, Truth, mean, ratio
from tradebot.core.config import PanelConfig
from tradebot.core.decision import SeatResponse, total_cost
from tradebot.core.enums import Action
from tradebot.core.money import ZERO, divide
from tradebot.core.schema import DomainModel, Money
from tradebot.decision.consensus import reach_consensus

#: Seats rate 1–5; `Decision.conviction` is 0–1 (DESIGN §6.5). A gap computed on two scales is
#: not a gap, so seat convictions are normalised before they are compared to anything.
SEAT_CONVICTION_SCALE = Decimal(5)

ROUND_ZERO = "round 0"
FINAL = "final"


class SeatMetrics(DomainModel):
    """One seat, one regime, one round label."""

    seat_id: str
    regime: str
    round_label: str
    turns: int = 0
    scored: int = 0
    correct: int = 0
    accuracy: Money = ZERO
    action_rate: Money = ZERO
    precision_on_action: Money = ZERO
    mean_conviction_correct: Money = ZERO
    mean_conviction_wrong: Money = ZERO
    mean_conviction_gap: Money = ZERO
    abstention_rate: Money = ZERO
    fallback_rate: Money = ZERO
    cost_per_vote: Money = ZERO
    latency_ms_per_vote: int = 0
    swings: int = 0
    swing_rate: Money = ZERO
    marginal_contribution: int = 0


def _conviction(response: SeatResponse) -> Decimal:
    return (
        divide(Decimal(response.vote.conviction), SEAT_CONVICTION_SCALE) if response.vote else ZERO
    )


def score_seat_votes(
    responses: Sequence[SeatResponse],
    *,
    truth: Truth | None,
    regime: Pool,
    round_label: str,
    primary: Mapping[str, str] | None = None,
) -> dict[str, SeatMetrics]:
    """Score each seat's turns in one round, against the panel's own §9.3 truth label."""
    by_seat: dict[str, list[SeatResponse]] = {}
    for response in responses:
        by_seat.setdefault(response.seat_id, []).append(response)

    metrics: dict[str, SeatMetrics] = {}
    for seat_id, turns in by_seat.items():
        voted = [t for t in turns if t.vote is not None]
        correct = (
            [t for t in voted if t.vote and t.vote.action in CORRECT_ACTIONS[truth]]
            if truth is not None
            else []
        )
        wrong = [t for t in voted if t not in correct] if truth is not None else []
        acted = [t for t in voted if t.vote and t.vote.action.is_tradable]
        expected = (primary or {}).get(seat_id)
        metrics[seat_id] = SeatMetrics(
            seat_id=seat_id,
            regime=regime.value,
            round_label=round_label,
            turns=len(turns),
            scored=len(voted) if truth is not None else 0,
            correct=len(correct),
            accuracy=ratio(len(correct), len(voted)) if truth is not None else ZERO,
            action_rate=ratio(len(acted), len(voted)),
            precision_on_action=ratio(sum(1 for t in acted if t in correct), len(acted)),
            mean_conviction_correct=mean([_conviction(t) for t in correct]),
            mean_conviction_wrong=mean([_conviction(t) for t in wrong]),
            mean_conviction_gap=(
                mean([_conviction(t) for t in correct]) - mean([_conviction(t) for t in wrong])
                if correct and wrong
                else ZERO
            ),
            abstention_rate=ratio(len(turns) - len(voted), len(turns)),
            # A seat that answered on its backup all sweep is a seat that was never tested, and
            # today nothing anywhere would say so.
            fallback_rate=(
                ratio(sum(1 for t in turns if t.fingerprint != expected), len(turns))
                if expected
                else ZERO
            ),
            # Through `total_cost`, so `basket` mode — one call answering for N instruments — is
            # not counted N times (DESIGN §6.5).
            cost_per_vote=ratio(total_cost(turns), len(voted)),
            latency_ms_per_vote=int(ratio(sum(t.latency_ms for t in voted), len(voted))),
        )
    return metrics


def swings(
    final_round: Sequence[SeatResponse], *, panel: PanelConfig, instrument_key: str
) -> dict[str, bool]:
    """Which seats' removal would have changed the panel's decision.

    Empty for a one-seat panel: removing the only seat leaves no panel, and reporting `False`
    would read as "this seat does not matter" — the opposite of the truth.
    """
    if len(panel.seats) < 2:
        return {}
    actual = reach_consensus(tuple(final_round), panel, instrument_key).action
    result: dict[str, bool] = {}
    for seat in panel.seats:
        without = panel.model_copy(
            update={"seats": tuple(s for s in panel.seats if s.seat_id != seat.seat_id)}
        )
        remaining = tuple(r for r in final_round if r.seat_id != seat.seat_id)
        counterfactual = reach_consensus(remaining, without, instrument_key).action
        result[seat.seat_id] = counterfactual is not actual
    return result


def marginal_contribution(*, seat_action: Action, panel_action: Action, truth: Truth | None) -> int:
    """+1 for a right dissent against a wrong panel, −1 for a wrong dissent against a right one.

    Zero when the seat agreed — it added no information — and zero when both were wrong, because
    the panel would have been wrong either way and the seat neither earned nor cost its slot.
    """
    if truth is None or seat_action is panel_action:
        return 0
    correct = CORRECT_ACTIONS[truth]
    seat_right = seat_action in correct
    panel_right = panel_action in correct
    if seat_right and not panel_right:
        return 1
    if panel_right and not seat_right:
        return -1
    return 0


def score_seats_for_instrument(
    responses: Sequence[SeatResponse],
    *,
    truth: Truth | None,
    regime: Pool,
    panel: PanelConfig,
    instrument_key: str,
) -> tuple[SeatMetrics, ...]:
    """Both rounds' tables for one instrument in one cycle."""
    primary = {seat.seat_id: f"{seat.provider_id}:{seat.model}" for seat in panel.seats}
    about = [r for r in responses if r.instrument_key == instrument_key]
    if not about:
        return ()
    last = max(r.round_index for r in about)
    rounds = {
        ROUND_ZERO: [r for r in about if r.round_index == 0],
        FINAL: [r for r in about if r.round_index == last],
    }
    return tuple(
        metrics
        for label, turns in rounds.items()
        for metrics in score_seat_votes(
            turns, truth=truth, regime=regime, round_label=label, primary=primary
        ).values()
    )


def rounds_are_identical(rows: Sequence[SeatMetrics]) -> bool:
    """§9.7: under `single_round` the two are the same, and the report says so rather than
    printing the same numbers twice."""
    zero = {r.seat_id: r for r in rows if r.round_label == ROUND_ZERO}
    final = {r.seat_id: r for r in rows if r.round_label == FINAL}
    return all(
        zero[seat_id].model_copy(update={"round_label": FINAL}) == final.get(seat_id)
        for seat_id in zero
    )


def score_seats(
    records: Sequence[CycleRecord],
    scored: Sequence[ScoredDecision],
    *,
    panel: PanelConfig,
) -> tuple[SeatMetrics, ...]:
    """Fold every cycle's seat tables into one row per (seat, regime, round label)."""
    truth_by: dict[tuple[str, str], Truth | None] = {
        (d.cycle_id, d.instrument_key): d.truth for d in scored
    }
    regime_by: dict[tuple[str, str], Pool] = {
        (d.cycle_id, d.instrument_key): d.regime for d in scored
    }
    panel_action: dict[tuple[str, str], Action] = {
        (d.cycle_id, d.instrument_key): d.action for d in scored
    }

    collected: list[SeatMetrics] = []
    influence: dict[tuple[str, str], list[int]] = {}
    swung: dict[tuple[str, str], int] = {}
    seen: dict[tuple[str, str], int] = {}
    for record in records:
        for context in record.snapshot.instruments:
            key = (record.cycle_id, context.instrument.key)
            if key not in truth_by:
                continue
            collected += score_seats_for_instrument(
                record.responses,
                truth=truth_by[key],
                regime=regime_by[key],
                panel=panel,
                instrument_key=context.instrument.key,
            )
            final = record.final_round_for(context.instrument.key)
            for seat_id, swung_here in swings(
                final, panel=panel, instrument_key=context.instrument.key
            ).items():
                index = (seat_id, regime_by[key].value)
                seen[index] = seen.get(index, 0) + 1
                swung[index] = swung.get(index, 0) + int(swung_here)
            for response in final:
                if response.vote is None:
                    continue
                index = (response.seat_id, regime_by[key].value)
                influence.setdefault(index, []).append(
                    marginal_contribution(
                        seat_action=response.vote.action,
                        panel_action=panel_action[key],
                        truth=truth_by[key],
                    )
                )

    return _fold(collected, swung=swung, seen=seen, influence=influence)


def _fold(
    rows: Sequence[SeatMetrics],
    *,
    swung: Mapping[tuple[str, str], int],
    seen: Mapping[tuple[str, str], int],
    influence: Mapping[tuple[str, str], Sequence[int]],
) -> tuple[SeatMetrics, ...]:
    """Sum per-cycle rows into one row per (seat, regime, round label), weighted by turns."""
    grouped: dict[tuple[str, str, str], list[SeatMetrics]] = {}
    for row in rows:
        grouped.setdefault((row.seat_id, row.regime, row.round_label), []).append(row)

    folded = []
    for (seat_id, regime, label), members in sorted(grouped.items()):
        turns = sum(m.turns for m in members)
        scored_ = sum(m.scored for m in members)
        correct = sum(m.correct for m in members)
        index = (seat_id, regime)
        folded.append(
            SeatMetrics(
                seat_id=seat_id,
                regime=regime,
                round_label=label,
                turns=turns,
                scored=scored_,
                correct=correct,
                accuracy=ratio(correct, scored_),
                action_rate=mean([m.action_rate for m in members]),
                precision_on_action=mean([m.precision_on_action for m in members]),
                mean_conviction_correct=mean([m.mean_conviction_correct for m in members]),
                mean_conviction_wrong=mean([m.mean_conviction_wrong for m in members]),
                mean_conviction_gap=mean([m.mean_conviction_gap for m in members]),
                abstention_rate=ratio(turns - scored_, turns),
                fallback_rate=mean([m.fallback_rate for m in members]),
                cost_per_vote=mean([m.cost_per_vote for m in members]),
                latency_ms_per_vote=int(mean([Decimal(m.latency_ms_per_vote) for m in members])),
                swings=swung.get(index, 0) if label == FINAL else 0,
                swing_rate=ratio(swung.get(index, 0), seen.get(index, 0))
                if label == FINAL
                else ZERO,
                marginal_contribution=sum(influence.get(index, ())) if label == FINAL else 0,
            )
        )
    return tuple(folded)
