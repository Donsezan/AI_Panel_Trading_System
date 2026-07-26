"""Deterministic position sizing. The panel never sizes; this does (DESIGN §6.6).

Buying is volatility-normalized, with ATR **absolute** — quote currency per unit:

    risk_amount   = basket_budget × risk_per_trade × size_hint_fraction
    stop_distance = stop_multiple × ATR
    qty           = risk_amount / stop_distance

The units are the point. Dividing additionally by price, as an earlier draft of the design did,
yields `1/currency` rather than asset units — negligible for BTC, absurd for a penny-priced
asset (REVIEW A2).

`risk_amount` is only a truthful "amount at risk" while a stop actually sits at
`stop_distance`. Where the venue cannot hold one, the position is unguarded between cycles and
sizing takes the configured haircut (DESIGN §6.7, R12).

Selling is different in kind, not degree: v1 is long-only, so a SELL closes part of what is
held. The size hint is applied to the existing position, and the result can never exceed it.

Failure semantics: a missing or non-positive ATR is a **veto**, never a fallback size. Without
a volatility estimate there is no defensible position size, and guessing one is how a risk
engine becomes decoration.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from tradebot.core.enums import RiskDecision, Side
from tradebot.core.money import ZERO, divide, multiply, percent_of, to_decimal
from tradebot.core.orders import RiskCheckResult
from tradebot.interfaces.risk import RiskProposal

SIZING_RULE = "sizing"


def _pass(detail: str, qty: Decimal) -> RiskCheckResult:
    return RiskCheckResult(
        rule=SIZING_RULE, decision=RiskDecision.PASS, detail=detail, observed=qty
    )


def _veto(detail: str) -> RiskCheckResult:
    return RiskCheckResult(rule=SIZING_RULE, decision=RiskDecision.VETO, detail=detail)


def _size_buy(proposal: RiskProposal) -> tuple[Decimal, RiskCheckResult]:
    if proposal.atr <= ZERO:
        return ZERO, _veto("ATR is not positive; no defensible size without a volatility estimate")

    hint_fraction = to_decimal(proposal.decision.size_hint.fraction)
    risk_amount = multiply(
        percent_of(proposal.basket_budget, proposal.policy.risk_per_trade_pct), hint_fraction
    )
    if proposal.unprotected:
        risk_amount = multiply(
            risk_amount,
            divide(Decimal(100) - proposal.policy.unprotected_haircut_pct, Decimal(100)),
        )

    stop_distance = multiply(proposal.policy.stop_loss_atr_multiple, proposal.atr)
    qty = divide(risk_amount, stop_distance)
    detail = (
        f"risk_amount={risk_amount} stop_distance={stop_distance}"
        f"{' (unprotected haircut applied)' if proposal.unprotected else ''}"
    )
    return qty, _pass(detail, qty)


def _size_sell(proposal: RiskProposal) -> tuple[Decimal, RiskCheckResult]:
    """Reduce-only: a fraction of what is held, never more."""
    if proposal.position.is_flat:
        return ZERO, _veto("SELL while flat would open a short; v1 is long-only")
    hint_fraction = to_decimal(proposal.decision.size_hint.fraction)
    qty = multiply(proposal.position.qty, hint_fraction)
    return qty, _pass(f"reduce-only: {hint_fraction} of {proposal.position.qty} held", qty)


_SIZERS: dict[Side, Callable[[RiskProposal], tuple[Decimal, RiskCheckResult]]] = {
    Side.BUY: _size_buy,
    Side.SELL: _size_sell,
}


def base_quantity(proposal: RiskProposal) -> tuple[Decimal, RiskCheckResult]:
    """The size the policy asks for, before any rule caps it."""
    return _SIZERS[proposal.decision.action.side](proposal)
