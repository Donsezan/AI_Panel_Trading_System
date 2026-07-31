"""SimBroker: the matching engine, and the `SUBMIT_UNKNOWN` scenario it exists to reproduce.

Written as behaviour every `BrokerAdapter` must share, so this file becomes the seed of the
Phase 5 contract suite that runs identically against CCXT and Alpaca (PLAN Phase 5).

The matching rules under test are the ones that make TTL, partial fills and protective legs
mean anything: an order fills only when the market trades through it, a limit never fills
better than its limit, and a filled OCO leg cancels its sibling inside the venue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import SubmitUnknownError
from tradebot.core.orders import OrderIntent
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.interfaces.broker import OrderRef

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
KEY = "sim:BTC/USDT"
ENTRY = "sim-ABCDEF"


def intent(
    *,
    side: Side = Side.BUY,
    qty: str = "0.5",
    price: str | None = "50000",
    stop: str | None = None,
    order_type: OrderType = OrderType.LIMIT,
    role: OrderRole = OrderRole.ENTRY,
    client_order_id: str = ENTRY,
    group_id: str = "",
    created_at: datetime = NOW,
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
        stop_price=Decimal(stop) if stop else None,
        role=role,
        group_id=group_id,
        created_at=created_at,
    )


def tick(*, last: str, high: str | None = None, low: str | None = None, at: datetime = NOW) -> Tick:
    price = Decimal(last)
    return Tick(
        instrument_key=KEY,
        bid=price,
        ask=price,
        last=price,
        high=Decimal(high) if high else price,
        low=Decimal(low) if low else price,
        covers_since=at,
        observed_at=at,
    )


async def status_of(broker: SimBroker, client_order_id: str = ENTRY) -> object:
    return await broker.fetch_order(OrderRef(client_order_id=client_order_id, instrument_key=KEY))


@pytest.fixture
def broker(clock: ManualClock) -> SimBroker:
    return SimBroker(clock, balances={"USDT": Decimal("100000")})


class TestMatching:
    async def test_a_marketable_limit_fills_at_its_limit_never_better(
        self, broker: SimBroker
    ) -> None:
        """Modelling price improvement would flatter the strategy."""
        broker.observe(tick(last="49000"))
        await broker.submit(intent())

        status = await status_of(broker)
        assert status.state is OrderState.FILLED
        assert status.fills[0].price == Decimal("50000")

    async def test_a_limit_behind_the_book_rests_instead_of_filling(
        self, broker: SimBroker
    ) -> None:
        """The behaviour TTL and the ExecutionMonitor exist for."""
        broker.observe(tick(last="51000"))
        ack = await broker.submit(intent(price="50000"))

        assert ack.state is OrderState.OPEN
        assert len(await broker.fetch_open_orders()) == 1

    async def test_a_resting_order_fills_when_the_market_trades_through_it(
        self, broker: SimBroker
    ) -> None:
        broker.observe(tick(last="51000"))
        await broker.submit(intent(price="50000"))

        fills = broker.observe(tick(last="50500", low="49900", at=NOW + timedelta(minutes=1)))

        assert len(fills) == 1
        assert fills[0].price == Decimal("50000")

    async def test_a_bar_that_closed_before_the_order_existed_cannot_fill_it(
        self, broker: SimBroker
    ) -> None:
        """Otherwise yesterday's daily candle triggers a stop placed this morning."""
        await broker.submit(intent(price="50000", created_at=NOW))

        stale = broker.observe(tick(last="50500", low="10", at=NOW - timedelta(days=1)))

        assert stale == ()

    async def test_a_market_order_slips_against_us(self, broker: SimBroker) -> None:
        broker.observe(tick(last="50000"))
        await broker.submit(intent(order_type=OrderType.MARKET, price=None))

        status = await status_of(broker)
        assert status.fills[0].price > Decimal("50000")

    async def test_a_market_sell_slips_the_other_way(self, broker: SimBroker) -> None:
        broker.observe(tick(last="50000"))
        await broker.submit(
            intent(side=Side.SELL, qty="0.1", order_type=OrderType.MARKET, price=None)
        )

        status = await status_of(broker)
        assert status.fills[0].price < Decimal("50000")

    async def test_a_market_order_with_no_reference_price_is_ambiguous_not_guessed(
        self, broker: SimBroker
    ) -> None:
        with pytest.raises(SubmitUnknownError):
            await broker.submit(intent(order_type=OrderType.MARKET, price=None))

    async def test_fees_are_charged_on_notional(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, fee_pct=Decimal("0.1"), balances={"USDT": Decimal("100000")})
        broker.observe(tick(last="49000"))
        await broker.submit(intent())

        status = await status_of(broker)
        assert status.fills[0].fee == Decimal("25")  # 0.1% of 25000

    async def test_partial_fills_leave_the_remainder_open(self, clock: ManualClock) -> None:
        """Partial fills are the normal case at a real venue, not an edge case."""
        broker = SimBroker(clock, fill_ratio=Decimal("0.4"), balances={"USDT": Decimal("100000")})
        broker.observe(tick(last="49000"))
        ack = await broker.submit(intent())

        assert ack.state is OrderState.PARTIALLY_FILLED
        assert len(await broker.fetch_open_orders()) == 1

    async def test_a_duplicate_client_order_id_is_rejected_not_filled_twice(
        self, broker: SimBroker
    ) -> None:
        """The idempotency key must be load-bearing at the venue, not only in our code."""
        broker.observe(tick(last="49000"))
        await broker.submit(intent())

        ack = await broker.submit(intent())

        assert ack.state is OrderState.REJECTED
        assert ack.reject_reason == "duplicate client_order_id"


class TestProtectiveLegs:
    async def test_a_stop_leg_does_not_fill_until_its_trigger_is_crossed(
        self, broker: SimBroker
    ) -> None:
        broker.observe(tick(last="50000"))
        await broker.submit(
            intent(
                side=Side.SELL,
                qty="0.1",
                price="47000",
                stop="48000",
                order_type=OrderType.STOP_LOSS_LIMIT,
                role=OrderRole.STOP_LOSS,
                client_order_id="sim-STOP",
                group_id=ENTRY,
            )
        )

        untouched = broker.observe(tick(last="49500", low="49000", at=NOW + timedelta(minutes=1)))
        triggered = broker.observe(tick(last="47500", low="46500", at=NOW + timedelta(minutes=2)))

        assert untouched == ()
        assert triggered[0].price == Decimal("47000")

    async def test_a_filled_leg_cancels_its_oco_sibling_inside_the_venue(
        self, broker: SimBroker
    ) -> None:
        """Without this, both exits fill and the second one sells a position that is gone."""
        broker.observe(tick(last="50000"))
        for leg in _oco_pair():
            await broker.submit(leg)

        broker.observe(tick(last="52500", high="53000", at=NOW + timedelta(minutes=1)))

        stop = await status_of(broker, "sim-STOP")
        target = await status_of(broker, "sim-TAKE")
        assert target.state is OrderState.FILLED
        assert stop.state is OrderState.CANCELLED

    async def test_cancelling_a_leg_releases_the_asset_it_locked(self, broker: SimBroker) -> None:
        broker.observe(tick(last="50000"))
        broker.credit("BTC", Decimal("0.1"))
        leg = _oco_pair()[0]
        await broker.submit(leg)

        before = (await broker.fetch_positions_and_balances()).balance("BTC")
        await broker.cancel(OrderRef(client_order_id="sim-STOP", instrument_key=KEY))
        after = (await broker.fetch_positions_and_balances()).balance("BTC")

        assert before.locked == Decimal("0.1")
        assert after.locked == Decimal(0)
        assert after.free == Decimal("0.1")


def _oco_pair() -> tuple[OrderIntent, OrderIntent]:
    return (
        intent(
            side=Side.SELL,
            qty="0.1",
            price="47000",
            stop="48000",
            order_type=OrderType.STOP_LOSS_LIMIT,
            role=OrderRole.STOP_LOSS,
            client_order_id="sim-STOP",
            group_id=ENTRY,
        ),
        intent(
            side=Side.SELL,
            qty="0.1",
            price="53000",
            stop="52000",
            order_type=OrderType.TAKE_PROFIT_LIMIT,
            role=OrderRole.TAKE_PROFIT,
            client_order_id="sim-TAKE",
            group_id=ENTRY,
        ),
    )


class TestAccount:
    async def test_balances_move_with_fills(self, clock: ManualClock) -> None:
        broker = SimBroker(clock, balances={"USDT": Decimal("100000")}, fee_pct=Decimal("0.1"))
        broker.observe(tick(last="49000"))
        await broker.submit(intent())

        state = await broker.fetch_positions_and_balances()
        assert state.balance("USDT").free == Decimal("74975")  # 100000 − 25000 − 25
        assert state.balance("BTC").free == Decimal("0.5")

    async def test_a_resting_buy_locks_the_funds_it_commits(self, broker: SimBroker) -> None:
        """The account state the reconciler diffs against must be truthful, not optimistic."""
        broker.observe(tick(last="51000"))
        await broker.submit(intent(price="50000"))

        usdt = (await broker.fetch_positions_and_balances()).balance("USDT")
        assert usdt.locked == Decimal("25000")
        assert usdt.free == Decimal("75000")
        assert usdt.total == Decimal("100000")

    async def test_positions_are_reported_for_the_reconciler(self, broker: SimBroker) -> None:
        broker.observe(tick(last="49000"))
        await broker.submit(intent())

        position = (await broker.fetch_positions_and_balances()).position(KEY)
        assert position is not None
        assert position.qty == Decimal("0.5")
        assert position.avg_entry == Decimal("50000")

    async def test_average_entry_survives_a_partial_exit(self, broker: SimBroker) -> None:
        broker.observe(tick(last="49000"))
        await broker.submit(intent())
        # Marketable on the sell side means at or below the bid, hence 48000 rather than 49500.
        await broker.submit(
            intent(side=Side.SELL, qty="0.2", price="48000", client_order_id="sim-EXIT")
        )

        position = (await broker.fetch_positions_and_balances()).position(KEY)
        assert position is not None
        assert position.qty == Decimal("0.3")
        assert position.avg_entry == Decimal("50000")


class TestSubmitUnknown:
    async def test_an_ambiguous_submit_raises_with_the_client_order_id(
        self, broker: SimBroker
    ) -> None:
        broker.observe(tick(last="49000"))
        broker.fail_next_submit = True
        with pytest.raises(SubmitUnknownError) as exc:
            await broker.submit(intent())
        assert exc.value.client_order_id == ENTRY

    async def test_the_order_still_exists_at_the_venue_afterwards(self, broker: SimBroker) -> None:
        """The whole scenario: the order landed, only the acknowledgement was lost."""
        broker.observe(tick(last="49000"))
        broker.fail_next_submit = True
        with pytest.raises(SubmitUnknownError):
            await broker.submit(intent())

        status = await status_of(broker)
        assert status.state is OrderState.FILLED

    async def test_an_order_the_venue_never_saw_reports_rejected(self, broker: SimBroker) -> None:
        status = await status_of(broker, "sim-NEVERSENT")
        assert status.state is OrderState.REJECTED
        assert status.reject_reason == "not found at venue"


class TestCancellation:
    async def test_an_open_order_can_be_cancelled(self, broker: SimBroker) -> None:
        broker.observe(tick(last="51000"))
        await broker.submit(intent(price="50000"))

        ack = await broker.cancel(OrderRef(client_order_id=ENTRY, instrument_key=KEY))

        assert ack.cancelled
        assert not await broker.fetch_open_orders()

    async def test_cancelling_a_filled_order_is_reported_not_raised(
        self, broker: SimBroker
    ) -> None:
        broker.observe(tick(last="49000"))
        await broker.submit(intent())
        ack = await broker.cancel(OrderRef(client_order_id=ENTRY, instrument_key=KEY))
        assert not ack.cancelled

    async def test_cancelling_an_unknown_order_is_reported_not_raised(
        self, broker: SimBroker
    ) -> None:
        ack = await broker.cancel(OrderRef(client_order_id="sim-NOPE", instrument_key=KEY))
        assert not ack.cancelled


class TestVenueReset:
    async def test_a_wipe_leaves_no_positions_and_no_orders(self, broker: SimBroker) -> None:
        """What a public testnet does roughly monthly, and what the reconciler must classify."""
        broker.observe(tick(last="49000"))
        await broker.submit(intent())

        broker.wipe({"USDT": Decimal("100000")})

        state = await broker.fetch_positions_and_balances()
        assert state.positions == ()
        assert not await broker.fetch_open_orders()


class TestCapabilities:
    def test_capabilities_are_declared_not_assumed(self, broker: SimBroker) -> None:
        capabilities = broker.capabilities()
        assert OrderType.LIMIT in capabilities.order_types
        assert capabilities.query_by_client_order_id
        assert not capabilities.venue_side_ttl  # TTL is bot-enforced (REVIEW B7)

    def test_linked_protective_orders_are_declared_so_both_legs_may_be_placed(
        self, broker: SimBroker
    ) -> None:
        capabilities = broker.capabilities()
        assert capabilities.protective_orders
        assert capabilities.oco_groups
