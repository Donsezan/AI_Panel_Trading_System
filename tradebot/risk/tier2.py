"""Tier-2: the global gate that sits between Tier-1 approval and the venue (DESIGN §6.6).

Tier 1 asks "is this a sensible trade for this basket?". Tier 2 asks "can the *portfolio* take
it?" — and it outranks every basket, because the failure it exists to prevent is two baskets
independently reaching a sensible conclusion about near-identical assets and jointly building a
position neither of them thinks it holds.

Enforcement is split, reflecting that venues cannot see each other:

* **Per-venue hard limits** are checked synchronously here, before any order is sent.
* **Cross-venue aggregate rules** (drawdown, daily loss) cannot block one venue's order using
  another venue's state, so they are enforced by the watchdog through basket pause or the kill
  switch instead — see `risk.watchdog`.

Tier 2 may veto or **shrink**: a shrink reduces quantity to the remaining headroom, and a shrink
that lands below an exchange minimum becomes a veto rather than a token order (DESIGN §6.6).

Failure semantics: every rule that cannot evaluate vetoes. A missing equity figure, an unknown
cluster or an absent last price are all "we do not know how exposed we are", and the answer to
that is never "send the order".
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import RiskDecision, Side
from tradebot.core.money import ZERO, QuantizedOrder, divide, multiply, percent_of, quantize_order
from tradebot.core.orders import OrderIntent, RiskCheckResult
from tradebot.core.schema import DomainModel
from tradebot.interfaces.risk import RiskProposal, RiskRule


def _headroom_rule(
    rule_id: str,
    *,
    limit_pct: Decimal,
    equity: Decimal,
    used: Decimal,
    price: Decimal,
    requested_qty: Decimal,
    detail: str,
) -> RiskCheckResult:
    """The shape every exposure limit shares: a ceiling, what is used, what remains.

    Written once because four rules differ only in which exposure they read; duplicating the
    arithmetic four times is how one of them ends up with the comparison inverted.
    """
    ceiling = percent_of(equity, limit_pct)
    headroom = ceiling - used
    if headroom <= ZERO:
        return RiskCheckResult(
            rule=rule_id,
            decision=RiskDecision.VETO,
            detail=f"{detail}: {used} is at the {ceiling} ceiling",
            limit=ceiling,
            observed=used,
        )
    max_qty = divide(headroom, price)
    return RiskCheckResult(
        rule=rule_id,
        decision=RiskDecision.ADJUSTED if max_qty < requested_qty else RiskDecision.PASS,
        detail=f"{detail}: headroom {headroom}",
        limit=ceiling,
        observed=used,
        max_qty=min(max_qty, requested_qty),
    )


class GrossExposureRule:
    """Caps everything deployed at once, across all baskets and instruments."""

    rule_id = "max_gross_exposure"

    def __init__(self, policy: GlobalRiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.decision.action.side is Side.SELL:
            return _pass(self.rule_id, requested_qty)
        return _headroom_rule(
            self.rule_id,
            limit_pct=self._policy.max_gross_exposure_pct,
            equity=proposal.equity,
            used=proposal.gross_exposure,
            price=proposal.price,
            requested_qty=requested_qty,
            detail="gross exposure",
        )


class InstrumentExposureRule:
    """Caps one instrument across *all* baskets — Tier-1 cannot see a sibling basket."""

    rule_id = "max_instrument_exposure"

    def __init__(self, policy: GlobalRiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.decision.action.side is Side.SELL:
            return _pass(self.rule_id, requested_qty)
        return _headroom_rule(
            self.rule_id,
            limit_pct=self._policy.max_instrument_exposure_pct,
            equity=proposal.equity,
            used=proposal.instrument_exposure,
            price=proposal.price,
            requested_qty=requested_qty,
            detail=f"{proposal.instrument.key} exposure",
        )


class ClusterExposureRule:
    """Caps a correlation bucket, so {BTC, ETH} cannot be maxed out twice as 'two ideas'."""

    rule_id = "max_cluster_exposure"

    def __init__(self, policy: GlobalRiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        if proposal.decision.action.side is Side.SELL:
            return _pass(self.rule_id, requested_qty)
        cluster = self._policy.cluster_for(proposal.instrument)
        if cluster is None:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail=f"{proposal.instrument.key} belongs to no correlation bucket, so its "
                "concentration cannot be bounded",
            )
        return _headroom_rule(
            self.rule_id,
            limit_pct=self._policy.max_cluster_exposure_pct,
            equity=proposal.equity,
            used=proposal.cluster_exposure,
            price=proposal.price,
            requested_qty=requested_qty,
            detail=f"cluster {cluster.cluster_id}",
        )


class PriceCollarRule:
    """Rejects an order priced far from the last trade — a fat finger or a stale book."""

    rule_id = "price_collar"

    def __init__(self, policy: GlobalRiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        last = proposal.last_price
        if last <= ZERO:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail="no last price to judge the order price against",
            )
        deviation = multiply(divide(abs(proposal.price - last), last), Decimal(100))
        collar = self._policy.price_collar_pct
        if deviation > collar:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail=f"order price {proposal.price} is {deviation}% from last {last}",
                limit=collar,
                observed=deviation,
            )
        return RiskCheckResult(
            rule=self.rule_id,
            decision=RiskDecision.PASS,
            limit=collar,
            observed=deviation,
            max_qty=requested_qty,
        )


class OrderRateRule:
    """Caps orders per rolling hour globally — the brake on a loop that will not stop trading."""

    rule_id = "max_orders_per_hour"

    def __init__(self, policy: GlobalRiskPolicy) -> None:
        self._policy = policy

    def evaluate(self, proposal: RiskProposal, requested_qty: Decimal) -> RiskCheckResult:
        placed = proposal.history.orders_last_hour
        cap = self._policy.max_orders_per_hour
        if placed >= cap:
            return RiskCheckResult(
                rule=self.rule_id,
                decision=RiskDecision.VETO,
                detail=f"{placed} orders in the trailing hour, cap {cap}",
                limit=Decimal(cap),
                observed=Decimal(placed),
            )
        return _pass(self.rule_id, requested_qty, f"{placed}/{cap} orders this hour")


def _pass(rule_id: str, qty: Decimal, detail: str = "") -> RiskCheckResult:
    return RiskCheckResult(rule=rule_id, decision=RiskDecision.PASS, detail=detail, max_qty=qty)


def default_tier2_rules(policy: GlobalRiskPolicy) -> tuple[RiskRule, ...]:
    return (
        PriceCollarRule(policy),
        OrderRateRule(policy),
        GrossExposureRule(policy),
        InstrumentExposureRule(policy),
        ClusterExposureRule(policy),
    )


class Tier2Verdict(DomainModel):
    """What the global gate did to an intent Tier-1 already approved."""

    intent: OrderIntent | None = None
    checks: tuple[RiskCheckResult, ...] = ()

    @property
    def approved(self) -> bool:
        return self.intent is not None

    @property
    def veto_reason(self) -> str:
        blocked = next((check for check in self.checks if check.blocked), None)
        return f"{blocked.rule}: {blocked.detail}" if blocked else ""


class Tier2RiskEngine:
    """Per-venue hard limits, evaluated synchronously for every intent."""

    def __init__(self, policy: GlobalRiskPolicy, rules: Sequence[RiskRule] | None = None) -> None:
        self._policy = policy
        self._rules = tuple(rules if rules is not None else default_tier2_rules(policy))

    def review(self, intent: OrderIntent, proposal: RiskProposal) -> Tier2Verdict:
        """Veto, shrink to fit, or pass the intent through unchanged."""
        qty = intent.qty
        checks: list[RiskCheckResult] = []
        for rule in self._rules:
            result = rule.evaluate(proposal, qty)
            checks.append(result)
            if result.blocked:
                return Tier2Verdict(checks=tuple(checks))
            qty = min(qty, result.max_qty) if result.max_qty is not None else qty

        if qty >= intent.qty:
            return Tier2Verdict(intent=intent, checks=tuple(checks))

        # A shrink is re-quantized: the reduced size must still be a legal order at the venue,
        # and one that has fallen below a minimum is a veto, never a token order.
        shrunk = quantize_order(
            qty,
            intent.limit_price or proposal.price,
            intent.side,
            proposal.instrument.trading_rules,
        )
        checks.append(_shrink_check(shrunk, intent.qty))
        if not shrunk.approved:
            return Tier2Verdict(checks=tuple(checks))
        return Tier2Verdict(
            intent=intent.model_copy(
                update={"qty": shrunk.qty, "risk_checks": (*intent.risk_checks, *checks)}
            ),
            checks=tuple(checks),
        )


def _shrink_check(shrunk: QuantizedOrder, requested: Decimal) -> RiskCheckResult:
    if shrunk.veto is not None:
        return RiskCheckResult(
            rule="tier2_shrink",
            decision=RiskDecision.VETO,
            detail=f"shrinking {requested} to fit portfolio headroom left {shrunk.veto}",
            observed=shrunk.qty,
        )
    return RiskCheckResult(
        rule="tier2_shrink",
        decision=RiskDecision.ADJUSTED,
        detail=f"shrunk {requested} to {shrunk.qty} to fit portfolio headroom",
        observed=shrunk.qty,
        max_qty=shrunk.qty,
    )
