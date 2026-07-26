"""Tier-1 risk: sizing arithmetic, rule caps, and the vetoes that must never be bypassed.

These are the tests that stand between a hallucination and an order, so every branch of every
money-affecting decision is covered — including the ones that fail closed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, Mode, RiskDecision, SizeHint
from tradebot.core.instrument import Instrument
from tradebot.core.portfolio import Position
from tradebot.interfaces.risk import RiskProposal
from tradebot.risk.rules import (
    LongOnlyRule,
    MaxBasketAllocationRule,
    MaxPositionSizeRule,
    MinConvictionRule,
)
from tradebot.risk.sizing import base_quantity
from tradebot.risk.tier1 import Tier1RiskEngine, basket_budget

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def proposal(
    instrument: Instrument,
    *,
    action: Action = Action.BUY,
    size: SizeHint = SizeHint.FULL,
    conviction: str = "0.8",
    held: str = "0",
    avg_entry: str = "50000",
    atr: str = "500",
    equity: str = "10000",
    exposure: str = "0",
    unprotected: bool = False,
    policy: RiskPolicy | None = None,
) -> RiskProposal:
    resolved = policy or RiskPolicy()
    return RiskProposal(
        decision=Decision(
            instrument_key=instrument.key,
            action=action,
            conviction=Decimal(conviction),
            size_hint=size,
        ),
        instrument=instrument,
        policy=resolved,
        position=Position(
            instrument_key=instrument.key, qty=Decimal(held), avg_entry=Decimal(avg_entry)
        ),
        price=Decimal("50000"),
        atr=Decimal(atr),
        equity=Decimal(equity),
        basket_budget=basket_budget(Decimal(equity), resolved.max_basket_allocation_pct),
        basket_exposure=Decimal(exposure),
        unprotected=unprotected,
    )


class TestBasketBudget:
    def test_budget_is_the_configured_share_of_equity(self) -> None:
        assert basket_budget(Decimal("10000"), Decimal("10")) == Decimal("1000")

    def test_no_equity_means_no_budget(self) -> None:
        assert basket_budget(Decimal("0"), Decimal("10")) == Decimal("0")


class TestSizing:
    def test_buy_size_is_volatility_normalized_in_asset_units(self, instrument: Instrument) -> None:
        """budget 1000 × 1% × full = 10 risked; stop = 2 × ATR(500) = 1000 → 0.01 units."""
        qty, check = base_quantity(proposal(instrument))
        assert qty == Decimal("0.01")
        assert check.decision is RiskDecision.PASS

    def test_size_hint_scales_the_risked_amount(self, instrument: Instrument) -> None:
        quarter, _ = base_quantity(proposal(instrument, size=SizeHint.QUARTER))
        full, _ = base_quantity(proposal(instrument, size=SizeHint.FULL))
        assert quarter == full / 4

    def test_higher_volatility_produces_a_smaller_position(self, instrument: Instrument) -> None:
        calm, _ = base_quantity(proposal(instrument, atr="500"))
        wild, _ = base_quantity(proposal(instrument, atr="2000"))
        assert wild < calm

    def test_units_are_asset_units_not_reciprocal_currency(self, instrument: Instrument) -> None:
        """A penny-priced asset must not size absurdly (REVIEW A2)."""
        cheap = instrument.model_copy(update={"symbol": "PENNY/USDT"})
        qty, _ = base_quantity(
            proposal(cheap, atr="0.01").model_copy(update={"price": Decimal("0.05")})
        )
        assert qty == Decimal("500")  # 10 risked / (2 × 0.01)

    def test_missing_volatility_is_a_veto_not_a_guess(self, instrument: Instrument) -> None:
        qty, check = base_quantity(proposal(instrument, atr="0"))
        assert check.decision is RiskDecision.VETO
        assert qty == 0

    def test_unprotected_position_takes_the_configured_haircut(
        self, instrument: Instrument
    ) -> None:
        guarded, _ = base_quantity(proposal(instrument))
        exposed, check = base_quantity(proposal(instrument, unprotected=True))
        assert exposed == guarded / 2
        assert "unprotected haircut" in check.detail

    def test_sell_sizes_from_the_holding_not_from_volatility(self, instrument: Instrument) -> None:
        qty, check = base_quantity(
            proposal(instrument, action=Action.SELL, size=SizeHint.HALF, held="0.4")
        )
        assert qty == Decimal("0.2")
        assert "reduce-only" in check.detail

    def test_sell_while_flat_is_vetoed_in_sizing_too(self, instrument: Instrument) -> None:
        _, check = base_quantity(proposal(instrument, action=Action.SELL, held="0"))
        assert check.decision is RiskDecision.VETO
        assert "long-only" in check.detail


class TestMinConvictionRule:
    def test_below_the_floor_vetoes(self, instrument: Instrument) -> None:
        result = MinConvictionRule().evaluate(proposal(instrument, conviction="0.4"), Decimal("1"))
        assert result.blocked
        assert result.limit == Decimal("0.6")

    def test_at_the_floor_passes(self, instrument: Instrument) -> None:
        result = MinConvictionRule().evaluate(proposal(instrument, conviction="0.6"), Decimal("1"))
        assert result.decision is RiskDecision.PASS


class TestLongOnlyRule:
    def test_sell_while_flat_is_vetoed(self, instrument: Instrument) -> None:
        """The cheapest-to-prevent catastrophic failure: an accidental equities short (R13)."""
        result = LongOnlyRule().evaluate(
            proposal(instrument, action=Action.SELL, held="0"), Decimal("1")
        )
        assert result.blocked
        assert "short" in result.detail

    def test_sell_is_capped_at_the_holding(self, instrument: Instrument) -> None:
        result = LongOnlyRule().evaluate(
            proposal(instrument, action=Action.SELL, held="0.3"), Decimal("0.5")
        )
        assert result.decision is RiskDecision.ADJUSTED
        assert result.max_qty == Decimal("0.3")

    def test_buys_are_unaffected(self, instrument: Instrument) -> None:
        result = LongOnlyRule().evaluate(proposal(instrument), Decimal("1"))
        assert result.decision is RiskDecision.PASS
        assert result.max_qty == Decimal("1")


class TestMaxPositionSizeRule:
    def test_caps_at_the_configured_share_of_the_basket_budget(
        self, instrument: Instrument
    ) -> None:
        """25% of a 1000 budget = 250 → 0.005 units at 50000."""
        result = MaxPositionSizeRule().evaluate(proposal(instrument), Decimal("1"))
        assert result.decision is RiskDecision.ADJUSTED
        assert result.max_qty == Decimal("0.005")

    def test_a_full_position_vetoes_further_buying(self, instrument: Instrument) -> None:
        result = MaxPositionSizeRule().evaluate(
            proposal(instrument, held="0.005"), Decimal("0.001")
        )
        assert result.blocked

    def test_headroom_smaller_than_the_request_adjusts(self, instrument: Instrument) -> None:
        result = MaxPositionSizeRule().evaluate(proposal(instrument, held="0.004"), Decimal("1"))
        assert result.decision is RiskDecision.ADJUSTED
        assert result.max_qty == Decimal("0.001")

    def test_does_not_constrain_a_sell(self, instrument: Instrument) -> None:
        result = MaxPositionSizeRule().evaluate(
            proposal(instrument, action=Action.SELL, held="1"), Decimal("0.5")
        )
        assert result.decision is RiskDecision.PASS


class TestMaxBasketAllocationRule:
    def test_exhausted_budget_vetoes(self, instrument: Instrument) -> None:
        result = MaxBasketAllocationRule().evaluate(
            proposal(instrument, exposure="1000"), Decimal("1")
        )
        assert result.blocked

    def test_remaining_headroom_caps_the_size(self, instrument: Instrument) -> None:
        result = MaxBasketAllocationRule().evaluate(
            proposal(instrument, exposure="900"), Decimal("1")
        )
        assert result.max_qty == Decimal("0.002")  # 100 headroom / 50000


class TestTier1Engine:
    def _engine(self, clock: ManualClock) -> Tier1RiskEngine:
        return Tier1RiskEngine(clock)

    def _approve(self, clock: ManualClock, prop: RiskProposal) -> object:
        return self._engine(clock).approve(prop, mode=Mode.SIM, basket_id="b1", cycle_id="c1")

    def test_approved_intent_carries_full_risk_provenance(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        outcome = self._engine(clock).approve(
            proposal(instrument), mode=Mode.SIM, basket_id="b1", cycle_id="c1"
        )
        assert outcome.approved
        assert outcome.intent is not None
        rules = {check.rule for check in outcome.intent.risk_checks}
        assert rules == {
            "sizing",
            "min_conviction",
            "long_only",
            "max_position_size",
            "max_basket_allocation",
            "venue_quantization",
        }

    def test_the_tightest_cap_wins_regardless_of_rule_order(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Sizing wants 0.01; max-position allows 0.005; basket headroom allows 0.002."""
        outcome = self._engine(clock).approve(
            proposal(instrument, exposure="900"), mode=Mode.SIM, basket_id="b1", cycle_id="c1"
        )
        assert outcome.intent is not None
        assert outcome.intent.qty == Decimal("0.002")

    def test_a_veto_produces_no_intent_and_a_reason(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        outcome = self._engine(clock).approve(
            proposal(instrument, conviction="0.1"), mode=Mode.SIM, basket_id="b1", cycle_id="c1"
        )
        assert not outcome.approved
        assert outcome.intent is None
        assert "min_conviction" in outcome.veto_reason

    def test_sub_minimum_size_is_vetoed_after_quantization(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Never bumped up to the venue minimum — that would oversize past the limit."""
        outcome = self._engine(clock).approve(
            proposal(instrument, equity="20"), mode=Mode.SIM, basket_id="b1", cycle_id="c1"
        )
        assert not outcome.approved
        assert "venue_quantization" in outcome.veto_reason

    def test_intents_are_idempotent_across_identical_calls(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        engine = self._engine(clock)
        first = engine.approve(proposal(instrument), mode=Mode.SIM, basket_id="b1", cycle_id="c1")
        second = engine.approve(proposal(instrument), mode=Mode.SIM, basket_id="b1", cycle_id="c1")
        assert first.intent is not None
        assert second.intent is not None
        assert first.intent.client_order_id == second.intent.client_order_id

    def test_quantized_price_is_passive_for_a_buy(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        outcome = self._engine(clock).approve(
            proposal(instrument).model_copy(update={"price": Decimal("50000.009")}),
            mode=Mode.SIM,
            basket_id="b1",
            cycle_id="c1",
        )
        assert outcome.intent is not None
        assert outcome.intent.limit_price == Decimal("50000.00")


class TestRiskPolicyValidation:
    @pytest.mark.parametrize(
        "field", ["max_basket_allocation_pct", "max_position_pct_of_basket", "risk_per_trade_pct"]
    )
    def test_percentages_must_be_within_range(self, field: str) -> None:
        with pytest.raises(ValueError, match="within"):
            RiskPolicy(**{field: Decimal("0")})  # type: ignore[arg-type]

    def test_conviction_floor_uses_the_0_to_1_scale(self) -> None:
        with pytest.raises(ValueError, match="0–1 scale"):
            RiskPolicy(min_conviction=Decimal("60"))

    def test_short_selling_cannot_be_enabled(self) -> None:
        """v1's rules model long exposure only; turning this off would silently invalidate them."""
        with pytest.raises(ValueError, match="long-only"):
            RiskPolicy(long_only=False)
