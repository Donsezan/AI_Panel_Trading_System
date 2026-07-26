"""The PortfolioAggregate, and the stablecoin peg check that guards it.

Every percentage-based risk limit is computed against equity, so valuing a depegged stablecoin
at 1.00 silently loosens all of them at exactly the moment the market is least safe.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Side
from tradebot.core.instrument import Instrument
from tradebot.core.orders import Fill
from tradebot.ledger.portfolio import Ledger
from tradebot.risk.aggregate import USD_STABLECOINS, aggregate, peg_deviation_pct


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


class TestAggregation:
    def test_venue_portfolios_sum_into_one_view(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        prices = {instrument.key: Decimal("50000")}
        ledgers = {"a": funded(clock, instrument), "b": funded(clock, instrument)}

        summary = aggregate(ledgers, (instrument,), prices, GlobalRiskPolicy(), as_of=clock.now())

        assert summary.gross_exposure == Decimal("10000")  # 2 × 0.1 × 50000
        assert {slice_.venue for slice_ in summary.venues} == {"a", "b"}

    def test_exposure_can_be_read_per_instrument_and_per_cluster(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        other = instrument.model_copy(update={"symbol": "ETH/USDT", "base_currency": "ETH"})
        prices = {instrument.key: Decimal("50000"), other.key: Decimal("3000")}
        ledger = funded(clock, instrument)
        ledger.apply_fill(
            Fill(
                fill_id="f2",
                client_order_id="sim-ETH",
                instrument_key=other.key,
                side=Side.BUY,
                qty=Decimal("1"),
                price=Decimal("3000"),
                filled_at=clock.now(),
            ),
            base_currency="ETH",
            quote_currency="USDT",
        )

        summary = aggregate(
            {"sim": ledger}, (instrument, other), prices, GlobalRiskPolicy(), as_of=clock.now()
        )

        assert summary.exposure_of(instrument.key) == Decimal("5000")
        assert summary.exposure_of(instrument.key, other.key) == Decimal("8000")

    def test_an_unknown_instrument_contributes_nothing_rather_than_raising(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            {instrument.key: Decimal("50000")},
            GlobalRiskPolicy(),
            as_of=clock.now(),
        )

        assert summary.exposure_of("sim:DOGE/USDT") == Decimal(0)

    def test_the_aggregate_is_immutable_in_substance_not_only_in_name(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """'The aggregate the decision saw' has to be a checkable claim."""
        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            {instrument.key: Decimal("50000")},
            GlobalRiskPolicy(),
            as_of=clock.now(),
        )

        assert isinstance(summary.per_instrument, tuple)


class TestPegCheck:
    def test_an_unquoted_stablecoin_reads_as_par(self) -> None:
        assert peg_deviation_pct({}, "USDT") == Decimal(0)

    def test_a_depeg_is_measured_in_percent(self) -> None:
        assert peg_deviation_pct({"USDT": Decimal("0.95")}, "USDT") == Decimal(5)

    def test_a_depegged_holding_freezes_the_aggregate(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            {instrument.key: Decimal("50000")},
            GlobalRiskPolicy(stablecoin_peg_tolerance_pct=Decimal(2)),
            as_of=clock.now(),
            stablecoin_prices={"USDT": Decimal("0.90")},
        )

        assert summary.frozen
        assert "off par" in summary.frozen_reason

    def test_a_small_wobble_does_not_freeze_it(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            {instrument.key: Decimal("50000")},
            GlobalRiskPolicy(stablecoin_peg_tolerance_pct=Decimal(2)),
            as_of=clock.now(),
            stablecoin_prices={"USDT": Decimal("0.995")},
        )

        assert not summary.frozen

    def test_a_currency_we_do_not_hold_cannot_freeze_the_aggregate(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            {instrument.key: Decimal("50000")},
            GlobalRiskPolicy(),
            as_of=clock.now(),
            stablecoin_prices={"DAI": Decimal("0.5")},
        )

        assert not summary.frozen


def test_the_stablecoin_list_is_a_named_assumption_not_a_hardcoded_price() -> None:
    """Par is assumed; the check above exists to falsify the assumption."""
    assert "USDT" in USD_STABLECOINS
    assert "BTC" not in USD_STABLECOINS
