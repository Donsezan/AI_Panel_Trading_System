"""The consensus rule: deterministic code, never an LLM (DESIGN §6.5).

The semantics that make this rule safe rather than merely decisive:

* **HOLD vs WAIT are different answers.** HOLD is an affirmative "keep the current position"
  vote and counts *against* acting. WAIT is the no-signal outcome — non-consensus, a degraded
  panel, or explicit uncertainty. Both produce no order; both are recorded distinctly, because
  the difference is the research signal.
* **Majorities are counted over the *original* seat count**, never over the seats that happened
  to answer. One abstention plus one BUY is not a mandate on a three-seat panel.
* **Non-consensus is an answer, not a failure to converge.** After the last round the panel
  gets `WAIT` with its disagreement recorded, rather than being pushed toward agreement it
  does not have (DESIGN [L6]).
* **The panel's size is reduced to its most conservative agreeing voice**, so a single
  enthusiastic seat cannot drag the size up.

Failure semantics: this function cannot fail — every input, including "every seat abstained",
maps to a valid `Decision`. That is deliberate. The consensus rule is the last place a cycle
could throw, and throwing here would leave a decision unrecorded.
"""

from __future__ import annotations

from collections import Counter
from decimal import ROUND_CEILING, Decimal

from tradebot.core.config import PanelConfig
from tradebot.core.decision import Decision, SeatResponse
from tradebot.core.enums import Action, SizeHint
from tradebot.core.money import ZERO, divide, multiply

PANEL_DEGRADED = "PANEL_DEGRADED"
PANEL_HOMOGENEOUS = "PANEL_HOMOGENEOUS"

MAX_CONVICTION_RATING = Decimal(5)
CONVICTION_SPAN = MAX_CONVICTION_RATING - Decimal(1)


def required_votes(panel: PanelConfig) -> int:
    """Votes needed for a tradable action, over the *original* seat count."""
    threshold = multiply(panel.qualified_majority, Decimal(panel.seat_count))
    return int(threshold.to_integral_value(rounding=ROUND_CEILING))


def _homogeneity_flags(responses: tuple[SeatResponse, ...]) -> tuple[str, ...]:
    """Flag a panel whose fallbacks collapsed it onto one model.

    Heterogeneity is a design control against sycophantic convergence; its silent loss has to
    be visible (DESIGN §6.5, R11).
    """
    fingerprints = Counter((r.provider_id, r.model) for r in responses)
    collapsed = len(responses) > 1 and max(fingerprints.values()) > 1
    return (PANEL_HOMOGENEOUS,) if collapsed else ()


def _conviction(agreeing: list[SeatResponse], seat_count: int) -> Decimal:
    """`((mean rating − 1) / 4) × agreement_fraction`, on the 0–1 scale (DESIGN §6.5)."""
    ratings = [Decimal(r.vote.conviction) for r in agreeing if r.vote is not None]
    if not ratings or seat_count == 0:
        return ZERO
    mean = divide(sum(ratings, start=ZERO), Decimal(len(ratings)))
    normalized = divide(mean - Decimal(1), CONVICTION_SPAN)
    agreement = divide(Decimal(len(agreeing)), Decimal(seat_count))
    return multiply(normalized, agreement)


def _most_conservative_size(agreeing: list[SeatResponse]) -> SizeHint:
    hints = [r.vote.size_hint for r in agreeing if r.vote is not None]
    return min(hints, key=lambda hint: hint.rank) if hints else SizeHint.NONE


def _dissent(responses: tuple[SeatResponse, ...], winner: Action) -> tuple[str, ...]:
    """Minority views, preserved verbatim. Suppressed dissent is what debate research warns of."""
    return tuple(
        f"{r.role}: {r.vote.thesis}"
        for r in responses
        if r.vote is not None and r.vote.action is not winner
    )


def _summary(agreeing: list[SeatResponse]) -> str:
    return " | ".join(f"{r.role}: {r.vote.thesis}" for r in agreeing if r.vote is not None)


def _wait(
    instrument_key: str,
    responses: tuple[SeatResponse, ...],
    panel: PanelConfig,
    reason: str,
    extra_flags: tuple[str, ...],
) -> Decision:
    abstentions = sum(1 for r in responses if r.abstained)
    return Decision(
        instrument_key=instrument_key,
        action=Action.WAIT,
        conviction=ZERO,
        size_hint=SizeHint.NONE,
        reasoning_summary=reason,
        dissent=tuple(f"{r.role}: {r.vote.thesis}" for r in responses if r.vote is not None),
        flags=extra_flags,
        votes_for=0,
        votes_total=panel.seat_count,
        abstentions=abstentions,
    )


def reach_consensus(
    responses: tuple[SeatResponse, ...], panel: PanelConfig, instrument_key: str
) -> Decision:
    """Fold the final round's seat responses into one `Decision`."""
    flags = _homogeneity_flags(responses)
    seat_count = panel.seat_count
    abstentions = sum(1 for response in responses if response.abstained)

    abstain_fraction = divide(Decimal(abstentions), Decimal(seat_count)) if seat_count else ZERO
    if abstain_fraction > panel.max_abstain_fraction:
        return _wait(
            instrument_key,
            responses,
            panel,
            f"{abstentions} of {seat_count} seats abstained",
            (*flags, PANEL_DEGRADED),
        )

    voted = [response for response in responses if response.vote is not None]
    tally = Counter(response.vote.action for response in voted if response.vote is not None)
    if not tally:
        return _wait(
            instrument_key, responses, panel, "no seat produced a vote", (*flags, PANEL_DEGRADED)
        )

    winner, votes = tally.most_common(1)[0]
    if votes < required_votes(panel):
        return _wait(
            instrument_key,
            responses,
            panel,
            f"no qualified majority: {dict(tally)} over {seat_count} seats",
            flags,
        )

    agreeing = [r for r in voted if r.vote is not None and r.vote.action is winner]
    return Decision(
        instrument_key=instrument_key,
        action=winner,
        conviction=_conviction(agreeing, seat_count),
        size_hint=_most_conservative_size(agreeing) if winner.is_tradable else SizeHint.NONE,
        reasoning_summary=_summary(agreeing),
        dissent=_dissent(responses, winner),
        flags=flags,
        votes_for=votes,
        votes_total=seat_count,
        abstentions=abstentions,
    )
