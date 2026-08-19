"""Alpaca's equity-specific behaviour: the parts no crypto venue has.

The contract suite already proves the adapter behaves like the others. What is left is everything
that exists *because* this is an equities venue — the session calendar, corporate actions, order
classes, extended hours — plus the wire format itself, asserted through the real transport over
`httpx.MockTransport` so the URLs, verbs and bodies are the ones Alpaca would receive.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from tests.fake_venues import FakeAlpacaApi, FakeVenueBook, alpaca_transport

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass, Mode, OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.orders import OrderIntent
from tradebot.execution.brokers.alpaca import (
    EXCHANGE_TZ,
    AlpacaAnnouncements,
    AlpacaBroker,
    AlpacaCalendar,
    parse_account,
    parse_announcement,
    parse_state,
)

NOW = datetime(2026, 7, 30, 15, 0, tzinfo=UTC)  # 11:00 in New York — the market is open


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def instrument() -> Instrument:
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


@pytest.fixture
def api() -> FakeAlpacaApi:
    return FakeAlpacaApi(FakeVenueBook(currency="USD"), clock_time=NOW)


@pytest.fixture
def broker(clock: ManualClock, instrument: Instrument, api: FakeAlpacaApi) -> AlpacaBroker:
    return AlpacaBroker(
        alpaca_transport(api, clock, mode=Mode.PAPER), clock, universe=lambda: (instrument,)
    )


def intent(instrument: Instrument, **overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "client_order_id": "pap-ENTRY",
        "basket_id": "b",
        "cycle_id": "c",
        "instrument_key": instrument.key,
        "side": Side.BUY,
        "qty": Decimal(10),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("195.50"),
        "created_at": NOW,
    }
    return OrderIntent(**{**base, **overrides})  # type: ignore[arg-type]


class TestWireFormat:
    async def test_an_order_posts_to_the_orders_endpoint_with_our_id(
        self, broker: AlpacaBroker, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        import json

        await broker.submit(intent(instrument))
        request = api.requests[-1]
        assert request.method == "POST"
        assert request.url.path == "/v2/orders"
        body = json.loads(request.content)
        assert body["symbol"] == "AAPL"
        assert body["side"] == "buy"
        assert body["type"] == "limit"
        assert body["limit_price"] == "195.50"
        assert body["client_order_id"] == "pap-ENTRY"

    async def test_credentials_travel_in_alpacas_headers_and_never_in_the_body(
        self, broker: AlpacaBroker, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        await broker.submit(intent(instrument))
        request = api.requests[-1]
        assert request.headers["APCA-API-KEY-ID"] == "test-key"
        assert request.headers["APCA-API-SECRET-KEY"] == "test-secret"
        assert b"test-secret" not in request.content

    async def test_a_lookup_by_our_id_uses_the_dedicated_endpoint(
        self, broker: AlpacaBroker, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        """`SUBMIT_UNKNOWN` recovery depends on this route existing (PLAN §2.3)."""
        await broker.submit(intent(instrument))
        from tradebot.interfaces.broker import OrderRef

        await broker.fetch_order(
            OrderRef(client_order_id="pap-ENTRY", instrument_key=instrument.key)
        )
        request = api.requests[-1]
        assert request.url.path == "/v2/orders:by_client_order_id"
        assert request.url.params["client_order_id"] == "pap-ENTRY"

    async def test_extended_hours_is_declared_on_every_order(
        self, clock: ManualClock, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        """Off unless configured: an extended-hours fill prints into a thin, wide book."""
        import json

        broker = AlpacaBroker(
            alpaca_transport(api, clock), clock, universe=lambda: (instrument,), extended_hours=True
        )
        await broker.submit(intent(instrument))
        assert json.loads(api.requests[-1].content)["extended_hours"] is True


class TestOrderClasses:
    def _legs(self, instrument: Instrument) -> tuple[OrderIntent, OrderIntent]:
        common: dict[str, object] = {
            "basket_id": "b",
            "cycle_id": "c",
            "instrument_key": instrument.key,
            "side": Side.SELL,
            "qty": Decimal(10),
            "group_id": "pap-ENTRY",
            "created_at": NOW,
        }
        stop = OrderIntent(
            client_order_id="pap-STOP",
            order_type=OrderType.STOP_LOSS_LIMIT,
            stop_price=Decimal("180"),
            limit_price=Decimal("179.10"),
            role=OrderRole.STOP_LOSS,
            **common,  # type: ignore[arg-type]
        )
        target = OrderIntent(
            client_order_id="pap-TARGET",
            order_type=OrderType.TAKE_PROFIT_LIMIT,
            stop_price=Decimal("210"),
            limit_price=Decimal("210"),
            role=OrderRole.TAKE_PROFIT,
            **common,  # type: ignore[arg-type]
        )
        return stop, target

    async def test_linked_legs_are_sent_as_one_oco_order(
        self, broker: AlpacaBroker, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        """Two free-standing exits over one holding can both fill (DESIGN §6.7, ADR 0011)."""
        import json

        stop, target = self._legs(instrument)
        await broker.submit_group((stop, target))
        body = json.loads(api.requests[-1].content)
        assert body["order_class"] == "oco"
        assert body["take_profit"]["limit_price"] == "210"
        assert body["stop_loss"]["stop_price"] == "180"
        assert body["stop_loss"]["limit_price"] == "179.10"

    async def test_extended_hours_gives_up_linked_legs_and_says_so(
        self, clock: ManualClock, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        """Alpaca cannot bracket an extended-hours order; declaring otherwise would place two."""
        broker = AlpacaBroker(
            alpaca_transport(api, clock), clock, universe=lambda: (instrument,), extended_hours=True
        )
        assert not broker.capabilities().oco_groups

    async def test_legs_of_different_sizes_are_refused(
        self, broker: AlpacaBroker, instrument: Instrument
    ) -> None:
        stop, target = self._legs(instrument)
        with pytest.raises(DataStaleError, match="one quantity"):
            await broker.submit_group((stop, target.model_copy(update={"qty": Decimal(9)})))


class TestStateMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("new", OrderState.OPEN),
            ("accepted", OrderState.OPEN),
            ("held", OrderState.OPEN),
            ("partially_filled", OrderState.PARTIALLY_FILLED),
            ("filled", OrderState.FILLED),
            ("canceled", OrderState.CANCELLED),
            ("expired", OrderState.EXPIRED),
            ("rejected", OrderState.REJECTED),
        ],
    )
    def test_alpaca_statuses_map_onto_the_lifecycle(
        self, status: str, expected: OrderState
    ) -> None:
        assert parse_state({"status": status}) is expected

    def test_a_pending_status_still_counts_as_working(self) -> None:
        """The venue may yet fill it, so the monitor must keep polling (DESIGN §6.7)."""
        assert parse_state({"status": "pending_new"}).is_open

    def test_an_unknown_status_fails_closed(self) -> None:
        with pytest.raises(DataStaleError, match="unknown order status"):
            parse_state({"status": "brand_new_status"})


class TestAccountParsing:
    def test_positions_and_cash_are_separate_unlike_a_spot_venue(
        self, instrument: Instrument
    ) -> None:
        state = parse_account(
            {"cash": "5000.25", "currency": "USD"},
            [{"symbol": "AAPL", "qty": "12", "avg_entry_price": "190.10"}],
            (instrument,),
            NOW,
        )
        assert state.qty(instrument.key) == Decimal(12)
        assert state.positions[0].avg_entry == Decimal("190.10")
        assert state.total("USD") == Decimal("5000.25")

    def test_an_untracked_symbol_is_ignored(self, instrument: Instrument) -> None:
        """A holding we do not trade is not ours to reconcile into a basket's position."""
        state = parse_account(
            {"cash": "0", "currency": "USD"},
            [{"symbol": "TSLA", "qty": "3", "avg_entry_price": "1"}],
            (instrument,),
            NOW,
        )
        assert state.positions == ()


class TestTheUniverseIsReadFresh:
    """The same rule as the crypto adapter, on the venue where a position is its own record.

    Alpaca reports positions directly, so nothing is double-counted here — but the symbol→
    instrument map is still what decides whether a position and its resting orders are *seen at
    all*, and a basket published while the process runs must not be invisible to reconciliation.
    """

    async def test_an_instrument_added_after_wiring_is_traded_and_seen(
        self, clock: ManualClock, instrument: Instrument, api: FakeAlpacaApi
    ) -> None:
        universe: list[Instrument] = []
        broker = AlpacaBroker(
            alpaca_transport(api, clock, mode=Mode.PAPER), clock, universe=lambda: tuple(universe)
        )
        universe.append(instrument)
        try:
            ack = await broker.submit(intent(instrument))

            assert ack.reject_reason is None
            assert [o.client_order_id for o in await broker.fetch_open_orders()] == ["pap-ENTRY"]
        finally:
            await broker.close()

    async def test_an_instrument_still_unknown_is_refused_by_name(
        self, clock: ManualClock, instrument: Instrument, api: FakeAlpacaApi
    ) -> None:
        """Fail closed stays fail closed: unknown precision and minimums are not tradable."""
        broker = AlpacaBroker(alpaca_transport(api, clock, mode=Mode.PAPER), clock, universe=tuple)
        try:
            with pytest.raises(DataStaleError, match="not configured on this alpaca adapter"):
                await broker.submit(intent(instrument))
        finally:
            await broker.close()


class TestCorporateActions:
    def test_a_three_for_one_split_becomes_a_ratio_of_three(self, instrument: Instrument) -> None:
        """Inverting this turns a split into a mismatch and halts a basket for a routine event."""
        action = parse_announcement(
            {
                "ca_type": "split",
                "target_symbol": "AAPL",
                "old_rate": "1",
                "new_rate": "3",
                "effective_date": "2026-07-29",
            },
            (instrument,),
        )
        assert action is not None
        assert action.ratio == Decimal(3)
        assert action.instrument_key == instrument.key
        assert action.effective_on == "2026-07-29"

    def test_a_reverse_split_halves_the_share_count(self, instrument: Instrument) -> None:
        action = parse_announcement(
            {
                "ca_type": "reverse_split",
                "target_symbol": "AAPL",
                "old_rate": "2",
                "new_rate": "1",
            },
            (instrument,),
        )
        assert action is not None
        assert action.ratio == Decimal("0.5")

    def test_a_cash_dividend_changes_no_share_count(self, instrument: Instrument) -> None:
        """It moves cash, not shares, so it must never explain a quantity difference."""
        assert (
            parse_announcement(
                {"ca_type": "cash_dividend", "target_symbol": "AAPL", "cash": "0.24"},
                (instrument,),
            )
            is None
        )

    def test_an_untracked_symbol_is_ignored(self, instrument: Instrument) -> None:
        assert (
            parse_announcement(
                {"ca_type": "split", "target_symbol": "TSLA", "old_rate": "1", "new_rate": "3"},
                (instrument,),
            )
            is None
        )

    def test_a_zero_old_rate_fails_closed(self, instrument: Instrument) -> None:
        """Dividing by it would be a crash mid-reconciliation, which halts the wrong thing."""
        with pytest.raises(DataStaleError, match="non-positive old_rate"):
            parse_announcement(
                {"ca_type": "split", "target_symbol": "AAPL", "old_rate": "0", "new_rate": "3"},
                (instrument,),
            )

    async def test_the_feed_is_queried_for_the_symbols_we_hold(
        self, clock: ManualClock, api: FakeAlpacaApi, instrument: Instrument
    ) -> None:
        announcements = AlpacaAnnouncements(alpaca_transport(api, clock))
        actions = await announcements.fetch(
            (instrument,), since=date(2026, 7, 23), until=date(2026, 7, 30)
        )
        assert [action.ratio for action in actions] == [Decimal(3)]
        assert api.requests[-1].url.params["symbol"] == "AAPL"

    async def test_no_instruments_means_no_call_at_all(
        self, clock: ManualClock, api: FakeAlpacaApi
    ) -> None:
        announcements = AlpacaAnnouncements(alpaca_transport(api, clock))
        assert await announcements.fetch((), since=date(2026, 7, 23), until=date(2026, 7, 30)) == ()
        assert not api.requests


class TestCalendar:
    async def test_the_market_is_open_during_the_session(
        self, clock: ManualClock, api: FakeAlpacaApi
    ) -> None:
        calendar = AlpacaCalendar(alpaca_transport(api, clock), clock)
        assert await calendar.is_open(NOW)

    async def test_the_market_is_closed_before_the_open(
        self, clock: ManualClock, api: FakeAlpacaApi
    ) -> None:
        calendar = AlpacaCalendar(alpaca_transport(api, clock), clock)
        before = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)  # 08:00 New York
        assert not await calendar.is_open(before)

    async def test_the_session_day_is_a_new_york_date_not_a_utc_one(
        self, clock: ManualClock, api: FakeAlpacaApi
    ) -> None:
        """A UTC rollover would reset the daily-loss baseline mid-session (DESIGN §6.6)."""
        calendar = AlpacaCalendar(alpaca_transport(api, clock), clock)
        after_hours = datetime(2026, 7, 31, 1, 0, tzinfo=UTC)  # 21:00 on the 30th in New York
        assert await calendar.session_day(after_hours) == "2026-07-30"

    async def test_the_next_open_is_found_when_the_market_is_shut(
        self, clock: ManualClock, api: FakeAlpacaApi
    ) -> None:
        calendar = AlpacaCalendar(alpaca_transport(api, clock), clock)
        shut = datetime(2026, 7, 30, 21, 0, tzinfo=UTC)  # 17:00 New York, after the close
        opens = await calendar.next_open(shut)
        assert opens is not None
        assert opens.astimezone(EXCHANGE_TZ).hour == 9

    async def test_an_open_market_has_no_next_open_to_wait_for(
        self, clock: ManualClock, api: FakeAlpacaApi
    ) -> None:
        calendar = AlpacaCalendar(alpaca_transport(api, clock), clock)
        assert await calendar.next_open(NOW) is None
