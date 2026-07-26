"""Domain models: the boundary that refuses bad data before it becomes an order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.core.decision import Decision, SeatResponse, SeatVote
from tradebot.core.enums import Action, OrderState, OrderType, RiskDecision, Side, SizeHint
from tradebot.core.errors import ConfigError, IllegalTransitionError, MoneyError
from tradebot.core.instrument import Instrument
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
        series = CandleSeries(
            instrument_key="k", timeframe="1h", candles=(candle(),), observed_at=NOW
        )
        series.require_fresh(NOW + timedelta(minutes=5), timedelta(minutes=15))
        with pytest.raises(Exception, match="old, limit"):
            series.require_fresh(NOW + timedelta(hours=2), timedelta(minutes=15))


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
