"""SimBroker: fill semantics, and the `SUBMIT_UNKNOWN` scenario it exists to reproduce.

Written as behaviour every `BrokerAdapter` must share, so this file becomes the seed of the
Phase 5 contract suite that runs identically against CCXT and Alpaca (PLAN Phase 5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import OrderState, OrderType, Side
from tradebot.core.errors import SubmitUnknownError
from tradebot.core.orders import OrderIntent
from tradebot.execution.sim_broker import SimBroker, average_price
from tradebot.interfaces.broker import OrderRef

NOW = datetime(2026, 3, 1, tzinfo=UTC)
KEY = "sim:BTC/USDT"


def intent(
    *,
    side: Side = Side.BUY,
    qty: str = "0.5",
    price: str | None = "50000",
    order_type: OrderType = OrderType.LIMIT,
    client_order_id: str = "sim-ABCDEF",
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=KEY,
        side=side,
        qty=Decimal(qty),
        order_type=order_type,
        limit_price=Decimal(price) if price else None,
        created_at=NOW,
    )


@pytest.fixture
def broker(clock: ManualClock) -> SimBroker:
    return SimBroker(clock, balances={"USDT": Decimal("100000")})


class TestFills:
    async def test_a_marketable_limit_fills_at_its_limit_never_better(
        self, broker: SimBroker
    ) -> None:
        """Modelling price improvement would flatter the strategy."""
        await broker.submit(intent())
        status = await broker.fetch_order(
            OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY)
        )
        assert status.state is OrderState.FILLED
        assert status.fills[0].price == Decimal("50000")

    async def test_a_market_order_slips_against_us(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, reference_prices={KEY: Decimal("50000")})
        await broker.submit(intent(order_type=OrderType.MARKET, price=None))
        status = await broker.fetch_order(
            OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY)
        )
        assert status.fills[0].price > Decimal("50000")

    async def test_a_market_sell_slips_the_other_way(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, reference_prices={KEY: Decimal("50000")})
        await broker.submit(intent(side=Side.SELL, order_type=OrderType.MARKET, price=None))
        status = await broker.fetch_order(
            OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY)
        )
        assert status.fills[0].price < Decimal("50000")

    async def test_fees_are_charged_on_notional(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, fee_pct=Decimal("0.1"), balances={"USDT": Decimal("100000")})
        await broker.submit(intent())
        status = await broker.fetch_order(
            OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY)
        )
        assert status.fills[0].fee == Decimal("25")  # 0.1% of 25000

    async def test_partial_fills_leave_the_remainder_open(self, clock: ManualClock) -> None:
        """Partial fills are the normal case at a real venue, not an edge case."""
        broker = SimBroker(clock, fill_ratio=Decimal("0.4"))
        ack = await broker.submit(intent())
        assert ack.state is OrderState.PARTIALLY_FILLED
        assert len(await broker.fetch_open_orders()) == 1

    async def test_balances_move_with_fills(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, balances={"USDT": Decimal("100000")}, fee_pct=Decimal("0.1"))
        await broker.submit(intent())
        state = await broker.fetch_positions_and_balances()
        assert state.balance("USDT").free == Decimal("74975")  # 100000 − 25000 − 25


class TestSubmitUnknown:
    async def test_an_ambiguous_submit_raises_with_the_client_order_id(
        self, broker: SimBroker
    ) -> None:
        broker.fail_next_submit = True
        with pytest.raises(SubmitUnknownError) as exc:
            await broker.submit(intent())
        assert exc.value.client_order_id == "sim-ABCDEF"

    async def test_the_order_still_exists_at_the_venue_afterwards(self, broker: SimBroker) -> None:
        """The whole scenario: the order landed, only the acknowledgement was lost."""
        broker.fail_next_submit = True
        with pytest.raises(SubmitUnknownError):
            await broker.submit(intent())

        status = await broker.fetch_order(
            OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY)
        )
        assert status.state is OrderState.FILLED

    async def test_an_order_the_venue_never_saw_reports_rejected(self, broker: SimBroker) -> None:
        status = await broker.fetch_order(
            OrderRef(client_order_id="sim-NEVERSENT", instrument_key=KEY)
        )
        assert status.state is OrderState.REJECTED
        assert status.reject_reason == "not found at venue"


class TestCancellation:
    async def test_an_open_order_can_be_cancelled(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, fill_ratio=Decimal("0.4"))
        await broker.submit(intent())
        ack = await broker.cancel(OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY))
        assert ack.cancelled
        assert not await broker.fetch_open_orders()

    async def test_cancelling_a_filled_order_is_reported_not_raised(
        self, broker: SimBroker
    ) -> None:
        await broker.submit(intent())
        ack = await broker.cancel(OrderRef(client_order_id="sim-ABCDEF", instrument_key=KEY))
        assert not ack.cancelled

    async def test_cancelling_an_unknown_order_is_reported_not_raised(
        self, broker: SimBroker
    ) -> None:
        ack = await broker.cancel(OrderRef(client_order_id="sim-NOPE", instrument_key=KEY))
        assert not ack.cancelled


class TestCapabilities:
    def test_capabilities_are_declared_not_assumed(self, broker: SimBroker) -> None:
        capabilities = broker.capabilities()
        assert OrderType.LIMIT in capabilities.order_types
        assert capabilities.query_by_client_order_id
        assert not capabilities.venue_side_ttl  # TTL is bot-enforced (REVIEW B7)

    def test_lacking_protective_orders_is_declared_so_risk_can_haircut(
        self, broker: SimBroker
    ) -> None:
        assert not broker.capabilities().protective_orders


class TestAveragePrice:
    def test_weights_by_quantity(self) -> None:
        from tradebot.core.orders import Fill

        fills = tuple(
            Fill(
                fill_id=f"f{i}",
                client_order_id="sim-ABCDEF",
                instrument_key=KEY,
                side=Side.BUY,
                qty=Decimal(qty),
                price=Decimal(price),
                filled_at=NOW,
            )
            for i, (qty, price) in enumerate([("1", "100"), ("3", "200")])
        )
        assert average_price(fills) == Decimal("175")

    def test_no_fills_is_zero_not_a_division_error(self) -> None:
        assert average_price(()) == 0
