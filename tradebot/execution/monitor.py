"""The `ExecutionMonitor`: one object owns an order from acknowledgement to terminal state.

Hummingbot's executor pattern, and for the same reason — an order that nobody owns is an order
whose partial fill nobody books and whose TTL nobody enforces. The monitor is the only thing
that polls the venue, and it does so **only while orders are actually open**, because a polling
storm against a venue is a rate-limit ban waiting to happen (PLAN §3.1).

What it owns (DESIGN §6.7):

* **Fills.** Every poll folds the venue's fills into the ledger, deduplicated by fill id.
* **TTL.** Binance spot has no venue-side good-till-time, so expiry is bot-enforced: at the
  deadline the remainder is cancelled and the fill ratio recorded.
* **Protective groups.** An entry that fills gets its venue-held stop (and, where the venue
  links legs, its take-profit). More of the entry filling replaces the legs at the larger size,
  because no venue lets a resting order's quantity be edited in place.
* **Group closure.** A leg reaching a terminal state takes its working siblings with it, so a
  stop that filled cannot leave a take-profit resting against a position that is gone.

Failure semantics: a venue error during a poll propagates to the caller's retry budget and the
order stays tracked — dropping it would orphan a live order. An entry that fills but whose
protective legs cannot be placed emits a `RISK_EVENT` and leaves the position flagged
unprotected; it is never silently left unguarded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderRole, OrderState, RiskTier
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.orders import Order
from tradebot.execution.protective import plan_legs
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import BrokerAdapter
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

#: Poll cadence while orders are open (DESIGN §6.7). Derived from the rate budget in Phase 3;
#: until then it is a floor slow enough that a burst cannot approach any venue's limit.
DEFAULT_POLL_INTERVAL = timedelta(seconds=10)


@dataclass(slots=True)
class _Tracked:
    order: Order
    instrument: Instrument
    #: How many times this group's protective legs have been replaced, so each replacement gets
    #: its own deterministic `client_order_id`.
    revision: int = 0
    legs: dict[str, Order] = field(default_factory=dict)
    #: Entry quantity the current legs guard. `None` until an entry fill has been protected.
    protected_qty: Decimal | None = None


class ExecutionMonitor:
    """Polls working orders and drives them to a terminal state."""

    def __init__(
        self,
        broker: BrokerAdapter,
        execution: ExecutionService,
        store: EventStore,
        clock: Clock,
        *,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._broker = broker
        self._execution = execution
        self._store = store
        self._clock = clock
        self._poll_interval = poll_interval
        self._tracked: dict[str, _Tracked] = {}
        #: One monitor serves every basket, because orders belong to the venue portfolio rather
        #: than to a basket. Two runners polling at once would each see an entry fill that is not
        #: yet protected and each place a protective group for it — twice the exit quantity
        #: against one position, which in a long-only system is an accidental short (R13).
        self._polling = asyncio.Lock()

    @property
    def tracked(self) -> tuple[Order, ...]:
        """Every order this monitor owns — entries and legs, working or just settled."""
        return tuple(
            order
            for group in self._tracked.values()
            for order in (group.order, *group.legs.values())
        )

    @property
    def working(self) -> tuple[Order, ...]:
        """Every order still capable of filling."""
        return tuple(order for order in self.tracked if order.state.is_open)

    def track(self, order: Order, instrument: Instrument) -> None:
        """Adopt an order. Used by execution and by the startup recovery sequence alike."""
        group = self._tracked.get(order.group_id)
        if group is None:
            self._tracked[order.group_id] = _Tracked(order=order, instrument=instrument)
            return
        if order.role.is_protective:
            group.legs[order.client_order_id] = order
        else:
            group.order = order

    async def settle(self, *, deadline: timedelta | None = None) -> tuple[Order, ...]:
        """Poll until every entry is terminal, or until `deadline` elapses.

        `--once` and the scenario tests use this instead of a background task, so a cycle's
        outcome is a determined fact rather than a race against a poller.
        """
        started = self._clock.now()
        while True:
            await self.poll()
            if not any(t.order.state.is_open for t in self._tracked.values()):
                break
            if deadline is not None and self._clock.now() - started >= deadline:
                break
            await self._clock.sleep(self._poll_interval.total_seconds())
        return tuple(t.order for t in self._tracked.values())

    async def poll(self) -> None:
        """One sweep: sync every working order, expire what is past its TTL, mind the groups."""
        async with self._polling:
            for group in list(self._tracked.values()):
                group.order = await self._sync(group, group.order)
                for client_order_id, leg in list(group.legs.items()):
                    group.legs[client_order_id] = await self._sync(group, leg)
                await self._maintain(group)

    async def _sync(self, group: _Tracked, order: Order) -> Order:
        if not order.state.is_open:
            return order
        order = await self._execution.sync(order, group.instrument)
        if order.is_expired(self._clock.now()):
            order = await self._execution.cancel(
                order, reason="ttl_expired", state=OrderState.EXPIRED
            )
        return order

    async def _maintain(self, group: _Tracked) -> None:
        """Keep the protective legs matched to what the entry has actually filled."""
        entry = group.order
        if entry.filled_qty > ZERO and entry.filled_qty != group.protected_qty:
            await self._replace_legs(group)
        if any(leg.state is OrderState.FILLED for leg in group.legs.values()):
            await self._close_group(group)

    async def _replace_legs(self, group: _Tracked) -> None:
        entry = group.order
        capabilities = self._broker.capabilities()
        plan = plan_legs(
            entry,
            group.instrument,
            capabilities,
            at=self._clock.now(),
            revision=group.revision + 1,
        )
        if not plan.protected:
            await self._record_unprotected(group, plan.unprotected_reason)
            return

        await self._cancel_legs(group, reason="resized_to_entry_fill")
        group.revision += 1
        placed = await self._execution.submit_group(plan.intents, group.instrument)
        for leg in placed:
            group.legs[leg.client_order_id] = leg
        group.protected_qty = entry.filled_qty
        events = self._execution.events_for(entry)
        await self._store.append(events.protective_placed(entry, tuple(placed)))

    async def _record_unprotected(self, group: _Tracked, reason: str) -> None:
        entry = group.order
        group.protected_qty = entry.filled_qty
        events = self._execution.events_for(entry)
        await self._store.append(
            events.protective_placed(entry, (), detail=reason),
            events.risk_event(
                tier=RiskTier.EXECUTION,
                rule="unprotected_position",
                scope=entry.instrument_key,
                action="flagged",
                detail=reason,
            ),
        )
        logger.warning(
            "position left without a venue-held stop",
            extra={"client_order_id": entry.client_order_id, "reason": reason},
        )

    async def _close_group(self, group: _Tracked) -> None:
        """An exit filled: the entry is done and any sibling leg must not outlive it."""
        await self._cancel_legs(group, reason="sibling_leg_filled")
        group.order = await self._execution.cancel(
            group.order, reason="group_closed", state=OrderState.CANCELLED
        )

    async def _cancel_legs(self, group: _Tracked, *, reason: str) -> None:
        for client_order_id, leg in list(group.legs.items()):
            group.legs[client_order_id] = await self._execution.cancel(
                leg, reason=reason, state=OrderState.CANCELLED
            )

    def forget(self, group_id: str) -> None:
        """Stop tracking a finished group, so the poll loop stays proportional to live work."""
        self._tracked.pop(group_id, None)

    def prune(self, *group_ids: str) -> None:
        """Forget settled groups. Named groups only, or all of them when none are named.

        A basket prunes *its own* groups: pruning everything would let one basket drop a group
        another basket's cycle is still reading, and the second would then report the order as it
        was before the poll rather than as it settled.
        """
        wanted = set(group_ids) or set(self._tracked)
        for group_id, group in list(self._tracked.items()):
            settled = not group.order.state.is_open and all(
                not leg.state.is_open for leg in group.legs.values()
            )
            if group_id in wanted and settled and group.order.role is OrderRole.ENTRY:
                self.forget(group_id)
