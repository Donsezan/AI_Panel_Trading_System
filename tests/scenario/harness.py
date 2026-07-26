"""A fully wired sim stack whose components a scenario can reach into to inject faults.

Every scenario runs the real startup sequence first, so DESIGN §8.2 is exercised on every test
rather than only in the tests that are about it — and so a scenario can never accidentally
trade with a kill switch that was never armed.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from tradebot.control.basket_runner import BasketRunner
from tradebot.control.context_builder import ContextBuilder
from tradebot.control.startup import Recovery, StartupSequence
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.enums import Mode
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.protocols import SingleRoundProtocol
from tradebot.decision.providers import StubLLMProvider
from tradebot.decision.seat import SeatRunner
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.execution.sim_broker import SimBroker, SimulatedMarket
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import Reconciler
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.schema import (
    cycles,
    fills,
    orders,
    positions,
    reconciliations,
    risk_events,
    round_trips,
)
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.tier1 import Tier1RiskEngine
from tradebot.risk.tier2 import Tier2RiskEngine
from tradebot.risk.watchdog import Watchdog

#: The fixture series ends well before the harness clock, so scenarios that are not about
#: staleness widen the budget; `TestDataFaults` uses the real default to prove it still trips.
IGNORE_STALENESS = timedelta(days=3650)

PROJECTIONS = (cycles, orders, fills, positions, risk_events, round_trips, reconciliations)


class Harness:
    """One sim stack, assembled the way `app.build_sim` assembles it."""

    def __init__(
        self,
        basket: Basket,
        clock: ManualClock,
        market_data: MarketDataProvider,
        responses: list[str],
        *,
        equity: Decimal = Decimal(10_000),
        fill_ratio: Decimal = Decimal(1),
        policy: GlobalRiskPolicy | None = None,
        staleness_tolerance: timedelta = IGNORE_STALENESS,
    ) -> None:
        self.clock = clock
        self.basket = basket
        self.policy = policy or GlobalRiskPolicy()
        engine = create_database(None)
        self.engine = engine
        self.writer = SingleWriter(engine)
        self.store = EventStore(engine, self.writer)
        self.ledger = Ledger(clock, venue="sim", balances={"USDT": equity})
        self.broker = SimBroker(clock, balances={"USDT": equity}, fill_ratio=fill_ratio)
        self.market_data = SimulatedMarket(market_data, self.broker)
        self.provider = StubLLMProvider(responses)
        self.states = RiskStateStore(engine, self.writer, clock)
        self.watchdog = Watchdog(self.policy, self.states, self.store, clock)
        self.execution = ExecutionService(self.broker, self.store, self.ledger, clock)
        self.monitor = ExecutionMonitor(self.broker, self.execution, self.store, clock)
        self.reconciler = Reconciler(
            self.broker,
            self.ledger,
            self.store,
            clock,
            mode=Mode.SIM,
            instruments=basket.instruments,
        )
        self.history = HistoryReader(engine, clock)
        self.context = ContextBuilder(
            self.market_data,
            self.ledger,
            clock,
            staleness_tolerance=staleness_tolerance,
            protective_orders_supported=self.broker.capabilities().protective_orders,
            trading_history=self.history,
        )
        self.startup = StartupSequence(
            self.store,
            self.ledger,
            self.reconciler,
            self.execution,
            self.monitor,
            self.states,
            self.watchdog,
            clock,
            instruments=basket.instruments,
        )
        self.runner = BasketRunner(
            basket,
            mode=Mode.SIM,
            context_builder=self.context,
            decision_engine=DecisionEngine(
                SingleRoundProtocol(SeatRunner({"stub": self.provider}, clock))
            ),
            risk_engine=Tier1RiskEngine(clock),
            tier2=Tier2RiskEngine(self.policy),
            watchdog=self.watchdog,
            history=self.history,
            execution=self.execution,
            monitor=self.monitor,
            ledger=self.ledger,
            store=self.store,
            clock=clock,
            global_policy=self.policy,
        )

    async def start(self) -> Recovery:
        return await self.startup.recover()

    def projections(self) -> dict[str, list[tuple[object, ...]]]:
        with self.store.engine.connect() as connection:
            return {
                table.name: [tuple(row) for row in connection.execute(select(table))]
                for table in PROJECTIONS
            }

    def risk_events(self) -> list[tuple[str, str, str]]:
        with self.store.engine.connect() as connection:
            return [
                (row.rule, row.action_taken, row.detail)
                for row in connection.execute(select(risk_events).order_by(risk_events.c.event_seq))
            ]

    def close(self) -> None:
        self.writer.close()
