"""Tier-1 rules. Each is a pure function of a `RiskProposal`, and each is exhaustively tested.

Rules return **caps**, not mutations: the engine composes them with `min()`, so no ordering of
rules can widen a limit an earlier rule imposed. A rule that cannot evaluate returns a veto —
absence of evidence is not evidence of safety.

Phase 1 ships the rules the walking skeleton genuinely enforces. Phase 2d adds cooldowns,
per-day trade caps, consecutive-loss auto-pause and protective-order policy behind this same
interface. Nothing is configurable that is not enforced.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.enums import RiskDecision, Side
from tradebot.core.money import ZERO, divide, percent_of
from tradebot.core.orders import RiskCheckResult
from tradebot.interfaces.risk import RiskProposal


class MinConvictionRule:
    """No order below the panel conviction floor, on the 0–1 scale (DESIGN §6.6)."""

    rule_id = "min_conviction"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        conviction = proposal.decision.conviction
        floor = proposal.policy.min_conviction
        if conviction < floor:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail=f"conviction {conviction} below floor {floor}",
                limit=floor,
                observed=conviction,
            )
        return RiskCheckResult(
            rule=self.rule_id,
            decision=RiskDecision.PASS,
            limit=floor,
            observed=conviction,
            max_qty=requested_qty,
        )


class LongOnlyRule:
    """SELL is reduce-only; SELL while flat is vetoed.

    The cheapest-to-prevent catastrophic failure in the system: spot crypto venues reject a
    naked sell, but an equities broker will happily open a margin short with borrow costs and
    unlimited-loss semantics that none of these rules model (REVIEW A6, R13).
    """

    rule_id = "long_only"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.decision.action.side is Side.BUY:
            return RiskCheckResult(
                rule=self.rule_id, decision=RiskDecision.PASS, max_qty=requested_qty
            )

        held = proposal.position.qty
        if held <= ZERO:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail="SELL while flat would open a short position",
                observed=held,
            )
        if requested_qty > held:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.ADJUSTED,
                detail=f"reduce-only: capped {requested_qty} to holding {held}",
                limit=held,
                observed=requested_qty,
                max_qty=held,
            )
        return RiskCheckResult(
            rule=self.rule_id, decision=RiskDecision.PASS, limit=held, max_qty=requested_qty
        )


class MaxPositionSizeRule:
    """Caps one instrument's share of the basket budget (DESIGN §6.6, default 25%)."""

    rule_id = "max_position_size"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.decision.action.side is Side.SELL:
            return RiskCheckResult(
                rule=self.rule_id, decision=RiskDecision.PASS, max_qty=requested_qty
            )

        ceiling = percent_of(proposal.basket_budget, proposal.policy.max_position_pct_of_basket)
        held_value = proposal.position.market_value(proposal.price)
        headroom = ceiling - held_value
        if headroom <= ZERO:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail=f"position value {held_value} already at the {ceiling} cap",
                limit=ceiling,
                observed=held_value,
            )

        max_qty = divide(headroom, proposal.price)
        decision = RiskDecision.ADJUSTED if max_qty < requested_qty else RiskDecision.PASS
        return RiskCheckResult(
            rule=self.rule_id,
            decision=decision,
            detail=f"headroom {headroom} at price {proposal.price}",
            limit=ceiling,
            observed=held_value,
            max_qty=min(max_qty, requested_qty),
        )


class MaxBasketAllocationRule:
    """Caps the whole basket's deployed value at its budget (DESIGN §6.6)."""

    rule_id = "max_basket_allocation"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.decision.action.side is Side.SELL:
            return RiskCheckResult(
                rule=self.rule_id, decision=RiskDecision.PASS, max_qty=requested_qty
            )

        headroom = proposal.basket_budget - proposal.basket_exposure
        if headroom <= ZERO:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail=f"basket exposure {proposal.basket_exposure} at budget "
                f"{proposal.basket_budget}",
                limit=proposal.basket_budget,
                observed=proposal.basket_exposure,
            )

        max_qty = divide(headroom, proposal.price)
        decision = RiskDecision.ADJUSTED if max_qty < requested_qty else RiskDecision.PASS
        return RiskCheckResult(
            rule=self.rule_id,
            decision=decision,
            detail=f"basket headroom {headroom}",
            limit=proposal.basket_budget,
            observed=proposal.basket_exposure,
            max_qty=min(max_qty, requested_qty),
        )


#: Order is irrelevant to the result — caps compose with `min()` — but it fixes the order in
#: which vetoes are reported, so the first recorded reason is the most fundamental one.
DEFAULT_TIER1_RULES: tuple[
    MinConvictionRule | LongOnlyRule | MaxPositionSizeRule | MaxBasketAllocationRule, ...
] = (
    MinConvictionRule(),
    LongOnlyRule(),
    MaxPositionSizeRule(),
    MaxBasketAllocationRule(),
)
