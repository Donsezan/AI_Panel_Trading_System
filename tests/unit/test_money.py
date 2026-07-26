"""Money layer: the arithmetic that everything else trusts.

Property tests state the invariants directly, because these are the guarantees the risk layer
relies on when it claims an order cannot exceed a limit.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tradebot.core.enums import Side
from tradebot.core.errors import MoneyError
from tradebot.core.money import (
    SizingVeto,
    TradingRules,
    check_minimums,
    divide,
    from_measurement,
    notional,
    percent_of,
    quantize_order,
    quantize_price,
    quantize_quantity,
    round_to_step,
    to_decimal,
)

# Realistic trading magnitudes: sub-cent altcoins through six-figure BTC.
amounts = st.decimals(min_value=Decimal("0"), max_value=Decimal("1e9"), places=8)
positive_amounts = st.decimals(min_value=Decimal("0.00000001"), max_value=Decimal("1e9"), places=8)
steps = st.sampled_from(
    [Decimal(s) for s in ("0.00000001", "0.0001", "0.001", "0.01", "0.05", "0.1", "1", "5", "100")]
)


def rules(**overrides: Decimal) -> TradingRules:
    defaults = {
        "lot_size": Decimal("0.001"),
        "tick_size": Decimal("0.01"),
        "min_qty": Decimal("0.001"),
        "min_notional": Decimal("10"),
    }
    return TradingRules(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestToDecimal:
    def test_rejects_float_outright(self) -> None:
        with pytest.raises(MoneyError, match="float is not accepted"):
            to_decimal(0.1)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["0.1", 5, Decimal("2.5")])
    def test_accepts_exact_representations(self, value: str | int | Decimal) -> None:
        assert to_decimal(value) == Decimal(str(value))

    def test_rejects_garbage(self) -> None:
        with pytest.raises(MoneyError, match="not a valid decimal"):
            to_decimal("not a number")

    def test_string_path_avoids_binary_rounding_error(self) -> None:
        assert to_decimal("0.1") + to_decimal("0.2") == to_decimal("0.3")


class TestFromMeasurement:
    def test_round_trips_a_float_exactly(self) -> None:
        assert from_measurement(0.1) == Decimal("0.1")

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite(self, value: float) -> None:
        with pytest.raises(MoneyError):
            from_measurement(value)

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(MoneyError, match="not a number"):
            from_measurement("1.5")  # type: ignore[arg-type]


class TestArithmetic:
    def test_divide_by_zero_raises_rather_than_producing_infinity(self) -> None:
        with pytest.raises(MoneyError, match="division by zero"):
            divide(Decimal("1"), Decimal("0"))

    def test_percent_of_uses_a_0_to_100_scale(self) -> None:
        assert percent_of(Decimal("200"), Decimal("2.5")) == Decimal("5")

    def test_notional_is_qty_times_price(self) -> None:
        assert notional(Decimal("0.5"), Decimal("100")) == Decimal("50")


class TestRoundToStep:
    def test_rejects_non_positive_step(self) -> None:
        with pytest.raises(MoneyError, match="step must be positive"):
            round_to_step(Decimal("1"), Decimal("0"), "ROUND_DOWN")

    @pytest.mark.parametrize(
        ("value", "step", "expected"),
        [
            ("7", "5", "5"),  # non-power-of-ten lot sizes exist
            ("2.677", "0.05", "2.65"),
            ("0.00000019", "0.00000001", "0.00000019"),
        ],
    )
    def test_floors_to_arbitrary_steps(self, value: str, step: str, expected: str) -> None:
        assert round_to_step(Decimal(value), Decimal(step), "ROUND_DOWN") == Decimal(expected)


class TestQuantizeQuantity:
    def test_rounds_down_never_up(self) -> None:
        assert quantize_quantity(Decimal("1.9999"), Decimal("0.001")) == Decimal("1.999")

    def test_rejects_negative_quantity(self) -> None:
        with pytest.raises(MoneyError, match="must not be negative"):
            quantize_quantity(Decimal("-1"), Decimal("0.001"))

    @given(qty=amounts, lot=steps)
    def test_never_increases_quantity(self, qty: Decimal, lot: Decimal) -> None:
        assert quantize_quantity(qty, lot) <= qty

    @given(qty=amounts, lot=steps)
    def test_is_idempotent(self, qty: Decimal, lot: Decimal) -> None:
        once = quantize_quantity(qty, lot)
        assert quantize_quantity(once, lot) == once

    @given(qty=amounts, lot=steps)
    def test_result_is_a_whole_number_of_lots(self, qty: Decimal, lot: Decimal) -> None:
        assert divide(quantize_quantity(qty, lot), lot) % 1 == 0


class TestQuantizePrice:
    def test_buy_price_rounds_down_to_stay_passive(self) -> None:
        assert quantize_price(Decimal("100.008"), Decimal("0.01"), Side.BUY) == Decimal("100.00")

    def test_sell_price_rounds_up_to_stay_passive(self) -> None:
        assert quantize_price(Decimal("100.002"), Decimal("0.01"), Side.SELL) == Decimal("100.01")

    def test_rejects_non_positive_price(self) -> None:
        with pytest.raises(MoneyError, match="price must be positive"):
            quantize_price(Decimal("0"), Decimal("0.01"), Side.BUY)

    @given(price=positive_amounts, tick=steps)
    def test_quantization_is_never_more_aggressive_than_intended(
        self, price: Decimal, tick: Decimal
    ) -> None:
        assert quantize_price(price, tick, Side.SELL) >= price
        buy = quantize_price(price, tick, Side.BUY)
        assume(buy > 0)
        assert buy <= price

    @given(price=positive_amounts, tick=steps, side=st.sampled_from(list(Side)))
    def test_is_idempotent(self, price: Decimal, tick: Decimal, side: Side) -> None:
        once = quantize_price(price, tick, side)
        assume(once > 0)
        assert quantize_price(once, tick, side) == once


class TestMinimums:
    @pytest.mark.parametrize(
        ("qty", "value", "expected"),
        [
            ("0", "0", SizingVeto.NON_POSITIVE_QTY),
            ("0.0005", "50", SizingVeto.BELOW_MIN_QTY),
            ("0.002", "5", SizingVeto.BELOW_MIN_NOTIONAL),
            ("0.002", "50", None),
        ],
    )
    def test_classifies_every_minimum(
        self, qty: str, value: str, expected: SizingVeto | None
    ) -> None:
        assert check_minimums(Decimal(qty), Decimal(value), rules()) is expected


class TestQuantizeOrder:
    def test_shrinks_to_venue_precision_and_approves(self) -> None:
        result = quantize_order(Decimal("0.123456"), Decimal("50000.007"), Side.BUY, rules())
        assert result.approved
        assert result.qty == Decimal("0.123")
        assert result.price == Decimal("50000.00")
        assert result.notional == Decimal("6150.00000")

    def test_below_minimum_is_a_veto_never_a_bump_up(self) -> None:
        """Bumping to the minimum would silently oversize past the risk limit that sized it."""
        result = quantize_order(
            Decimal("0.0015"), Decimal("50000"), Side.BUY, rules(min_qty=Decimal("0.002"))
        )
        assert not result.approved
        assert result.veto is SizingVeto.BELOW_MIN_QTY
        assert result.qty == Decimal("0.001")

    def test_size_smaller_than_one_lot_is_vetoed_not_rounded_up(self) -> None:
        result = quantize_order(Decimal("0.0001"), Decimal("50000"), Side.BUY, rules())
        assert result.veto is SizingVeto.NON_POSITIVE_QTY
        assert result.qty == 0

    def test_quantization_can_push_a_marginal_order_below_min_notional(self) -> None:
        """Pre-quantization the order cleared min_notional; rounding down took it under."""
        intended = quantize_order(
            Decimal("1.0009"), Decimal("9.999"), Side.BUY, rules(lot_size=Decimal("1"))
        )
        assert notional(Decimal("1.0009"), Decimal("9.999")) > Decimal("10")
        assert intended.veto is SizingVeto.BELOW_MIN_NOTIONAL

    @given(qty=positive_amounts, price=positive_amounts, lot=steps, tick=steps)
    @settings(max_examples=200)
    def test_buy_notional_never_exceeds_the_intended_notional(
        self, qty: Decimal, price: Decimal, lot: Decimal, tick: Decimal
    ) -> None:
        """The invariant the risk layer depends on: quantization cannot overspend."""
        result = quantize_order(
            qty, price, Side.BUY, rules(lot_size=lot, tick_size=tick, min_notional=Decimal("0"))
        )
        assert result.notional <= notional(qty, price)
        assert result.qty <= qty


class TestTradingRules:
    @pytest.mark.parametrize("field", ["lot_size", "tick_size"])
    def test_rejects_non_positive_steps(self, field: str) -> None:
        with pytest.raises(MoneyError, match="must be positive"):
            rules(**{field: Decimal("0")})

    @pytest.mark.parametrize("field", ["min_qty", "min_notional"])
    def test_rejects_negative_minimums(self, field: str) -> None:
        with pytest.raises(MoneyError, match="must not be negative"):
            rules(**{field: Decimal("-1")})
