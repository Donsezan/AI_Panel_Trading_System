"""ExecutionMonitor: TTL enforcement, idempotent booking, and protective-group upkeep.

Each of these is a way an order can quietly go wrong after the submit succeeded — which is the
half of an order's life that a cycle-based system does not watch by default.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.orders import OrderIntent, ProtectivePlan
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import OrderRef
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
        poll_interval=timedelta(seconds=1),
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
) -> OrderIntent:
    return OrderIntent(
        client_order_id="sim-ENTRY",
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=Side.BUY,
        qty=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(price),
        protective=plan,
        ttl_seconds=ttl,
        created_at=clock.now(),
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

        # Both legs are cancelled at the venue directly, bypassing the monitor: SimBroker's
        # venue-native OCO cancels a sibling only on a *fill* (`_cancel_siblings`, called from
        # `_fill`) — ADR 0011 scopes that guarantee to the R13 hazard of both legs paying out, not
        # to a stray cancel — so cancelling one leg here would leave the other resting at its own
        # 0.5 and the group's `resting_qty` unchanged, telling us nothing.
        for leg in group.legs.values():
            await broker.cancel(
                OrderRef(client_order_id=leg.client_order_id, instrument_key=instrument.key)
            )
        clock.advance(30)
        # `monitor._sync` only, not `monitor.poll()`: a full poll's `_maintain` would see the
        # entry's fill still unguarded and re-arm fresh legs at the same 0.5 in the same sweep —
        # correct behaviour, but it would re-cover the gap this test exists to observe. Reaching
        # into `_sync` isolates "re-read the venue" from "act on what was read", which `poll`
        # deliberately does not separate in production.
        for client_order_id, leg in list(group.legs.items()):
            group.legs[client_order_id] = await monitor._sync(group, leg)

        assert group.resting_qty < Decimal("0.5"), "a leg cancelled at the venue is not guarding"

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
    async def test_settle_returns_once_every_entry_is_terminal(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, plan=None)

        settled = await monitor.settle()

        assert [order.state for order in settled] == [OrderState.FILLED]

    async def test_settle_gives_up_at_its_deadline_rather_than_blocking_forever(
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

        settled = await monitor.settle(deadline=timedelta(seconds=5))

        assert settled[0].state.is_open

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
