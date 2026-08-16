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
* a basket quarantined by its operator  → `QUARANTINED`, after the snapshot
* stale market data                     → `DATA_STALE`
* degraded panel / no consensus         → `PANEL_DEGRADED` or `NO_ACTION`
* any Tier-1 or Tier-2 veto             → `RISK_VETOED`
* a fail-closed error mid-cycle         → `FAILED`, and the basket is halted for review
* a shadow panel failing                → nothing; it is recorded and the outcome is unchanged

The cycle never raises past `run_once`; it records what happened and returns.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import Clock
from tradebot.core.config import Basket, ConfigRef, GlobalRiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import BasketStatus, CycleOutcome, Mode, OrderState, RiskTier, Side
from tradebot.core.errors import DataStaleError, FailClosedError
from tradebot.core.events import EventFactory
from tradebot.core.ids import new_uuid
from tradebot.core.instrument import Instrument
from tradebot.core.logging import correlate, get_logger
from tradebot.core.market import Quote
from tradebot.core.money import ZERO, multiply, percent_of
from tradebot.core.orders import Order
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.shadow import ShadowEvaluator
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.risk import RiskProposal
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import PortfolioAggregate, aggregate
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

#: Why a whole-basket quarantine ends the cycle *after* the snapshot rather than in `_gate`: the
#: operator asked for market data and indicators to keep flowing so the basket can be put back
#: into service on evidence. Only the panel and everything downstream of it are skipped, and
#: `QuarantineRule` is what actually guarantees no order escapes either way (ADR 0022).
QUARANTINED = "quarantined by the operator; snapshot taken, no panel run"


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
        venue: str,
        marks: Marks,
        universe: Callable[[], tuple[Instrument, ...]],
        global_policy: GlobalRiskPolicy | None = None,
        config_refs: tuple[ConfigRef, ...] = (),
        quote_currency: str = "USDT",
        risk_timeframe: str = "1h",
        shadow: ShadowEvaluator | None = None,
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
        #: Which venue would have taken this cycle's orders. Recorded on every cycle so the
        #: promotion report can tell the evidence base from an adapter integration check that
        #: shares the same database (DESIGN §9).
        self._venue = venue
        #: Shared with every other basket and with the supervisor's sweep. Written from this
        #: cycle's snapshot, read by the valuation — a cache, never an authority (ADR 0027).
        self._marks = marks
        #: Every configured instrument, read fresh at each use because a basket published while
        #: the process runs changes it.
        self._universe = universe
        self._policy = global_policy or GlobalRiskPolicy()
        #: The configuration versions this runner was built from, recorded on every cycle it
        #: starts so a past decision is re-read against the limits that produced it (DESIGN §6.1).
        self._config_refs = config_refs
        self._quote_currency = quote_currency
        self._risk_timeframe = risk_timeframe
        #: The challenger, evaluated on this cycle's snapshot after the champion has acted.
        #: `None` whenever the basket declares no `shadow_panel` — which is the default.
        self._shadow = shadow

    @property
    def basket(self) -> Basket:
        return self._basket

    async def run_once(self) -> CycleResult:
        cycle_id = new_uuid()
        events = EventFactory(
            clock=self._clock, basket_id=self._basket.basket_id, cycle_id=cycle_id
        )
        with correlate(cycle_id=cycle_id, basket_id=self._basket.basket_id):
            await self._store.append(events.cycle_started(self._config_refs, self._venue))
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
        verdict = await self._watchdog.check(self._valuation(self._clock.now()))
        if not verdict.may_trade:
            return verdict.reason or "trading is halted"
        return ""

    async def _run(self, cycle_id: str, events: EventFactory) -> CycleResult:
        snapshot = await self._context.build(self._basket)
        await self._store.append(events.snapshot_frozen(snapshot))
        # Every basket writes the marks every basket is valued against, from quotes this cycle has
        # already paid for. That is what makes portfolio equity one number rather than one per
        # basket, each blind to the others' holdings (PHASE_12 Finding 2).
        for context in snapshot.instruments:
            self._marks.observe_quote(context.quote)
        if self._basket.risk_policy.quarantined:
            return await self._finish(
                cycle_id, events, CycleOutcome.QUARANTINED, detail=QUARANTINED
            )

        # One call covers the whole basket, so the panel's cost ceiling and its decision mode
        # are the engine's to enforce; the runner only sees decisions (DESIGN §6.5).
        panel = await self._decisions.deliberate(snapshot, self._basket)
        await self._store.append(*(events.seat_responded(r) for r in panel.responses))

        decisions: list[Decision] = []
        orders: list[Order] = []
        vetoed = False

        for decision in panel.decisions:
            await self._store.append(events.decision_made(decision))
            decisions.append(decision)
            if not decision.is_actionable:
                continue

            instrument = self._basket.instrument(decision.instrument_key)
            order = await self._act(snapshot, instrument, decision, cycle_id, events)
            if order is None:
                vetoed = True
            else:
                orders.append(order)

        settled = await self._settle(orders)
        # Last, and deliberately after the champion has acted: the challenger is a research
        # record, so it may not delay an order or change what this cycle decided (ADR 0018).
        if self._shadow is not None:
            await self._shadow.evaluate(snapshot, self._basket, events)

        outcome = self._classify(decisions, settled, vetoed=vetoed)
        return await self._finish(
            cycle_id,
            events,
            outcome,
            decisions=decisions,
            orders=settled,
            cost=panel.cost_usd,
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
        self._monitor.prune(*{order.group_id for order in orders})
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

    def _valuation(self, as_of: UtcDatetime) -> PortfolioAggregate:
        """What the portfolio is worth, over the whole configured universe.

        The universe rather than this basket's instruments: gross exposure, per-instrument
        exposure and a cluster's membership are all portfolio-wide questions, and answering them
        from one basket's slice made Tier-2 blind to its siblings (PHASE_12 Finding 6).
        """
        return aggregate(
            {self._venue: self._ledger},
            self._universe(),
            self._marks,
            self._policy,
            as_of=as_of,
            notional_currency=self._quote_currency,
        )

    def _build_proposal(
        self, snapshot: ContextSnapshot, instrument: Instrument, decision: Decision
    ) -> RiskProposal:
        context = snapshot.context_for(instrument.key)
        atr = context.indicator("ATR", self._risk_timeframe)
        prices = {i.instrument.key: i.quote.last for i in snapshot.instruments}
        summary = self._valuation(snapshot.as_of)
        equity = summary.equity
        cluster = self._policy.cluster_members(instrument, self._universe())
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
        """A refused order is not a placed one.

        An order the venue rejected, or that the self-trade check refused to send, reached no
        market. Counting it as `ORDERS_PLACED` would make the promotion gates read a cycle that
        traded nothing as a cycle that traded (DESIGN §9).
        """
        if any(order.state is not OrderState.REJECTED for order in orders):
            return CycleOutcome.ORDERS_PLACED
        if vetoed or orders:
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
