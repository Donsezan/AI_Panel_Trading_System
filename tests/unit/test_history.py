"""Trading history read from the projections, and the Tier-1 rules that meter on it.

These limits are read from the database on purpose. A cooldown or a daily cap held in memory is
cleared by a crash, which turns a crash loop into an unmetered trading loop.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import (
    Action,
    CycleOutcome,
    OrderRole,
    OrderType,
    RiskDecision,
    Side,
    SizeHint,
)
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument
from tradebot.core.orders import Order, OrderIntent
from tradebot.core.portfolio import Position, RoundTrip
from tradebot.interfaces.risk import RiskProposal, TradingHistory
from tradebot.ledger.history import HistoryReader
from tradebot.persistence.store import EventStore
from tradebot.risk.rules import ConsecutiveLossRule, CooldownRule, DailyTradeCapRule

BASKET = "b1"


def events(clock: ManualClock, cycle_id: str) -> EventFactory:
    return EventFactory(clock=clock, basket_id=BASKET, cycle_id=cycle_id)


async def record_cycle(
    store: EventStore,
    clock: ManualClock,
    instrument: Instrument,
    cycle_id: str,
    *,
    traded: bool = False,
    role: OrderRole = OrderRole.ENTRY,
) -> None:
    factory = events(clock, cycle_id)
    await store.append(factory.cycle_started())
    if traded:
        await store.append(factory.order_submitted(_order(instrument, clock, cycle_id, role)))
    await store.append(factory.cycle_completed(CycleOutcome.ORDERS_PLACED, Decimal(0)))


def _order(instrument: Instrument, clock: ManualClock, cycle_id: str, role: OrderRole) -> Order:
    return Order.from_intent(
        OrderIntent(
            client_order_id=f"sim-{cycle_id}-{role.value}",
            basket_id=BASKET,
            cycle_id=cycle_id,
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("50000"),
            role=role,
            created_at=clock.now(),
        )
    )


async def record_trip(
    store: EventStore, clock: ManualClock, instrument: Instrument, pnl: str
) -> None:
    await store.append(
        events(clock, "c-trip").round_trip_closed(
            RoundTrip(
                instrument_key=instrument.key,
                qty=Decimal("0.1"),
                entry_price=Decimal("50000"),
                exit_price=Decimal("50000"),
                realized_pnl=Decimal(pnl),
                closed_at=clock.now(),
            )
        )
    )


@pytest.fixture
def reader(store: EventStore, clock: ManualClock) -> HistoryReader:
    return HistoryReader(store.engine, clock)


class TestHistoryReader:
    async def test_an_instrument_never_traded_has_no_cooldown_to_serve(
        self, reader: HistoryReader, instrument: Instrument
    ) -> None:
        history = reader.for_instrument(BASKET, instrument.key)

        assert history.cycles_since_trade is None
        assert history.trades_today == 0

    async def test_cycles_since_a_trade_are_counted(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await record_cycle(store, clock, instrument, "c1", traded=True)
        clock.advance(600)
        await record_cycle(store, clock, instrument, "c2")

        assert reader.for_instrument(BASKET, instrument.key).cycles_since_trade == 1

    async def test_only_entries_count_as_trades(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Counting protective legs would exhaust a six-trade daily cap in two decisions."""
        await record_cycle(store, clock, instrument, "c1", traded=True)
        await record_cycle(store, clock, instrument, "c2", traded=True, role=OrderRole.STOP_LOSS)

        assert reader.for_instrument(BASKET, instrument.key).trades_today == 1

    async def test_a_trade_yesterday_is_not_a_trade_today(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await record_cycle(store, clock, instrument, "c1", traded=True)
        clock.advance(int(timedelta(days=1).total_seconds()))

        assert reader.for_instrument(BASKET, instrument.key).trades_today == 0

    async def test_the_hourly_order_count_spans_every_basket(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The Tier-2 rate budget is global; one basket's storm bans the whole key."""
        await record_cycle(store, clock, instrument, "c1", traded=True)

        assert reader.for_instrument("other-basket", instrument.key).orders_last_hour == 1

    async def test_an_order_from_two_hours_ago_is_outside_the_window(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await record_cycle(store, clock, instrument, "c1", traded=True)
        clock.advance(int(timedelta(hours=2).total_seconds()))

        assert reader.for_instrument(BASKET, instrument.key).orders_last_hour == 0

    async def test_consecutive_losses_stop_at_the_first_win(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        for pnl in ("-10", "-20", "5", "-30"):
            await record_trip(store, clock, instrument, pnl)

        assert reader.for_instrument(BASKET, instrument.key).consecutive_losses == 1

    async def test_an_unbroken_run_of_losses_is_counted_in_full(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        for pnl in ("5", "-10", "-20", "-30"):
            await record_trip(store, clock, instrument, pnl)

        assert reader.for_instrument(BASKET, instrument.key).consecutive_losses == 3

    async def test_a_scratch_trade_is_not_a_loss(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await record_trip(store, clock, instrument, "0")

        assert reader.for_instrument(BASKET, instrument.key).consecutive_losses == 0


def proposal(instrument: Instrument, history: TradingHistory, **policy: object) -> RiskProposal:
    return RiskProposal(
        decision=Decision(
            instrument_key=instrument.key,
            action=Action.BUY,
            conviction=Decimal("0.9"),
            size_hint=SizeHint.HALF,
        ),
        instrument=instrument,
        policy=RiskPolicy(**policy),  # type: ignore[arg-type]
        position=Position(instrument_key=instrument.key),
        price=Decimal("50000"),
        atr=Decimal("500"),
        equity=Decimal("10000"),
        basket_budget=Decimal("1000"),
        basket_exposure=Decimal(0),
        history=history,
    )


class TestMeteringRules:
    def test_a_recent_trade_is_blocked_by_the_cooldown(self, instrument: Instrument) -> None:
        result = CooldownRule().evaluate(
            proposal(instrument, TradingHistory(cycles_since_trade=1), cooldown_cycles=2),
            Decimal("1"),
        )

        assert result.blocked

    def test_an_elapsed_cooldown_permits_the_trade(self, instrument: Instrument) -> None:
        result = CooldownRule().evaluate(
            proposal(instrument, TradingHistory(cycles_since_trade=2), cooldown_cycles=2),
            Decimal("1"),
        )

        assert result.decision is RiskDecision.PASS

    def test_a_first_ever_trade_is_never_on_cooldown(self, instrument: Instrument) -> None:
        result = CooldownRule().evaluate(
            proposal(instrument, TradingHistory(), cooldown_cycles=2), Decimal("1")
        )

        assert result.decision is RiskDecision.PASS

    def test_the_daily_cap_blocks_at_the_limit_not_after_it(self, instrument: Instrument) -> None:
        rule = DailyTradeCapRule()

        assert not rule.evaluate(
            proposal(instrument, TradingHistory(trades_today=5), max_trades_per_day=6),
            Decimal("1"),
        ).blocked
        assert rule.evaluate(
            proposal(instrument, TradingHistory(trades_today=6), max_trades_per_day=6),
            Decimal("1"),
        ).blocked

    def test_a_run_of_losses_blocks_and_names_the_auto_pause(self, instrument: Instrument) -> None:
        result = ConsecutiveLossRule().evaluate(
            proposal(instrument, TradingHistory(consecutive_losses=4), max_consecutive_losses=4),
            Decimal("1"),
        )

        assert result.blocked
        assert "auto-paused" in result.detail

    def test_a_shorter_run_still_trades(self, instrument: Instrument) -> None:
        result = ConsecutiveLossRule().evaluate(
            proposal(instrument, TradingHistory(consecutive_losses=3), max_consecutive_losses=4),
            Decimal("1"),
        )

        assert result.decision is RiskDecision.PASS


class TestHoldingPeriod:
    async def test_a_never_opened_position_has_been_held_for_nothing(
        self, reader: HistoryReader
    ) -> None:
        assert reader.held_cycles(BASKET, None) == 0

    async def test_the_holding_period_counts_completed_cycles_since_the_position_opened(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Derived, so it survives a restart: an in-memory counter would report a five-day
        position as brand new every time the process came back."""
        opened = clock.now()
        for cycle in ("c1", "c2", "c3"):
            clock.advance(600)
            await record_cycle(store, clock, instrument, cycle)

        assert reader.held_cycles(BASKET, opened) == 3

    async def test_cycles_before_the_position_opened_do_not_count(
        self,
        store: EventStore,
        reader: HistoryReader,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await record_cycle(store, clock, instrument, "c1")
        clock.advance(600)
        opened = clock.now()
        clock.advance(600)
        await record_cycle(store, clock, instrument, "c2")

        assert reader.held_cycles(BASKET, opened) == 1
