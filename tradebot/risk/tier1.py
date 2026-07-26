"""Tier-1 risk engine: turn a `Decision` into an `OrderIntent`, or into a recorded refusal.

This is the gate DESIGN [L3] is about — no LLM output reaches a venue without passing through
here. The engine is deliberately boring: size, cap, quantize, veto. Every step records a
`RiskCheckResult`, so "why was this order this size" is answerable from the event log alone,
without re-running anything against state that has since changed.

Failure semantics: every path that is not a fully approved, venue-legal order produces an
outcome with `intent=None` and the reason recorded. The engine never raises for a business
reason and never returns a partially validated intent.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import Mode, OrderType, RiskDecision, Side
from tradebot.core.ids import client_order_id
from tradebot.core.money import ZERO, QuantizedOrder, multiply, percent_of, quantize_order
from tradebot.core.orders import OrderIntent, ProtectivePlan, RiskCheckResult
from tradebot.core.schema import DomainModel
from tradebot.interfaces.risk import RiskProposal, RiskRule
from tradebot.risk.rules import DEFAULT_TIER1_RULES
from tradebot.risk.sizing import base_quantity

QUANTIZATION_RULE = "venue_quantization"


class RiskOutcome(DomainModel):
    """The engine's verdict, with full provenance either way."""

    instrument_key: str
    intent: OrderIntent | None = None
    checks: tuple[RiskCheckResult, ...] = ()

    @property
    def approved(self) -> bool:
        return self.intent is not None

    @property
    def blocking_check(self) -> RiskCheckResult | None:
        return next((check for check in self.checks if check.blocked), None)

    @property
    def veto_reason(self) -> str:
        blocked = self.blocking_check
        return f"{blocked.rule}: {blocked.detail}" if blocked else ""


class Tier1RiskEngine:
    """Applies a basket's Tier-1 policy to one proposed trade."""

    def __init__(self, clock: Clock, rules: Sequence[RiskRule] = DEFAULT_TIER1_RULES) -> None:
        self._clock = clock
        self._rules = tuple(rules)

    def approve(
        self,
        proposal: RiskProposal,
        *,
        mode: Mode,
        basket_id: str,
        cycle_id: str,
        seq: int = 0,
        ttl_seconds: int | None = None,
    ) -> RiskOutcome:
        instrument = proposal.instrument
        qty, sizing_check = base_quantity(proposal)
        checks = [sizing_check]
        if sizing_check.blocked:
            return RiskOutcome(instrument_key=instrument.key, checks=tuple(checks))

        for rule in self._rules:
            result = rule.evaluate(proposal, qty)
            checks.append(result)
            if result.blocked:
                return RiskOutcome(instrument_key=instrument.key, checks=tuple(checks))
            qty = min(qty, result.max_qty) if result.max_qty is not None else qty

        side = proposal.decision.action.side
        quantized = quantize_order(qty, proposal.price, side, instrument.trading_rules)
        checks.append(_quantization_check(quantized, qty))
        if not quantized.approved:
            return RiskOutcome(instrument_key=instrument.key, checks=tuple(checks))

        intent = OrderIntent(
            client_order_id=client_order_id(
                mode=mode,
                basket_id=basket_id,
                cycle_id=cycle_id,
                instrument=instrument.key,
                seq=seq,
            ),
            basket_id=basket_id,
            cycle_id=cycle_id,
            instrument_key=instrument.key,
            side=side,
            qty=quantized.qty,
            order_type=OrderType.LIMIT,
            limit_price=quantized.price,
            protective=protective_plan(proposal, quantized.price),
            ttl_seconds=ttl_seconds,
            risk_checks=tuple(checks),
            created_at=self._clock.now(),
        )
        return RiskOutcome(instrument_key=instrument.key, intent=intent, checks=tuple(checks))


def protective_plan(proposal: RiskProposal, entry_price: Decimal) -> ProtectivePlan | None:
    """Where this entry's stop and target sit, in the same ATR units that sized it.

    Returned only for an opening trade on a venue that can hold the legs. A reducing SELL needs
    no protection — it *is* the exit — and an unprotected venue has already been charged the
    sizing haircut instead (DESIGN §6.6, §6.7).
    """
    if proposal.decision.action.side is not Side.BUY or proposal.unprotected:
        return None
    if proposal.atr <= ZERO:
        return None
    policy = proposal.policy
    stop = entry_price - multiply(policy.stop_loss_atr_multiple, proposal.atr)
    if stop <= ZERO:
        return None
    return ProtectivePlan(
        stop_price=stop,
        take_profit_price=entry_price + multiply(policy.take_profit_atr_multiple, proposal.atr),
        limit_offset_pct=policy.protective_limit_offset_pct,
    )


def _quantization_check(quantized: QuantizedOrder, requested: Decimal) -> RiskCheckResult:
    """Record what venue precision did to the size — including when it kills the order."""
    if quantized.veto is not None:
        return RiskCheckResult(
            rule=QUANTIZATION_RULE,
            decision=RiskDecision.VETO,
            detail=f"{quantized.veto} after quantizing {requested} to venue precision",
            observed=quantized.qty,
        )
    decision = RiskDecision.ADJUSTED if quantized.qty < requested else RiskDecision.PASS
    return RiskCheckResult(
        rule=QUANTIZATION_RULE,
        decision=decision,
        detail=f"quantized {requested} to {quantized.qty} at {quantized.price}",
        observed=quantized.qty,
        max_qty=quantized.qty,
    )


def basket_budget(equity: Decimal, allocation_pct: Decimal) -> Decimal:
    """The slice of portfolio equity a basket may deploy — the denominator of Tier-1 limits."""
    return percent_of(equity, allocation_pct) if equity > ZERO else ZERO
