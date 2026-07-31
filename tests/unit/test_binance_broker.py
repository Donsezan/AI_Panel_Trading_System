"""Binance's order vocabulary, tested with plain dictionaries.

The contract suite proves the adapter *behaves* like every other adapter. This proves it reads and
writes Binance's wire format correctly, which is a different question and the one where a mistake
is silent: an inverted OCO leg pair places a stop above the market, and a price serialised as
`1E-5` is rejected by a filter nobody was watching.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.fake_venues import FakeBinanceTransport, FakeVenueBook

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass, OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.orders import OrderIntent
from tradebot.execution.brokers.binance import (
    ORDER_STATES,
    BinanceSpotBroker,
    parse_account,
    parse_state,
    parse_trade,
)
from tradebot.interfaces.broker import OrderRef

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        symbol="BTC/USDT",
        venue="binance",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )


@pytest.fixture
def transport() -> FakeBinanceTransport:
    return FakeBinanceTransport(FakeVenueBook(), server_time=NOW)


@pytest.fixture
def broker(
    clock: ManualClock, instrument: Instrument, transport: FakeBinanceTransport
) -> BinanceSpotBroker:
    return BinanceSpotBroker(transport, clock, instruments=(instrument,))


def intent(instrument: Instrument, **overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "client_order_id": "pap-ENTRY",
        "basket_id": "b",
        "cycle_id": "c",
        "instrument_key": instrument.key,
        "side": Side.BUY,
        "qty": Decimal("0.5"),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("50000.25"),
        "created_at": NOW,
    }
    return OrderIntent(**{**base, **overrides})  # type: ignore[arg-type]


class TestStateMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("NEW", OrderState.OPEN),
            ("PARTIALLY_FILLED", OrderState.PARTIALLY_FILLED),
            ("FILLED", OrderState.FILLED),
            ("CANCELED", OrderState.CANCELLED),
            ("EXPIRED", OrderState.EXPIRED),
            ("REJECTED", OrderState.REJECTED),
        ],
    )
    def test_every_binance_status_maps_to_one_lifecycle_state(
        self, status: str, expected: OrderState
    ) -> None:
        assert parse_state({"status": status}) is expected

    def test_expired_in_match_is_a_rejection_not_an_expiry(self) -> None:
        """Self-trade prevention killed it before it rested; it never had a TTL to outlive."""
        assert parse_state({"status": "EXPIRED_IN_MATCH"}) is OrderState.REJECTED

    def test_an_unknown_status_fails_closed(self) -> None:
        """Guessing at a status we do not recognise is how an open order looks terminal."""
        with pytest.raises(DataStaleError, match="unknown order status"):
            parse_state({"status": "SOMETHING_NEW"})

    def test_every_mapped_state_is_a_real_lifecycle_state(self) -> None:
        assert all(isinstance(state, OrderState) for state in ORDER_STATES.values())


class TestOrderParams:
    async def test_a_limit_order_carries_price_time_in_force_and_our_id(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        await broker.submit(intent(instrument))
        endpoint, params = transport.book.calls[-1]
        assert endpoint == "newOrder"
        assert params["symbol"] == "BTCUSDT"
        assert params["side"] == "BUY"
        assert params["type"] == "LIMIT"
        assert params["price"] == "50000.25"
        assert params["timeInForce"] == "GTC"
        assert params["newClientOrderId"] == "pap-ENTRY"
        assert params["recvWindow"] == 5000

    async def test_a_market_order_carries_no_price_or_time_in_force(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        """A market order with a `timeInForce` is rejected by the venue."""
        await broker.submit(intent(instrument, order_type=OrderType.MARKET, limit_price=None))
        _, params = transport.book.calls[-1]
        assert params["type"] == "MARKET"
        assert "price" not in params
        assert "timeInForce" not in params

    async def test_small_quantities_are_sent_as_fixed_point_not_scientific_notation(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        """`str(Decimal("1E-5"))` is `1E-5`, which Binance rejects as an invalid quantity."""
        await broker.submit(intent(instrument, qty=Decimal("0.00001")))
        _, params = transport.book.calls[-1]
        assert params["quantity"] == "0.00001"
        assert "E" not in str(params["quantity"])

    async def test_an_order_type_the_venue_cannot_express_fails_closed(
        self, broker: BinanceSpotBroker, instrument: Instrument
    ) -> None:
        """Silently downgrading a stop to a market order is not the order risk approved."""
        from tradebot.execution.brokers import binance as module

        original = dict(module.ORDER_TYPES)
        module.ORDER_TYPES.clear()  # type: ignore[attr-defined]
        try:
            with pytest.raises(DataStaleError, match="cannot express"):
                await broker.submit(intent(instrument))
        finally:
            module.ORDER_TYPES.update(original)  # type: ignore[attr-defined]

    async def test_an_unconfigured_instrument_is_refused(
        self, broker: BinanceSpotBroker, instrument: Instrument
    ) -> None:
        """Its lot size and minimums are unknown, so any order on it is unquantizable."""
        with pytest.raises(DataStaleError, match="not configured"):
            await broker.submit(intent(instrument, instrument_key="binance:DOGE/USDT"))


class TestOcoParams:
    def _legs(self, instrument: Instrument) -> tuple[OrderIntent, OrderIntent]:
        common: dict[str, object] = {
            "basket_id": "b",
            "cycle_id": "c",
            "instrument_key": instrument.key,
            "side": Side.SELL,
            "qty": Decimal("0.5"),
            "group_id": "pap-ENTRY",
            "created_at": NOW,
        }
        stop = OrderIntent(
            client_order_id="pap-STOP",
            order_type=OrderType.STOP_LOSS_LIMIT,
            stop_price=Decimal("48000"),
            limit_price=Decimal("47760"),
            role=OrderRole.STOP_LOSS,
            **common,  # type: ignore[arg-type]
        )
        target = OrderIntent(
            client_order_id="pap-TARGET",
            order_type=OrderType.TAKE_PROFIT_LIMIT,
            stop_price=Decimal("53000"),
            limit_price=Decimal("53265"),
            role=OrderRole.TAKE_PROFIT,
            **common,  # type: ignore[arg-type]
        )
        return stop, target

    async def test_the_stop_goes_below_and_the_target_above_for_a_long_exit(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        """Swapping the pair places a stop above the market, where it triggers instantly."""
        stop, target = self._legs(instrument)
        await broker.submit_group((stop, target))
        endpoint, params = transport.book.calls[-1]
        assert endpoint == "newOco"
        assert params["belowClientOrderId"] == "pap-STOP"
        assert params["belowStopPrice"] == "48000"
        assert params["aboveClientOrderId"] == "pap-TARGET"
        assert params["aboveStopPrice"] == "53000"
        assert params["side"] == "SELL"
        assert params["listClientOrderId"] == "pap-ENTRY"

    async def test_one_quantity_covers_both_legs(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        stop, target = self._legs(instrument)
        await broker.submit_group((stop, target))
        _, params = transport.book.calls[-1]
        assert params["quantity"] == "0.5"

    async def test_legs_of_different_sizes_are_refused(
        self, broker: BinanceSpotBroker, instrument: Instrument
    ) -> None:
        """An OCO list carries one quantity; sending the larger would exit more than is held."""
        stop, target = self._legs(instrument)
        with pytest.raises(DataStaleError, match="one quantity"):
            await broker.submit_group((stop, target.model_copy(update={"qty": Decimal("0.4")})))

    async def test_a_leg_the_venue_does_not_report_fails_closed(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        """The legs exist at the venue either way, so a missing report is resolved by query."""
        stop, target = self._legs(instrument)
        transport.overrides["newOco"] = lambda _params: {
            "orderListId": 1,
            "listOrderStatus": "EXECUTING",
            "orderReports": [],
        }
        with pytest.raises(DataStaleError, match="reported no state"):
            await broker.submit_group((stop, target))


class TestAccountParsing:
    def test_a_spot_balance_is_the_position(self, instrument: Instrument) -> None:
        """Holding 0.4 BTC and being long 0.4 BTC/USDT are the same fact on a spot venue."""
        state = parse_account(
            {
                "balances": [
                    {"asset": "USDT", "free": "1000.50", "locked": "25.00"},
                    {"asset": "BTC", "free": "0.4", "locked": "0.1"},
                ]
            },
            (instrument,),
            NOW,
        )
        assert state.qty(instrument.key) == Decimal("0.5")  # free + locked: we still hold it
        assert state.total("USDT") == Decimal("1025.50")

    def test_avg_entry_is_left_to_the_ledger(self, instrument: Instrument) -> None:
        """The venue does not know our cost basis; inventing one corrupts the source of truth."""
        state = parse_account(
            {"balances": [{"asset": "BTC", "free": "0.4", "locked": "0"}]}, (instrument,), NOW
        )
        assert state.positions[0].avg_entry == Decimal(0)

    def test_a_zero_balance_is_not_a_position(self, instrument: Instrument) -> None:
        state = parse_account(
            {"balances": [{"asset": "BTC", "free": "0", "locked": "0"}]}, (instrument,), NOW
        )
        assert state.positions == ()

    def test_an_untraded_asset_is_still_a_balance(self, instrument: Instrument) -> None:
        """Dust in a coin we do not trade is real money and belongs in the diff."""
        state = parse_account(
            {"balances": [{"asset": "BNB", "free": "1.5", "locked": "0"}]}, (instrument,), NOW
        )
        assert state.total("BNB") == Decimal("1.5")


class TestTradeParsing:
    def test_a_trade_becomes_a_fill_with_the_venues_id_and_fee(
        self, instrument: Instrument
    ) -> None:
        fill = parse_trade(
            {
                "id": 987,
                "qty": "0.25",
                "price": "50000.10",
                "commission": "0.0125",
                "commissionAsset": "USDT",
                "time": int(NOW.timestamp() * 1000),
            },
            "pap-ENTRY",
            instrument,
            Side.BUY,
            observed_at=NOW,
        )
        assert fill.fill_id == "binance-987"
        assert fill.qty == Decimal("0.25")
        assert fill.price == Decimal("50000.10")
        assert fill.fee == Decimal("0.0125")
        assert fill.fee_currency == "USDT"

    def test_the_side_comes_from_the_order_not_the_trade(self, instrument: Instrument) -> None:
        """A submit's embedded fills report no side at all, so the order's is the only truth."""
        fill = parse_trade(
            {"id": 1, "qty": "1", "price": "100"},
            "pap-ENTRY",
            instrument,
            Side.SELL,
            observed_at=NOW,
        )
        assert fill.side is Side.SELL

    def test_a_missing_quantity_fails_closed(self, instrument: Instrument) -> None:
        """A fill with no quantity is not a fill; defaulting it to zero would book a phantom."""
        with pytest.raises(DataStaleError, match="missing qty"):
            parse_trade(
                {"id": 1, "price": "100"}, "pap-ENTRY", instrument, Side.BUY, observed_at=NOW
            )


class TestStatusReporting:
    async def test_binances_zero_price_placeholder_is_not_read_as_a_price(
        self, broker: BinanceSpotBroker, instrument: Instrument
    ) -> None:
        """Binance writes an absent stop as `"0.00000000"`; a zero limit crosses everything."""
        await broker.submit(intent(instrument))
        status = await broker.fetch_order(
            _ref(instrument, "pap-ENTRY"),
        )
        assert status.stop_price is None
        assert status.limit_price == Decimal("50000.25")

    async def test_open_orders_do_not_pay_for_a_trade_lookup(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        """`myTrades` costs 20 weight; reconciliation and the self-trade check book nothing."""
        await broker.submit(intent(instrument))
        transport.book.fill("pap-ENTRY", Decimal("0.5"), Decimal("50000"))
        await broker.fetch_open_orders()
        assert all(endpoint != "myTrades" for endpoint, _ in transport.book.calls)

    async def test_a_filled_order_is_asked_for_its_trades(
        self,
        broker: BinanceSpotBroker,
        transport: FakeBinanceTransport,
        instrument: Instrument,
    ) -> None:
        """Fees and trade ids are what the ledger books from, and a total carries neither."""
        await broker.submit(intent(instrument))
        transport.book.fill("pap-ENTRY", Decimal("0.5"), Decimal("50000"))
        status = await broker.fetch_order(_ref(instrument, "pap-ENTRY"))
        assert [fill.fill_id for fill in status.fills] == ["binance-5000"]

    async def test_the_venue_clock_is_read_from_server_time(
        self, broker: BinanceSpotBroker
    ) -> None:
        assert await broker.server_time() == NOW

    async def test_key_restrictions_are_reported(self, broker: BinanceSpotBroker) -> None:
        """The startup assertion that a live key cannot withdraw depends on this (PLAN §3.2)."""
        assert await broker.withdrawals_enabled() is False


class TestCapabilities:
    def test_oco_and_protective_orders_are_declared(self, broker: BinanceSpotBroker) -> None:
        capabilities = broker.capabilities()
        assert capabilities.protective_orders
        assert capabilities.oco_groups

    def test_no_venue_side_ttl_is_claimed(self, broker: BinanceSpotBroker) -> None:
        """Spot offers GTC/IOC/FOK only, so TTL stays bot-enforced."""
        assert not broker.capabilities().venue_side_ttl

    def test_the_client_order_id_cap_matches_binances_documented_limit(
        self, broker: BinanceSpotBroker
    ) -> None:
        assert broker.capabilities().max_client_order_id_length == 36


def _ref(instrument: Instrument, client_order_id: str) -> OrderRef:
    return OrderRef(client_order_id=client_order_id, instrument_key=instrument.key)
