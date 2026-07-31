"""Refusing to trade with ourselves, and — just as important — not refusing anything else.

Both venues treat self-matching as an abuse pattern, so this is an account-safety control
(PLAN §3.3). But a check that is too eager is worse than none: vetoing an entry because a
*protective stop* is resting would leave positions unguarded, which is the R12 hazard the stop
exists to prevent. Both directions are asserted here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.orders import OrderIntent
from tradebot.execution.selftrade import crossing_order
from tradebot.interfaces.broker import OrderStatus

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
KEY = "binance:BTC/USDT"


def intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "client_order_id": "pap-NEW",
        "basket_id": "b",
        "cycle_id": "c",
        "instrument_key": KEY,
        "side": Side.BUY,
        "qty": Decimal(1),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal(100),
        "created_at": NOW,
    }
    return OrderIntent(**{**base, **overrides})  # type: ignore[arg-type]


def resting(**overrides: object) -> OrderStatus:
    base: dict[str, object] = {
        "client_order_id": "pap-OLD",
        "venue_order_id": "1",
        "instrument_key": KEY,
        "state": OrderState.OPEN,
        "requested_qty": Decimal(1),
        "filled_qty": Decimal(0),
        "observed_at": NOW,
        "side": Side.SELL,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal(99),
    }
    return OrderStatus(**{**base, **overrides})  # type: ignore[arg-type]


class TestCrossingIsCaught:
    def test_a_buy_above_our_own_resting_sell_would_match(self) -> None:
        assert crossing_order(intent(), [resting()]) is not None

    def test_a_sell_below_our_own_resting_buy_would_match(self) -> None:
        crossing = crossing_order(
            intent(side=Side.SELL, limit_price=Decimal(99)),
            [resting(side=Side.BUY, limit_price=Decimal(100))],
        )
        assert crossing is not None

    def test_a_market_order_crosses_whatever_is_there(self) -> None:
        """The dangerous case: it takes the best price, and the best price may be ours."""
        crossing = crossing_order(
            intent(order_type=OrderType.MARKET, limit_price=None), [resting()]
        )
        assert crossing is not None

    def test_an_unreported_price_counts_as_crossing(self) -> None:
        """Fail closed: "we cannot tell" and "it is fine" are not the same answer."""
        assert crossing_order(intent(), [resting(limit_price=None)]) is not None

    def test_an_unreported_side_counts_as_opposing(self) -> None:
        assert crossing_order(intent(), [resting(side=None)]) is not None


class TestNotCrossingIsAllowed:
    def test_prices_that_do_not_meet_are_left_alone(self) -> None:
        assert crossing_order(intent(), [resting(limit_price=Decimal(101))]) is None

    def test_the_same_side_never_crosses(self) -> None:
        assert crossing_order(intent(), [resting(side=Side.BUY, limit_price=Decimal(100))]) is None

    def test_another_instrument_is_irrelevant(self) -> None:
        assert crossing_order(intent(), [resting(instrument_key="binance:ETH/USDT")]) is None

    def test_a_terminal_order_is_not_resting(self) -> None:
        assert crossing_order(intent(), [resting(state=OrderState.FILLED)]) is None

    def test_an_order_never_crosses_itself(self) -> None:
        """Re-reading our own submitted order must not veto its own resubmission path."""
        assert crossing_order(intent(), [resting(client_order_id="pap-NEW")]) is None


class TestProtectiveLegsAreNotTheHazard:
    def test_an_untriggered_stop_is_not_a_resting_price(self) -> None:
        """A stop's limit sits below the market until it triggers.

        Treating it as resting would veto every entry made while a stop is in place — which is
        every entry after the first — and leave the next position unguarded (R12).
        """
        stop = resting(
            order_type=OrderType.STOP_LOSS_LIMIT,
            stop_price=Decimal(90),
            limit_price=Decimal("89.5"),
        )
        assert crossing_order(intent(), [stop]) is None

    def test_a_protective_leg_is_never_itself_checked(self) -> None:
        """Refusing to place an exit is far worse than a self-match (DESIGN §6.7)."""
        leg = intent(
            role=OrderRole.STOP_LOSS,
            order_type=OrderType.STOP_LOSS_LIMIT,
            side=Side.SELL,
            stop_price=Decimal(90),
            limit_price=Decimal("89.5"),
        )
        assert crossing_order(leg, [resting(side=Side.BUY, limit_price=Decimal(100))]) is None

    def test_a_take_profit_resting_as_a_plain_limit_is_still_checked(self) -> None:
        """Alpaca's target leg *is* a resting limit, and a market buy would take it."""
        target = resting(order_type=OrderType.LIMIT, limit_price=Decimal(99))
        assert crossing_order(intent(order_type=OrderType.MARKET, limit_price=None), [target])


class TestTheFirstCrossingIsReported:
    def test_the_reported_order_is_one_that_actually_crosses(self) -> None:
        crossing = crossing_order(
            intent(),
            [
                resting(client_order_id="pap-FAR", limit_price=Decimal(150)),
                resting(client_order_id="pap-NEAR", limit_price=Decimal(99)),
            ],
        )
        assert crossing is not None
        assert crossing.client_order_id == "pap-NEAR"

    def test_no_resting_orders_means_nothing_to_cross(self) -> None:
        assert crossing_order(intent(), []) is None


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_an_order_never_crosses_a_book_of_its_own_side(side: Side) -> None:
    """Property: same-side orders queue behind each other; they never match."""
    same_side = [
        resting(client_order_id=f"pap-{n}", side=side, limit_price=Decimal(100 + n))
        for n in range(5)
    ]
    assert crossing_order(intent(side=side), same_side) is None
