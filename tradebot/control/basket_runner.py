"""The cycle loop: gate → snapshot → panel → Tier-1 → Tier-2 → execution → settle → record.

One runner owns one basket, and it is the only thing that trades that basket's instruments —
no position is ever mutated from two code paths (PLAN §2.6). Every step emits an event, so the
whole cycle reconstructs from the log alone.

The **gate comes first**, before any money is spent on a panel: a tripped kill switch, a halted
basket, a frozen portfolio aggregate or a breached daily-loss limit all end the cycle as
`BLOCKED` without a decision being taken. Recording that as a cycle rather than skipping it is
deliberate — a halt that leaves no trace in the log is a halt nobody can audit.

Failure semantics — every one of these ends the cycle with no order and a recorded outcome:
* kill switch / halt / frozen aggregate → `BLOCKED`
* stale market data                     → `DATA_STALE`
* degraded panel / no consensus         → `PANEL_DEGRADED` or `NO_ACTION`
* any Tier-1 or Tier-2 veto             → `RISK_VETOED`
* a fail-closed error mid-cycle         → `FAILED`, and the basket is halted for review

The cycle never raises past `run_once`; it records what happened and returns.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import Clock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import BasketStatus, CycleOutcome, Mode, RiskTier, Side
from tradebot.core.errors import DataStaleError, FailClosedError
from tradebot.core.events import EventFactory
from tradebot.core.ids import new_uuid
from tradebot.core.instrument import Instrument
from tradebot.core.logging import correlate, get_logger
from tradebot.core.market import Quote
from tradebot.core.money import ZERO, multiply, percent_of
from tradebot.core.orders import Order
from tradebot.core.schema import DomainModel
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.risk import RiskProposal
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import aggregate
from tradebot.risk.rules import AUTO_PAUSE_RULE
from tradebot.risk.tier1 import RiskOutcome, Tier1RiskEngine, basket_budget
from tradebot.risk.tier2 import Tier2RiskEngine
from tradebot.risk.watchdog import Watchdog

logger = get_logger(__name__)

#: The touch each side must cross, and the direction it crosses in.
_TOUCH: dict[Side, Callable[[Quote], Decimal]] = {
    Side.BUY: lambda quote: quote.ask,
    Side.SELL: lambda quote: quote.bid,
}
_CROSS_SIGN: dict[Side, Decimal] = {Side.BUY: Decimal(1), Side.SELL: Decimal(-1)}


def marketable_price(quote: Quote, side: Side, cross_pct: Decimal) -> Decimal:
    """A limit price that will actually trade.

    Pricing exactly at the touch looks marketable and is not: quantization rounds a buy limit
    *down* and a sell limit *up*, both away from the market, so the order rests one tick behind
    the book and the decision quietly expires at TTL. Crossing by a configured fraction is what
    "marketable limit" means in practice (DESIGN §6.7).
    """
    touch = _TOUCH[side](quote)
    return touch + multiply(percent_of(touch, cross_pct), _CROSS_SIGN[side])


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
        tier2: Tier2RiskEngine,
        watchdog: Watchdog,
        history: HistoryReader,
        execution: ExecutionService,
        monitor: ExecutionMonitor,
        ledger: Ledger,
        store: EventStore,
        clock: Clock,
        global_policy: GlobalRiskPolicy | None = None,
        quote_currency: str = "USDT",
        risk_timeframe: str = "1h",
    ) -> None:
        self._basket = basket
        self._mode = mode
        self._context = context_builder
        self._decisions = decision_engine
        self._risk = risk_engine
        self._tier2 = tier2
        self._watchdog = watchdog
        self._history = history
        self._execution = execution
        self._monitor = monitor
        self._ledger = ledger
        self._store = store
        self._clock = clock
        self._policy = global_policy or GlobalRiskPolicy()
        self._quote_currency = quote_currency
        self._risk_timeframe = risk_timeframe

    @property
    def basket(self) -> Basket:
        return self._basket

    async def run_once(self) -> CycleResult:
        cycle_id = new_uuid()
        events = EventFactory(
            clock=self._clock, basket_id=self._basket.basket_id, cycle_id=cycle_id
        )
        with correlate(cycle_id=cycle_id, basket_id=self._basket.basket_id):
            await self._store.append(events.cycle_started())
            try:
                blocked = await self._gate()
                if blocked:
                    return await self._finish(
                        cycle_id, events, CycleOutcome.BLOCKED, detail=blocked
                    )
                return await self._run(cycle_id, events)
            except DataStaleError as exc:
                return await self._finish(
                    cycle_id, events, CycleOutcome.DATA_STALE, detail=str(exc)
                )
            except FailClosedError as exc:
                logger.error("cycle failed closed", extra={"error": str(exc)})
                await self._watchdog.halt_basket(self._basket.basket_id, str(exc))
                return await self._finish(cycle_id, events, CycleOutcome.FAILED, detail=str(exc))

    async def _gate(self) -> str:
        """Everything that must be true before a cycle is worth running. Returns why not."""
        if self._basket.status is not BasketStatus.ACTIVE:
            return f"basket status is {self._basket.status.value}"
        verdict = await self._watchdog.check(self._equity())
        if not verdict.may_trade:
            return verdict.reason or "trading is halted"
        return ""

    def _equity(self) -> Decimal:
        return self._ledger.equity(self._prices(), quote_currency=self._quote_currency)

    def _prices(self) -> dict[str, Decimal]:
        """Last marks the ledger already knows. Refreshed from the snapshot once one exists."""
        return {
            position.instrument_key: position.avg_entry for position in self._ledger.positions()
        }

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

        settled = await self._settle(orders)
        outcome = self._classify(decisions, settled, vetoed=vetoed)
        return await self._finish(
            cycle_id, events, outcome, decisions=decisions, orders=settled, cost=cost
        )

    async def _settle(self, orders: list[Order]) -> tuple[Order, ...]:
        """One sweep to book whatever filled immediately, then hand the rest to the monitor.

        The cycle deliberately does **not** wait out the TTL. An order's life is longer than a
        decision's: it rests, the monitor polls it, and if the process dies first the startup
        sequence adopts it from the database (DESIGN §8.2 step 3). Blocking here would stall
        every other basket behind one unfilled limit.
        """
        if not orders:
            return ()
        await self._monitor.poll()
        settled = {order.client_order_id: order for order in self._monitor.tracked}
        self._monitor.prune()
        return tuple(settled.get(order.client_order_id, order) for order in orders)

    async def _act(
        self,
        snapshot: ContextSnapshot,
        instrument: Instrument,
        decision: Decision,
        cycle_id: str,
        events: EventFactory,
    ) -> Order | None:
        """Size, risk-check through both tiers, and submit — or record why not."""
        proposal = self._build_proposal(snapshot, instrument, decision)
        outcome = self._risk.approve(
            proposal,
            mode=self._mode,
            basket_id=self._basket.basket_id,
            cycle_id=cycle_id,
            ttl_seconds=self._basket.order_ttl_seconds,
        )
        await self._store.append(
            events.risk_checked(instrument.key, outcome.checks, approved=outcome.approved)
        )
        if outcome.intent is None:
            await self._on_tier1_veto(outcome, instrument, events)
            return None

        verdict = self._tier2.review(outcome.intent, proposal)
        await self._store.append(
            events.risk_checked(instrument.key, verdict.checks, approved=verdict.approved)
        )
        if verdict.intent is None:
            logger.info("tier 2 declined the intent", extra={"reason": verdict.veto_reason})
            return None

        order = await self._execution.submit(verdict.intent, instrument)
        self._monitor.track(order, instrument)
        return order

    async def _on_tier1_veto(
        self, outcome: RiskOutcome, instrument: Instrument, events: EventFactory
    ) -> None:
        """A veto is normally just no trade — except the one that means the basket must stop."""
        blocking = outcome.blocking_check
        logger.info("risk declined the proposal", extra={"reason": outcome.veto_reason})
        if blocking is not None and blocking.rule == AUTO_PAUSE_RULE:
            await self._store.append(
                events.risk_event(
                    tier=RiskTier.TIER1,
                    rule=AUTO_PAUSE_RULE,
                    scope=instrument.key,
                    action="basket_paused",
                    detail=blocking.detail,
                )
            )
            await self._watchdog.halt_basket(self._basket.basket_id, blocking.detail)

    def _build_proposal(
        self, snapshot: ContextSnapshot, instrument: Instrument, decision: Decision
    ) -> RiskProposal:
        context = snapshot.context_for(instrument.key)
        atr = context.indicator("ATR", self._risk_timeframe)
        prices = {i.instrument.key: i.quote.last for i in snapshot.instruments}
        equity = self._ledger.equity(prices, quote_currency=self._quote_currency)
        summary = aggregate(
            {instrument.venue: self._ledger},
            self._basket.instruments,
            prices,
            self._policy,
            as_of=snapshot.as_of,
            quote_currency=self._quote_currency,
        )
        cluster = self._policy.cluster_members(instrument, self._basket.instruments)
        return RiskProposal(
            decision=decision,
            instrument=instrument,
            policy=self._basket.risk_policy,
            position=self._ledger.position(instrument.key),
            price=marketable_price(
                context.quote,
                decision.action.side,
                self._basket.risk_policy.marketable_cross_pct,
            ),
            last_price=context.quote.last,
            atr=atr.value if atr else ZERO,
            equity=equity,
            basket_budget=basket_budget(equity, self._basket.risk_policy.max_basket_allocation_pct),
            basket_exposure=self._ledger.exposure(
                tuple(i.key for i in self._basket.instruments), prices
            ),
            gross_exposure=summary.gross_exposure,
            instrument_exposure=summary.exposure_of(instrument.key),
            cluster_exposure=summary.exposure_of(*cluster),
            history=self._history.for_instrument(self._basket.basket_id, instrument.key),
            unprotected=context.unprotected_position,
        )

    @staticmethod
    def _classify(
        decisions: list[Decision], orders: tuple[Order, ...], *, vetoed: bool
    ) -> CycleOutcome:
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
        orders: tuple[Order, ...] = (),
        cost: Decimal = ZERO,
        detail: str = "",
    ) -> CycleResult:
        await self._store.append(events.cycle_completed(outcome, cost))
        logger.info("cycle completed", extra={"outcome": outcome.value, "detail": detail})
        return CycleResult(
            cycle_id=cycle_id,
            basket_id=self._basket.basket_id,
            outcome=outcome,
            decisions=tuple(decisions or ()),
            orders=orders,
            detail=detail,
        )
