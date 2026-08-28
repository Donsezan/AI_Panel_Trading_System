"""ExecutionMonitor: TTL enforcement, idempotent booking, and protective-group upkeep.

Each of these is a way an order can quietly go wrong after the submit succeeded — which is the
half of an order's life that a cycle-based system does not watch by default.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tradebot.core.clock import ManualClock
from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import RetryableError
from tradebot.core.events import EventType
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.orders import Fill, Order, OrderIntent, ProtectivePlan
from tradebot.core.portfolio import Position
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
        assert not risk, "nothing is at risk, so nothing is flagged"

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

        # Down to dust: 0.0001 at 49 000 is 4.90, under the instrument's min_notional of 10.
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        clock.advance(30)
        await monitor.poll()

        # Back up, guardable again, then down to the same dust a second time.
        book(
            ledger,
            Fill(
                fill_id="refill",
                client_order_id="sim-ELSEWHERE",
                instrument_key=instrument.key,
                side=Side.BUY,
                qty=Decimal("0.4999"),
                price=Decimal("49000"),
                filled_at=clock.now(),
            ),
            instrument,
        )
        clock.advance(30)
        await monitor.poll()
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        clock.advance(30)
        await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == [
            "unprotected_position",
            "unprotected_position",
        ], "the second time it becomes unguardable is a second fact, not a repeat"


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
