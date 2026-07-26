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
    def test_held_cycles_advance_only_while_a_position_exists(self, ledger: Ledger) -> None:
        ledger.mark_cycle_held(KEY)
        assert ledger.position(KEY).held_cycles == 0

        book(ledger, fill(Side.BUY, "1", "100"))
        ledger.mark_cycle_held(KEY)
        ledger.mark_cycle_held(KEY)
        assert ledger.position(KEY).held_cycles == 2


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
