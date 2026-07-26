"""The cycle loop: snapshot → panel → risk → execution → record.

One runner owns one basket, and it is the only thing that trades that basket's instruments —
no position is ever mutated from two code paths (PLAN §2.6). Every step emits an event, so the
chain `CYCLE_STARTED → SNAPSHOT_FROZEN → SEAT_RESPONDED → DECISION_MADE → RISK_CHECKED →
ORDER_SUBMITTED → FILL_RECEIVED → CYCLE_COMPLETED` reconstructs the whole cycle from the log
alone.

Failure semantics — every one of these ends the cycle with no order and a recorded outcome:
* stale market data          → `DATA_STALE`
* degraded panel / no consensus → `PANEL_DEGRADED` or `NO_ACTION`
* any Tier-1 veto            → `RISK_VETOED`
* a fail-closed error mid-cycle → `FAILED`, and the caller halts the basket

The cycle never raises past `run_once`; it records what happened and returns.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.decision import Decision
from tradebot.core.enums import CycleOutcome, Mode, Side
from tradebot.core.errors import DataStaleError, FailClosedError
from tradebot.core.events import EventFactory
from tradebot.core.ids import new_uuid
from tradebot.core.instrument import Instrument
from tradebot.core.logging import correlate, get_logger
from tradebot.core.market import Quote
from tradebot.core.money import ZERO
from tradebot.core.orders import Order
from tradebot.core.schema import DomainModel
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.risk import RiskProposal
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore
from tradebot.risk.tier1 import Tier1RiskEngine, basket_budget

logger = get_logger(__name__)

#: Cross the spread deliberately: buy at the ask, sell at the bid.
_MARKETABLE_PRICE: dict[Side, Callable[[Quote], Decimal]] = {
    Side.BUY: lambda quote: quote.ask,
    Side.SELL: lambda quote: quote.bid,
}


class CycleResult(DomainModel):
    """What one cycle did. Returned for tests and the dashboard; the log is authoritative."""

    cycle_id: str
    basket_id: str
    outcome: CycleOutcome
    decisions: tuple[Decision, ...] = ()
    orders: tuple[Order, ...] = ()
    detail: str = ""


class BasketRunner:
    """Runs decision cycles for exactly one basket."""

    def __init__(
        self,
        basket: Basket,
        *,
        mode: Mode,
        context_builder: ContextBuilder,
        decision_engine: DecisionEngine,
        risk_engine: Tier1RiskEngine,
        execution: ExecutionService,
        ledger: Ledger,
        store: EventStore,
        clock: Clock,
        quote_currency: str = "USDT",
        risk_timeframe: str = "1h",
    ) -> None:
        self._basket = basket
        self._mode = mode
        self._context = context_builder
        self._decisions = decision_engine
        self._risk = risk_engine
        self._execution = execution
        self._ledger = ledger
        self._store = store
        self._clock = clock
        self._quote_currency = quote_currency
        self._risk_timeframe = risk_timeframe

    async def run_once(self) -> CycleResult:
        cycle_id = new_uuid()
        events = EventFactory(
            clock=self._clock, basket_id=self._basket.basket_id, cycle_id=cycle_id
        )
        with correlate(cycle_id=cycle_id, basket_id=self._basket.basket_id):
            await self._store.append(events.cycle_started())
            try:
                return await self._run(cycle_id, events)
            except DataStaleError as exc:
                return await self._finish(
                    cycle_id, events, CycleOutcome.DATA_STALE, detail=str(exc)
                )
            except FailClosedError as exc:
                logger.error("cycle failed closed", extra={"error": str(exc)})
                return await self._finish(cycle_id, events, CycleOutcome.FAILED, detail=str(exc))

    async def _run(self, cycle_id: str, events: EventFactory) -> CycleResult:
        snapshot = await self._context.build(self._basket)
        await self._store.append(events.snapshot_frozen(snapshot))

        decisions: list[Decision] = []
        orders: list[Order] = []
        cost = ZERO
        vetoed = False

        for instrument in self._basket.instruments:
            decision, deliberation = await self._decisions.decide(
                snapshot, self._basket.panel, instrument.key
            )
            cost += deliberation.cost_usd
            await self._store.append(
                *(events.seat_responded(r) for r in deliberation.responses),
                events.decision_made(decision),
            )
            decisions.append(decision)

            if not decision.is_actionable:
                continue

            order = await self._act(snapshot, instrument, decision, cycle_id, events)
            if order is None:
                vetoed = True
            else:
                orders.append(order)

        outcome = self._classify(decisions, orders, vetoed=vetoed)
        return await self._finish(
            cycle_id, events, outcome, decisions=decisions, orders=orders, cost=cost
        )

    async def _act(
        self,
        snapshot: ContextSnapshot,
        instrument: Instrument,
        decision: Decision,
        cycle_id: str,
        events: EventFactory,
    ) -> Order | None:
        """Size, risk-check, and submit — or record why not."""
        proposal = await self._build_proposal(snapshot, instrument, decision)
        outcome = self._risk.approve(
            proposal,
            mode=self._mode,
            basket_id=self._basket.basket_id,
            cycle_id=cycle_id,
        )
        await self._store.append(
            events.risk_checked(instrument.key, outcome.checks, approved=outcome.approved)
        )
        if outcome.intent is None:
            logger.info("risk declined the proposal", extra={"reason": outcome.veto_reason})
            return None
        return await self._execution.execute(outcome.intent, instrument, events)

    async def _build_proposal(
        self, snapshot: ContextSnapshot, instrument: Instrument, decision: Decision
    ) -> RiskProposal:
        context = snapshot.context_for(instrument.key)
        atr = context.indicator("ATR", self._risk_timeframe)
        prices = {i.instrument.key: i.quote.last for i in snapshot.instruments}
        equity = self._ledger.equity(prices, quote_currency=self._quote_currency)
        return RiskProposal(
            decision=decision,
            instrument=instrument,
            policy=self._basket.risk_policy,
            position=self._ledger.position(instrument.key),
            price=_MARKETABLE_PRICE[decision.action.side](context.quote),
            atr=atr.value if atr else ZERO,
            equity=equity,
            basket_budget=basket_budget(equity, self._basket.risk_policy.max_basket_allocation_pct),
            basket_exposure=self._ledger.exposure(
                tuple(i.key for i in self._basket.instruments), prices
            ),
            unprotected=context.unprotected_position,
        )

    @staticmethod
    def _classify(decisions: list[Decision], orders: list[Order], *, vetoed: bool) -> CycleOutcome:
        if orders:
            return CycleOutcome.ORDERS_PLACED
        if vetoed:
            return CycleOutcome.RISK_VETOED
        if any("PANEL_DEGRADED" in decision.flags for decision in decisions):
            return CycleOutcome.PANEL_DEGRADED
        return CycleOutcome.NO_ACTION

    async def _finish(
        self,
        cycle_id: str,
        events: EventFactory,
        outcome: CycleOutcome,
        *,
        decisions: list[Decision] | None = None,
        orders: list[Order] | None = None,
        cost: Decimal = ZERO,
        detail: str = "",
    ) -> CycleResult:
        await self._store.append(events.cycle_completed(outcome, cost))
        logger.info("cycle completed", extra={"outcome": outcome.value})
        return CycleResult(
            cycle_id=cycle_id,
            basket_id=self._basket.basket_id,
            outcome=outcome,
            decisions=tuple(decisions or ()),
            orders=tuple(orders or ()),
            detail=detail,
        )
