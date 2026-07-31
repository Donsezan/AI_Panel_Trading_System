"""The fail-closed branches in the venue layer, and the self-trade refusal end to end.

Every assertion here is on a path that only runs when something is wrong: a venue answering with
the wrong shape, an instrument nobody configured, an order that would trade against our own. These
are the branches PLAN §6.4 requires to be *tested* rather than merely written, because they are
the ones a happy-path run never reaches and a bad day always does.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from tests.fake_venues import FakeAlpacaApi, FakeBinanceTransport, FakeVenueBook, alpaca_transport

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass, OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import DataStaleError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.orders import OrderIntent
from tradebot.execution.brokers.alpaca import AlpacaBroker
from tradebot.execution.brokers.binance import BinanceSpotBroker
from tradebot.execution.brokers.calendars import ContinuousCalendar
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.execution.selftrade import SELF_TRADE_RULE
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import OrderRef
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def crypto() -> Instrument:
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
def equity() -> Instrument:
    return Instrument(
        symbol="AAPL",
        venue="alpaca",
        asset_class=AssetClass.EQUITY,
        base_currency="AAPL",
        quote_currency="USD",
        lot_size=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal(1),
    )


def entry(instrument: Instrument, **overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "client_order_id": "pap-ENTRY",
        "basket_id": "b",
        "cycle_id": "c",
        "instrument_key": instrument.key,
        "side": Side.BUY,
        "qty": Decimal(1),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal(100),
        "created_at": NOW,
    }
    return OrderIntent(**{**base, **overrides})  # type: ignore[arg-type]


class TestBinanceMalformedResponses:
    """A venue answering with the wrong shape must stop the cycle, never be guessed at."""

    async def test_a_non_object_account_payload_fails_closed(
        self, clock: ManualClock, crypto: Instrument
    ) -> None:
        transport = FakeBinanceTransport(FakeVenueBook())
        transport.overrides["account"] = lambda _p: ["not", "an", "object"]
        broker = BinanceSpotBroker(transport, clock, instruments=(crypto,))
        with pytest.raises(DataStaleError, match="non-object"):
            await broker.fetch_positions_and_balances()

    async def test_a_clock_reply_without_a_time_fails_closed(
        self, clock: ManualClock, crypto: Instrument
    ) -> None:
        """A skew check that silently succeeds against a missing answer checks nothing."""
        transport = FakeBinanceTransport(FakeVenueBook())
        transport.overrides["time"] = lambda _p: {}
        broker = BinanceSpotBroker(transport, clock, instruments=(crypto,))
        with pytest.raises(DataStaleError, match="serverTime"):
            await broker.server_time()

    async def test_an_unreadable_timestamp_falls_back_to_our_own(
        self, clock: ManualClock, crypto: Instrument
    ) -> None:
        """A missing `transactTime` is cosmetic; a missing quantity is not, and that one raises."""
        transport = FakeBinanceTransport(FakeVenueBook())
        transport.overrides["newOrder"] = lambda p: {
            "clientOrderId": p["newClientOrderId"],
            "status": "NEW",
            "orderId": 1,
            "transactTime": "not-a-number",
        }
        broker = BinanceSpotBroker(transport, clock, instruments=(crypto,))
        ack = await broker.submit(entry(crypto))
        assert ack.accepted_at == clock.now()

    async def test_a_venue_that_will_not_report_restrictions_answers_none(
        self, clock: ManualClock, crypto: Instrument
    ) -> None:
        """The spot testnet has no `sapi` at all, and "would not say" is not "may not withdraw"."""
        transport = FakeBinanceTransport(FakeVenueBook())
        transport.overrides["apiRestrictions"] = lambda _p: {}
        broker = BinanceSpotBroker(transport, clock, instruments=(crypto,))
        assert await broker.withdrawals_enabled() is None

    async def test_closing_the_broker_closes_its_transport(
        self, clock: ManualClock, crypto: Instrument
    ) -> None:
        """A leaked exchange session keeps the process alive after a cycle."""
        transport = FakeBinanceTransport(FakeVenueBook())
        await BinanceSpotBroker(transport, clock, instruments=(crypto,)).close()
        assert transport.closed


class TestAlpacaMalformedResponses:
    async def test_a_non_object_account_payload_fails_closed(
        self, clock: ManualClock, equity: Instrument
    ) -> None:
        api = FakeAlpacaApi(FakeVenueBook(currency="USD"))
        api.overrides["/v2/account"] = lambda _request: httpx.Response(200, json=[])
        broker = AlpacaBroker(alpaca_transport(api, clock), clock, instruments=(equity,))
        with pytest.raises(DataStaleError, match="non-object"):
            await broker.fetch_positions_and_balances()

    async def test_a_clock_reply_without_a_timestamp_fails_closed(
        self, clock: ManualClock, equity: Instrument
    ) -> None:
        api = FakeAlpacaApi(FakeVenueBook(currency="USD"))
        api.overrides["/v2/clock"] = lambda _request: httpx.Response(200, json={})
        broker = AlpacaBroker(alpaca_transport(api, clock), clock, instruments=(equity,))
        with pytest.raises(DataStaleError, match="no timestamp"):
            await broker.server_time()

    async def test_an_unconfigured_instrument_is_refused(
        self, clock: ManualClock, equity: Instrument
    ) -> None:
        api = FakeAlpacaApi(FakeVenueBook(currency="USD"))
        broker = AlpacaBroker(alpaca_transport(api, clock), clock, instruments=(equity,))
        with pytest.raises(DataStaleError, match="not configured"):
            await broker.submit(entry(equity, instrument_key="alpaca:TSLA"))

    async def test_a_non_exit_role_cannot_join_an_oco_order(
        self, clock: ManualClock, equity: Instrument
    ) -> None:
        """An entry in an OCO group would place a second entry disguised as an exit."""
        api = FakeAlpacaApi(FakeVenueBook(currency="USD"))
        broker = AlpacaBroker(alpaca_transport(api, clock), clock, instruments=(equity,))
        legs = (
            entry(equity, client_order_id="pap-A", side=Side.SELL),
            entry(
                equity,
                client_order_id="pap-B",
                side=Side.SELL,
                role=OrderRole.STOP_LOSS,
                order_type=OrderType.STOP_LOSS_LIMIT,
                stop_price=Decimal(90),
            ),
        )
        with pytest.raises(DataStaleError, match="not an exit leg"):
            await broker.submit_group(legs)

    async def test_cancelling_an_order_the_venue_forgot_is_reported(
        self, clock: ManualClock, equity: Instrument
    ) -> None:
        """Alpaca cancels by *its* id, so an order it cannot resolve cannot be cancelled."""
        api = FakeAlpacaApi(FakeVenueBook(currency="USD"))
        broker = AlpacaBroker(alpaca_transport(api, clock), clock, instruments=(equity,))
        ack = await broker.cancel(OrderRef(client_order_id="pap-GONE", instrument_key=equity.key))
        assert not ack.cancelled
        assert "no record" in ack.detail

    async def test_closing_the_broker_closes_its_transport(
        self, clock: ManualClock, equity: Instrument
    ) -> None:
        api = FakeAlpacaApi(FakeVenueBook(currency="USD"))
        broker = AlpacaBroker(alpaca_transport(api, clock), clock, instruments=(equity,))
        await broker.close()  # no exception: the client belongs to whoever created it


class TestContinuousCalendar:
    """Crypto never closes, and that has to be *an implementation* rather than a special case."""

    async def test_it_is_always_open(self, clock: ManualClock) -> None:
        assert await ContinuousCalendar("binance").is_open(NOW)

    async def test_the_day_is_the_utc_date(self, clock: ManualClock) -> None:
        """DESIGN §6.6's crypto boundary for the daily-loss baseline."""
        calendar = ContinuousCalendar("binance")
        assert await calendar.session_day(NOW) == "2026-07-30"
        assert await calendar.session_day(NOW + timedelta(hours=13)) == "2026-07-31"

    async def test_there_is_never_a_next_open_to_wait_for(self, clock: ManualClock) -> None:
        assert await ContinuousCalendar("binance").next_open(NOW) is None


class TestSelfTradeRefusal:
    """The pre-submit check, exercised through the service that owns the durable record."""

    async def _service(
        self, clock: ManualClock, store: EventStore, ledger: Ledger, crypto: Instrument
    ) -> tuple[ExecutionService, SimBroker]:
        broker = SimBroker(clock, venue_id="binance", balances={"USDT": Decimal(100_000)})
        broker.observe(
            Tick(
                instrument_key=crypto.key,
                bid=Decimal(105),
                ask=Decimal(105),
                last=Decimal(105),
                high=Decimal(105),
                low=Decimal(105),
                covers_since=NOW,
                observed_at=NOW,
            )
        )
        return ExecutionService(broker, store, ledger, clock), broker

    async def test_an_order_crossing_our_own_resting_order_never_reaches_the_venue(
        self, clock: ManualClock, store: EventStore, ledger: Ledger, crypto: Instrument
    ) -> None:
        execution, broker = await self._service(clock, store, ledger, crypto)
        await execution.submit(entry(crypto, client_order_id="pap-RESTING"), crypto)

        crossing = entry(
            crypto, client_order_id="pap-CROSSER", side=Side.SELL, limit_price=Decimal(99)
        )
        order = await execution.submit(crossing, crypto)

        assert order.state is OrderState.REJECTED
        resting = {status.client_order_id for status in await broker.fetch_open_orders()}
        assert "pap-CROSSER" not in resting, "the refused order must never have been sent"

    async def test_the_refusal_is_recorded_as_a_risk_event(
        self, clock: ManualClock, store: EventStore, ledger: Ledger, crypto: Instrument
    ) -> None:
        """Why an order was refused must be answerable from the log alone."""
        execution, _ = await self._service(clock, store, ledger, crypto)
        await execution.submit(entry(crypto, client_order_id="pap-RESTING"), crypto)
        await execution.submit(
            entry(crypto, client_order_id="pap-CROSSER", side=Side.SELL, limit_price=Decimal(99)),
            crypto,
        )

        events = [event for event in store.read_all() if event.type is EventType.RISK_EVENT]
        assert any(event.payload.get("rule") == SELF_TRADE_RULE for event in events)

    async def test_a_non_crossing_order_is_submitted_normally(
        self, clock: ManualClock, store: EventStore, ledger: Ledger, crypto: Instrument
    ) -> None:
        """The check must not become the hazard: ordinary orders go through untouched."""
        execution, _ = await self._service(clock, store, ledger, crypto)
        await execution.submit(entry(crypto, client_order_id="pap-RESTING"), crypto)
        order = await execution.submit(
            entry(crypto, client_order_id="pap-HIGHER", side=Side.SELL, limit_price=Decimal(120)),
            crypto,
        )
        assert order.state is not OrderState.REJECTED

    async def test_a_protective_leg_is_never_refused(
        self, clock: ManualClock, store: EventStore, ledger: Ledger, crypto: Instrument
    ) -> None:
        """Refusing an exit leaves the position unguarded — far worse than a self-match (R12)."""
        execution, _ = await self._service(clock, store, ledger, crypto)
        await execution.submit(entry(crypto, client_order_id="pap-RESTING"), crypto)
        leg = entry(
            crypto,
            client_order_id="pap-STOP",
            side=Side.SELL,
            role=OrderRole.STOP_LOSS,
            order_type=OrderType.STOP_LOSS_LIMIT,
            stop_price=Decimal(90),
            limit_price=Decimal("89.5"),
            group_id="pap-RESTING",
        )
        order = await execution.submit(leg, crypto)
        assert order.state is not OrderState.REJECTED
