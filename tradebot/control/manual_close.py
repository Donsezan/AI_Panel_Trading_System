"""Closing a position by hand — through the ordinary path, with no side door.

DESIGN §6.10 and PLAN §5 are both explicit: a manual close goes through the *same*
`OrderIntent` → Tier-1 → Tier-2 → `ExecutionService` path as a decision the panel made. So this
module builds a proposal and hands it to the same engines the runner uses. It does not
construct an order, it does not skip a rule, and there is no argument that makes it do either.

Two things follow, and both are load-bearing.

**The proposal is flagged `operator_initiated`, and this is the only place that sets it.** The
*metering* rules — cooldown, the daily trade cap, the loss streak, the hourly order rate — then
stand aside, because every one of them exists to stop the **panel** over-trading and none was
written with a human exit in mind. A system that cannot be flattened by its operator during a
loss streak has the control backwards. Each rule that stands aside still answers and records
that it did, so the log shows exactly which ones did and why: the risk layer decides, in tested
deterministic code, which is what makes it an auditable decision rather than a bypass
([ADR 0015](../../docs/adr/0015-an-operator-exit-is-exempt-from-metering-rules.md)).

**Nothing about correctness or venue legality is exempt.** `LongOnlyRule` still clamps the
quantity to the holding, quantization still enforces the venue's lot and minimum, and Tier-2's
price collar still refuses a fat finger. Those are what stop a close being *wrong*, as opposed
to merely being *metered*.

**A tripped kill switch does not block a close.** The switch stops the bot from trading; it must
not trap a human's exit — and `flatten_on_kill` existing at all shows the design contemplates
leaving positions at kill time (DESIGN §6.6). Stated here and asserted in the tests, because it
is exactly the sort of thing that is otherwise true only by accident.

A manual close is **not a cycle**. It carries a `manual-…` correlation id so its events group
together, and deliberately writes no row to the `cycles` projection: cooldowns are counted in
completed cycles, and letting an operator's close advance every basket's cooldown would make a
risk limit depend on how often a human intervened.

Failure semantics: an unknown basket, an instrument the basket does not hold, a flat position, a
stale quote, or a missing Tier-2 policy each refuse before anything is sent. A veto from either
tier refuses and is recorded with full provenance. Only a fully approved intent reaches the venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from tradebot.control.basket_runner import marketable_price
from tradebot.control.config_store import ConfigStore
from tradebot.core.clock import Clock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, ConfigKind, Mode, RiskTier, Side, SizeHint
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.events import EventFactory
from tradebot.core.ids import new_uuid
from tradebot.core.instrument import Instrument
from tradebot.core.logging import correlate, get_logger
from tradebot.core.market import Quote
from tradebot.core.money import ZERO
from tradebot.core.orders import Order, RiskCheckResult
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.interfaces.risk import RiskProposal
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore
from tradebot.risk.tier1 import Tier1RiskEngine, basket_budget
from tradebot.risk.tier2 import Tier2RiskEngine

logger = get_logger(__name__)

MANUAL_CLOSE_RULE = "manual_close"

#: How old a quote may be before a manual close refuses to price against it. A close is not
#: urgent enough to justify sending an order priced off a book that has stopped updating.
DEFAULT_QUOTE_TOLERANCE = timedelta(minutes=5)

#: A human asking to close is maximally convinced, by construction — the conviction floor exists
#: to gate the *panel*, and a rating invented for an operator would be a fiction either way.
MANUAL_CONVICTION = Decimal(1)


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """What the request did: an order, or the recorded reason there is none."""

    instrument_key: str
    correlation_id: str
    order: Order | None = None
    checks: tuple[RiskCheckResult, ...] = ()
    reason: str = ""
    #: What was held when the close was proposed, so a shrunk order can be reported as one.
    held_qty: Decimal = ZERO

    @property
    def submitted(self) -> bool:
        return self.order is not None

    @property
    def partial(self) -> bool:
        """Whether less than the whole holding is being closed.

        Tier-2's per-order notional cap *shrinks* rather than vetoes, so an operator can ask to
        close a position and have part of it closed. Reporting that as "closed" would leave them
        believing they are flat when they are not — the one thing a close must never do.
        """
        return self.order is not None and self.order.qty < self.held_qty


class ManualCloser:
    """Turns "close this position" into an order, or into a recorded refusal."""

    def __init__(
        self,
        *,
        clock: Clock,
        mode: Mode,
        configs: ConfigStore,
        ledger: Ledger,
        prices: MarketDataProvider,
        history: HistoryReader,
        execution: ExecutionService,
        monitor: ExecutionMonitor,
        store: EventStore,
        quote_currency: str,
        quote_tolerance: timedelta = DEFAULT_QUOTE_TOLERANCE,
    ) -> None:
        self._clock = clock
        self._mode = mode
        self._configs = configs
        self._ledger = ledger
        self._prices = prices
        self._history = history
        self._execution = execution
        self._monitor = monitor
        self._store = store
        self._quote_currency = quote_currency
        self._tolerance = quote_tolerance

    def closable(self) -> tuple[tuple[str, str], ...]:
        """`(basket_id, instrument_key)` pairs an operator may close right now.

        A held instrument that no basket lists is deliberately absent: without a basket there is
        no Tier-1 policy to evaluate the close against, and inventing one would be the side door
        this module exists not to have.
        """
        held = {p.instrument_key for p in self._ledger.positions() if not p.is_flat}
        return tuple(
            (record.ref.config_id, instrument.key)
            for record in self._configs.baskets()
            for instrument in record.document.instruments
            if instrument.key in held
        )

    async def close(self, basket_id: str, instrument_key: str, *, actor: str) -> CloseOutcome:
        correlation_id = f"manual-{new_uuid()}"
        events = EventFactory(clock=self._clock, basket_id=basket_id, cycle_id=correlation_id)
        with correlate(cycle_id=correlation_id, basket_id=basket_id):
            # Recorded before anything is evaluated: that a human asked must survive a refusal,
            # an exception, and a crash between the two.
            await self._store.append(
                events.risk_event(
                    tier=RiskTier.EXECUTION,
                    rule=MANUAL_CLOSE_RULE,
                    scope=instrument_key,
                    action="requested",
                    detail=f"manual close of {instrument_key} requested by {actor}",
                )
            )
            return await self._run(basket_id, instrument_key, correlation_id, events, actor)

    async def _run(
        self,
        basket_id: str,
        instrument_key: str,
        correlation_id: str,
        events: EventFactory,
        actor: str,
    ) -> CloseOutcome:
        basket = self._basket(basket_id)
        instrument = _instrument_of(basket, instrument_key)
        proposal = await self._proposal(basket, instrument, actor)

        outcome = Tier1RiskEngine(self._clock).approve(
            proposal,
            mode=self._mode,
            basket_id=basket_id,
            cycle_id=correlation_id,
            ttl_seconds=basket.order_ttl_seconds,
        )
        await self._store.append(
            events.risk_checked(instrument.key, outcome.checks, approved=outcome.approved)
        )
        if outcome.intent is None:
            return await self._refused(
                instrument.key, correlation_id, events, outcome.checks, outcome.veto_reason
            )

        policy = self._policy()
        verdict = Tier2RiskEngine(policy).review(outcome.intent, proposal)
        checks = (*outcome.checks, *verdict.checks)
        await self._store.append(
            events.risk_checked(instrument.key, verdict.checks, approved=verdict.approved)
        )
        if verdict.intent is None:
            return await self._refused(
                instrument.key, correlation_id, events, checks, verdict.veto_reason
            )

        order = await self._execution.submit(verdict.intent, instrument)
        self._monitor.track(order, instrument)
        await self._store.append(
            events.risk_event(
                tier=RiskTier.EXECUTION,
                rule=MANUAL_CLOSE_RULE,
                scope=instrument.key,
                action="order_submitted",
                detail=f"{order.client_order_id} for {order.qty}, requested by {actor}",
            )
        )
        logger.warning(
            "manual close submitted",
            extra={"actor": actor, "instrument": instrument.key, "qty": str(order.qty)},
        )
        return CloseOutcome(
            instrument_key=instrument.key,
            correlation_id=correlation_id,
            order=order,
            checks=checks,
            held_qty=proposal.position.qty,
        )

    async def _refused(
        self,
        instrument_key: str,
        correlation_id: str,
        events: EventFactory,
        checks: tuple[RiskCheckResult, ...],
        reason: str,
    ) -> CloseOutcome:
        await self._store.append(
            events.risk_event(
                tier=RiskTier.EXECUTION,
                rule=MANUAL_CLOSE_RULE,
                scope=instrument_key,
                action="refused",
                detail=reason,
            )
        )
        logger.warning("manual close refused", extra={"instrument": instrument_key, "why": reason})
        return CloseOutcome(
            instrument_key=instrument_key,
            correlation_id=correlation_id,
            checks=checks,
            reason=reason,
        )

    async def _proposal(self, basket: Basket, instrument: Instrument, actor: str) -> RiskProposal:
        position = self._ledger.position(instrument.key)
        if position.is_flat:
            raise ConfigError(f"nothing to close: no position is held in {instrument.key}")

        quote = self._fresh_quote(await self._prices.get_quote(instrument))
        prices = {instrument.key: quote.last}
        equity = self._ledger.equity(prices, quote_currency=self._quote_currency)
        policy = basket.risk_policy
        return RiskProposal(
            # `size_hint=FULL` against a reduce-only SELL is the whole position, and nothing
            # about "full" can exceed what is held — `_size_sell` clamps to the holding.
            decision=Decision(
                instrument_key=instrument.key,
                action=Action.SELL,
                conviction=MANUAL_CONVICTION,
                size_hint=SizeHint.FULL,
                reasoning_summary=f"manual close requested by {actor}",
            ),
            instrument=instrument,
            policy=policy,
            position=position,
            price=marketable_price(quote, Side.SELL, policy.marketable_cross_pct),
            last_price=quote.last,
            # A reducing SELL is not volatility-sized and carries no protective legs, so no ATR
            # is read. Passing a fabricated one would put a number in the provenance record that
            # nothing computed.
            atr=ZERO,
            equity=equity,
            basket_budget=basket_budget(equity, policy.max_basket_allocation_pct),
            basket_exposure=self._ledger.exposure(tuple(i.key for i in basket.instruments), prices),
            history=self._history.for_instrument(basket.basket_id, instrument.key),
            # The only place in the system that sets this. It exempts the *metering* rules —
            # cooldown, the daily cap, the loss streak, the hourly rate — from a strictly
            # risk-reducing act, and every rule that stands aside records that it did
            # (ADR 0015). Nothing about correctness or venue legality is exempt.
            operator_initiated=True,
        )

    def _fresh_quote(self, quote: Quote) -> Quote:
        age = self._clock.now() - quote.observed_at
        if age > self._tolerance:
            raise DataStaleError(
                f"the quote for {quote.instrument_key} is {age} old, tolerance {self._tolerance}; "
                "refusing to price a close against a book that has stopped updating"
            )
        return quote

    def _basket(self, basket_id: str) -> Basket:
        record = self._configs.latest(ConfigKind.BASKET, basket_id)
        if record is None or not record.usable:
            raise ConfigError(f"no basket {basket_id} is in service")
        basket: Basket = record.document
        return basket

    def _policy(self) -> GlobalRiskPolicy:
        record = self._configs.global_risk()
        if record is None:
            raise ConfigError("no Tier-2 policy is published; refusing to send any order")
        policy: GlobalRiskPolicy = record.document
        return policy


def _instrument_of(basket: Basket, instrument_key: str) -> Instrument:
    """The basket's own instrument, or a refusal a caller can report.

    A held position that the named basket does not list is the orphan case: there is no Tier-1
    policy to judge the close against, and `Basket.instrument` signals it with a `KeyError` that
    is not a `TradebotError` — so it is translated here rather than escaping as a 500.
    """
    try:
        return basket.instrument(instrument_key)
    except KeyError as exc:
        raise ConfigError(
            f"basket {basket.basket_id} does not hold {instrument_key}, so there is no Tier-1 "
            "policy to evaluate a close against. Add the instrument to a basket, or close it at "
            "the venue."
        ) from exc
