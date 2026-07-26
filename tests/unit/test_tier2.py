"""Tier-2: the global gate that outranks every basket.

The failure Tier 2 exists to prevent is two baskets independently reaching a sensible conclusion
about near-identical assets and jointly building a position neither of them thinks it holds. So
every rule here is tested at its boundary, and every rule that cannot evaluate is tested to veto.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.config import CorrelationCluster, GlobalRiskPolicy, RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, AssetClass, OrderType, RiskDecision, Side, SizeHint
from tradebot.core.instrument import Instrument
from tradebot.core.orders import OrderIntent
from tradebot.core.portfolio import Position
from tradebot.interfaces.risk import RiskProposal, TradingHistory
from tradebot.risk.tier2 import (
    ClusterExposureRule,
    GrossExposureRule,
    InstrumentExposureRule,
    OrderRateRule,
    PriceCollarRule,
    Tier2RiskEngine,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def proposal(
    instrument: Instrument,
    *,
    action: Action = Action.BUY,
    price: str = "50000",
    last: str = "50000",
    equity: str = "10000",
    gross: str = "0",
    per_instrument: str = "0",
    cluster: str = "0",
    orders_last_hour: int = 0,
    held: str = "0",
) -> RiskProposal:
    return RiskProposal(
        decision=Decision(
            instrument_key=instrument.key,
            action=action,
            conviction=Decimal("0.8"),
            size_hint=SizeHint.HALF,
        ),
        instrument=instrument,
        policy=RiskPolicy(),
        position=Position(instrument_key=instrument.key, qty=Decimal(held)),
        price=Decimal(price),
        last_price=Decimal(last),
        atr=Decimal("500"),
        equity=Decimal(equity),
        basket_budget=Decimal(equity) / 10,
        basket_exposure=Decimal(0),
        gross_exposure=Decimal(gross),
        instrument_exposure=Decimal(per_instrument),
        cluster_exposure=Decimal(cluster),
        history=TradingHistory(orders_last_hour=orders_last_hour),
    )


def intent(instrument: Instrument, qty: str = "0.02") -> OrderIntent:
    return OrderIntent(
        client_order_id="sim-ABCDEF",
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50000"),
        created_at=NOW,
    )


class TestExposureRules:
    def test_gross_exposure_caps_what_is_deployed_at_once(self, instrument: Instrument) -> None:
        rule = GrossExposureRule(GlobalRiskPolicy(max_gross_exposure_pct=Decimal(80)))

        result = rule.evaluate(proposal(instrument, gross="7900"), Decimal("1"))

        assert result.decision is RiskDecision.ADJUSTED
        assert result.max_qty == Decimal("100") / Decimal("50000")

    def test_a_portfolio_already_at_its_ceiling_is_vetoed(self, instrument: Instrument) -> None:
        rule = GrossExposureRule(GlobalRiskPolicy())

        result = rule.evaluate(proposal(instrument, gross="8000"), Decimal("1"))

        assert result.blocked

    def test_one_instrument_is_capped_across_every_basket(self, instrument: Instrument) -> None:
        """Tier-1's per-basket cap cannot see a sibling basket holding the same thing."""
        rule = InstrumentExposureRule(GlobalRiskPolicy(max_instrument_exposure_pct=Decimal(20)))

        result = rule.evaluate(proposal(instrument, per_instrument="2000"), Decimal("1"))

        assert result.blocked

    def test_a_correlation_bucket_is_capped(self, instrument: Instrument) -> None:
        rule = ClusterExposureRule(GlobalRiskPolicy(max_cluster_exposure_pct=Decimal(40)))

        result = rule.evaluate(proposal(instrument, cluster="4000"), Decimal("1"))

        assert result.blocked

    def test_an_instrument_in_no_bucket_is_vetoed_not_waved_through(
        self, instrument: Instrument
    ) -> None:
        """An unbounded concentration is exactly what the rule exists to stop."""
        rule = ClusterExposureRule(
            GlobalRiskPolicy(
                clusters=(
                    CorrelationCluster(cluster_id="equities", asset_classes=(AssetClass.EQUITY,)),
                )
            )
        )

        result = rule.evaluate(proposal(instrument), Decimal("1"))

        assert result.blocked
        assert "no correlation bucket" in result.detail

    def test_a_reducing_sell_is_never_blocked_by_an_exposure_cap(
        self, instrument: Instrument
    ) -> None:
        """Being over a limit must not prevent getting back under it."""
        for rule in (
            GrossExposureRule(GlobalRiskPolicy()),
            InstrumentExposureRule(GlobalRiskPolicy()),
            ClusterExposureRule(GlobalRiskPolicy()),
        ):
            result = rule.evaluate(
                proposal(instrument, action=Action.SELL, gross="99999", held="1"), Decimal("1")
            )
            assert not result.blocked


class TestOrderSanity:
    def test_a_price_far_from_the_last_trade_is_rejected(self, instrument: Instrument) -> None:
        rule = PriceCollarRule(GlobalRiskPolicy(price_collar_pct=Decimal(5)))

        result = rule.evaluate(proposal(instrument, price="60000", last="50000"), Decimal("1"))

        assert result.blocked
        assert result.observed == Decimal(20)

    def test_a_price_inside_the_collar_passes(self, instrument: Instrument) -> None:
        rule = PriceCollarRule(GlobalRiskPolicy(price_collar_pct=Decimal(5)))

        result = rule.evaluate(proposal(instrument, price="51000", last="50000"), Decimal("1"))

        assert result.decision is RiskDecision.PASS

    def test_no_last_price_is_a_veto_not_a_pass(self, instrument: Instrument) -> None:
        """Fail closed: not knowing the market is not the same as the price being fine."""
        rule = PriceCollarRule(GlobalRiskPolicy())

        result = rule.evaluate(proposal(instrument, last="0"), Decimal("1"))

        assert result.blocked

    def test_the_hourly_order_budget_is_enforced(self, instrument: Instrument) -> None:
        rule = OrderRateRule(GlobalRiskPolicy(max_orders_per_hour=20))

        assert rule.evaluate(proposal(instrument, orders_last_hour=19), Decimal("1")).decision is (
            RiskDecision.PASS
        )
        assert rule.evaluate(proposal(instrument, orders_last_hour=20), Decimal("1")).blocked


class TestEngine:
    def test_an_intent_inside_every_limit_passes_through_unchanged(
        self, instrument: Instrument
    ) -> None:
        engine = Tier2RiskEngine(GlobalRiskPolicy())
        original = intent(instrument)

        verdict = engine.review(original, proposal(instrument))

        assert verdict.approved
        assert verdict.intent is not None
        assert verdict.intent.qty == original.qty

    def test_an_intent_over_headroom_is_shrunk_to_fit(self, instrument: Instrument) -> None:
        engine = Tier2RiskEngine(GlobalRiskPolicy(max_gross_exposure_pct=Decimal(80)))

        verdict = engine.review(intent(instrument, "1"), proposal(instrument, gross="7500"))

        assert verdict.approved
        assert verdict.intent is not None
        assert verdict.intent.qty < Decimal("1")
        assert verdict.intent.qty * Decimal("50000") <= Decimal("500")

    def test_a_shrink_below_a_venue_minimum_becomes_a_veto(self, instrument: Instrument) -> None:
        """A token order is not a smaller version of the trade; it is a fee (DESIGN §6.6)."""
        engine = Tier2RiskEngine(GlobalRiskPolicy(max_gross_exposure_pct=Decimal(80)))

        # 5 USDT of headroom quantizes to a legal lot but a notional below the venue's 10.
        verdict = engine.review(intent(instrument, "1"), proposal(instrument, gross="7995"))

        assert not verdict.approved
        assert "below_min_notional" in verdict.veto_reason

    def test_a_shrink_records_its_provenance_on_the_intent(self, instrument: Instrument) -> None:
        engine = Tier2RiskEngine(GlobalRiskPolicy())

        verdict = engine.review(intent(instrument, "1"), proposal(instrument, gross="7500"))

        assert verdict.intent is not None
        rules = {check.rule for check in verdict.intent.risk_checks}
        assert "tier2_shrink" in rules

    def test_a_veto_names_the_rule_that_produced_it(self, instrument: Instrument) -> None:
        engine = Tier2RiskEngine(GlobalRiskPolicy())

        verdict = engine.review(intent(instrument), proposal(instrument, price="99999"))

        assert not verdict.approved
        assert verdict.veto_reason.startswith("price_collar")

    def test_the_tightest_rule_wins_regardless_of_order(self, instrument: Instrument) -> None:
        engine = Tier2RiskEngine(GlobalRiskPolicy())
        tight = proposal(instrument, gross="7000", per_instrument="1900", cluster="3000")

        verdict = engine.review(intent(instrument, "1"), tight)

        assert verdict.intent is not None
        assert verdict.intent.qty * Decimal("50000") <= Decimal("100")


@pytest.mark.parametrize(
    "field",
    [
        "max_gross_exposure_pct",
        "max_instrument_exposure_pct",
        "max_cluster_exposure_pct",
        "max_daily_loss_pct",
        "max_drawdown_pct",
    ],
)
def test_every_percentage_limit_rejects_nonsense(field: str) -> None:
    with pytest.raises(ValueError, match="within"):
        GlobalRiskPolicy(**{field: Decimal(0)})


def test_the_kill_switch_does_not_liquidate_by_default() -> None:
    """Flattening into a broken market is often worse, and it is the operator's call."""
    assert GlobalRiskPolicy().flatten_on_kill is False
