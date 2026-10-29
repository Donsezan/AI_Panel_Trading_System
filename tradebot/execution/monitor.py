"""The `ExecutionMonitor`: one object owns an order from acknowledgement to terminal state.

Hummingbot's executor pattern, and for the same reason — an order that nobody owns is an order
whose partial fill nobody books and whose TTL nobody enforces. The monitor is the only thing
that polls the venue, and it does so **only while orders are actually open**: `poll` over an
empty tracked set reaches nothing, and `_sync` returns early for an order already terminal, so
the venue cost is proportional to live work rather than to how often something asks. That is
what lets the supervisor's sweep call it on every tick without a polling storm becoming a
rate-limit ban (PLAN §3.1).

**Who calls it.** Three kinds of caller in a running process, and all are needed (KNOWN_GAPS §5):

* the snapshot's settle hook and `BasketRunner._settle`, both scoped to the basket's instruments
  (ADR 0030). The hook books what the venue did while the snapshot read its candles, before the
  position is read; `_settle` books what the cycle's own orders did, so the outcome is a
  determined fact rather than a race;
* `ManualCloser`, scoped, for the same reason: a close empties a holding whose protective legs
  are still resting at the venue;
* `Supervisor._poll_orders`, unscoped, on the resync sweep, whether or not anything cycled. A
  venue-held stop fires *between* cycles by construction (ADR 0004), so the cycle that placed an
  order can never be the thing that notices. In a real `sim.db`, 41 orders span 196 cycles.

**The cadence belongs to the caller, not to this module.** It used to own a
`DEFAULT_POLL_INTERVAL` of ten seconds, which only `settle()` ever read — and `settle()` had no
production caller, so nothing in the process polled on that cadence or any other. Today the
cadence is `Supervisor.DEFAULT_RESYNC_SECONDS`, thirty seconds, because the sweep is what calls
`poll`. DESIGN §6.7 asks for "~10s while orders are open"; a sweep that ran three times as often
would also run the drift check and the valuation sweep three times as often, so reconciling the
two is a decision about *that* loop rather than a constant this module can hold.

What it owns (DESIGN §6.7):

* **Fills.** Every poll folds the venue's fills into the ledger, deduplicated by fill id.
* **TTL.** Binance spot has no venue-side good-till-time, so expiry is bot-enforced: at the
  deadline the remainder is cancelled and the fill ratio recorded.
* **Protective groups.** An entry that fills gets its venue-held stop (and, where the venue
  links legs, its take-profit). More of the entry filling replaces the legs at the larger size,
  because no venue lets a resting order's quantity be edited in place.
* **Group closure.** A leg reaching a terminal state takes its working siblings with it, so a
  stop that filled cannot leave a take-profit resting against a position that is gone.
* **Group retirement.** A group whose share of the holding has been sold away by some other path
  is closed and forgotten, so the next entry is guarded by its own plan and never by a dead
  group's stale legs (RCA 2026-10-28 §4 item 2).

Failure semantics: a venue error during a poll propagates to the caller's retry budget and the
order stays tracked — dropping it would orphan a live order. Groups fail independently: every
group is attempted, every failure is logged, and the most severe class is re-raised after the
others have been synced and maintained (a fail-closed error is never masked by a retryable one).
A fill the ledger refuses (`UnbookableFillError`) poisons only its own group: one `RISK_EVENT`, the
group stays tracked with the fill unbooked, and the error is raised only to a caller whose scope
contains that group — it sizes against that ledger. An unscoped sweep records it and halts
nothing; the owning basket halts at its next scoped poll (ADR 0030). An entry that fills but whose
protective legs cannot be placed emits a `RISK_EVENT` and leaves the position flagged
unprotected; it is never silently left unguarded.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderRole, OrderState, RiskTier, Side
from tradebot.core.errors import FatalError, RetryableError, UnbookableFillError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO, multiply
from tradebot.core.orders import Order
from tradebot.execution.protective import plan_legs
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import BrokerAdapter
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

#: Sign applied to a group's stop trigger when ranking, keyed on the side that *opened* the
#: position. A long is opened BUY and its stops sit below the market, so the tightest — the one
#: that fires first on the way down — is the highest, and negating sorts it first. A short's
#: tightest stop is the lowest, so it sorts unnegated. Per group rather than per list: the
#: direction is a property of the position, not of whichever group happened to be tracked first.
#:
#: A table rather than an `if`, as `_EXIT_SIDE` and `_OFFSET_SIGN` are in `protective.py`. v1 is
#: long-only so only the BUY row is ever taken; the table keeps the module honest rather than
#: assuming. Age was the first proposal and is a proxy that inverts in a falling market — an entry
#: at 100 stopped at 95 and a later one at 90 stopped at 85, newest-first keeps the 85.
_RANK_SIGN: dict[Side, Decimal] = {Side.BUY: Decimal(-1), Side.SELL: Decimal(1)}

UNBOOKABLE_FILL_RULE = "unbookable_fill"
#: The monitor's record of releasing a group it will no longer track: retired (its holding is
#: gone), closed (its own leg filled) or lapsed (the entry ended with nothing filled). Not a flag
#: — none of them leaves anything at risk. A `RISK_EVENT` because that is how the monitor records
#: acting on a group by itself, as `group_poisoned` is; a group never disappears unrecorded.
TRACKED_GROUP_RULE = "tracked_group"


def _severity(error: Exception) -> int:
    """Lower is more severe: a later fail-closed error must not hide behind an earlier retryable."""
    if isinstance(error, FatalError):
        return 0
    # An unclassified exception is not known to be transient, so it is never ranked as retryable.
    return 2 if isinstance(error, RetryableError) else 1


@dataclass(slots=True)
class _Tracked:
    order: Order
    instrument: Instrument
    #: How many times this group's protective legs have been replaced, so each replacement gets
    #: its own deterministic `client_order_id`. The one thing here that cannot be derived: two
    #: replacements at the same size must not collide.
    revision: int = 0
    legs: dict[str, Order] = field(default_factory=dict)
    #: The target the legs now resting for this group were last *requested* for — zero when none
    #: of ours rest, which is the same fact as "requested for nothing". Deliberately the request
    #: and not `resting_qty`: `plan_legs` floors its quantity to `lot_size`, so a target that is
    #: not a lot multiple can never equal the venue's answer, and a trigger comparing the two
    #: cancels and re-places both legs on every poll for ever (design D2, review C2).
    #: `binance.parse_account` builds a `Position.qty` straight from `free + locked`, which after
    #: a base-asset fee is routinely off the `stepSize` grid — so that is a reconciled live
    #: account, not a hypothetical.
    requested_qty: Decimal = ZERO
    #: The target whose *deterministic* refusal — below `min_qty` / `min_notional` — has already
    #: been reported, so that report fires once per target rather than once per poll. `_maintain`
    #: does reason from it, and that is sound only because it is set on that one path: the same
    #: target refuses identically next poll, so a retry buys nothing. A *venue* failure never sets
    #: it, because that one must be retried (review C1).
    unprotected_at: Decimal | None = None
    #: Set when the ledger refused to book this group's fill. The group stays tracked and the fill
    #: stays unbooked until the holding allows it, so nothing pretends it never happened.
    poisoned: UnbookableFillError | None = None
    #: How much of the instrument's holding this group may guard, as of the last allocation. It
    #: grows only with this entry's own fills and never grows back after a reduction took it away:
    #: a later entry's shares are that entry's, not a ladder for an older group to climb back up
    #: (RCA 2026-10-28 §4 item 2). See `ExecutionMonitor._entitle`.
    entitled: Decimal = ZERO
    #: The `net_filled` that `entitled` already accounts for, so each poll sees only the change.
    credited: Decimal = ZERO

    @property
    def net_filled(self) -> Decimal:
        """What this group's own orders put into the holding: the entry's fills less its legs'."""
        return self.order.filled_qty - sum(
            (leg.filled_qty for leg in self.legs.values()), start=ZERO
        )

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
    ) -> None:
        self._broker = broker
        self._execution = execution
        self._store = store
        self._clock = clock
        self._tracked: dict[str, _Tracked] = {}
        #: Groups the most recent poll settled and released. Kept visible through `tracked` until
        #: the next poll begins, because `BasketRunner._settle` and `ManualCloser` read `tracked`
        #: straight after their own poll to report how their orders settled: a release inside that
        #: poll must change nothing they report, let alone an order.
        self._released: dict[str, _Tracked] = {}
        #: The last surplus of `held` over the instrument's entitlements that was logged, so the
        #: warning fires when the figure changes rather than on every sweep.
        self._surplus_logged: dict[str, Decimal] = {}
        #: One monitor serves every basket, because orders belong to the venue portfolio rather
        #: than to a basket. Two runners polling at once would each see an entry fill that is not
        #: yet protected and each place a protective group for it — twice the exit quantity
        #: against one position, which in a long-only system is an accidental short (R13).
        self._polling = asyncio.Lock()

    @property
    def tracked(self) -> tuple[Order, ...]:
        """Every order this monitor owns — entries and legs, working or just settled.

        Includes the groups the most recent poll released (see `_released`); all of those are
        terminal, so `working` is unaffected.
        """
        return tuple(
            order
            for group in (*self._tracked.values(), *self._released.values())
            for order in (group.order, *group.legs.values())
        )

    @property
    def working(self) -> tuple[Order, ...]:
        """Every order still capable of filling."""
        return tuple(order for order in self.tracked if order.state.is_open)

    def track(self, order: Order, instrument: Instrument) -> None:
        """Adopt an order. Used by execution and by the startup recovery sequence alike."""
        self._released.pop(order.group_id, None)
        group = self._tracked.get(order.group_id)
        if group is None:
            self._tracked[order.group_id] = _Tracked(order=order, instrument=instrument)
            return
        if order.role.is_protective:
            group.legs[order.client_order_id] = order
        else:
            group.order = order

    async def poll(self, instrument_keys: frozenset[str] | None = None) -> None:
        """One sweep: sync every working order, expire what is past its TTL, mind the groups.

        `instrument_keys` scopes the sweep to the groups on those instruments (ADR 0030): a cycle
        settles its own basket's holdings before reading them, and must not pay venue calls for,
        or fail on, a sibling basket's orders. A scope holding no group reaches no venue. `None`
        is every group, which is what the supervisor's sweep and a replay want.
        """
        async with self._polling:
            # The previous poll's callers have read what it released; this poll reports its own.
            self._released.clear()
            in_scope = self._in_scope(instrument_keys)
            failures: list[tuple[_Tracked, Exception]] = []
            synced: list[_Tracked] = []
            for group in in_scope:
                try:
                    await self._sync_group(group)
                except UnbookableFillError as error:
                    # Contained to this group (ADR 0030): the fill stays unbooked and tracked, and
                    # its siblings go on syncing, booking and being maintained.
                    try:
                        await self._poison(group, error)
                    except Exception as failure:
                        # The record itself failed: collected like any other, and the group stays
                        # unpoisoned so the next poll tries to record it again.
                        failures.append((group, failure))
                except Exception as error:
                    # Collected, not swallowed: every one is logged and the most severe is
                    # re-raised below, so the error's class still governs the caller's handling.
                    failures.append((group, error))
                else:
                    try:
                        await self._unpoison(group)
                    except Exception as failure:
                        # Still poisoned, so the next poll records the clearing again.
                        failures.append((group, failure))
                    else:
                        synced.append(group)
            # After the sync loop, never inside it: `_sync` books fills as it reads them, so a stop
            # that filled this sweep has already reduced the position. Computing per group inside
            # the loop would size some groups against a pre-fill holding and others against a
            # post-fill one (design §2.4).
            targets = self._targets()
            # A group adopted while a sync awaited the venue is minded in this sweep too.
            adopted = [g for g in self._in_scope(instrument_keys) if g not in in_scope]
            for group in (*synced, *adopted):
                try:
                    await self._maintain(group, targets.get(group.order.group_id))
                except Exception as error:
                    # Same contract as above: collected, logged, re-raised by severity.
                    failures.append((group, error))
            for failed, problem in failures:
                logger.error(
                    "a group failed to poll",
                    extra={
                        "group_id": failed.order.group_id,
                        "client_order_id": failed.order.client_order_id,
                        "error": f"{type(problem).__name__}: {problem}",
                    },
                )
            raising = [problem for _, problem in failures]
            if instrument_keys is not None:
                # A caller scoped to a poisoned group sizes against this ledger, so it must be
                # told; an unscoped sweep sizes nothing and is not (the basket halts at its own
                # next scoped poll).
                raising.extend(g.poisoned for g in in_scope if g.poisoned is not None)
            if raising:
                raise min(raising, key=_severity)

    def _in_scope(self, instrument_keys: frozenset[str] | None) -> list[_Tracked]:
        # Evaluated twice per poll: once to sync, once more so a group adopted while a venue call
        # was awaited is still maintained in the same sweep.
        return [
            group
            for group in self._tracked.values()
            if instrument_keys is None or group.order.instrument_key in instrument_keys
        ]

    async def _sync_group(self, group: _Tracked) -> None:
        """Entry first, then each leg, assigning as it goes so a later failure loses nothing.

        The synced order is assigned *before* its TTL cancel is attempted, never after: the sync
        has already booked its fills into the ledger and the log, and a cancel that then fails (a
        single 5xx is enough) must not leave the group holding the pre-sync order — the next
        poll's `new_fills` would see the same fills as new and book them a second time (final
        review I3, KNOWN_GAPS §13).
        """
        try:
            group.order = await self._sync(group, group.order)
        except UnbookableFillError as error:
            group.order = error.partial_order or group.order
            raise
        group.order = await self._expire(group.order)
        for client_order_id, leg in list(group.legs.items()):
            try:
                group.legs[client_order_id] = await self._sync(group, leg)
            except UnbookableFillError as error:
                # Fills booked before the refused one are kept on the order. Without this the next
                # poll re-books them (`new_fills` dedupes against the order) and drains the ledger.
                group.legs[client_order_id] = error.partial_order or leg
                raise
            group.legs[client_order_id] = await self._expire(group.legs[client_order_id])

    async def _unpoison(self, group: _Tracked) -> None:
        """A sync that no longer raises ends the poison; say so, so the window is in the log."""
        poison = group.poisoned
        if poison is None:
            return
        events = self._execution.events_for(group.order)
        await self._store.append(
            events.risk_event(
                tier=RiskTier.EXECUTION,
                rule=UNBOOKABLE_FILL_RULE,
                scope=poison.instrument_key,
                action="group_unpoisoned",
                detail=(
                    f"{poison.client_order_id}: synced without error "
                    f"(entry {group.order.state.value}, filled {group.order.filled_qty}); "
                    "the ledger was known to be wrong until now"
                ),
            )
        )
        group.poisoned = None

    async def _poison(self, group: _Tracked, error: UnbookableFillError) -> None:
        """Record, once, that a group carries a fill the ledger refuses; keep it tracked."""
        if group.poisoned is None:
            events = self._execution.events_for(group.order)
            detail = (
                f"{error.client_order_id}: a fill of {error.qty} exceeds the holding of "
                f"{error.held}; left unbooked and still tracked"
            )
            await self._store.append(
                events.risk_event(
                    tier=RiskTier.EXECUTION,
                    rule=UNBOOKABLE_FILL_RULE,
                    scope=error.instrument_key,
                    action="group_poisoned",
                    detail=detail,
                )
            )
            logger.error(
                "fill cannot be booked; group poisoned",
                extra={
                    "client_order_id": error.client_order_id,
                    "instrument_key": error.instrument_key,
                    "detail": detail,
                },
            )
        group.poisoned = error

    async def _sync(self, group: _Tracked, order: Order) -> Order:
        """Fold the venue's truth into one order. Books fills; never cancels (see `_expire`)."""
        if not order.state.is_open:
            return order
        return await self._execution.sync(order, group.instrument)

    async def _expire(self, order: Order) -> Order:
        """Cancel the remainder of an order past its bot-enforced TTL; anything else is as is."""
        if not order.is_expired(self._clock.now()):
            return order
        return await self._execution.cancel(order, reason="ttl_expired", state=OrderState.EXPIRED)

    async def _maintain(self, group: _Tracked, target: Decimal | None) -> None:
        """Keep the protective legs matched to the *position*, not to this entry's fills.

        KNOWN_GAPS §4: the legs tracked `entry.filled_qty`, so a SELL from any other path — another
        cycle's exit decision, an ADR 0015 operator close — reduced the holding while the legs kept
        their original size, and the oversized order rested at the venue until it triggered.
        """
        # A leg filling ends the group, so there is nothing left to resize. Checked first: with
        # `resting_qty` read live, a filled leg and its cancelled OCO sibling both leave `is_open`,
        # so a replace check ahead of this one arms a fresh group against a position that has
        # already gone flat and then cancels it.
        filled = [leg for leg in group.legs.values() if leg.state is OrderState.FILLED]
        if filled:
            await self._close_group(group)
            # Released here rather than by the runner's prune: a stop fills *between* cycles, after
            # the cycle that placed it has already pruned what it named, so nothing else would.
            await self._release(
                group,
                action="group_closed",
                detail=(
                    f"{group.order.client_order_id}: its {filled[0].role.value} leg "
                    f"{filled[0].client_order_id} filled; the group is closed"
                ),
            )
            return
        if self._lapsed(group):
            await self._release(
                group,
                action="group_lapsed",
                detail=(
                    f"{group.order.client_order_id}: ended {group.order.state.value} "
                    "with nothing filled"
                ),
            )
            return
        if target is None:
            # Outside the allocation, which is not the same fact as a target of zero. Zero means
            # "the holding behind this group is gone, release its legs"; `None` means there is
            # nothing here to size — the entry carries no protective plan. Conflating them cancels
            # the legs of every group adopted at startup, because the `orders` projection does not
            # persist `protective` and `_persisted_open_orders` rebuilds the entry without it.
            return
        if self._spent(group):
            # Ahead of the minimums suppression: a spent group is retired whatever was last
            # refused for it, or it would linger with its entitlement at zero for ever.
            await self._retire(group)
            return
        if target == group.unprotected_at:
            # A deterministic refusal that has already been reported, and already acted on — the
            # legs were cancelled when it was found. `plan_legs` is a pure function of the target
            # and the venue's rules, so the same target refuses again: retrying it would file the
            # same `unprotected_position` every poll. This is the *only* suppression, and it is
            # sound only because nothing else sets the marker — a venue failure is transient and
            # must be retried (review C1).
            return
        if self._needs_new_legs(group, target):
            await self._replace_legs(group, target)

    def _needs_new_legs(self, group: _Tracked, target: Decimal) -> bool:
        """Whether the legs must be cancelled and re-placed to guard `target`.

        Two clauses, and each is load-bearing:

        * **The requested target moved.** Compared against what the resting legs were *requested*
          for, never against `resting_qty`, because those are not the same kind of number:
          `plan_legs` floors its quantity to `lot_size`, so a target that is not a lot multiple
          can never equal the venue's answer and would replace the legs on every poll for ever
          (review C2). Like against like, so a position that is not moving is a group that is not
          moving.
        * **The legs vanished while something is still held.** Design D2: legs cancelled at the
          venue — by the venue itself, or by an operator in its own UI — are re-armed on the next
          poll. The first clause alone would see an unchanged target, do nothing, and leave the
          position bare with the monitor believing it guarded.
        """
        return target != group.requested_qty or (target > ZERO and group.resting_qty <= ZERO)

    async def _replace_legs(self, group: _Tracked, target: Decimal) -> None:
        if target <= ZERO:
            # Nothing is held behind this group any more. Not "unprotected" — that event means
            # money is at risk with no stop, and this guards nothing and risks nothing (§2.3).
            await self._cancel_legs(group, reason="released_to_position")
            group.requested_qty = ZERO
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
            # Cancel *first*. What is resting was sized for a larger holding: if it triggered the
            # venue would reject it for insufficient balance, so leaving it is a false protection
            # on top of a true report (design §2.3).
            await self._cancel_legs(group, reason="below_venue_minimums")
            group.requested_qty = ZERO
            # Deterministic, so the repeat is worth suppressing: the same target below the same
            # minimums refuses identically next poll. Set at this call site rather than inside
            # `_record_unprotected`, which both refusal paths share — the venue-failure path below
            # must leave no marker behind (review C1).
            group.unprotected_at = target
            await self._record_unprotected(group, plan.unprotected_reason)
            return

        await self._cancel_legs(group, reason="resized_to_position")
        # From here until `submit_group` returns, nothing of ours rests. Recorded before the call
        # because it has to survive the exception below: the retry on the next poll is the only
        # thing that re-arms a bare position, and it is driven off this field.
        group.requested_qty = ZERO
        group.revision += 1
        try:
            placed = await self._execution.submit_group(plan.intents, group.instrument)
        except Exception as error:
            # The legs are already cancelled, so the position is bare until the next poll retries
            # — and it does retry, because nothing on this path suppresses it: `requested_qty` is
            # back to zero against a non-zero target, and `unprotected_at` is deliberately left
            # alone. A venue error is transient; de-duplicating it the way a below-minimums
            # refusal is de-duplicated disarms the position permanently on one 5xx (review C1).
            # The venue error still reaches the caller's retry budget — what changes is that the
            # log says what state this left behind instead of showing a cancel and then nothing.
            #
            # `except Exception` is deliberate and is not a swallow: it records and then
            # re-raises, so the error's class still governs handling exactly as before —
            # `RetryableError` / `FailClosedError` / `FatalError` reach the caller unchanged.
            # Narrowing this to those three types would silently skip the recording for any
            # *unexpected* exception, which is the precise absence this task exists to close.
            await self._record_unprotected(group, f"placement failed: {error}")
            raise
        for leg in placed:
            group.legs[leg.client_order_id] = leg
        group.requested_qty = target
        group.unprotected_at = None
        events = self._execution.events_for(entry)
        await self._store.append(events.protective_placed(entry, tuple(placed)))

    @staticmethod
    def _spent(group: _Tracked) -> bool:
        """The entry is done filling, did fill, and none of what it bought is left to guard.

        `filled_qty > 0` keeps an entry that expired unfilled out of it: that one never guarded
        anything, so there is nothing to retire and the runner's own `prune` forgets it.
        """
        return (
            group.entitled <= ZERO
            and not group.order.state.is_open
            and group.order.filled_qty > ZERO
        )

    @staticmethod
    def _lapsed(group: _Tracked) -> bool:
        """An entry that ended — expired, cancelled, rejected — having filled nothing and placed no
        legs. Typically one whose TTL ran out on the supervisor's sweep, after its own cycle's
        `prune` had already run, so nothing else would ever forget it."""
        return (
            group.order.role is OrderRole.ENTRY
            and not group.order.state.is_open
            and group.order.filled_qty <= ZERO
            and not group.legs
        )

    async def _retire(self, group: _Tracked) -> None:
        """Close a group whose holding is gone, record it, and release it.

        RCA 2026-10-28 §4 item 2: a group only released to zero stayed tracked with its stop and
        take-profit in hand, and the next entry's shares were handed to it first. Released only
        after the cancel and the record have both succeeded, so a venue or store error leaves it
        tracked and the next poll retries the whole retirement.
        """
        await self._cancel_legs(group, reason="released_to_position")
        entry = group.order
        await self._release(
            group,
            action="group_retired",
            detail=(
                f"{entry.client_order_id}: none of the {entry.filled_qty} it bought is left in the "
                "holding; its legs are cancelled and a later entry is guarded by its own plan"
            ),
        )

    async def _release(self, group: _Tracked, *, action: str, detail: str) -> None:
        """Record that this group is no longer tracked, then stop tracking it.

        The record comes first: a store failure leaves the group tracked, and the next poll
        records and releases it again. Moved to `_released`, not dropped, so a caller reading
        `tracked` straight after this poll still sees each order exactly as it settled.
        """
        entry = group.order
        events = self._execution.events_for(entry)
        await self._store.append(
            events.risk_event(
                tier=RiskTier.EXECUTION,
                rule=TRACKED_GROUP_RULE,
                scope=entry.instrument_key,
                action=action,
                detail=detail,
            )
        )
        logger.info(
            "group released", extra={"group_id": entry.group_id, "action": action, "detail": detail}
        )
        released = self._tracked.pop(entry.group_id, None)
        if released is not None:
            self._released[entry.group_id] = released

    async def _record_unprotected(self, group: _Tracked, reason: str) -> None:
        """Say that this position is bare, whichever way it got there.

        Shared by both refusal paths and *reports only*: the operator needs to know the guard is
        missing whether the plan refused or the venue did. Which of the two it was decides whether
        the attempt repeats, and that is the caller's to record — a marker set here would be set
        for both (review C1).
        """
        entry = group.order
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
            self._entitle(instrument_key, groups)
            budget = max(
                ZERO, self._execution.held(instrument_key) - self._committed(instrument_key)
            )
            for group in groups:
                target = min(group.entitled, budget)
                targets[group.order.group_id] = target
                budget -= target
        return targets

    def _entitle(self, instrument_key: str, groups: list[_Tracked]) -> None:
        """Move each group's entitlement by what happened since the last allocation.

        RCA 2026-10-28 §4 item 2. Entitling every group to its `filled_qty` let one whose holding a
        discretionary SELL had emptied claim the *next* entry's shares, tightest stop first, and
        re-place its months-old legs. So an entitlement only ever grows by the group's own entry
        fills and only ever shrinks otherwise — by its own legs' fills, or by a reduction from some
        other path. It is *held* that is shared out here, not `held - committed`: a resting SELL
        sells nothing until it fills, so one that expires lets the group re-arm to what it had.

        New fills are shared out first, then what each group already had, both tightest stop
        first. A shortfall therefore lands on the older entitlements: the shares an entry has just
        bought are that entry's, and what is missing is what was sold — including when the exit and
        the re-entry both land between the same two polls. With nothing new it is exactly the old
        tightest-first allocation, so the tighter stop still keeps its cover.
        """
        budget = max(ZERO, self._execution.held(instrument_key))
        moved = [group.net_filled - group.credited for group in groups]
        new = [max(change, ZERO) for change in moved]
        # A group's own exits come off its own entitlement before anything is shared out.
        kept = [
            max(ZERO, group.entitled + min(change, ZERO))
            for group, change in zip(groups, moved, strict=True)
        ]
        granted = [ZERO] * len(groups)
        for claims in (new, kept):
            for index, claim in enumerate(claims):
                take = min(claim, budget)
                granted[index] += take
                budget -= take
        for group, entitled in zip(groups, granted, strict=True):
            group.entitled = entitled
            group.credited = group.net_filled
        self._report_surplus(instrument_key, budget if groups else ZERO)

    def _report_surplus(self, instrument_key: str, surplus: Decimal) -> None:
        """Say so when more is held than the live groups are entitled to guard.

        Two causes, and from here they cannot be told apart. A deposit, or a position adopted at a
        restart, is unguarded by design and is no `unprotected_position`. But the ratchet never
        grows back, so a ledger that under-read for a poll — a fill booked whose order update was
        then lost — leaves a group under-guarded for good, and this line is the only trace of it.
        Logged when the figure changes, not on every sweep.
        """
        if surplus <= ZERO:
            self._surplus_logged.pop(instrument_key, None)
            return
        if self._surplus_logged.get(instrument_key) == surplus:
            return
        self._surplus_logged[instrument_key] = surplus
        logger.warning(
            "more is held than the protective groups are entitled to guard",
            extra={"instrument_key": instrument_key, "unguarded": str(surplus)},
        )

    def _protectable(self, instrument_key: str) -> list[_Tracked]:
        """This instrument's groups that can hold legs at all, tightest stop first.

        A group whose entry carries no `ProtectivePlan` is not one of them: a reducing SELL *is*
        the exit and an unprotected venue was charged the sizing haircut instead (`protective_plan`
        returns `None` for both). Running one through `plan_legs` is what made every filled
        discretionary SELL file an `unprotected_position` for an order that needs none.
        """
        ranked = [
            (
                (
                    multiply(plan.stop_price, _RANK_SIGN[group.order.side]),
                    group.order.created_at,
                    group.order.client_order_id,
                ),
                group,
            )
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
        # Ties break on creation then id because startup adopts orders from the database in
        # arbitrary order, and the allocation must survive a restart unchanged. Ascending, because
        # `_RANK_SIGN` already points the tightest stop at the smallest key for whichever side
        # opened it — a `reverse` flag here would have to know the side too, and be right about it.
        ranked.sort(key=lambda pair: pair[0])
        return [group for _, group in ranked]

    def _committed(self, instrument_key: str) -> Decimal:
        """Quantity our own working sells already commit, outside a group's *own* protective legs.

        A discretionary exit or an ADR 0015 operator close still resting reserves the base asset at
        the venue exactly as a stop does — and so does a protective leg that ended up as a group's
        `order`. That happens: `track()` makes a recovered leg the group's own `order` when its
        entry was already terminal and so was never re-adopted, and that leg's reservation is as
        real as an entry's. What must never be double-counted is a group's own `legs`, which this
        sum never reads — that is what the budget `_targets` computes is being divided among.
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
        self._released.pop(group_id, None)

    def prune(self, *group_ids: str) -> None:
        """Forget settled groups. Named groups only, or all of them when none are named.

        A basket prunes *its own* groups: pruning everything would let one basket drop a group
        another basket's cycle is still reading, and the second would then report the order as it
        was before the poll rather than as it settled.
        """
        everything = {**self._tracked, **self._released}
        wanted = set(group_ids) or set(everything)
        for group_id, group in everything.items():
            settled = not group.order.state.is_open and all(
                not leg.state.is_open for leg in group.legs.values()
            )
            if group_id in wanted and settled and group.order.role is OrderRole.ENTRY:
                self.forget(group_id)
