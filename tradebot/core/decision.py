"""Panel output: what a seat may say, and what the consensus rule produces from it.

`SeatVote` is the hard output contract. A response that fails it is a *failed vote*, never a
best-effort parse — the whole point of validating structured output is that malformed input
cannot become a smaller, subtler order (DESIGN [L8]).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from tradebot.core.enums import Action, SizeHint
from tradebot.core.schema import DomainModel, Money, UtcDatetime

MAX_THESIS_WORDS = 200


class SeatVote(DomainModel):
    """One seat's structured position on one instrument.

    Cross-field rules are part of the contract, not a formality: a tradable action with
    `size_hint = none` is incoherent, and a non-tradable action carrying a size is a model
    hedging its bets in a way the risk layer would otherwise have to interpret.
    """

    action: Action
    conviction: int = Field(ge=1, le=5)
    size_hint: SizeHint
    thesis: str
    key_risks: tuple[str, ...] = ()
    invalidation: str = ""

    @model_validator(mode="after")
    def _check_coherence(self) -> SeatVote:
        if self.action is Action.ABSTAIN:
            raise ValueError("ABSTAIN is assigned by the panel, never claimed by a seat")
        if self.action.is_tradable and self.size_hint is SizeHint.NONE:
            raise ValueError(f"{self.action} requires a size_hint other than none")
        if not self.action.is_tradable and self.size_hint is not SizeHint.NONE:
            raise ValueError(f"{self.action} must carry size_hint none")
        if len(self.thesis.split()) > MAX_THESIS_WORDS:
            raise ValueError(f"thesis exceeds {MAX_THESIS_WORDS} words")
        return self


class SeatResponse(DomainModel):
    """One seat's turn in one round, including the failures.

    An abstention is recorded with its raw text and reason so a degraded panel can be
    investigated after the fact rather than inferred from a gap.
    """

    seat_id: str
    role: str
    provider_id: str
    model: str
    round_index: int
    instrument_key: str
    vote: SeatVote | None = None
    abstain_reason: str | None = None
    raw_text: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    responded_at: UtcDatetime

    @model_validator(mode="after")
    def _check_abstention_is_explained(self) -> SeatResponse:
        if (self.vote is None) is not (self.abstain_reason is not None):
            raise ValueError("a seat response has either a vote or an abstain_reason")
        return self

    @property
    def abstained(self) -> bool:
        return self.vote is None


class Deliberation(DomainModel):
    """The full transcript of one panel run, plus what it cost.

    Kept whole — including abstentions and losing arguments — because the drill-down view over
    this transcript is the core research artifact the system exists to produce (DESIGN §6.10).
    """

    instrument_key: str
    protocol_id: str
    rounds: int
    responses: tuple[SeatResponse, ...] = ()
    cost_usd: Money = Decimal(0)

    @property
    def final_round(self) -> tuple[SeatResponse, ...]:
        """Only the last round votes; earlier rounds are the record of how it got there."""
        if not self.responses:
            return ()
        last = max(response.round_index for response in self.responses)
        return tuple(r for r in self.responses if r.round_index == last)


class Decision(DomainModel):
    """The panel's validated output for one instrument — a proposal, not an order.

    `conviction` is on the 0–1 scale throughout the system; seats rate 1–5 and the consensus
    rule normalizes (DESIGN §6.5). All risk thresholds are stated on this scale.
    """

    instrument_key: str
    action: Action
    conviction: Money = Decimal(0)
    size_hint: SizeHint = SizeHint.NONE
    reasoning_summary: str = ""
    dissent: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    votes_for: int = 0
    votes_total: int = 0
    abstentions: int = 0

    @model_validator(mode="after")
    def _check_conviction_scale(self) -> Decision:
        if not Decimal(0) <= self.conviction <= Decimal(1):
            raise ValueError(f"conviction must be 0–1, got {self.conviction}")
        return self

    @property
    def is_actionable(self) -> bool:
        """Whether this decision asks for an order at all. Risk still has the final say."""
        return self.action.is_tradable and self.size_hint is not SizeHint.NONE
