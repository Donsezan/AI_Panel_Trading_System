"""A seat is not a panel (spec §9.7).

Every `SeatResponse` already recorded carries the seat, the vote, the round, the latency, the
tokens, the cost and the `fingerprint` — the binding that actually answered after any fallback.
So this costs no new data and no new provider calls, and it answers the question an operator
tuning seats actually has: which of them is carrying the result.

**Round 0 is reported separately from the final vote, and the split is not cosmetic.** Under
`blind_then_debate` a seat's later votes are contaminated by its peers *by design* — that is what
the debate is for. "Which seat reasons well" and "which seat is easily talked round" are different
questions, and one column cannot answer both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab.calibration_days import Pool
from tradebot.core.decision import SeatResponse, SeatVote
from tradebot.core.enums import Action, SizeHint

AT = datetime(2024, 1, 1, tzinfo=UTC)
KEY = "binance:BTC/USDT"


def response(
    seat_id: str,
    action: Action | None,
    *,
    round_index: int = 0,
    conviction: int = 4,
    model: str = "primary",
    latency_ms: int = 100,
    cost: str = "0.01",
    call_id: str = "",
) -> SeatResponse:
    vote = (
        None
        if action is None
        else SeatVote(
            action=action,
            conviction=conviction,
            size_hint=SizeHint.HALF if action.is_tradable else SizeHint.NONE,
            thesis="because",
        )
    )
    return SeatResponse(
        seat_id=seat_id,
        role="analyst",
        provider_id="openrouter",
        model=model,
        round_index=round_index,
        instrument_key=KEY,
        vote=vote,
        abstain_reason=None if vote else "provider unreachable",
        responded_at=AT,
        latency_ms=latency_ms,
        cost_usd=Decimal(cost),
        **({"call_id": call_id} if call_id else {}),
    )


def scored(verdict: sc.Verdict, truth: sc.Truth) -> sc.ScoredDecision:
    return sc.ScoredDecision(
        cycle_id="c",
        as_of=AT,
        instrument_key=KEY,
        regime=Pool.NORMAL,
        action=Action.BUY,
        conviction=Decimal("0.8"),
        asked_for_an_order=True,
        holding=False,
        truth=truth,
        verdict=verdict,
    )


def test_a_seat_is_scored_against_the_same_truth_label() -> None:
    metrics = st.score_seat_votes(
        [response("trend", Action.BUY)],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["trend"].accuracy == Decimal(1)


def test_a_seat_that_stood_aside_from_a_rally_is_wrong() -> None:
    metrics = st.score_seat_votes(
        [response("risk", Action.WAIT)],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["risk"].accuracy == Decimal(0)


def test_the_abstention_rate_is_over_every_turn() -> None:
    metrics = st.score_seat_votes(
        [response("flaky", None), response("flaky", Action.BUY)],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["flaky"].abstention_rate == Decimal("0.5")
    assert metrics["flaky"].scored == 1, "an abstention has no vote to score"


def test_the_fallback_rate_reads_the_fingerprint() -> None:
    """A seat that answered on its backup all sweep is a seat that was never tested, and today
    nothing would say so (§9.7)."""
    metrics = st.score_seat_votes(
        [
            response("trend", Action.BUY, model="primary"),
            response("trend", Action.BUY, model="backup"),
        ],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
        primary={"trend": "openrouter:primary"},
    )
    assert metrics["trend"].fallback_rate == Decimal("0.5")


def test_cost_and_latency_are_per_answered_vote() -> None:
    """A seat marginally better and four times slower is a different trade-off at 1h cadence
    than at 24h."""
    metrics = st.score_seat_votes(
        [
            response("trend", Action.BUY, latency_ms=100, cost="0.02"),
            response("trend", None, latency_ms=900, cost="0.00"),
        ],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["trend"].latency_ms_per_vote == 100
    assert metrics["trend"].cost_per_vote == Decimal("0.02")


def test_cost_is_deduplicated_by_call_id() -> None:
    """In `basket` mode one call answers for every instrument; `total_cost` is the only sanctioned
    way to total money, and this must go through it."""
    shared = "one-call"
    metrics = st.score_seat_votes(
        [
            response("trend", Action.BUY, cost="0.05", call_id=shared),
            response("trend", Action.BUY, cost="0.05", call_id=shared),
        ],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["trend"].cost_per_vote == Decimal("0.025")


def test_seat_conviction_is_normalised_to_the_decision_scale() -> None:
    """Seats rate 1–5, `Decision.conviction` is 0–1. A gap on two scales is not a gap."""
    high = st.score_seat_votes(
        [response("a", Action.BUY, conviction=5)],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert high["a"].mean_conviction_correct == Decimal(1)
