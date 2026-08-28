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
from tradebot.core.enums import OrderRole, OrderState, RiskTier, Side
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

#: Which end of the stop-price ordering is funded first when the holding cannot cover every group
#: on an instrument, keyed on the side that *opened* the position. A long is opened BUY and its
#: stops sit below the market, so the tightest — the one that fires first on the way down — is the
#: highest, and `reverse=True` funds it first (design D3).
#:
#: A table rather than an `if`, as `_EXIT_SIDE` and `_OFFSET_SIGN` are in `protective.py`. v1 is
#: long-only so only the BUY row is ever taken; the table keeps the module honest rather than
#: assuming. Age was the first proposal and is a proxy that inverts in a falling market — an entry
#: at 100 stopped at 95 and a later one at 90 stopped at 85, newest-first keeps the 85.
_TIGHTEST_FIRST: dict[Side, bool] = {Side.BUY: True, Side.SELL: False}


@dataclass(slots=True)
class _Tracked:
    order: Order
    instrument: Instrument
    #: How many times this group's protective legs have been replaced, so each replacement gets
    #: its own deterministic `client_order_id`. The one thing here that cannot be derived: two
    #: replacements at the same size must not collide.
    revision: int = 0
    legs: dict[str, Order] = field(default_factory=dict)
    #: The target last reported as unguardable, so that report fires once per target rather than
    #: once per poll. A de-duplication marker; nothing reasons from it.
    unprotected_at: Decimal | None = None

    @property
    def resting_qty(self) -> Decimal:
        """How much of the holding the venue is currently guarding for this group.

        A `max`, never a sum: with OCO the stop and the take-profit rest at the same size and the
        venue's order list reserves the coins once, not twice — summing would halve every group on
        the first poll after arming. Read off the legs `poll` has just re-synced, so this is the
        venue's own answer rather than a counter that can drift (design D2).
        """
        return max(
            (leg.remaining_qty for leg in self.legs.values() if leg.state.is_open),
            default=ZERO,
        )


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
            # After the sync loop, never inside it: `_sync` books fills as it reads them, so a stop
            # that filled this sweep has already reduced the position. Computing per group inside
            # the loop would size some groups against a pre-fill holding and others against a
            # post-fill one (design §2.4).
            targets = self._targets()
            for group in list(self._tracked.values()):
                await self._maintain(group, targets.get(group.order.group_id, ZERO))

    async def _sync(self, group: _Tracked, order: Order) -> Order:
        if not order.state.is_open:
            return order
        order = await self._execution.sync(order, group.instrument)
        if order.is_expired(self._clock.now()):
            order = await self._execution.cancel(
                order, reason="ttl_expired", state=OrderState.EXPIRED
            )
        return order

    async def _maintain(self, group: _Tracked, target: Decimal) -> None:
        """Keep the protective legs matched to the *position*, not to this entry's fills.

        KNOWN_GAPS §4: the legs tracked `entry.filled_qty`, so a SELL from any other path — another
        cycle's exit decision, an ADR 0015 operator close — reduced the holding while the legs kept
        their original size, and the oversized order rested at the venue until it triggered.
        """
        # A leg filling ends the group, so there is nothing left to resize. Checked first: with
        # `resting_qty` read live, a filled leg and its cancelled OCO sibling both leave `is_open`,
        # so a replace check ahead of this one arms a fresh group against a position that has
        # already gone flat and then cancels it.
        if any(leg.state is OrderState.FILLED for leg in group.legs.values()):
            await self._close_group(group)
            return
        if target != group.resting_qty and target != group.unprotected_at:
            await self._replace_legs(group, target)

    async def _replace_legs(self, group: _Tracked, target: Decimal) -> None:
        if target <= ZERO:
            # Nothing is held behind this group any more. Not "unprotected" — that event means
            # money is at risk with no stop, and this guards nothing and risks nothing (§2.3).
            await self._cancel_legs(group, reason="released_to_position")
            group.unprotected_at = None
            return

        entry = group.order
        capabilities = self._broker.capabilities()
        plan = plan_legs(
            entry,
            group.instrument,
            capabilities,
            at=self._clock.now(),
            qty=target,
            revision=group.revision + 1,
        )
        if not plan.protected:
            await self._record_unprotected(group, plan.unprotected_reason, target)
            return

        await self._cancel_legs(group, reason="resized_to_position")
        group.revision += 1
        placed = await self._execution.submit_group(plan.intents, group.instrument)
        for leg in placed:
            group.legs[leg.client_order_id] = leg
        group.unprotected_at = None
        events = self._execution.events_for(entry)
        await self._store.append(events.protective_placed(entry, tuple(placed)))

    async def _record_unprotected(self, group: _Tracked, reason: str, target: Decimal) -> None:
        entry = group.order
        group.unprotected_at = target
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

    def _targets(self) -> dict[str, Decimal]:
        """How much of each instrument's holding each group's legs may guard.

        The invariant this exists for is that the sum over one instrument's groups never exceeds
        the holding — KNOWN_GAPS §4 is what its absence cost. A per-*group* clamp makes it worse:
        two groups guarding 0.0351 and 0.0852 against a position of 0.1116 would each resize to
        0.1116 and 0.0852, resting 0.1968 against 0.1116.
        """
        targets: dict[str, Decimal] = {}
        for instrument_key in {group.order.instrument_key for group in self._tracked.values()}:
            groups = self._protectable(instrument_key)
            budget = max(
                ZERO, self._execution.held(instrument_key) - self._committed(instrument_key)
            )
            for group in groups:
                target = min(group.order.filled_qty, budget)
                targets[group.order.group_id] = target
                budget -= target
        return targets

    def _protectable(self, instrument_key: str) -> list[_Tracked]:
        """This instrument's groups that can hold legs at all, tightest stop first.

        A group whose entry carries no `ProtectivePlan` is not one of them: a reducing SELL *is*
        the exit and an unprotected venue was charged the sizing haircut instead (`protective_plan`
        returns `None` for both). Running one through `plan_legs` is what made every filled
        discretionary SELL file an `unprotected_position` for an order that needs none.
        """
        ranked = [
            ((plan.stop_price, group.order.created_at, group.order.client_order_id), group)
            for group in self._tracked.values()
            if group.order.instrument_key == instrument_key
            and (plan := group.order.protective) is not None
            # A group whose leg has already filled is closing, not live — `_maintain` tears it
            # down this same poll regardless of what target it is handed. Leaving it in the
            # ranking would still allocate it budget a group that is still guarding a position
            # needs: it would re-arm a fresh group against a holding it no longer has, and starve
            # the live group behind it in the ranking to a target of 0 — cancelling legs that were
            # guarding a position which is still open.
            and not any(leg.state is OrderState.FILLED for leg in group.legs.values())
        ]
        if not ranked:
            return []
        # Ties break on creation then id because startup adopts orders from the database in
        # arbitrary order, and the allocation must survive a restart unchanged.
        ranked.sort(key=lambda pair: pair[0], reverse=_TIGHTEST_FIRST[ranked[0][1].order.side])
        return [group for _, group in ranked]

    def _committed(self, instrument_key: str) -> Decimal:
        """Quantity our own working sells already commit, outside the protective legs.

        A discretionary exit or an ADR 0015 operator close still resting reserves the base asset at
        the venue exactly as a stop does. Only entries are considered — `legs` holds protective
        orders by construction, and those are what the budget is being divided among.
        """
        return sum(
            (
                group.order.remaining_qty
                for group in self._tracked.values()
                if group.order.instrument_key == instrument_key
                and group.order.side is Side.SELL
                and group.order.state.is_open
            ),
            start=ZERO,
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
