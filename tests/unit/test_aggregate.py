"""The PortfolioAggregate: the one answer to "what is this portfolio worth".

Every percentage-based risk limit is computed against equity, so three things must hold at once:
equity is **mark-to-market** and never cost basis (PHASE_12 Finding 1), **every** balance is
valued or freezes rather than being silently worth zero (Finding 3), and portfolio-wide numbers
span the configured universe rather than one basket (Finding 6).

A depegged stablecoin valued at 1.00 overstates equity and loosens every limit at exactly the
moment the market is least safe, which is what the peg check exists to catch — and it is fed a
real price here for the first time (Finding 5).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Side
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.orders import Fill
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import Ledger
from tradebot.risk.aggregate import USD_STABLECOINS, aggregate, peg_deviation_pct, value_cash


def funded(clock: ManualClock, instrument: Instrument, *, qty: str = "0.1") -> Ledger:
    ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})
    ledger.apply_fill(
        Fill(
            fill_id="f1",
            client_order_id="sim-ENTRY",
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=Decimal(qty),
            price=Decimal("50000"),
            filled_at=clock.now(),
        ),
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
    )
    return ledger


def marked(clock: ManualClock, **prices: str) -> Marks:
    """Marks keyed by instrument key, written as `sim__BTC_USDT="50000"`."""
    marks = Marks()
    for key, price in prices.items():
        marks.observe(key.replace("__", ":").replace("_", "/"), Decimal(price), clock.now())
    return marks


def summarise(
    ledgers: dict[str, Ledger],
    universe: tuple[Instrument, ...],
    marks: Marks,
    clock: ManualClock,
    policy: GlobalRiskPolicy | None = None,
):
    return aggregate(
        ledgers,
        universe,
        marks,
        policy or GlobalRiskPolicy(),
        as_of=clock.now(),
        notional_currency="USDT",
    )


class TestAggregation:
    def test_venue_portfolios_sum_into_one_view(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        marks = marked(clock, sim__BTC_USDT="50000")
        ledgers = {"a": funded(clock, instrument), "b": funded(clock, instrument)}

        summary = summarise(ledgers, (instrument,), marks, clock)

        assert summary.gross_exposure == Decimal("10000")  # 2 × 0.1 × 50000
        assert {slice_.venue for slice_ in summary.venues} == {"a", "b"}

    def test_exposure_can_be_read_per_instrument_and_per_cluster(
        self, clock: ManualClock, instrument: Instrument, second_instrument: Instrument
    ) -> None:
        ledger = funded(clock, instrument)
        ledger.apply_fill(
            Fill(
                fill_id="f2",
                client_order_id="sim-ETH",
                instrument_key=second_instrument.key,
                side=Side.BUY,
                qty=Decimal("1"),
                price=Decimal("3000"),
                filled_at=clock.now(),
            ),
            base_currency="ETH",
            quote_currency="USDT",
        )
        marks = marked(clock, sim__BTC_USDT="50000", sim__ETH_USDT="3000")

        summary = summarise({"sim": ledger}, (instrument, second_instrument), marks, clock)

        assert summary.exposure_of(instrument.key) == Decimal("5000")
        assert summary.exposure_of(instrument.key, second_instrument.key) == Decimal("8000")

    def test_an_unknown_instrument_contributes_nothing_rather_than_raising(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marked(clock, sim__BTC_USDT="50000"),
            clock,
        )

        assert summary.exposure_of("sim:DOGE/USDT") == Decimal(0)

    def test_the_aggregate_is_immutable_in_substance_not_only_in_name(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """'The aggregate the decision saw' has to be a checkable claim."""
        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marked(clock, sim__BTC_USDT="50000"),
            clock,
        )

        assert isinstance(summary.per_instrument, tuple)


class TestMarkToMarket:
    def test_equity_is_cash_plus_marks_not_cost(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Finding 1: 10,000 USDT, 0.1 BTC bought at 50,000, BTC halves to 25,000."""
        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marked(clock, sim__BTC_USDT="25000"),
            clock,
        )

        assert summary.equity == Decimal("7500")  # 5000 cash + 0.1 × 25000
        assert not summary.frozen

    def test_a_stale_mark_freezes_rather_than_falling_back_to_cost(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        marks = marked(clock, sim__BTC_USDT="25000")
        ledger = funded(clock, instrument)
        clock.set(clock.now() + timedelta(minutes=6))

        summary = summarise({"sim": ledger}, (instrument,), marks, clock)

        assert summary.frozen
        assert instrument.key in summary.frozen_reason

    def test_an_unmarked_position_freezes(self, clock: ManualClock, instrument: Instrument) -> None:
        summary = summarise({"sim": funded(clock, instrument)}, (instrument,), Marks(), clock)

        assert summary.frozen
        assert instrument.key in summary.frozen_reason

    def test_a_flat_portfolio_never_freezes_and_needs_no_marks(self, clock: ManualClock) -> None:
        """A fresh database and the seeded demo must run with no venue call at all."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})

        summary = summarise({"sim": ledger}, (), Marks(), clock)

        assert not summary.frozen
        assert summary.equity == Decimal(10_000)

    def test_non_quote_cash_is_valued_rather_than_worth_nothing(self, clock: ManualClock) -> None:
        """Finding 3: 1,000 USDT + 9,000 USDC used to value at 1,000."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(1000), "USDC": Decimal(9000)})

        summary = summarise({"sim": ledger}, (), Marks(), clock)

        assert summary.equity == Decimal(10_000)
        assert summary.cash == Decimal(10_000)

    def test_an_unvaluable_balance_freezes_and_names_the_currency(self, clock: ManualClock) -> None:
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(1000), "DOGE": Decimal(50)})

        summary = summarise({"sim": ledger}, (), Marks(), clock)

        assert summary.frozen
        assert "DOGE" in summary.frozen_reason

    def test_a_zero_balance_in_an_unvaluable_currency_does_not_freeze(
        self, clock: ManualClock
    ) -> None:
        """Dust that has been fully converted away is not a reason to stop trading."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(1000), "DOGE": ZERO})

        summary = summarise({"sim": ledger}, (), Marks(), clock)

        assert not summary.frozen

    def test_an_unvaluable_balance_is_valued_once_it_has_a_mark(self, clock: ManualClock) -> None:
        """Rung 4: the sweep resolves `{CUR}/{notional}` and the freeze clears on its own."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(1000), "DOGE": Decimal(50)})
        marks = Marks()
        marks.observe("DOGE", Decimal("2"), clock.now())

        summary = summarise({"sim": ledger}, (), marks, clock)

        assert not summary.frozen
        assert summary.equity == Decimal(1100)  # 1000 + 50 × 2

    def test_a_base_asset_balance_is_not_counted_twice(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """`funded` leaves 0.1 BTC as both a position and a balance."""
        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marked(clock, sim__BTC_USDT="50000"),
            clock,
        )

        assert summary.equity == Decimal(10_000)  # 5000 cash + 0.1 × 50000, counted once

    def test_a_frozen_aggregate_reports_no_exposure_numbers(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """ "We do not know what this is worth" is not compatible with quoting a figure."""
        summary = summarise({"sim": funded(clock, instrument)}, (instrument,), Marks(), clock)

        assert summary.equity == ZERO
        assert summary.gross_exposure == ZERO
        assert summary.per_instrument == ()


class TestGrossExposureSpansTheUniverse:
    def test_exposure_covers_every_configured_instrument_not_one_baskets(
        self, clock: ManualClock, instrument: Instrument, second_instrument: Instrument
    ) -> None:
        """Finding 6: `gross_exposure` used to omit every sibling basket's positions."""
        ledger = funded(clock, instrument)
        ledger.apply_fill(
            Fill(
                fill_id="f2",
                client_order_id="sim-ETH",
                instrument_key=second_instrument.key,
                side=Side.BUY,
                qty=Decimal("1"),
                price=Decimal("3000"),
                filled_at=clock.now(),
            ),
            base_currency="ETH",
            quote_currency="USDT",
        )
        marks = marked(clock, sim__BTC_USDT="50000", sim__ETH_USDT="3000")

        summary = summarise({"sim": ledger}, (instrument, second_instrument), marks, clock)

        assert summary.gross_exposure == Decimal("8000")  # 5000 + 3000


class TestPegCheck:
    def test_an_unquoted_stablecoin_reads_as_par(self) -> None:
        assert peg_deviation_pct({}, "USDT") == Decimal(0)

    def test_a_depeg_is_measured_in_percent(self) -> None:
        assert peg_deviation_pct({"USDT": Decimal("0.95")}, "USDT") == Decimal(5)

    def test_a_depegged_holding_freezes_the_aggregate(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        marks = marked(clock, sim__BTC_USDT="50000")
        marks.observe("USDT", Decimal("0.90"), clock.now())

        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marks,
            clock,
            GlobalRiskPolicy(stablecoin_peg_tolerance_pct=Decimal(2)),
        )

        assert summary.frozen
        assert "off par" in summary.frozen_reason

    def test_a_small_wobble_does_not_freeze_it(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        marks = marked(clock, sim__BTC_USDT="50000")
        marks.observe("USDT", Decimal("0.995"), clock.now())

        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marks,
            clock,
            GlobalRiskPolicy(stablecoin_peg_tolerance_pct=Decimal(2)),
        )

        assert not summary.frozen

    def test_a_currency_we_do_not_hold_cannot_freeze_the_aggregate(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        marks = marked(clock, sim__BTC_USDT="50000")
        marks.observe("DAI", Decimal("0.5"), clock.now())

        summary = summarise({"sim": funded(clock, instrument)}, (instrument,), marks, clock)

        assert not summary.frozen

    def test_an_unmarked_stablecoin_still_reads_as_par(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """The assumption stands until a quote falsifies it — that is what the list documents."""
        summary = summarise(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marked(clock, sim__BTC_USDT="50000"),
            clock,
        )

        assert not summary.frozen


def test_the_stablecoin_list_is_a_named_assumption_not_a_hardcoded_price() -> None:
    """Par is assumed; the check above exists to falsify the assumption."""
    assert "USDT" in USD_STABLECOINS
    assert "BTC" not in USD_STABLECOINS


class TestValueCash:
    """The four rungs, in order. Rung 3 before rung 4 is what stops a double count."""

    def _value(
        self,
        currency: str,
        amount: Decimal,
        *,
        clock: ManualClock,
        marks: Marks | None = None,
        position_currencies: frozenset[str] = frozenset(),
    ) -> Decimal | None:
        return value_cash(
            currency,
            amount,
            marks or Marks(),
            notional_currency="USDT",
            position_currencies=position_currencies,
            now=clock.now(),
            tolerance=timedelta(minutes=5),
        )

    def test_rung_1_the_notional_currency_is_face_value(self, clock: ManualClock) -> None:
        assert self._value("USDT", Decimal(1000), clock=clock) == Decimal(1000)

    def test_rung_2_a_usd_stablecoin_is_par(self, clock: ManualClock) -> None:
        """Finding 3: 9,000 USDC used to contribute nothing at all."""
        assert self._value("USDC", Decimal(9000), clock=clock) == Decimal(9000)

    def test_rung_3_a_configured_base_asset_is_already_a_position(self, clock: ManualClock) -> None:
        assert self._value(
            "BTC", Decimal("0.1"), clock=clock, position_currencies=frozenset({"BTC"})
        ) == Decimal(0)

    def test_rung_3_precedes_rung_4_so_a_holding_is_never_counted_twice(
        self, clock: ManualClock
    ) -> None:
        marks = Marks()
        marks.observe("BTC", Decimal("50000"), clock.now())

        assert self._value(
            "BTC",
            Decimal("0.1"),
            clock=clock,
            marks=marks,
            position_currencies=frozenset({"BTC"}),
        ) == Decimal(0)

    def test_rung_4_an_unconfigured_currency_is_valued_at_its_mark(
        self, clock: ManualClock
    ) -> None:
        marks = Marks()
        marks.observe("DOGE", Decimal("0.5"), clock.now())

        assert self._value("DOGE", Decimal(100), clock=clock, marks=marks) == Decimal(50)

    def test_rung_5_an_unmarked_currency_has_no_admissible_valuation(
        self, clock: ManualClock
    ) -> None:
        assert self._value("DOGE", Decimal(100), clock=clock) is None

    def test_a_stale_currency_mark_is_no_valuation_rather_than_an_old_one(
        self, clock: ManualClock
    ) -> None:
        marks = Marks()
        marks.observe("DOGE", Decimal("0.5"), clock.now())
        clock.set(clock.now() + timedelta(minutes=6))

        assert self._value("DOGE", Decimal(100), clock=clock, marks=marks) is None
