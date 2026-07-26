"""Ledger: positions and balances driven by fills, and only by fills."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import Side
from tradebot.core.errors import ReconciliationMismatchError
from tradebot.core.orders import Fill
from tradebot.ledger.portfolio import Ledger

NOW = datetime(2026, 3, 1, tzinfo=UTC)
KEY = "sim:BTC/USDT"


def fill(side: Side, qty: str, price: str, fee: str = "0", fill_id: str = "f") -> Fill:
    return Fill(
        fill_id=fill_id,
        client_order_id="sim-ABC",
        instrument_key=KEY,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency="USDT",
        filled_at=NOW,
    )


def book(ledger: Ledger, f: Fill) -> None:
    ledger.apply_fill(f, base_currency="BTC", quote_currency="USDT")


class TestPositions:
    def test_absence_is_a_flat_position_not_none(self, ledger: Ledger) -> None:
        position = ledger.position("sim:NOTHING")
        assert position.is_flat
        assert position.qty == 0

    def test_a_buy_opens_a_position_at_its_fill_price(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "0.5", "50000"))
        position = ledger.position(KEY)
        assert position.qty == Decimal("0.5")
        assert position.avg_entry == Decimal("50000")
        assert position.opened_at == NOW

    def test_a_second_buy_averages_the_entry(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "100", fill_id="f1"))
        book(ledger, fill(Side.BUY, "1", "200", fill_id="f2"))
        assert ledger.position(KEY).avg_entry == Decimal("150")

    def test_partial_fills_accumulate_like_any_other_fill(self, ledger: Ledger) -> None:
        """Positions move on fills, never on an order reaching a terminal state (PLAN §2.5)."""
        for index in range(4):
            book(ledger, fill(Side.BUY, "0.25", "100", fill_id=f"f{index}"))
        assert ledger.position(KEY).qty == Decimal("1.00")

    def test_a_sell_realizes_pnl_and_leaves_the_entry_alone(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "100", fill_id="f1"))
        book(ledger, fill(Side.SELL, "0.5", "150", fill_id="f2"))
        position = ledger.position(KEY)
        assert position.qty == Decimal("0.5")
        assert position.realized_pnl == Decimal("25")
        assert position.avg_entry == Decimal("100")

    def test_closing_out_resets_the_entry(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "100", fill_id="f1"))
        book(ledger, fill(Side.SELL, "1", "120", fill_id="f2"))
        position = ledger.position(KEY)
        assert position.is_flat
        assert position.avg_entry == 0
        assert position.realized_pnl == Decimal("20")
        assert position.opened_at is None

    def test_selling_more_than_held_is_ledger_corruption_not_a_short(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "0.5", "100", fill_id="f1"))
        with pytest.raises(ReconciliationMismatchError, match="long-only"):
            book(ledger, fill(Side.SELL, "1", "100", fill_id="f2"))


class TestBalances:
    def test_a_buy_spends_quote_and_gains_base(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "0.1", "50000", fee="5"))
        assert ledger.balance("USDT") == Decimal("4995")  # 10000 − 5000 − 5 fee
        assert ledger.balance("BTC") == Decimal("0.1")

    def test_a_sell_returns_quote_less_fees(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "0.1", "50000", fill_id="f1"))
        book(ledger, fill(Side.SELL, "0.1", "60000", fee="6", fill_id="f2"))
        assert ledger.balance("USDT") == Decimal("10994")  # 10000 − 5000 + 6000 − 6
        assert ledger.balance("BTC") == Decimal("0.0")


class TestValuation:
    def test_equity_is_cash_plus_marked_holdings(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "0.1", "50000"))
        equity = ledger.equity({KEY: Decimal("60000")}, quote_currency="USDT")
        assert equity == Decimal("11000")  # 5000 cash + 0.1 × 60000

    def test_an_unpriced_holding_is_valued_at_cost_not_dropped(self, ledger: Ledger) -> None:
        """Dropping it would understate exposure and quietly loosen every percentage limit."""
        book(ledger, fill(Side.BUY, "0.1", "50000"))
        assert ledger.equity({}, quote_currency="USDT") == Decimal("10000")

    def test_exposure_sums_the_named_instruments(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "0.1", "50000"))
        assert ledger.exposure((KEY,), {KEY: Decimal("50000")}) == Decimal("5000")
        assert ledger.exposure(("sim:OTHER",), {}) == 0

    def test_unrealized_pnl_percent_is_on_cost_basis(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "100"))
        assert ledger.position(KEY).unrealized_pnl_pct(Decimal("110")) == Decimal("10")

    def test_flat_position_reports_zero_pnl_percent(self, ledger: Ledger) -> None:
        assert ledger.position(KEY).unrealized_pnl_pct(Decimal("100")) == 0


class TestHoldingPeriod:
    def test_a_position_records_when_it_left_flat(self, ledger: Ledger) -> None:
        """How long it has been held derives from this; a counter would reset on every restart."""
        assert ledger.position(KEY).opened_at is None

        book(ledger, fill(Side.BUY, "1", "100"))

        assert ledger.position(KEY).opened_at == NOW

    def test_adding_to_a_position_keeps_its_original_opening(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "100", fill_id="b1"))
        book(ledger, fill(Side.BUY, "1", "110", fill_id="b2"))

        assert ledger.position(KEY).opened_at == NOW

    def test_closing_a_position_clears_its_opening(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "100", fill_id="b1"))
        book(ledger, fill(Side.SELL, "1", "100", fill_id="s1"))

        assert ledger.position(KEY).opened_at is None


class TestSnapshot:
    def test_snapshot_is_a_read_only_view_of_venue_shape(
        self, ledger: Ledger, clock: ManualClock
    ) -> None:
        book(ledger, fill(Side.BUY, "0.1", "50000"))
        state = ledger.snapshot()
        assert state.venue == "sim"
        assert state.observed_at == clock.now()
        assert state.position(KEY) is not None
        assert state.balance("USDT") is not None
        assert state.balance("EUR") is None


class TestRoundTrips:
    def test_a_position_returning_to_flat_closes_a_round_trip(self, ledger: Ledger) -> None:
        """The unit the consecutive-loss rule counts — the only unit where 'was that a loss'
        has an answer."""
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))
        booking = ledger.apply_fill(
            fill(Side.SELL, "1", "51000", fill_id="s1"),
            base_currency="BTC",
            quote_currency="USDT",
        )

        trip = booking.round_trip
        assert trip is not None
        assert trip.realized_pnl == Decimal("1000")
        assert not trip.is_loss

    def test_a_partial_exit_does_not_close_the_trip(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))
        booking = ledger.apply_fill(
            fill(Side.SELL, "0.4", "51000", fill_id="s1"),
            base_currency="BTC",
            quote_currency="USDT",
        )

        assert booking.round_trip is None

    def test_partial_fills_in_and_out_aggregate_into_one_trip(self, ledger: Ledger) -> None:
        """Counting fills instead would auto-pause a basket for a scratch exit split in three."""
        book(ledger, fill(Side.BUY, "0.5", "50000", fill_id="b1"))
        book(ledger, fill(Side.BUY, "0.5", "52000", fill_id="b2"))
        book(ledger, fill(Side.SELL, "0.6", "53000", fill_id="s1"))
        booking = ledger.apply_fill(
            fill(Side.SELL, "0.4", "53000", fill_id="s2"),
            base_currency="BTC",
            quote_currency="USDT",
        )

        trip = booking.round_trip
        assert trip is not None
        assert trip.qty == Decimal("1")
        assert trip.entry_price == Decimal("51000")
        assert trip.realized_pnl == Decimal("2000")

    def test_fees_count_against_the_trip_on_both_legs(self, ledger: Ledger) -> None:
        """A scratch exit that paid two commissions is a losing trade, and must read as one."""
        book(ledger, fill(Side.BUY, "1", "50000", fee="25", fill_id="b1"))
        booking = ledger.apply_fill(
            fill(Side.SELL, "1", "50000", fee="25", fill_id="s1"),
            base_currency="BTC",
            quote_currency="USDT",
        )

        trip = booking.round_trip
        assert trip is not None
        assert trip.realized_pnl == Decimal("-50")
        assert trip.is_loss

    def test_a_new_position_starts_a_new_trip(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))
        book(ledger, fill(Side.SELL, "1", "49000", fill_id="s1"))
        book(ledger, fill(Side.BUY, "1", "48000", fill_id="b2"))
        booking = ledger.apply_fill(
            fill(Side.SELL, "1", "49000", fill_id="s2"),
            base_currency="BTC",
            quote_currency="USDT",
        )

        trip = booking.round_trip
        assert trip is not None
        assert trip.realized_pnl == Decimal("1000"), "the previous trip's loss is not carried"


class TestExternalFlows:
    def test_a_deposit_moves_the_balance(self, ledger: Ledger) -> None:
        from tradebot.ledger.portfolio import ExternalFlow

        total = ledger.apply_external_change(
            ExternalFlow(currency="USDT", amount=Decimal("5000"), reason="deposit")
        )

        assert total == Decimal("15000")

    def test_a_withdrawal_is_recognisable_as_one(self) -> None:
        from tradebot.ledger.portfolio import ExternalFlow

        assert ExternalFlow(currency="USDT", amount=Decimal("-1")).is_withdrawal


class TestVenueAdoption:
    def test_a_venue_position_replaces_ours(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))

        from tradebot.core.portfolio import Position

        adopted = ledger.adopt_position(
            Position(instrument_key=KEY, qty=Decimal("0.9"), avg_entry=Decimal("50000"))
        )

        assert adopted.qty == Decimal("0.9")
        assert ledger.position(KEY).qty == Decimal("0.9")

    def test_locked_funds_are_reported_separately_from_free(self, ledger: Ledger) -> None:
        ledger.set_locked("USDT", Decimal("2500"))

        balance = ledger.snapshot().balance("USDT")
        assert balance is not None
        assert balance.free == Decimal("7500")
        assert balance.locked == Decimal("2500")
        assert balance.total == Decimal("10000")


class TestReplay:
    async def test_the_ledger_rebuilds_from_the_event_log_alone(
        self, ledger: Ledger, store, clock: ManualClock
    ) -> None:
        from tradebot.core.events import EventFactory

        events = EventFactory(clock=clock, basket_id="b1", cycle_id="c1")
        booking = ledger.apply_fill(
            fill(Side.BUY, "1", "50000", fill_id="b1"),
            base_currency="BTC",
            quote_currency="USDT",
        )
        await store.append(
            events.fill_received(fill(Side.BUY, "1", "50000", fill_id="b1"), _stub_order()),
            events.position_updated(booking.position),
        )

        fresh = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        applied = fresh.replay(store.read_all(), {KEY: ("BTC", "USDT")})

        assert applied == 1
        assert fresh.position(KEY).qty == Decimal("1")
        assert fresh.balance("USDT") == ledger.balance("USDT")

    async def test_an_external_change_replays_too(
        self, ledger: Ledger, store, clock: ManualClock
    ) -> None:
        from tradebot.core.events import EventFactory

        events = EventFactory(clock=clock, basket_id="b1", cycle_id="c1")
        await store.append(events.external_change("USDT", Decimal("2500"), "deposit"))

        fresh = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        fresh.replay(store.read_all(), {KEY: ("BTC", "USDT")})

        assert fresh.balance("USDT") == Decimal("12500")

    async def test_replaying_a_fill_on_an_unknown_instrument_raises(
        self, ledger: Ledger, store, clock: ManualClock
    ) -> None:
        """Silently skipping it would leave a ledger that quietly disagrees with the log."""
        from tradebot.core.events import EventFactory

        events = EventFactory(clock=clock, basket_id="b1", cycle_id="c1")
        await store.append(
            events.fill_received(fill(Side.BUY, "1", "50000", fill_id="b1"), _stub_order())
        )

        with pytest.raises(ReconciliationMismatchError, match="unknown instrument"):
            Ledger(clock, venue="sim", balances={}).replay(store.read_all(), {})


def _stub_order():
    from tradebot.core.enums import OrderType
    from tradebot.core.orders import Order, OrderIntent

    return Order.from_intent(
        OrderIntent(
            client_order_id="sim-ABC",
            basket_id="b1",
            cycle_id="c1",
            instrument_key=KEY,
            side=Side.BUY,
            qty=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("50000"),
            created_at=NOW,
        )
    )


class TestMarkToMarket:
    def test_unrealized_pnl_marks_every_holding(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))

        assert ledger.unrealized_pnl({KEY: Decimal("51000")}) == Decimal("1000")

    def test_an_unpriced_holding_is_marked_at_cost_not_dropped(self, ledger: Ledger) -> None:
        """Dropping it would understate exposure and loosen every percentage-based limit."""
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))

        assert ledger.unrealized_pnl({}) == Decimal(0)

    def test_realized_pnl_sums_across_instruments(self, ledger: Ledger) -> None:
        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))
        book(ledger, fill(Side.SELL, "1", "51000", fill_id="s1"))

        assert ledger.realized_pnl() == Decimal("1000")

    def test_adopting_a_flat_position_abandons_its_open_round_trip(self, ledger: Ledger) -> None:
        """After a venue reset there is no trip to close; carrying one forward would invent PnL."""
        from tradebot.core.portfolio import Position

        book(ledger, fill(Side.BUY, "1", "50000", fill_id="b1"))
        ledger.adopt_position(Position(instrument_key=KEY))

        book(ledger, fill(Side.BUY, "1", "48000", fill_id="b2"))
        booking = ledger.apply_fill(
            fill(Side.SELL, "1", "49000", fill_id="s1"),
            base_currency="BTC",
            quote_currency="USDT",
        )

        trip = booking.round_trip
        assert trip is not None
        assert trip.entry_price == Decimal("48000")
