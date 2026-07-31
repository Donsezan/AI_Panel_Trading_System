"""Panel output: what a seat may say, and what the consensus rule produces from it.

`SeatVote` is the hard output contract. A response that fails it is a *failed vote*, never a
best-effort parse — the whole point of validating structured output is that malformed input
cannot become a smaller, subtler order (DESIGN [L8]).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from pydantic import Field, model_validator

from tradebot.core.enums import Action, DecisionMode, SizeHint
from tradebot.core.ids import new_uuid
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime

MAX_THESIS_WORDS = 200
MAX_BASKET_VIEW_WORDS = 300


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
        if not self.thesis.strip():
            # The thesis is not decoration: it is what the dissent record, the reasoning summary
            # and the drill-down view are made of. A vote with no stated reasoning is a vote
            # nobody can audit afterwards, which is the one thing this system exists to prevent.
            raise ValueError("a vote must state a thesis")
        if len(self.thesis.split()) > MAX_THESIS_WORDS:
            raise ValueError(f"thesis exceeds {MAX_THESIS_WORDS} words")
        return self


class BasketAssessment(DomainModel):
    """A seat's answer in `basket` decision mode: one vote per instrument, plus a cross-asset view.

    The same hard contract as `SeatVote`, applied to a mapping. Keys are the venue symbols the
    prompt listed; the seat runner maps them back to instrument keys and rejects any symbol that
    was not asked about — a vote on an instrument we never mentioned is a hallucination, not an
    opinion (DESIGN §7.1).
    """

    assessments: dict[str, SeatVote]
    basket_view: str = ""

    @model_validator(mode="after")
    def _check_not_empty(self) -> BasketAssessment:
        if not self.assessments:
            raise ValueError("a basket assessment must cover at least one instrument")
        if len(self.basket_view.split()) > MAX_BASKET_VIEW_WORDS:
            raise ValueError(f"basket_view exceeds {MAX_BASKET_VIEW_WORDS} words")
        return self


class SeatResponse(DomainModel):
    """One seat's turn on one instrument in one round, including the failures.

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

    #: The provider call this response came out of. In `basket` mode one call answers for every
    #: instrument, so several responses share a `call_id` — and its tokens and cost describe that
    #: one call, not each response. Anything totalling money must therefore group by `call_id`;
    #: `total_cost` is the only sanctioned way to do it.
    call_id: str = Field(default_factory=new_uuid)
    cost_usd: Money = Decimal(0)

    @model_validator(mode="after")
    def _check_abstention_is_explained(self) -> SeatResponse:
        if (self.vote is None) is not (self.abstain_reason is not None):
            raise ValueError("a seat response has either a vote or an abstain_reason")
        return self

    @property
    def abstained(self) -> bool:
        return self.vote is None

    @property
    def fingerprint(self) -> str:
        """The binding that actually answered — after any fallback substitution."""
        return f"{self.provider_id}:{self.model}"


def total_cost(responses: Iterable[SeatResponse]) -> Decimal:
    """Sum the cost of the *distinct provider calls* behind these responses.

    Summing `cost_usd` directly would double-count `basket` mode, where one call produces a
    response per instrument. The dashboard's $/decision figure is only honest if this is the
    only place the arithmetic happens (DESIGN §6.5).
    """
    by_call = {response.call_id: response.cost_usd for response in responses}
    return sum(by_call.values(), start=ZERO)


class Deliberation(DomainModel):
    """The full transcript of one panel run, plus what it cost.

    Kept whole — including abstentions and losing arguments — because the drill-down view over
    this transcript is the core research artifact the system exists to produce (DESIGN §6.10).

    One run covers one instrument in `per_asset` mode and the whole basket in `basket` mode,
    which is why `instrument_keys` is plural.
    """

    deliberation_id: str = Field(default_factory=new_uuid)
    instrument_keys: tuple[str, ...]
    protocol_id: str
    decision_mode: DecisionMode = DecisionMode.PER_ASSET
    rounds: int
    responses: tuple[SeatResponse, ...] = ()
    #: True when the per-cycle cost budget ended the debate before `max_rounds` (DESIGN §6.5).
    budget_truncated: bool = False

    @model_validator(mode="after")
    def _check_covers_something(self) -> Deliberation:
        if not self.instrument_keys:
            raise ValueError("a deliberation covers at least one instrument")
        return self

    @property
    def cost_usd(self) -> Decimal:
        return total_cost(self.responses)

    @property
    def final_round(self) -> tuple[SeatResponse, ...]:
        """Only the last round votes; earlier rounds are the record of how it got there."""
        if not self.responses:
            return ()
        last = max(response.round_index for response in self.responses)
        return tuple(r for r in self.responses if r.round_index == last)

    def final_round_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        """The final round's responses about one instrument — what the consensus rule reads."""
        return tuple(r for r in self.final_round if r.instrument_key == instrument_key)


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


class PanelOutcome(DomainModel):
    """Everything one cycle's deliberation produced, for every instrument in the basket.

    The engine returns this rather than a decision at a time so that the per-cycle cost budget
    has a scope to live in, and so `basket` mode — one panel run answering for N instruments —
    is expressible without the caller knowing which mode ran.
    """

    decisions: tuple[Decision, ...] = ()
    deliberations: tuple[Deliberation, ...] = ()

    @property
    def responses(self) -> tuple[SeatResponse, ...]:
        return tuple(r for d in self.deliberations for r in d.responses)

    @property
    def cost_usd(self) -> Decimal:
        """What the cycle actually spent. De-duplicated across deliberations by provider call."""
        return total_cost(self.responses)

    @property
    def budget_truncated(self) -> bool:
        return any(d.budget_truncated for d in self.deliberations)
