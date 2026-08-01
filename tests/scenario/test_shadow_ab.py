"""Rung 3: the A/B challenger inside a real cycle.

The unit tests prove the evaluator records what it should. These prove the thing that actually
matters — that a challenger sitting inside the live loop cannot place an order, cannot change the
cycle's outcome, and cannot take a cycle down with it when it fails (ADR 0018).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from tests.conftest import TIMEFRAMES

from tradebot.app import Application, build_sim
from tradebot.control.basket_runner import CycleResult
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, ProviderSettings, SeatConfig
from tradebot.core.enums import Action, CycleOutcome, ProviderKind
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.market import timeframe_interval
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.schema import orders
from tradebot.validation.comparison import Comparison

pytestmark = pytest.mark.scenario


#: Both panels declare the same offline provider, identically — which is what the `Basket`
#: validator demands of a shared id, and what lets one wired pool answer for both.
STUB = ProviderSettings(provider_id="stub", kind=ProviderKind.STUB)


def a_panel(panel_id: str, model: str) -> PanelConfig:
    return PanelConfig(
        panel_id=panel_id,
        providers=(STUB,),
        seats=(SeatConfig(seat_id="analyst", role="Analyst", provider_id="stub", model=model),),
    )


def a_basket(instrument: Instrument, *, shadow: PanelConfig | None) -> Basket:
    return Basket(
        basket_id="ab",
        name="A/B basket",
        instruments=(instrument,),
        panel=a_panel("champion", "champ-model"),
        shadow_panel=shadow,
        timeframes=TIMEFRAMES,
    )


BARS = 400


@pytest.fixture
def replayed(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    """A series ending at the harness clock, so nothing aborts as `DATA_STALE`."""
    return ReplayMarketData(
        {
            (instrument.key, timeframe): synthetic_candles(
                start=clock.now() - timeframe_interval(timeframe) * BARS,
                timeframe=timeframe,
                count=BARS,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for timeframe in TIMEFRAMES
        },
        clock,
    )


async def application_for(
    basket: Basket, clock: ManualClock, market_data: ReplayMarketData
) -> AsyncIterator[Application]:
    application = await build_sim(clock=clock, baskets=(basket,), market_data=market_data)
    await application.recover()
    yield application
    await application.shutdown()


async def one_cycle(application: Application) -> CycleResult:
    (result,) = await application.supervisor.run_once()
    return result


def open_orders(application: Application) -> list[str]:
    with application.store.engine.connect() as connection:
        return [row.client_order_id for row in connection.execute(orders.select())]


class TestChallengerInTheLoop:
    async def test_it_is_evaluated_on_the_same_cycle_and_never_trades(
        self, instrument: Instrument, clock: ManualClock, replayed: ReplayMarketData
    ) -> None:
        basket = a_basket(instrument, shadow=a_panel("challenger", "chall-model"))
        async for application in application_for(basket, clock, replayed):
            result = await one_cycle(application)

            events = application.store.event_types(result.cycle_id)
            assert EventType.SHADOW_EVALUATED in events
            # One decision was made and one order placed: the champion's. The challenger's
            # verdict exists only inside its own event.
            assert events.count(EventType.DECISION_MADE) == 1
            (shadow,) = application.store.read_types(EventType.SHADOW_EVALUATED)
            assert shadow.payload["panel_id"] == "challenger"

    async def test_the_challenger_is_evaluated_after_the_champion_has_acted(
        self, instrument: Instrument, clock: ManualClock, replayed: ReplayMarketData
    ) -> None:
        """A research record may not sit between a decision and the order it justified."""
        basket = a_basket(instrument, shadow=a_panel("challenger", "chall-model"))
        async for application in application_for(basket, clock, replayed):
            result = await one_cycle(application)

            events = list(application.store.event_types(result.cycle_id))
            assert events.index(EventType.ORDER_SUBMITTED) < events.index(
                EventType.SHADOW_EVALUATED
            )
            assert events.index(EventType.SHADOW_EVALUATED) < events.index(
                EventType.CYCLE_COMPLETED
            )

    async def test_running_a_challenger_changes_neither_the_outcome_nor_the_orders(
        self, instrument: Instrument, clock: ManualClock, replayed: ReplayMarketData
    ) -> None:
        with_shadow = a_basket(instrument, shadow=a_panel("challenger", "chall-model"))
        without = a_basket(instrument, shadow=None)

        async for application in application_for(with_shadow, clock, replayed):
            shadowed = await one_cycle(application)
            shadowed_orders = len(open_orders(application))

        async for application in application_for(without, clock, replayed):
            plain = await one_cycle(application)
            plain_orders = len(open_orders(application))

        assert shadowed.outcome is plain.outcome is CycleOutcome.ORDERS_PLACED
        assert shadowed_orders == plain_orders

    async def test_the_cycle_cost_excludes_the_challenger(
        self, instrument: Instrument, clock: ManualClock, replayed: ReplayMarketData
    ) -> None:
        """`$/decision` for the panel that traded has to stay a true figure."""
        basket = a_basket(instrument, shadow=a_panel("challenger", "chall-model"))
        async for application in application_for(basket, clock, replayed):
            await one_cycle(application)

            comparison = Comparison.gather(application.store)
            assert comparison.compared_cycles == 1
            assert comparison.champion_cost == Decimal(0)  # the stub panel is free
            assert comparison.challenger_cost == Decimal(0)


class TestChallengerFailure:
    async def test_a_challenger_whose_provider_is_unreachable_leaves_the_cycle_intact(
        self, instrument: Instrument, clock: ManualClock, replayed: ReplayMarketData
    ) -> None:
        """Its seat abstains and it resolves to WAIT; the champion still places its order."""
        unreachable = PanelConfig(
            panel_id="challenger",
            seats=(
                SeatConfig(seat_id="analyst", role="Analyst", provider_id="ghost", model="nope"),
            ),
        )
        basket = a_basket(instrument, shadow=unreachable)
        async for application in application_for(basket, clock, replayed):
            result = await one_cycle(application)

            assert result.outcome is CycleOutcome.ORDERS_PLACED
            assert open_orders(application)
            (shadow,) = application.store.read_types(EventType.SHADOW_EVALUATED)
            assert [d["action"] for d in shadow.payload["decisions"]] == [Action.WAIT.value]


class TestComparisonOverTheLoop:
    async def test_the_report_pairs_what_the_two_panels_said(
        self, instrument: Instrument, clock: ManualClock, replayed: ReplayMarketData
    ) -> None:
        basket = a_basket(instrument, shadow=a_panel("challenger", "chall-model"))
        async for application in application_for(basket, clock, replayed):
            await one_cycle(application)

            comparison = Comparison.gather(application.store)
            (pairing,) = comparison.pairings
            assert pairing.instrument_key == instrument.key
            assert pairing.champion is Action.BUY
            assert comparison.challenger_panels == ("challenger",)
