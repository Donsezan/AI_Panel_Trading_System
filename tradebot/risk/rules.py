"""Tier-1 rules. Each is a pure function of a `RiskProposal`, and each is exhaustively tested.

Rules return **caps**, not mutations: the engine composes them with `min()`, so no ordering of
rules can widen a limit an earlier rule imposed. A rule that cannot evaluate returns a veto —
absence of evidence is not evidence of safety.

Every limit in the DESIGN §6.6 Tier-1 table is enforced here by a named rule, and nothing is
configurable that is not enforced. The metering rules (cooldown, daily cap, consecutive losses)
read history derived from the event log rather than from memory, so a restart cannot reset a
limit that exists precisely to survive one.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.enums import RiskDecision, Side
from tradebot.core.money import ZERO, divide, percent_of
from tradebot.core.orders import RiskCheckResult
from tradebot.interfaces.risk import RiskProposal, RiskRule


def _allow(rule_id: str, qty: Decimal, detail: str = "") -> RiskCheckResult:
    """A rule that does not bind still records a verdict — silence is not provenance."""
    return RiskCheckResult(rule=rule_id, decision=RiskDecision.PASS, detail=detail, max_qty=qty)


def _block(rule_id: str, detail: str, *, limit: Decimal | None = None) -> RiskCheckResult:
    return RiskCheckResult(rule=rule_id, decision=RiskDecision.VETO, detail=detail, limit=limit)


#: What a metering rule records when it declines to meter a human's exit. A `PASS` with a stated
#: reason, not silence: the event log has to show which rules stood aside and why, which is what
#: makes this an auditable decision by the risk layer rather than a bypass around it (ADR 0015).
STOOD_ASIDE = "stood aside: an operator exit reduces exposure and is not metered"


def _stand_aside(rule_id: str, qty: Decimal) -> RiskCheckResult:
    return _allow(rule_id, qty, STOOD_ASIDE)


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


class CooldownRule:
    """No re-entry into an instrument for `cooldown_cycles` after it last traded.

    A panel handed the same chart every ten minutes will keep reaching the same conclusion; the
    cooldown is what stops one thesis from being expressed as six orders (DESIGN §6.6).
    """

    rule_id = "cooldown"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.is_operator_exit:
            return _stand_aside(self.rule_id, requested_qty)
        elapsed = proposal.history.cycles_since_trade
        required = proposal.policy.cooldown_cycles
        if elapsed is None or elapsed >= required:
            return _allow(self.rule_id, requested_qty)
        return _block(
            self.rule_id,
            f"traded {elapsed} cycles ago; cooldown is {required}",
            limit=Decimal(required),
        )


class DailyTradeCapRule:
    """Caps orders per basket per day — the brake on a decision loop that will not settle."""

    rule_id = "max_trades_per_day"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.is_operator_exit:
            return _stand_aside(self.rule_id, requested_qty)
        placed = proposal.history.trades_today
        cap = proposal.policy.max_trades_per_day
        if placed >= cap:
            return _block(
                self.rule_id, f"{placed} orders already placed today, cap {cap}", limit=Decimal(cap)
            )
        return _allow(self.rule_id, requested_qty, f"{placed}/{cap} orders used today")


class ConsecutiveLossRule:
    """Vetoes once losing round trips pile up, so the basket can be auto-paused for review.

    A run of losses is the signal that the thesis, the data, or the model has stopped working;
    continuing to size normally through it is how a bad week becomes a bad account.
    """

    rule_id = "max_consecutive_losses"

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        # The starkest case for standing aside: a loss streak is exactly when an operator most
        # needs to be able to flatten, and this is the rule that would otherwise stop them.
        if proposal.is_operator_exit:
            return _stand_aside(self.rule_id, requested_qty)
        losses = proposal.history.consecutive_losses
        cap = proposal.policy.max_consecutive_losses
        if losses >= cap:
            return _block(
                self.rule_id,
                f"{losses} consecutive losing round trips, limit {cap}; basket auto-paused",
                limit=Decimal(cap),
            )
        return _allow(self.rule_id, requested_qty, f"{losses}/{cap} consecutive losses")


#: Order is irrelevant to the result — caps compose with `min()` — but it fixes the order in
#: which vetoes are reported, so the first recorded reason is the most fundamental one.
DEFAULT_TIER1_RULES: tuple[RiskRule, ...] = (
    MinConvictionRule(),
    LongOnlyRule(),
    ConsecutiveLossRule(),
    CooldownRule(),
    DailyTradeCapRule(),
    MaxPositionSizeRule(),
    MaxBasketAllocationRule(),
)

#: The rule whose veto means "stop this basket", not merely "not this trade" (DESIGN §6.6).
AUTO_PAUSE_RULE = ConsecutiveLossRule.rule_id
