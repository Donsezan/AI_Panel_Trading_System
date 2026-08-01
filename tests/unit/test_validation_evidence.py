"""Folding the event log into report facts.

The gates a promotion decision turns on are counted here, so the counting is what these tests
are about: what is an incident, what is not, and what a window includes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tradebot.core.clock import ManualClock
from tradebot.core.decision import Decision
from tradebot.core.enums import (
    Action,
    BasketStatus,
    CycleOutcome,
    KillSwitchState,
    OrderState,
    OrderType,
    ReconcileClass,
    RiskTier,
    Side,
    SizeHint,
)
from tradebot.core.events import EventFactory
from tradebot.core.orders import Fill, Order, OrderIntent
from tradebot.core.portfolio import RoundTrip
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.persistence.store import EventStore
from tradebot.validation.evidence import UNKNOWN_VENUE, Evidence, IncidentKind


class _Report(DomainModel):
    """The shape the reconciler writes into a `RECONCILED` payload."""

    venue: str
    classification: ReconcileClass
    observed_at: UtcDatetime
    differences: tuple[str, ...] = ()


def events_for(clock: ManualClock, cycle_id: str = "c1") -> EventFactory:
    return EventFactory(clock=clock, basket_id="demo", cycle_id=cycle_id)


def order_of(clock: ManualClock, client_order_id: str = "sim-abc") -> Order:
    return Order.from_intent(
        OrderIntent(
            client_order_id=client_order_id,
            basket_id="demo",
            cycle_id="c1",
            instrument_key="sim:BTC/USDT",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            qty=Decimal("0.001"),
            limit_price=Decimal("50000"),
            created_at=clock.now(),
        )
    )


async def a_cycle(
    store: EventStore,
    clock: ManualClock,
    *,
    cycle_id: str,
    venue: str = "sim",
    outcome: CycleOutcome = CycleOutcome.NO_ACTION,
    cost: Decimal = Decimal("0.01"),
) -> None:
    events = events_for(clock, cycle_id)
    await store.append(events.cycle_started((), venue))
    await store.append(events.cycle_completed(outcome, cost))


class TestCycles:
    async def test_a_cycle_carries_its_venue_outcome_and_cost(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_cycle(store, clock, cycle_id="c1", venue="binance", cost=Decimal("0.25"))

        (cycle,) = Evidence.gather(store).cycles
        assert (cycle.venue, cycle.outcome, cycle.cost_usd) == (
            "binance",
            CycleOutcome.NO_ACTION,
            Decimal("0.25"),
        )
        assert cycle.completed

    async def test_an_unstamped_cycle_is_unknown_rather_than_assumed(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """A cycle that cannot name its venue must never be counted towards a promotion."""
        await store.append(events_for(clock).cycle_started())

        (cycle,) = Evidence.gather(store).cycles
        assert cycle.venue == UNKNOWN_VENUE
        assert not cycle.completed

    async def test_only_evidence_venues_count(self, store: EventStore, clock: ManualClock) -> None:
        await a_cycle(store, clock, cycle_id="c1", venue="sim")
        await a_cycle(store, clock, cycle_id="c2", venue="binance")

        evidence = Evidence.gather(store)
        assert len(evidence.for_venues(frozenset({"sim"}))) == 1
        assert evidence.cycles_by_venue == {"sim": 1, "binance": 1}

    async def test_a_window_excludes_what_falls_outside_it(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_cycle(store, clock, cycle_id="early")
        clock.advance(timedelta(days=2).total_seconds())
        boundary = clock.now()
        await a_cycle(store, clock, cycle_id="late")

        recent = Evidence.gather(store, since=boundary)
        assert [cycle.cycle_id for cycle in recent.cycles] == ["late"]

    async def test_cost_totals_over_the_window(self, store: EventStore, clock: ManualClock) -> None:
        await a_cycle(store, clock, cycle_id="c1", cost=Decimal("0.10"))
        await a_cycle(store, clock, cycle_id="c2", cost=Decimal("0.05"))

        assert Evidence.gather(store).cost_usd == Decimal("0.15")


class TestIncidents:
    async def test_a_failed_cycle_needed_a_human(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        await a_cycle(store, clock, cycle_id="c1", outcome=CycleOutcome.FAILED)

        (incident,) = Evidence.gather(store).incidents
        assert incident.kind is IncidentKind.CYCLE_FAILED

    async def test_a_vetoed_cycle_did_not(self, store: EventStore, clock: ManualClock) -> None:
        """A veto is the system working. Counting it would make the gate unreachable."""
        await a_cycle(store, clock, cycle_id="c1", outcome=CycleOutcome.RISK_VETOED)
        await store.append(
            events_for(clock).risk_event(
                tier=RiskTier.TIER1,
                rule="min_conviction",
                scope="sim:BTC/USDT",
                action="vetoed",
                detail="conviction below the floor",
            )
        )

        evidence = Evidence.gather(store)
        assert evidence.incidents == ()
        assert evidence.risk_events == {"min_conviction/vetoed": 1}

    async def test_a_tripped_kill_switch_is_an_incident_and_a_re_arm_is_not(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        events = events_for(clock)
        await store.append(
            events.kill_switch_changed(KillSwitchState.TRIPPED, "drawdown 12%", actor="watchdog"),
            events.kill_switch_changed(KillSwitchState.ARMED, "re-armed", actor="cli"),
        )

        kinds = [incident.kind for incident in Evidence.gather(store).incidents]
        assert kinds == [IncidentKind.KILL_SWITCH]

    async def test_a_halted_basket_is_an_incident_and_un_halting_is_not(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        events = events_for(clock)
        await store.append(
            events.basket_status_changed("demo", BasketStatus.HALTED, "3 failed cycles"),
            events.basket_status_changed("demo", BasketStatus.ACTIVE, "un-halted by cli"),
        )

        (incident,) = Evidence.gather(store).incidents
        assert (incident.kind, incident.scope) == (IncidentKind.BASKET_HALTED, "demo")

    async def test_an_order_still_unresolved_at_the_window_close_is_stranded(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """`SUBMIT_UNKNOWN` is a normal transient; one that never resolved is not (PLAN §2.3)."""
        events = events_for(clock)
        order = order_of(clock)
        await store.append(events.order_submitted(order))
        await store.append(
            events.order_state_changed(
                order.transition_to(OrderState.SUBMIT_UNKNOWN, at=clock.now()),
                OrderState.PENDING_SUBMIT,
            )
        )

        (incident,) = Evidence.gather(store).incidents
        assert (incident.kind, incident.scope) == (IncidentKind.ORDER_STRANDED, "sim-abc")

    async def test_an_order_that_reached_a_terminal_state_is_not(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        events = events_for(clock)
        submitted = order_of(clock).transition_to(OrderState.SUBMITTED, at=clock.now())
        await store.append(events.order_submitted(submitted))
        await store.append(
            events.order_state_changed(
                submitted.transition_to(OrderState.CANCELLED, at=clock.now()), OrderState.SUBMITTED
            )
        )

        evidence = Evidence.gather(store)
        assert evidence.incidents == ()
        assert evidence.order_states == {OrderState.CANCELLED: 1}


class TestReconciliation:
    async def _record(
        self, store: EventStore, clock: ManualClock, classification: ReconcileClass
    ) -> Evidence:
        await store.append(
            events_for(clock).reconciled(
                _Report(venue="sim", classification=classification, observed_at=clock.now())
            )
        )
        return Evidence.gather(store)

    async def test_a_mismatch_is_unclean_and_an_incident(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        evidence = await self._record(store, clock, ReconcileClass.MISMATCH)

        assert len(evidence.unclean_reconciliations) == 1
        assert [i.kind for i in evidence.incidents] == [IncidentKind.RECON_MISMATCH]

    async def test_explainable_drift_is_clean(self, store: EventStore, clock: ManualClock) -> None:
        evidence = await self._record(store, clock, ReconcileClass.DRIFT)

        assert evidence.unclean_reconciliations == ()
        assert evidence.incidents == ()

    async def test_a_venue_reset_is_excluded_rather_than_held_against_the_soak(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        """Binance's testnet wipes itself monthly; that is an operational fact, not a defect."""
        evidence = await self._record(store, clock, ReconcileClass.VENUE_RESET)

        assert evidence.unclean_reconciliations == ()
        assert evidence.reconciliations[0].excluded


class TestActivity:
    async def test_decisions_are_counted_by_action(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        events = events_for(clock)
        await store.append(
            events.decision_made(
                Decision(
                    instrument_key="sim:BTC/USDT",
                    action=Action.BUY,
                    conviction=Decimal("0.8"),
                    size_hint=SizeHint.HALF,
                )
            ),
            events.decision_made(Decision(instrument_key="sim:ETH/USDT", action=Action.WAIT)),
        )

        assert Evidence.gather(store).actions == {"BUY": 1, "WAIT": 1}

    async def test_fills_and_round_trips_are_counted(
        self, store: EventStore, clock: ManualClock
    ) -> None:
        events = events_for(clock)
        order = order_of(clock)
        fill = Fill(
            fill_id="f1",
            client_order_id=order.client_order_id,
            instrument_key="sim:BTC/USDT",
            side="buy",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
            fee=Decimal("0.05"),
            fee_currency="USDT",
            filled_at=clock.now(),
        )
        await store.append(
            events.fill_received(fill, order),
            events.round_trip_closed(
                RoundTrip(
                    instrument_key="sim:BTC/USDT",
                    qty=Decimal("0.001"),
                    entry_price=Decimal("50000"),
                    exit_price=Decimal("49000"),
                    realized_pnl=Decimal("-1.00"),
                    opened_at=clock.now(),
                    closed_at=clock.now(),
                )
            ),
        )

        evidence = Evidence.gather(store)
        assert evidence.fills == 1
        assert evidence.realized_pnl == Decimal("-1.00")
        assert evidence.losing_trips == 1
