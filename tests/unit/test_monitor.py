"""ExecutionMonitor: TTL enforcement, idempotent booking, and protective-group upkeep.

Each of these is a way an order can quietly go wrong after the submit succeeded — which is the
half of an order's life that a cycle-based system does not watch by default.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tradebot.core.clock import ManualClock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import FailClosedError, RetryableError, UnbookableFillError, VenueError
from tradebot.core.events import Event, EventType
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.orders import Fill, Order, OrderIntent, ProtectivePlan
from tradebot.core.portfolio import Position
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import OrderRef, OrderStatus
from tradebot.ledger.portfolio import Ledger
from tradebot.persistence.store import EventStore

PLAN = ProtectivePlan(stop_price=Decimal("48000"), take_profit_price=Decimal("54000"))


@pytest.fixture
def broker(clock: ManualClock) -> SimBroker:
    return SimBroker(clock, balances={"USDT": Decimal(100_000)})


@pytest.fixture
def monitor(
    broker: SimBroker, store: EventStore, ledger: Ledger, clock: ManualClock
) -> ExecutionMonitor:
    return ExecutionMonitor(
        broker,
        ExecutionService(broker, store, ledger, clock),
        store,
        clock,
    )


def tick(
    instrument: Instrument,
    clock: ManualClock,
    *,
    last: str,
    high: str | None = None,
    low: str | None = None,
) -> Tick:
    price = Decimal(last)
    return Tick(
        instrument_key=instrument.key,
        bid=price,
        ask=price,
        last=price,
        high=Decimal(high) if high else price,
        low=Decimal(low) if low else price,
        covers_since=clock.now(),
        observed_at=clock.now(),
    )


def entry_intent(
    instrument: Instrument,
    clock: ManualClock,
    *,
    price: str = "50000",
    qty: str = "0.5",
    ttl: int | None = 60,
    plan: ProtectivePlan | None = PLAN,
    coid: str = "sim-ENTRY",
    side: Side = Side.BUY,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=coid,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=side,
        qty=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(price),
        protective=plan,
        ttl_seconds=ttl,
        created_at=clock.now(),
    )


def external_sell(instrument: Instrument, clock: ManualClock, *, qty: str) -> Fill:
    """A reduction booked by some other path: another cycle's exit, or an operator close.

    KNOWN_GAPS §4's own case was a plain cycle SELL (`sim-R7GB2OIBDAQWPVTG`), not a manual close.
    """
    return Fill(
        fill_id=f"external-{qty}",
        client_order_id="sim-ELSEWHERE",
        instrument_key=instrument.key,
        side=Side.SELL,
        qty=Decimal(qty),
        price=Decimal("49000"),
        filled_at=clock.now(),
    )


def book(ledger: Ledger, fill: Fill, instrument: Instrument) -> None:
    ledger.apply_fill(
        fill,
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
    )


async def submit_entry(
    monitor: ExecutionMonitor,
    broker: SimBroker,
    store: EventStore,
    ledger: Ledger,
    clock: ManualClock,
    instrument: Instrument,
    **kwargs: object,
) -> None:
    service = ExecutionService(broker, store, ledger, clock)
    order = await service.submit(entry_intent(instrument, clock, **kwargs), instrument)  # type: ignore[arg-type]
    monitor.track(order, instrument)


class TestTtl:
    async def test_an_unfilled_order_is_cancelled_at_its_ttl(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Binance spot has no venue-side good-till-time, so expiry is ours to enforce."""
        broker.observe(tick(instrument, clock, last="51000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, price="45000")

        await monitor.poll()
        assert monitor.working, "the order rests while inside its TTL"

        clock.advance(61)
        await monitor.poll()

        assert not monitor.working
        assert monitor.tracked[0].state is OrderState.EXPIRED

    async def test_an_order_with_no_ttl_keeps_working(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="51000"))
        await submit_entry(
            monitor, broker, store, ledger, clock, instrument, price="45000", ttl=None
        )

        clock.advance(100_000)
        await monitor.poll()

        assert monitor.working

    async def test_a_partial_fill_at_ttl_keeps_its_fills_and_cancels_the_rest(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The remainder is cancelled and the fill ratio recorded (DESIGN §8.1)."""
        broker = SimBroker(clock, balances={"USDT": Decimal(100_000)}, fill_ratio=Decimal("0.4"))
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, plan=None), instrument)
        monitor.track(order, instrument)

        clock.advance(61)
        await monitor.poll()

        settled = monitor.tracked[0]
        assert settled.state is OrderState.EXPIRED
        assert settled.filled_qty == Decimal("0.2")
        assert ledger.position(instrument.key).qty == Decimal("0.2")

    async def test_a_fill_booked_before_a_failed_ttl_cancel_is_not_booked_again(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Final review I3: the sync books the fill, then the cancel fails transiently.

        Before the fix the synced order was only assigned once the cancel returned, so the group
        kept the pre-sync order and the next poll saw the same fill as new and booked it again —
        a holding inflated by every retry, which a later reduce-only SELL is sized from.
        """
        broker = SimBroker(clock, balances={"USDT": Decimal(100_000)}, fill_ratio=Decimal("0.4"))
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="51000"))
        order = await service.submit(
            entry_intent(instrument, clock, price="45000", plan=None), instrument
        )
        monitor.track(order, instrument)
        clock.advance(61)
        broker.observe(tick(instrument, clock, last="45000", low="44000"))

        real_cancel = broker.cancel
        failures = [VenueError("503 on cancel")]

        async def flaky_cancel(order_ref: OrderRef) -> object:
            if failures:
                raise failures.pop()
            return await real_cancel(order_ref)

        monkeypatch.setattr(broker, "cancel", flaky_cancel)

        with pytest.raises(VenueError):
            await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.2")

        await monitor.poll()

        assert ledger.position(instrument.key).qty == Decimal("0.2"), "booked once, not twice"
        settled = monitor.tracked[0]
        assert settled.state is OrderState.EXPIRED
        assert settled.filled_qty == Decimal("0.2")


class TestBooking:
    async def test_polling_twice_does_not_book_a_fill_twice(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The monitor re-reads the same order every poll; a double count is a phantom position."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, plan=None)

        await monitor.poll()
        await monitor.poll()

        assert ledger.position(instrument.key).qty == Decimal("0.5")
        fills = [e for e in store.read_all() if e.type is EventType.FILL_RECEIVED]
        assert len(fills) == 1


class TestScopedPoll:
    """ADR 0030: a cycle settles its own basket's instruments, and nothing else's."""

    async def test_a_scoped_poll_touches_only_the_named_instruments(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        # Both entries rest below the market, then both markets trade through them.
        broker.observe(tick(instrument, clock, last="51000"))
        broker.observe(tick(second_instrument, clock, last="3000"))
        await submit_entry(
            monitor, broker, store, ledger, clock, instrument, price="50000", ttl=None, plan=None
        )
        await submit_entry(
            monitor,
            broker,
            store,
            ledger,
            clock,
            second_instrument,
            price="2900",
            qty="1",
            ttl=None,
            plan=None,
            coid="sim-ETH",
        )
        clock.advance(60)
        broker.observe(tick(instrument, clock, last="49000"))
        broker.observe(tick(second_instrument, clock, last="2800"))

        await monitor.poll(frozenset({second_instrument.key}))

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert ledger.position(instrument.key).is_flat, "outside the scope: not synced"

        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.5")

    async def test_a_scope_holding_no_group_reaches_no_venue(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        """The live-cost claim in ADR 0030: a basket holding nothing pays nothing."""
        broker.observe(tick(instrument, clock, last="51000"))
        await submit_entry(
            monitor, broker, store, ledger, clock, instrument, price="50000", plan=None, ttl=None
        )
        calls: list[str] = []
        real = broker.fetch_order

        async def counted(ref: OrderRef) -> object:
            calls.append(ref.client_order_id)
            return await real(ref)

        broker.fetch_order = counted  # type: ignore[method-assign, assignment]
        await monitor.poll(frozenset({second_instrument.key}))

        assert calls == []


class TestProtectiveGroups:
    async def test_a_filled_entry_gets_its_venue_held_legs(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument)

        await monitor.poll()

        legs = [order for order in monitor.tracked if order.role.is_protective]
        assert {leg.role for leg in legs} == {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}
        assert all(leg.state.is_open for leg in legs)
        assert all(leg.group_id == "sim-ENTRY" for leg in legs)

    async def test_the_guarded_quantity_is_read_off_the_legs_not_remembered(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Design D2: `poll` already re-reads every leg from the venue, so a counter beside that
        answer is a second opinion that can drift — and its drift *is* KNOWN_GAPS §4."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        group = monitor._tracked["sim-ENTRY"]
        assert group.resting_qty == Decimal("0.5")
        first = {leg.client_order_id for leg in group.legs.values()}

        # Both legs, cancelled directly at the venue: SimBroker's OCO cancels a sibling only on a
        # *fill* (`_cancel_siblings`, called from `_fill`) — ADR 0011 scopes that guarantee to the
        # R13 hazard of both legs paying out, not to a stray cancel — so cancelling only one leg
        # would leave the other resting at its own 0.5 and prove nothing about `resting_qty`.
        for leg in group.legs.values():
            await broker.cancel(
                OrderRef(client_order_id=leg.client_order_id, instrument_key=instrument.key)
            )
        clock.advance(30)
        await monitor.poll()

        # A remembered counter would still read 0.5 here, see no change from the fill it recorded
        # long ago, and do nothing — the position would sit unguarded until the entry's own fill
        # next moved. Reading the venue's live answer instead means `poll` notices the gap itself
        # and `_maintain` re-arms it: a fresh pair of legs at the same target quantity, sharing no
        # id with the pair that was just cancelled.
        live = {
            leg.client_order_id
            for leg in group.legs.values()
            if leg.role.is_protective and leg.state.is_open
        }
        assert live and not live & first, "fresh legs were placed, not the cancelled ones"
        assert all(group.legs[client_order_id].qty == Decimal("0.5") for client_order_id in live)

    async def test_legs_are_replaced_when_more_of_the_entry_fills(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """No venue lets a resting order's quantity be edited, so a resize is cancel-and-replace."""
        broker = SimBroker(clock, balances={"USDT": Decimal(100_000)}, fill_ratio=Decimal("0.5"))
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()
        first = {leg.client_order_id for leg in monitor.tracked if leg.role.is_protective}

        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        live = {
            leg.client_order_id
            for leg in monitor.tracked
            if leg.role.is_protective and leg.state.is_open
        }
        assert live and not live & first, "the old legs are cancelled, not left resting"
        assert all(
            leg.qty == ledger.position(instrument.key).qty
            for leg in monitor.tracked
            if leg.role.is_protective and leg.state.is_open
        )

    async def test_a_filled_stop_closes_the_group(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        # A bar that fell from 49000 through the 48000 trigger to 46000: for the stop to arm,
        # the price has to have come from above it.
        clock.advance(60)
        broker.observe(tick(instrument, clock, last="46500", high="49000", low="46000"))
        await monitor.poll()

        legs = [order for order in monitor.tracked if order.role.is_protective]
        assert any(leg.state is OrderState.FILLED for leg in legs)
        assert not any(leg.state.is_open for leg in legs), "no exit outlives the position"
        assert ledger.position(instrument.key).is_flat

    async def test_a_venue_without_protective_orders_flags_the_position_loudly(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Unprotected is an explicit, visible risk — never a silent one (DESIGN §6.7, R12)."""
        tiny = instrument.model_copy(update={"min_notional": Decimal("1000000")})
        broker = SimBroker(clock, balances={"USDT": Decimal(100_000)})
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(tiny, clock, last="49000"))
        order = await service.submit(entry_intent(tiny, clock), tiny)
        monitor.track(order, tiny)

        await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == ["unprotected_position"]

    async def test_an_unprotected_position_is_flagged_once_not_every_poll(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A warning repeated every ten seconds is a warning nobody reads."""
        tiny = instrument.model_copy(update={"min_notional": Decimal("1000000")})
        broker = SimBroker(clock, balances={"USDT": Decimal(100_000)})
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(tiny, clock, last="49000"))
        order = await service.submit(entry_intent(tiny, clock), tiny)
        monitor.track(order, tiny)

        await monitor.poll()
        await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert len(risk) == 1


class TestLifecycle:
    async def test_a_settled_group_stops_being_polled(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Poll work must stay proportional to live orders, or the venue bans the key."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, plan=None)
        await monitor.poll()

        monitor.prune()

        assert monitor.tracked == ()

    async def test_a_group_with_resting_legs_is_kept(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The legs outlive the cycle: that is the whole point of holding them at the venue."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument)
        await monitor.poll()

        monitor.prune()

        assert any(order.role.is_protective for order in monitor.tracked)


class TestHeld:
    async def test_held_answers_from_the_ledger(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Deliberately the ledger and not the venue — design D2.

        A monitor that asked the venue and quietly resized to its figure would absorb the one
        alarm `Reconciler` exists to raise (ADR 0006, KNOWN_GAPS §1).
        """
        service = ExecutionService(broker, store, ledger, clock)
        assert service.held(instrument.key) == Decimal(0)

        broker.observe(tick(instrument, clock, last="49000"))
        await service.submit(entry_intent(instrument, clock), instrument)

        assert service.held(instrument.key) == ledger.position(instrument.key).qty
        assert service.held(instrument.key) > Decimal(0)


class TestLegsTrackThePosition:
    """KNOWN_GAPS §4. The monitor had no view of the position, so a SELL from any other path
    reduced the holding while the legs kept their original size."""

    async def test_legs_shrink_when_the_position_is_reduced_elsewhere(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.5")

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        live = [leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open]
        assert live, "the position still exists, so it is still guarded"
        assert {leg.qty for leg in live} == {Decimal("0.3")}

    async def test_a_position_closed_elsewhere_releases_the_legs_without_flagging_it(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Zero is not "unprotected": that event means money is at risk with no stop behind it,
        and this group now guards nothing and risks nothing (design §2.3)."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        book(ledger, external_sell(instrument, clock, qty="0.5"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        assert not [leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open]
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        # The group is retired and says so (RCA 2026-10-28 §4 item 2) — a record, not a flag.
        assert [(e.payload["rule"], e.payload["action_taken"]) for e in risk] == [
            ("tracked_group", "group_retired")
        ], "nothing is at risk, so nothing is flagged"

    @pytest.mark.parametrize("tight_first", [True, False])
    async def test_the_tighter_stop_keeps_its_cover_whichever_was_opened_first(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        tight_first: bool,
    ) -> None:
        """Design D3. For a long the tightest stop is the *highest* — it fires first on the way
        down, so it is the cover worth keeping. Parametrized because age is a proxy that inverts:
        one order of these two fails under oldest-first, the other under newest-first."""
        tight = ("sim-TIGHT", ProtectivePlan(stop_price=Decimal("48000")))
        wide = ("sim-WIDE", ProtectivePlan(stop_price=Decimal("45000")))
        broker.observe(tick(instrument, clock, last="49000"))
        for coid, plan in (tight, wide) if tight_first else (wide, tight):
            await submit_entry(
                monitor,
                broker,
                store,
                ledger,
                clock,
                instrument,
                coid=coid,
                qty="0.3",
                plan=plan,
                ttl=None,
            )
            clock.advance(1)
        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.6")

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        guarded = {
            leg.group_id: leg.qty
            for leg in monitor.tracked
            if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
        }
        assert guarded == {"sim-TIGHT": Decimal("0.3"), "sim-WIDE": Decimal("0.1")}

    async def test_a_working_discretionary_sell_reduces_the_budget(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A resting exit reserves the base asset exactly as a stop does. Ignoring it commits
        0.5 + 0.2 against a holding of 0.5, and a real venue rejects one of them (design §2.2)."""
        service = ExecutionService(broker, store, ledger, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        # Well above the market, so it rests rather than crossing.
        exit_order = await service.submit(
            entry_intent(
                instrument,
                clock,
                coid="sim-EXIT",
                qty="0.2",
                price="60000",
                side=Side.SELL,
                plan=None,
                ttl=None,
            ),
            instrument,
        )
        monitor.track(exit_order, instrument)
        clock.advance(30)
        await monitor.poll()

        stops = [
            leg.qty
            for leg in monitor.tracked
            if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
        ]
        assert stops == [Decimal("0.3")]
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert not risk, "a reducing SELL is the exit; it needs no protection and reports none"

    async def test_a_filled_stop_does_not_starve_the_surviving_group(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Two groups each hold 0.3 of a 0.6 position; the tighter stop fires on its own — no
        external SELL involved — and takes the position to 0.3. Ranking a *closing* group ahead
        of a live one in `_targets` would still hand it the whole remaining budget: it would
        re-arm a fresh group against a holding it no longer has, and starve the surviving group to
        a target of 0 — cancelling its legs and leaving the surviving 0.3 unguarded. This is the
        case where the bug silently leaves real money unguarded, rather than merely over-cancelling
        or under-sizing."""
        tight = ("sim-TIGHT", ProtectivePlan(stop_price=Decimal("48000")))
        wide = ("sim-WIDE", ProtectivePlan(stop_price=Decimal("45000")))
        broker.observe(tick(instrument, clock, last="49000"))
        for coid, plan in (tight, wide):
            await submit_entry(
                monitor,
                broker,
                store,
                ledger,
                clock,
                instrument,
                coid=coid,
                qty="0.3",
                plan=plan,
                ttl=None,
            )
            clock.advance(1)
        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.6")
        guarded_before = {
            leg.group_id: leg.qty
            for leg in monitor.tracked
            if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
        }
        assert guarded_before == {"sim-TIGHT": Decimal("0.3"), "sim-WIDE": Decimal("0.3")}

        # A bar whose low crosses TIGHT's trigger (48000) but stays above WIDE's (45000): only
        # the tighter stop arms and fills. `high` clears TIGHT's limit (48000 less the 0.5%
        # offset, 47760) so the armed leg actually trades through rather than resting untriggered.
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="47000", high="49000", low="47000"))
        await monitor.poll()

        assert ledger.position(instrument.key).qty == Decimal("0.3"), "the tight stop filled"
        live = {
            leg.group_id: leg.qty
            for leg in monitor.tracked
            if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
        }
        assert live == {"sim-WIDE": Decimal("0.3")}, "the surviving position stays guarded"

    async def test_a_group_recovered_without_its_protective_plan_keeps_its_legs(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Fix round 1, finding 1. `orders` has no `protective` column, so every order a restart
        recovers has `protective=None` — including one guarding a live position with legs already
        resting at the venue (`startup._persisted_open_orders`). Absent from the allocation must
        not read the same as a target of zero: the first poll after every restart would otherwise
        cancel the legs of every group it adopted, silently — no `unprotected_position` event,
        because the zero-target path in `_replace_legs` is "released to position", not "flagged"."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        group = monitor._tracked["sim-ENTRY"]
        assert group.resting_qty == Decimal("0.5")
        before = {leg.client_order_id for leg in group.legs.values() if leg.state.is_open}

        # What a restart actually hands back: the same entry, still holding the position, with
        # its legs still resting at the venue, but no protective plan — the `orders` projection
        # never persisted one.
        group.order = group.order.model_copy(update={"protective": None})
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        live = {
            leg.client_order_id
            for leg in group.legs.values()
            if leg.role.is_protective and leg.state.is_open
        }
        assert live == before, "a plan a restart cannot recover must not cost the position its legs"
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert not risk, "nothing changed, so nothing is flagged"

    async def test_a_shrink_below_venue_minimums_cancels_the_legs_and_reports(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Today the reason is recorded and the legs are left resting — an oversized order at the
        venue *and* a report saying the position is unguarded, both false (design §2.3)."""
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        # 0.0001 BTC at 49 000 is 4.90, below the instrument's min_notional of 10.
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        clock.advance(30)
        await monitor.poll()

        assert not [
            leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open
        ], "an oversized leg must not outlive the holding it was sized for"
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == ["unprotected_position"]

    async def test_a_failed_placement_after_the_cancel_is_recorded_before_it_propagates(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The window is forced by the venue (design D4); what must not happen is a cancellation
        followed by silence, leaving the state to be inferred from an absence."""

        class _FailingGroupBroker(SimBroker):
            fail_group = False

            async def submit_group(self, intents):  # type: ignore[no-untyped-def]
                if self.fail_group:
                    self.fail_group = False
                    raise RetryableError("venue unavailable")
                return await super().submit_group(intents)

        broker = _FailingGroupBroker(clock, balances={"USDT": Decimal(100_000)})
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        broker.fail_group = True
        clock.advance(30)
        with pytest.raises(RetryableError):
            await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == ["unprotected_position"]
        assert "venue unavailable" in risk[0].payload["detail"]

    async def test_a_successful_placement_re_arms_the_report(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """`unprotected_at` is cleared when legs are placed (design §2.3). Without that, a position
        that becomes unguardable, recovers, and becomes unguardable again at the same size is
        reported once and never again."""
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        async def rest_an_exit(coid: str) -> None:
            # Down to dust: 0.0001 at 49 000 is 4.90, under the instrument's min_notional of 10.
            # A *resting* exit rather than a booked one: a group's entitlement never grows back
            # after a filled reduction (RCA 2026-10-28 §4 item 2), so the only way back up to
            # guardable is a commitment that lapses.
            exit_order = await service.submit(
                entry_intent(
                    instrument,
                    clock,
                    coid=coid,
                    qty="0.4999",
                    price="60000",
                    side=Side.SELL,
                    plan=None,
                    ttl=60,
                ),
                instrument,
            )
            monitor.track(exit_order, instrument)
            clock.advance(30)
            await monitor.poll()

        await rest_an_exit("sim-EXIT-1")
        # The exit lapses: guardable again at the full size, then down to the same dust again.
        clock.advance(60)
        await monitor.poll()
        assert monitor._tracked["sim-ENTRY"].resting_qty == Decimal("0.5")
        await rest_an_exit("sim-EXIT-2")

        # The lapsed exit is released with its own `tracked_group` record; only the flags count.
        risk = [
            e
            for e in store.read_all()
            if e.type is EventType.RISK_EVENT and e.payload["rule"] != "tracked_group"
        ]
        assert [e.payload["rule"] for e in risk] == [
            "unprotected_position",
            "unprotected_position",
        ], "the second time it becomes unguardable is a second fact, not a repeat"


LATER_PLAN = ProtectivePlan(stop_price=Decimal("38000"), take_profit_price=Decimal("44000"))


def _guarded(monitor: ExecutionMonitor) -> dict[str, Decimal]:
    """Each group's resting stop, by group: what the venue is guarding, and for whom."""
    return {
        leg.group_id: leg.qty
        for leg in monitor.tracked
        if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
    }


def _retired(store: EventStore) -> list[Event]:
    return [
        e
        for e in store.read_all()
        if e.type is EventType.RISK_EVENT and e.payload["action_taken"] == "group_retired"
    ]


class TestAGroupWhoseHoldingIsGoneDoesNotComeBack:
    """RCA 2026-10-28 §4 item 2. A group released by a discretionary SELL kept its `filled_qty`
    entitlement, ranked first on its stale, tighter stop, and took the next entry's shares —
    re-placing March's legs against May's position. In live the venue rejects those prices and the
    new position is left with no venue-held stop at all."""

    async def _entry_a(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(
            monitor, broker, store, ledger, clock, instrument, coid="sim-A", ttl=None
        )
        await monitor.poll()
        assert _guarded(monitor) == {"sim-A": Decimal("0.5")}

    async def _entry_b(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        *,
        qty: str,
    ) -> None:
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="40000"))
        await submit_entry(
            monitor,
            broker,
            store,
            ledger,
            clock,
            instrument,
            coid="sim-B",
            price="40000",
            qty=qty,
            plan=LATER_PLAN,
            ttl=None,
        )

    async def test_a_new_entry_is_guarded_by_its_own_plan_not_a_released_group(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await self._entry_a(monitor, broker, store, ledger, clock, instrument)
        book(ledger, external_sell(instrument, clock, qty="0.5"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        await self._entry_b(monitor, broker, store, ledger, clock, instrument, qty="0.3")
        await monitor.poll()

        assert _guarded(monitor) == {"sim-B": Decimal("0.3")}
        stops = {
            leg.stop_price
            for leg in monitor.working
            if leg.role is OrderRole.STOP_LOSS and leg.group_id == "sim-B"
        }
        assert stops == {LATER_PLAN.stop_price}, "B is guarded at B's own stop"
        assert _armed(store)[-1].aggregate_id == "sim-B"
        assert "sim-A" not in monitor._tracked, "A's holding is gone, so A is forgotten"
        (retired,) = _retired(store)
        assert retired.payload["scope"] == instrument.key
        assert "sim-A" in retired.payload["detail"]
        flagged = [
            e
            for e in store.read_all()
            if e.type is EventType.RISK_EVENT and e.payload["rule"] == "unprotected_position"
        ]
        assert not flagged

    async def test_a_partial_sell_then_a_new_entry_does_not_grow_the_old_group_back(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        await self._entry_a(monitor, broker, store, ledger, clock, instrument)
        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()
        assert _guarded(monitor) == {"sim-A": Decimal("0.3")}

        await self._entry_b(monitor, broker, store, ledger, clock, instrument, qty="0.4")
        await monitor.poll()

        assert _guarded(monitor) == {"sim-A": Decimal("0.3"), "sim-B": Decimal("0.4")}
        assert not _retired(store), "A still holds 0.3, so it is still live"

    async def test_a_sell_and_a_new_entry_between_the_same_two_polls(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """No poll separates the exit from the re-entry: a resting exit that fills just before the
        next cycle's entry does. The shares just bought belong to the entry that bought them; what
        is missing is what the older group lost."""
        await self._entry_a(monitor, broker, store, ledger, clock, instrument)
        book(ledger, external_sell(instrument, clock, qty="0.5"), instrument)
        await self._entry_b(monitor, broker, store, ledger, clock, instrument, qty="0.3")
        await monitor.poll()

        assert _guarded(monitor) == {"sim-B": Decimal("0.3")}
        assert "sim-A" not in monitor._tracked

    async def test_a_resting_sell_that_expires_lets_the_group_re_arm_to_its_capacity(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A resting exit commits the base asset but sells none of it: the group's legs shrink
        while it rests and grow back when it expires, because nothing was ever taken away."""
        await self._entry_a(monitor, broker, store, ledger, clock, instrument)
        service = ExecutionService(broker, store, ledger, clock)
        exit_order = await service.submit(
            entry_intent(
                instrument,
                clock,
                coid="sim-EXIT",
                qty="0.2",
                price="60000",
                side=Side.SELL,
                plan=None,
                ttl=60,
            ),
            instrument,
        )
        monitor.track(exit_order, instrument)
        clock.advance(30)
        await monitor.poll()
        assert _guarded(monitor) == {"sim-A": Decimal("0.3")}

        clock.advance(60)
        await monitor.poll()

        assert _guarded(monitor) == {"sim-A": Decimal("0.5")}
        assert not _retired(store)


def _released(store: EventStore, action: str) -> list[Event]:
    return [
        e
        for e in store.read_all()
        if e.type is EventType.RISK_EVENT
        and e.payload["rule"] == "tracked_group"
        and e.payload["action_taken"] == action
    ]


def _submitted(store: EventStore) -> int:
    return sum(1 for e in store.read_all() if e.type is EventType.ORDER_SUBMITTED)


class TestSettledGroupsAreReleasedWhateverCycleMadeThem:
    """Task 7b review: the runner prunes only the groups its own cycle named, so a group that
    settled later — on the supervisor's sweep — was never forgotten. Each is now released by the
    poll that settles it, with a record, and stays visible through `tracked` until the next poll
    so a caller reading straight after its own poll reports the order as it settled."""

    async def test_a_group_closed_by_its_own_stop_is_released_and_recorded(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        orders_before = _submitted(store)

        clock.advance(60)
        broker.observe(tick(instrument, clock, last="46500", high="49000", low="46000"))
        await monitor.poll()

        assert "sim-ENTRY" not in monitor._tracked
        (closed,) = _released(store, "group_closed")
        assert "sim-ENTRY" in closed.payload["detail"]
        # What a caller reading straight after this poll sees: the orders as they settled.
        settled = {order.client_order_id: order for order in monitor.tracked}
        assert any(
            o.role is OrderRole.STOP_LOSS and o.state is OrderState.FILLED for o in settled.values()
        )
        assert _submitted(store) == orders_before, "releasing a group places nothing"

        await monitor.poll()
        assert monitor.tracked == ()
        assert len(_released(store, "group_closed")) == 1, "recorded once"

    async def test_an_entry_that_lapses_unfilled_on_a_later_sweep_is_released(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="51000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, price="45000")
        await monitor.poll()
        monitor.prune("sim-ENTRY")  # the cycle's own prune: still working, so it is kept
        assert monitor.working

        clock.advance(61)
        await monitor.poll()

        assert "sim-ENTRY" not in monitor._tracked
        (lapsed,) = _released(store, "group_lapsed")
        assert "expired" in lapsed.payload["detail"]
        # `BasketRunner._settle` builds exactly this map after its poll and falls back to the
        # pre-poll order for anything missing: the release must not change what it reports.
        settled = {order.client_order_id: order for order in monitor.tracked}
        assert settled["sim-ENTRY"].state is OrderState.EXPIRED
        assert not monitor.working

        await monitor.poll()
        assert monitor.tracked == ()

    async def test_the_runners_prune_still_forgets_a_released_group(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="51000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, price="45000")
        clock.advance(61)
        await monitor.poll()

        monitor.prune("sim-ENTRY")

        assert monitor.tracked == ()


class TestRetirementRetries:
    """`_retire` releases only after the cancel and the record have both succeeded."""

    async def _flat_group(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        book(ledger, external_sell(instrument, clock, qty="0.5"), instrument)
        clock.advance(30)

    async def test_a_venue_failure_on_the_cancel_leaves_it_tracked_and_the_next_poll_retires(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._flat_group(monitor, broker, store, ledger, clock, instrument)
        real_cancel = broker.cancel
        failures = iter([VenueError("503 on cancel")])

        async def flaky_cancel(ref: OrderRef):  # type: ignore[no-untyped-def]
            failure = next(failures, None)
            if failure is not None:
                raise failure
            return await real_cancel(ref)

        monkeypatch.setattr(broker, "cancel", flaky_cancel)

        with pytest.raises(VenueError):
            await monitor.poll()
        assert "sim-ENTRY" in monitor._tracked
        assert not _released(store, "group_retired")

        await monitor.poll()

        assert "sim-ENTRY" not in monitor._tracked
        assert len(_released(store, "group_retired")) == 1
        assert not monitor.working, "every leg is cancelled"

    async def test_a_store_failure_on_the_record_leaves_it_tracked_and_the_next_poll_retires(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await self._flat_group(monitor, broker, store, ledger, clock, instrument)
        real_append = store.append
        failed: list[bool] = []

        async def flaky_append(*events: Event) -> None:
            if not failed and any(e.payload.get("action_taken") == "group_retired" for e in events):
                failed.append(True)
                raise OSError("disk full")
            await real_append(*events)

        monkeypatch.setattr(store, "append", flaky_append)

        with pytest.raises(OSError):
            await monitor.poll()
        assert "sim-ENTRY" in monitor._tracked, "no record, so not released"

        await monitor.poll()

        assert "sim-ENTRY" not in monitor._tracked
        assert len(_released(store, "group_retired")) == 1


def _monitor_and_ledger(
    broker: SimBroker,
    store: EventStore,
    clock: ManualClock,
    instrument: Instrument,
    holding: str,
) -> tuple[ExecutionMonitor, Ledger]:
    ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(100_000)})
    ledger.adopt_position(Position(instrument_key=instrument.key, qty=Decimal(holding)))
    service = ExecutionService(broker, store, ledger, clock)
    return ExecutionMonitor(broker, service, store, clock), ledger


def _hold(ledger: Ledger, instrument: Instrument, qty: str) -> None:
    ledger.adopt_position(Position(instrument_key=instrument.key, qty=Decimal(qty)))


def _stop_leg(instrument: Instrument, clock: ManualClock, *, group: str, filled: str) -> Order:
    intent = OrderIntent(
        client_order_id=f"{group}-SL",
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=Side.SELL,
        qty=Decimal("0.3"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("47000"),
        stop_price=Decimal("47000"),
        role=OrderRole.STOP_LOSS,
        group_id=group,
        created_at=clock.now(),
    )
    leg = Order.from_intent(intent).transition_to(OrderState.SUBMITTED, at=clock.now())
    return leg.with_fill(
        Fill(
            fill_id=f"{group}-SL-1",
            client_order_id=leg.client_order_id,
            instrument_key=instrument.key,
            side=Side.SELL,
            qty=Decimal(filled),
            price=Decimal("47000"),
            filled_at=clock.now(),
        )
    )


class TestEntitlementAccounting:
    """The `_entitle` bookkeeping, driven through `_targets` with the ledger set by hand."""

    def test_an_own_leg_partly_filling_comes_off_its_own_group(
        self,
        broker: SimBroker,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The tight group's own stop sold 0.1. Shared out tightest-first, the shortfall would
        land on the *wide* group; it is the tight group's own exit and comes off the tight one."""
        monitor, ledger = _monitor_and_ledger(broker, store, clock, instrument, "0.6")
        for coid, stop in (("TIGHT", "48000"), ("WIDE", "45000")):
            monitor.track(
                _filled_entry(
                    instrument, clock, coid=coid, filled=Decimal("0.3"), stop=Decimal(stop)
                ),
                instrument,
            )
        assert monitor._targets() == {"TIGHT": Decimal("0.3"), "WIDE": Decimal("0.3")}

        monitor.track(_stop_leg(instrument, clock, group="TIGHT", filled="0.1"), instrument)
        _hold(ledger, instrument, "0.5")

        assert monitor._targets() == {"TIGHT": Decimal("0.2"), "WIDE": Decimal("0.3")}

    def test_an_entry_filling_across_polls_grows_by_its_own_fills_only(
        self,
        broker: SimBroker,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A partly filled entry, a reduction that takes all of it, then the rest of the entry
        fills: the group is entitled to the new fill and nothing of what was sold."""
        monitor, ledger = _monitor_and_ledger(broker, store, clock, instrument, "0.2")
        entry = _filled_entry(instrument, clock, coid="E", filled=Decimal("0.2"), stop=Decimal("1"))
        entry = entry.model_copy(
            update={"qty": Decimal("0.5"), "state": OrderState.PARTIALLY_FILLED}
        )
        monitor.track(entry, instrument)
        assert monitor._targets() == {"E": Decimal("0.2")}

        _hold(ledger, instrument, "0")
        assert monitor._targets() == {"E": ZERO}
        assert "E" in monitor._tracked and monitor._tracked["E"].order.state.is_open

        more = entry.with_fill(
            Fill(
                fill_id="E-2",
                client_order_id="E",
                instrument_key=instrument.key,
                side=Side.BUY,
                qty=Decimal("0.3"),
                price=Decimal("50000"),
                filled_at=clock.now(),
            )
        )
        monitor.track(more, instrument)
        _hold(ledger, instrument, "0.3")

        assert monitor._targets() == {"E": Decimal("0.3")}

    def test_more_held_than_entitled_is_logged_once_per_figure(
        self,
        broker: SimBroker,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Review minor 1: the ratchet never grows back, so a shortfall it leaves is said out
        loud. Logged, not flagged — a deposit is unguarded by design and the two look alike."""
        monitor, ledger = _monitor_and_ledger(broker, store, clock, instrument, "0.3")
        monitor.track(
            _filled_entry(instrument, clock, coid="E", filled=Decimal("0.3"), stop=Decimal("1")),
            instrument,
        )
        monitor._targets()
        _hold(ledger, instrument, "0.2")
        monitor._targets()
        _hold(ledger, instrument, "0.3")

        with caplog.at_level("WARNING"):
            monitor._targets()
            monitor._targets()

        warned = [r for r in caplog.records if "entitled to guard" in r.getMessage()]
        assert len(warned) == 1
        assert monitor._targets() == {"E": Decimal("0.2")}, "the ratchet does not grow back"
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert not risk


def _armed(store: EventStore) -> list[Event]:
    """Every `PROTECTIVE_PLACED` that actually armed legs.

    The same event type carries the *unprotected* report — `protected: False`, no legs — so
    counting the type alone would conflate arming with failing to arm.
    """
    return [
        event
        for event in store.read_all()
        if event.type is EventType.PROTECTIVE_PLACED and event.payload["protected"]
    ]


class TestTheReplaceTriggerRetriesAndSettles:
    """Final review, C1 and C2 — both about *when* `_maintain` decides to replace the legs.

    Each spans two of the branch's tasks, which is why neither per-task review could see it: one
    path gave the position up for good on a transient venue error, the other could never stop
    replacing the legs it had just placed.
    """

    async def test_a_venue_failure_is_retried_and_a_later_poll_re_arms_the_legs(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """C1. `_record_unprotected` set `unprotected_at` from both of its call sites, and
        `_maintain` skipped the replacement while the target still matched it — so one
        `RetryableError` cancelled the legs and no later poll ever placed another. The reviewer's
        reproduction, exactly: entry 0.5, an external sell of 0.2, one failure, then three healthy
        polls, ending with 0.3 BTC held, zero protective legs, and zero further events.

        A venue failure is transient by nature: the same call succeeds seconds later, and the
        position is bare in the meantime because the cancel deliberately comes first (design D4).
        """

        class _FailingGroupBroker(SimBroker):
            fail_group = False

            async def submit_group(self, intents):  # type: ignore[no-untyped-def]
                if self.fail_group:
                    self.fail_group = False
                    raise RetryableError("venue unavailable")
                return await super().submit_group(intents)

        broker = _FailingGroupBroker(clock, balances={"USDT": Decimal(100_000)})
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        broker.fail_group = True
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        with pytest.raises(RetryableError):
            await monitor.poll()
        assert not [
            leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open
        ], "the cancel comes first, so the position is bare until a poll re-arms it"

        for _ in range(3):
            clock.advance(30)
            broker.observe(tick(instrument, clock, last="49000"))
            await monitor.poll()

        live = [leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open]
        assert {leg.qty for leg in live} == {Decimal("0.3")}, "the next poll armed it again"
        assert len(_armed(store)) == 2, "armed once, re-armed once, then left alone"

    async def test_a_below_minimums_refusal_is_reported_once_however_many_polls(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The other half of C1: a *deterministic* refusal must stay de-duplicated.

        `plan_legs` is a pure function of the target and the venue's rules, so this same target
        refuses identically on every poll — and a warning repeated every ten seconds is a warning
        nobody reads. The retry the venue-failure path now gets must not become a report per poll
        here; that is why the marker moved to the one call site rather than being dropped.
        """
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        # 0.0001 BTC at 49 000 is 4.90, below the instrument's min_notional of 10.
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        for _ in range(4):
            clock.advance(30)
            broker.observe(tick(instrument, clock, last="49000"))
            await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == ["unprotected_position"]

    async def test_a_holding_that_is_not_a_lot_multiple_reaches_a_steady_state(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """C2. The trigger compared a *requested* target with the venue's *reported* quantity,
        and `plan_legs` floors its quantity to `lot_size` — so a holding off the lot grid could
        never match and every poll cancelled both legs and placed two more: six
        `PROTECTIVE_PLACED` in six polls, `revision` reaching 6, the legs never moving off
        0.30000, and a fresh unprotected window each time.

        Not hypothetical: `binance.parse_account` builds `Position.qty` straight from the raw
        `free + locked` balance, which after a base-asset fee is routinely not a `stepSize`
        multiple, and `Reconciler` adopts it. Sim never sees it because sim quantities are always
        lot-quantized, which is why a soak would not have found this either.
        """
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        # 0.5 - 0.199999 leaves 0.300001 held, one lot-tenth off the 0.00001 grid.
        book(ledger, external_sell(instrument, clock, qty="0.199999"), instrument)
        for _ in range(6):
            clock.advance(30)
            broker.observe(tick(instrument, clock, last="49000"))
            await monitor.poll()

        assert len(_armed(store)) == 2, "armed once, resized once, then left alone"
        assert monitor._tracked["sim-ENTRY"].revision == 2, "no id burned per poll"
        live = [leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open]
        assert {leg.qty for leg in live} == {Decimal("0.30000")}, "floored, and then left there"
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert not risk, "an expressible-if-floored target is guarded, not flagged"


def _monitor_over(
    broker: SimBroker,
    store: EventStore,
    clock: ManualClock,
    instrument: Instrument,
    holding: Decimal,
) -> ExecutionMonitor:
    ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(100_000)})
    ledger.adopt_position(Position(instrument_key=instrument.key, qty=holding))
    return ExecutionMonitor(broker, ExecutionService(broker, store, ledger, clock), store, clock)


def _filled_entry(
    instrument: Instrument, clock: ManualClock, *, coid: str, filled: Decimal, stop: Decimal
) -> Order:
    """An entry that has already filled `filled`, with its stop at `stop`.

    The lifecycle table in `orders.py` has no PENDING_SUBMIT → FILLED edge — a fresh order must
    pass through SUBMITTED first (DESIGN §6.7's "an order in an impossible state must never reach
    a venue"). `submit_group` is what does that on a real path; here it is one explicit
    `transition_to` so the helper stays a plain constructor with no venue involved.
    """
    intent = OrderIntent(
        client_order_id=coid,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=Side.BUY,
        qty=filled,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50000"),
        protective=ProtectivePlan(stop_price=stop, take_profit_price=stop + Decimal("10000")),
        created_at=clock.now(),
    )
    order = Order.from_intent(intent).transition_to(OrderState.SUBMITTED, at=clock.now())
    return order.with_fill(
        Fill(
            fill_id=f"{coid}-1",
            client_order_id=coid,
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=filled,
            price=Decimal("50000"),
            filled_at=clock.now(),
        )
    )


class TestAllocationInvariant:
    @given(
        fills=st.lists(
            st.decimals(min_value=Decimal("0.001"), max_value=Decimal("10"), places=3),
            min_size=1,
            max_size=5,
        ),
        stops=st.lists(
            st.decimals(min_value=Decimal("100"), max_value=Decimal("60000"), places=2),
            min_size=5,
            max_size=5,
            unique=True,
        ),
        holding=st.decimals(min_value=Decimal(0), max_value=Decimal("20"), places=3),
    )
    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_the_total_allocated_never_exceeds_the_holding(
        self,
        broker: SimBroker,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
        fills: list[Decimal],
        stops: list[Decimal],
        holding: Decimal,
    ) -> None:
        """Spec §3. Whatever the reduction was and wherever it came from, the sum of what the
        groups may guard is at most what is held. This is the property KNOWN_GAPS §4 violated."""
        monitor = _monitor_over(broker, store, clock, instrument, holding)
        for index, fill in enumerate(fills):
            monitor.track(
                _filled_entry(instrument, clock, coid=f"g{index}", filled=fill, stop=stops[index]),
                instrument,
            )

        targets = monitor._targets()

        assert sum(targets.values(), start=ZERO) <= holding
        for index, fill in enumerate(fills):
            assert targets[f"g{index}"] <= fill, "a group never guards more than its own fill"
        assert all(target >= ZERO for target in targets.values())


ETH_PLAN = ProtectivePlan(stop_price=Decimal("2700"), take_profit_price=Decimal("3200"))


async def poisoned_pair(
    monitor: ExecutionMonitor,
    broker: SimBroker,
    store: EventStore,
    ledger: Ledger,
    clock: ManualClock,
    btc: Instrument,
    eth: Instrument,
    *,
    eth_plan: ProtectivePlan | None = None,
) -> None:
    """BTC: an armed 0.5 stop the venue has filled, against a ledger something else already cut
    to 0.2 -- unbookable. ETH: an entry the venue has filled since -- bookable. BTC is first."""
    broker.observe(tick(btc, clock, last="49000"))
    await submit_entry(monitor, broker, store, ledger, clock, btc, ttl=None)
    await monitor.poll()
    book(ledger, external_sell(btc, clock, qty="0.3"), btc)
    await submit_entry(
        monitor, broker, store, ledger, clock, eth,
        price="2900", qty="1", ttl=None, plan=eth_plan, coid="sim-ETH",
    )  # fmt: skip
    clock.advance(60)
    broker.observe(tick(btc, clock, last="46500", high="49000", low="46000"))
    broker.observe(tick(eth, clock, last="2800"))


def unbookable_events(store: EventStore) -> list[Event]:
    return [
        e
        for e in store.read_all()
        if e.type is EventType.RISK_EVENT and e.payload["rule"] == "unbookable_fill"
    ]


def open_stops(monitor: ExecutionMonitor, instrument: Instrument) -> list[Order]:
    return [
        o
        for o in monitor.tracked
        if o.role is OrderRole.STOP_LOSS and o.instrument_key == instrument.key and o.state.is_open
    ]


class TestAnUnbookableFillIsContained:
    """ADR 0030 / memo Option D: one fill the ledger refuses must not wedge every other group."""

    async def test_a_poisoned_group_does_not_stop_a_sibling_fill_being_booked(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)

        await monitor.poll()  # unscoped: the sweep's call, and it must not raise

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert ledger.position(instrument.key).qty == Decimal("0.2")

    async def test_the_poison_reaches_only_a_caller_scoped_to_it(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)

        with pytest.raises(UnbookableFillError, match="exceeds holding") as raised:
            await monitor.poll(frozenset({instrument.key}))
        assert raised.value.qty == Decimal("0.5") and raised.value.held == Decimal("0.2")
        await monitor.poll(frozenset({second_instrument.key}))

    async def test_an_unscoped_sweep_records_the_poison_and_halts_nothing(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        """The owning basket halts at its next scoped poll, through the FAILED path; the sweep
        itself neither raises nor halts anything."""
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)

        await monitor.poll()

        assert len(unbookable_events(store)) == 1
        halts = [e for e in store.read_all() if e.type is EventType.BASKET_STATUS_CHANGED]
        assert halts == []
        with pytest.raises(UnbookableFillError):
            await monitor.poll(frozenset({instrument.key}))

    async def test_it_is_recorded_once_and_the_fill_is_never_dropped(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        for _ in range(3):
            await monitor.poll()

        (event,) = unbookable_events(store)
        assert event.payload["scope"] == instrument.key
        assert "0.5" in event.payload["detail"] and "0.2" in event.payload["detail"]
        assert open_stops(monitor, instrument), "still tracked, still unbooked"

    async def test_a_poisoned_group_books_once_the_holding_allows_it(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        await monitor.poll()
        book(
            ledger,
            Fill(
                fill_id="restored",
                client_order_id="sim-ELSEWHERE",
                instrument_key=instrument.key,
                side=Side.BUY,
                qty=Decimal("0.3"),
                price=Decimal("49000"),
                filled_at=clock.now(),
            ),
            instrument,
        )

        await monitor.poll(frozenset({instrument.key}))  # no longer raises

        assert ledger.position(instrument.key).is_flat

    async def test_a_sibling_is_still_maintained_while_another_group_is_poisoned(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(
            monitor, broker, store, ledger, clock, instrument, second_instrument,
            eth_plan=ETH_PLAN,
        )  # fmt: skip

        await monitor.poll()

        assert open_stops(monitor, second_instrument), "the healthy group was armed"

    async def test_a_venue_error_on_one_group_still_maintains_the_others(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        await submit_entry(
            monitor, broker, store, ledger, clock, second_instrument,
            price="2900", qty="1", ttl=None, plan=ETH_PLAN, coid="sim-ETH",
        )  # fmt: skip
        clock.advance(60)
        broker.observe(tick(second_instrument, clock, last="2800"))
        real = broker.fetch_order

        async def btc_down(ref: OrderRef) -> object:
            if ref.instrument_key == instrument.key:
                raise VenueError("503 from the venue")
            return await real(ref)

        broker.fetch_order = btc_down  # type: ignore[method-assign]
        with pytest.raises(VenueError):
            await monitor.poll()

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert open_stops(monitor, second_instrument), "armed despite its sibling's error"

    async def test_a_fail_closed_error_is_not_masked_by_an_earlier_retryable_one(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """BTC (tracked first) fails retryably, ETH fails closed: the caller must see the latter,
        because the class is the handling instruction -- and both must be logged."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await submit_entry(
            monitor, broker, store, ledger, clock, second_instrument,
            price="2900", qty="1", ttl=None, plan=ETH_PLAN, coid="sim-ETH",
        )  # fmt: skip

        async def down(ref: OrderRef) -> object:
            if ref.instrument_key == instrument.key:
                raise VenueError("503 from the venue")
            raise FailClosedError("venue answered something unreadable")

        broker.fetch_order = down  # type: ignore[method-assign]
        with caplog.at_level("ERROR"), pytest.raises(FailClosedError) as raised:
            await monitor.poll()

        assert not isinstance(raised.value, RetryableError)
        text = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
        assert "sim-ENTRY" in text and "sim-ETH" in text, "every failure is logged"


class TestContainmentFollowUps:
    """Review round: a poisoned retry is idempotent, and every path out of it is recorded."""

    async def test_a_poisoned_retry_does_not_rebook_the_fills_it_already_booked(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        """The stop filled in two trades; only the second exceeds the holding. Trade 1 must be
        booked once, not on every 30s retry."""
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        real = broker.fetch_order

        async def two_trades(ref: OrderRef) -> OrderStatus:
            status = await real(ref)
            if len(status.fills) == 1 and status.fills[0].qty == Decimal("0.5"):
                whole = status.fills[0]
                first = whole.model_copy(update={"fill_id": "trade-1", "qty": Decimal("0.1")})
                second = whole.model_copy(update={"fill_id": "trade-2", "qty": Decimal("0.4")})
                return status.model_copy(update={"fills": (first, second)})
            return status

        broker.fetch_order = two_trades  # type: ignore[method-assign]

        def received() -> int:
            return len([e for e in store.read_all() if e.type is EventType.FILL_RECEIVED])

        await monitor.poll()
        held, count = ledger.position(instrument.key).qty, received()
        assert held == Decimal("0.1"), "trade 1 booked once"
        for _ in range(3):
            await monitor.poll()
            await monitor.poll(frozenset({second_instrument.key}))

        assert ledger.position(instrument.key).qty == held
        assert received() == count

    async def test_a_failure_to_record_the_poison_does_not_skip_the_siblings(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await poisoned_pair(
            monitor, broker, store, ledger, clock, instrument, second_instrument,
            eth_plan=ETH_PLAN,
        )  # fmt: skip
        real_append = store.append
        failing = {"on": True}

        async def append(*events: Event) -> object:
            if failing["on"] and any(e.type is EventType.RISK_EVENT for e in events):
                raise RuntimeError("disk full")
            return await real_append(*events)

        store.append = append  # type: ignore[method-assign]
        with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="disk full"):
            await monitor.poll()

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert open_stops(monitor, second_instrument), "the sibling was still maintained"
        assert "disk full" in " ".join(str(r.__dict__) for r in caplog.records)
        assert unbookable_events(store) == []

        failing["on"] = False
        await monitor.poll()  # the record is retried because the group was left unpoisoned
        assert len(unbookable_events(store)) == 1

    async def test_clearing_a_poison_is_recorded(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        await monitor.poll()
        book(
            ledger,
            Fill(
                fill_id="restored",
                client_order_id="sim-ELSEWHERE",
                instrument_key=instrument.key,
                side=Side.BUY,
                qty=Decimal("0.3"),
                price=Decimal("49000"),
                filled_at=clock.now(),
            ),
            instrument,
        )

        await monitor.poll()
        await monitor.poll()

        actions = [e.payload["action_taken"] for e in unbookable_events(store)]
        assert actions == ["group_poisoned", "group_unpoisoned"]

    async def test_an_unclassified_error_outranks_a_retryable_one(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(
            monitor, broker, store, ledger, clock, instrument, price="40000", ttl=None
        )
        await submit_entry(
            monitor, broker, store, ledger, clock, second_instrument,
            price="2900", qty="1", ttl=None, plan=ETH_PLAN, coid="sim-ETH",
        )  # fmt: skip

        async def down(ref: OrderRef) -> object:
            if ref.instrument_key == instrument.key:
                raise VenueError("503 from the venue")
            raise RuntimeError("a bug, not a venue answer")

        broker.fetch_order = down  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="a bug"):
            await monitor.poll()

    async def test_a_group_adopted_during_the_sync_is_maintained_in_that_sweep(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(
            monitor, broker, store, ledger, clock, instrument, price="40000", ttl=None
        )
        service = ExecutionService(broker, store, ledger, clock)
        eth = await service.submit(
            entry_intent(
                second_instrument, clock, price="2900", qty="1", ttl=None, plan=ETH_PLAN,
                coid="sim-ETH",
            ),
            second_instrument,
        )  # fmt: skip
        broker.observe(tick(second_instrument, clock, last="2800"))
        filled = await service.sync(eth, second_instrument)
        real = broker.fetch_order
        adopted = {"done": False}

        async def adopt_midway(ref: OrderRef) -> OrderStatus:
            if not adopted["done"]:
                adopted["done"] = True
                monitor.track(filled, second_instrument)
            return await real(ref)

        broker.fetch_order = adopt_midway  # type: ignore[method-assign]

        await monitor.poll()

        assert open_stops(monitor, second_instrument), "armed in the sweep that adopted it"
