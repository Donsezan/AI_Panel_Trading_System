"""Domain models: the boundary that refuses bad data before it becomes an order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.core.config import (
    Basket,
    CorrelationCluster,
    GlobalRiskPolicy,
    PanelConfig,
    RiskPolicy,
    Schedule,
    SeatConfig,
)
from tradebot.core.decision import Decision, SeatResponse, SeatVote
from tradebot.core.enums import (
    Action,
    AssetClass,
    OrderRole,
    OrderState,
    OrderType,
    RiskDecision,
    Side,
    SizeHint,
)
from tradebot.core.errors import (
    ConfigError,
    DataStaleError,
    IllegalTransitionError,
    MoneyError,
)
from tradebot.core.instrument import Instrument, base_currencies_of
from tradebot.core.market import Candle, CandleSeries, Quote
from tradebot.core.orders import Fill, Order, OrderIntent, RiskCheckResult, assert_legal_transition
from tradebot.core.portfolio import Position
from tradebot.core.schema import canonical_json

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def candle(offset: int = 0, close: str = "100") -> Candle:
    return Candle(
        open_time=NOW + timedelta(hours=offset),
        close_time=NOW + timedelta(hours=offset + 1),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal("5"),
    )


class TestMoneyFields:
    def test_float_is_refused_at_the_model_boundary(self, instrument: Instrument) -> None:
        """pydantic would coerce float→Decimal silently; `Money` refuses instead."""
        with pytest.raises(MoneyError, match="float is not accepted"):
            instrument.model_copy(update={"lot_size": 0.001}).model_validate(
                {**instrument.model_dump(), "lot_size": 0.001}
            )

    @pytest.mark.parametrize("typed", ["0,5", "10%", "1 000", "abc", ""])
    def test_unreadable_text_is_a_located_validation_error(
        self, instrument: Instrument, typed: str
    ) -> None:
        """The opposite of a float: an operator typed it, so it must be catchable and located.

        pydantic converts only `ValueError`, and `MoneyError` is not one — before `parse_money`
        this escaped the model and reached the dashboard as a 500 naming no field.
        """
        with pytest.raises(ValidationError) as caught:
            Instrument.model_validate({**instrument.model_dump(), "lot_size": typed})

        assert [error["loc"] for error in caught.value.errors()] == [("lot_size",)]
        assert "not a valid decimal amount" in caught.value.errors()[0]["msg"]

    def test_decimal_survives_a_json_round_trip_exactly(self, instrument: Instrument) -> None:
        restored = Instrument.model_validate_json(instrument.model_dump_json())
        assert restored.lot_size == instrument.lot_size
        assert restored == instrument

    def test_money_serializes_as_a_string_not_a_number(self, instrument: Instrument) -> None:
        """JSON has no decimal type; a numeric would round-trip through float."""
        assert '"lot_size":"0.00001"' in instrument.model_dump_json()


class TestImmutability:
    def test_models_are_frozen(self, instrument: Instrument) -> None:
        with pytest.raises(ValidationError):
            instrument.symbol = "ETH/USDT"  # type: ignore[misc]

    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Position(instrument_key="x", surprise=1)  # type: ignore[call-arg]


class TestTime:
    def test_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="naive datetime"):
            Quote(
                instrument_key="k",
                bid=Decimal(1),
                ask=Decimal(2),
                last=Decimal(1),
                observed_at=datetime(2026, 3, 1, 12, 0),
            )


class TestInstrument:
    def test_key_identifies_venue_and_symbol(self, instrument: Instrument) -> None:
        assert instrument.key == "sim:BTC/USDT"

    def test_trading_rules_mirror_venue_precision(self, instrument: Instrument) -> None:
        rules = instrument.trading_rules
        assert rules.lot_size == instrument.lot_size
        assert rules.min_notional == instrument.min_notional


class TestCandleSeries:
    def test_rejects_high_below_low(self) -> None:
        with pytest.raises(ValidationError, match="below low"):
            candle().model_copy(update={"high": Decimal("1")}).model_validate(
                {**candle().model_dump(), "high": "1"}
            )

    def test_rejects_out_of_order_candles(self) -> None:
        with pytest.raises(ValidationError, match="oldest first"):
            CandleSeries(
                instrument_key="k",
                timeframe="1h",
                candles=(candle(2), candle(0)),
                observed_at=NOW,
            )

    def test_stale_series_fails_closed(self) -> None:
        """The bar closed at `NOW`; five minutes later it is fresh, two hours later it is not."""
        series = CandleSeries(
            instrument_key="k", timeframe="1h", candles=(candle(-1),), observed_at=NOW
        )
        series.require_fresh(NOW + timedelta(minutes=5), timedelta(minutes=15))
        with pytest.raises(DataStaleError, match="old, limit"):
            series.require_fresh(NOW + timedelta(hours=2), timedelta(minutes=15))

    def test_a_bar_closing_after_the_cycle_is_a_look_ahead_leak(self) -> None:
        """Both providers cut at the cutoff, so a future bar means two clocks — and a backtest
        built on one of them would be quietly meaningless ([L12])."""
        series = CandleSeries(
            instrument_key="k", timeframe="1h", candles=(candle(),), observed_at=NOW
        )

        with pytest.raises(DataStaleError, match="look-ahead leak"):
            series.require_fresh(NOW + timedelta(minutes=5), timedelta(minutes=15))


class TestQuote:
    def test_rejects_a_crossed_book(self) -> None:
        with pytest.raises(ValidationError, match="crossed quote"):
            Quote(
                instrument_key="k",
                bid=Decimal(10),
                ask=Decimal(9),
                last=Decimal(10),
                observed_at=NOW,
            )


class TestSeatVote:
    def test_tradable_action_requires_a_size(self) -> None:
        with pytest.raises(ValidationError, match="requires a size_hint"):
            SeatVote(action=Action.BUY, conviction=4, size_hint=SizeHint.NONE, thesis="t")

    def test_non_tradable_action_must_not_carry_a_size(self) -> None:
        with pytest.raises(ValidationError, match="must carry size_hint none"):
            SeatVote(action=Action.HOLD, conviction=3, size_hint=SizeHint.HALF, thesis="t")

    def test_seat_cannot_claim_abstain(self) -> None:
        """ABSTAIN is assigned by the panel when a seat fails, never self-reported."""
        with pytest.raises(ValidationError, match="assigned by the panel"):
            SeatVote(action=Action.ABSTAIN, conviction=1, size_hint=SizeHint.NONE, thesis="t")

    @pytest.mark.parametrize("conviction", [0, 6, -1])
    def test_conviction_stays_on_the_1_to_5_scale(self, conviction: int) -> None:
        with pytest.raises(ValidationError):
            SeatVote(action=Action.HOLD, conviction=conviction, size_hint=SizeHint.NONE, thesis="t")

    def test_thesis_length_is_bounded(self) -> None:
        with pytest.raises(ValidationError, match="exceeds 200 words"):
            SeatVote(
                action=Action.HOLD,
                conviction=3,
                size_hint=SizeHint.NONE,
                thesis=" ".join(["word"] * 201),
            )


class TestSeatResponse:
    def test_an_abstention_must_carry_a_reason(self) -> None:
        with pytest.raises(ValidationError, match="either a vote or an abstain_reason"):
            SeatResponse(
                seat_id="s",
                role="r",
                provider_id="p",
                model="m",
                round_index=0,
                instrument_key="k",
                responded_at=NOW,
            )


class TestDecision:
    def test_conviction_is_bounded_to_the_0_to_1_scale(self) -> None:
        with pytest.raises(ValidationError, match="conviction must be"):
            Decision(instrument_key="k", action=Action.BUY, conviction=Decimal("1.5"))

    @pytest.mark.parametrize(
        ("action", "hint", "actionable"),
        [
            (Action.BUY, SizeHint.HALF, True),
            (Action.BUY, SizeHint.NONE, False),
            (Action.HOLD, SizeHint.NONE, False),
            (Action.WAIT, SizeHint.NONE, False),
        ],
    )
    def test_actionability(self, action: Action, hint: SizeHint, actionable: bool) -> None:
        decision = Decision(instrument_key="k", action=action, size_hint=hint)
        assert decision.is_actionable is actionable


class TestOrderIntent:
    def _intent(self, **overrides: object) -> OrderIntent:
        base: dict[str, object] = {
            "client_order_id": "sim-ABC",
            "basket_id": "b1",
            "cycle_id": "c1",
            "instrument_key": "sim:BTC/USDT",
            "side": Side.BUY,
            "qty": Decimal("0.5"),
            "order_type": OrderType.LIMIT,
            "limit_price": Decimal("50000"),
            "created_at": NOW,
        }
        return OrderIntent(**{**base, **overrides})  # type: ignore[arg-type]

    def test_rejects_non_positive_quantity(self) -> None:
        with pytest.raises(ValidationError, match="must be positive"):
            self._intent(qty=Decimal(0))

    def test_limit_order_requires_a_price(self) -> None:
        with pytest.raises(ValidationError, match="requires a limit_price"):
            self._intent(limit_price=None)

    def test_a_vetoed_proposal_can_never_become_an_intent(self) -> None:
        """The last structural guard: a veto in provenance means there is no order."""
        with pytest.raises(ValidationError, match="vetoed proposal"):
            self._intent(
                risk_checks=(RiskCheckResult(rule="r", decision=RiskDecision.VETO, detail="nope"),)
            )

    def test_notional_is_qty_times_price(self) -> None:
        assert self._intent().notional == Decimal("25000.0")


class TestOrderLifecycle:
    def _order(self) -> Order:
        return Order(
            client_order_id="sim-ABC",
            basket_id="b1",
            cycle_id="c1",
            instrument_key="sim:BTC/USDT",
            side=Side.BUY,
            qty=Decimal("1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("100"),
            created_at=NOW,
            updated_at=NOW,
        )

    def _fill(self, qty: str, fill_id: str = "f1") -> Fill:
        return Fill(
            fill_id=fill_id,
            client_order_id="sim-ABC",
            instrument_key="sim:BTC/USDT",
            side=Side.BUY,
            qty=Decimal(qty),
            price=Decimal("100"),
            filled_at=NOW,
        )

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (OrderState.PENDING_SUBMIT, OrderState.SUBMITTED),
            (OrderState.PENDING_SUBMIT, OrderState.SUBMIT_UNKNOWN),
            (OrderState.SUBMIT_UNKNOWN, OrderState.FILLED),
            (OrderState.SUBMIT_UNKNOWN, OrderState.FAILED),
            (OrderState.OPEN, OrderState.PARTIALLY_FILLED),
            (OrderState.PARTIALLY_FILLED, OrderState.FILLED),
        ],
    )
    def test_legal_transitions(self, start: OrderState, target: OrderState) -> None:
        assert_legal_transition(start, target)

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (OrderState.PENDING_SUBMIT, OrderState.FILLED),
            (OrderState.FILLED, OrderState.OPEN),
            (OrderState.CANCELLED, OrderState.FILLED),
            (OrderState.SUBMITTED, OrderState.PENDING_SUBMIT),
            # There is no path back from SUBMIT_UNKNOWN to submitting again.
            (OrderState.SUBMIT_UNKNOWN, OrderState.PENDING_SUBMIT),
        ],
    )
    def test_illegal_transitions_raise_rather_than_log(
        self, start: OrderState, target: OrderState
    ) -> None:
        with pytest.raises(IllegalTransitionError):
            assert_legal_transition(start, target)

    def test_partial_then_complete_fill(self) -> None:
        order = self._order().transition_to(OrderState.SUBMITTED, at=NOW)
        order = order.with_fill(self._fill("0.4"))
        assert order.state is OrderState.PARTIALLY_FILLED
        assert order.remaining_qty == Decimal("0.6")
        assert order.fill_ratio == Decimal("0.4")

        order = order.with_fill(self._fill("0.6", "f2"))
        assert order.state is OrderState.FILLED
        assert order.filled_qty == Decimal("1")
        assert order.avg_fill_price == Decimal("100")

    def test_overfill_is_a_contradiction_not_a_clamp(self) -> None:
        order = self._order().transition_to(OrderState.SUBMITTED, at=NOW)
        with pytest.raises(IllegalTransitionError, match="exceed order quantity"):
            order.with_fill(self._fill("1.5"))

    def test_a_fill_for_another_order_is_rejected(self) -> None:
        order = self._order().transition_to(OrderState.SUBMITTED, at=NOW)
        stray = self._fill("0.1").model_copy(update={"client_order_id": "sim-OTHER"})
        with pytest.raises(IllegalTransitionError, match="belongs to"):
            order.with_fill(stray)

    def test_from_intent_starts_pending(self) -> None:
        intent = TestOrderIntent()._intent()
        assert Order.from_intent(intent).state is OrderState.PENDING_SUBMIT


class TestCanonicalJson:
    def test_key_order_does_not_change_the_hash_input(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


class TestOrderIntentValidation:
    """The last deterministic gate before anything reaches a venue."""

    def _intent(self, **overrides: object) -> OrderIntent:
        fields: dict[str, object] = {
            "client_order_id": "sim-ABCDEF",
            "basket_id": "b1",
            "cycle_id": "c1",
            "instrument_key": "sim:BTC/USDT",
            "side": Side.BUY,
            "qty": Decimal("0.5"),
            "order_type": OrderType.LIMIT,
            "limit_price": Decimal("50000"),
            "created_at": NOW,
        }
        return OrderIntent(**{**fields, **overrides})  # type: ignore[arg-type]

    def test_a_limit_order_without_a_price_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requires a limit_price"):
            self._intent(limit_price=None)

    def test_a_stop_order_without_a_trigger_is_refused(self) -> None:
        """A triggered order with no trigger would rest forever, guarding nothing."""
        with pytest.raises(ValueError, match="requires a stop_price"):
            self._intent(order_type=OrderType.STOP_LOSS_LIMIT, role=OrderRole.STOP_LOSS)

    def test_a_market_order_needs_neither(self) -> None:
        intent = self._intent(order_type=OrderType.MARKET, limit_price=None)

        assert intent.notional == 0

    def test_an_entry_is_its_own_group(self) -> None:
        assert self._intent().group_id == "sim-ABCDEF"

    def test_an_explicit_group_is_kept(self) -> None:
        assert self._intent(group_id="sim-PARENT").group_id == "sim-PARENT"

    def test_a_ttl_becomes_an_absolute_deadline(self) -> None:
        assert self._intent(ttl_seconds=60).expires_at() == NOW + timedelta(seconds=60)

    def test_no_ttl_means_no_deadline(self) -> None:
        assert self._intent().expires_at() is None


class TestFillValidation:
    def test_a_zero_price_fill_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive quantity and price"):
            Fill(
                fill_id="f1",
                client_order_id="sim-ABCDEF",
                instrument_key="sim:BTC/USDT",
                side=Side.BUY,
                qty=Decimal("1"),
                price=Decimal(0),
                filled_at=NOW,
            )


class TestConfigLimits:
    """Every configurable limit rejects a value that would make it meaningless."""

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("max_basket_allocation_pct", Decimal(0)),
            ("max_position_pct_of_basket", Decimal(101)),
            ("risk_per_trade_pct", Decimal(-1)),
        ],
    )
    def test_percentages_must_be_within_zero_to_one_hundred(
        self, field: str, value: Decimal
    ) -> None:
        with pytest.raises(ValueError, match="within"):
            RiskPolicy(**{field: value})  # type: ignore[arg-type]

    def test_conviction_is_on_the_zero_to_one_scale(self) -> None:
        with pytest.raises(ValueError, match="0–1 scale"):
            RiskPolicy(min_conviction=Decimal(60))

    def test_the_unprotected_haircut_cannot_remove_the_whole_position(self) -> None:
        with pytest.raises(ValueError, match="within"):
            RiskPolicy(unprotected_haircut_pct=Decimal(100))

    def test_a_target_inside_the_stop_is_refused(self) -> None:
        """A take-profit nearer than the stop guarantees a losing expectancy."""
        with pytest.raises(ValueError, match="must exceed"):
            RiskPolicy(stop_loss_atr_multiple=Decimal(3), take_profit_atr_multiple=Decimal(2))

    def test_a_non_positive_atr_multiple_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            RiskPolicy(stop_loss_atr_multiple=Decimal(0))

    def test_long_only_cannot_be_turned_off(self) -> None:
        """Shorting ripples through every other rule; v1 does not model it (DESIGN §12)."""
        with pytest.raises(ValueError, match="long-only"):
            RiskPolicy(long_only=False)

    def test_a_ttl_buffer_must_leave_a_positive_order_lifetime(
        self, instrument: Instrument
    ) -> None:
        with pytest.raises(ValueError, match="positive order lifetime"):
            Basket(
                basket_id="b",
                name="b",
                instruments=(instrument,),
                panel=PanelConfig(
                    panel_id="p",
                    seats=(SeatConfig(seat_id="s", role="r", provider_id="stub", model="m"),),
                ),
                schedule=Schedule(every_seconds=60),
                ttl_buffer_seconds=60,
            )

    def test_duplicate_cluster_ids_are_refused(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            GlobalRiskPolicy(
                clusters=(
                    CorrelationCluster(cluster_id="x"),
                    CorrelationCluster(cluster_id="x"),
                )
            )

    def test_cluster_membership_resolves_across_a_universe(self, instrument: Instrument) -> None:
        other = instrument.model_copy(update={"symbol": "ETH/USDT"})
        equity = instrument.model_copy(update={"symbol": "AAPL", "asset_class": AssetClass.EQUITY})

        members = GlobalRiskPolicy().cluster_members(instrument, (instrument, other, equity))

        assert set(members) == {instrument.key, other.key}


class TestBaseCurrencies:
    """One definition of "already a position", shared by the reconciler and the valuation."""

    def test_it_names_every_base_asset_once(
        self, instrument: Instrument, second_instrument: Instrument
    ) -> None:
        assert base_currencies_of((instrument, second_instrument)) == frozenset({"BTC", "ETH"})

    def test_it_is_empty_for_no_instruments(self) -> None:
        assert base_currencies_of(()) == frozenset()

    def test_two_instruments_sharing_a_base_contribute_one_entry(
        self, instrument: Instrument
    ) -> None:
        other = instrument.model_copy(update={"symbol": "BTC/USDC", "quote_currency": "USDC"})

        assert base_currencies_of((instrument, other)) == frozenset({"BTC"})


class TestMarkStaleness:
    """How old a price may be and still value a position (PHASE_12 §3.2)."""

    def test_it_defaults_to_five_minutes(self) -> None:
        assert GlobalRiskPolicy().mark_staleness_seconds == 300
        assert GlobalRiskPolicy().mark_tolerance == timedelta(minutes=5)

    def test_a_non_positive_tolerance_is_refused(self) -> None:
        """A zero tolerance freezes the portfolio permanently, which is not a limit."""
        with pytest.raises(ValidationError):
            GlobalRiskPolicy(mark_staleness_seconds=0)

    def test_an_existing_policy_document_gains_the_default(self) -> None:
        """Stored policies predate the field; they read back with the default, not a failure."""
        stored = {"max_drawdown_pct": "10"}

        assert GlobalRiskPolicy.model_validate(stored).mark_staleness_seconds == 300
