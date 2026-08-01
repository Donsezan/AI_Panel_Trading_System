"""What justifies waking a human at 03:00, and what does not.

An alerting rule that fires too readily is worse than one that fires late: an operator who has
learned to swipe the channel away is an operator who will swipe away the kill switch too.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.clock import ManualClock
from tradebot.core.enums import (
    BasketStatus,
    CycleOutcome,
    KillSwitchState,
    ReconcileClass,
)
from tradebot.core.events import EventFactory
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.interfaces.alerts import AlertKind
from tradebot.ops.rules import RuleState, evaluate


class _Report(DomainModel):
    """The shape the reconciler writes into a `RECONCILED` payload."""

    venue: str
    classification: ReconcileClass
    observed_at: UtcDatetime


def events_for(clock: ManualClock) -> EventFactory:
    return EventFactory(clock=clock, basket_id="demo", cycle_id="c1")


class TestUrgentTriggers:
    def test_a_tripped_kill_switch_alerts(self, clock: ManualClock) -> None:
        event = events_for(clock).kill_switch_changed(
            KillSwitchState.TRIPPED, "drawdown 12% below the mark", actor="max_drawdown"
        )

        alert = evaluate(event, RuleState())
        assert alert is not None
        assert alert.kind is AlertKind.KILL_SWITCH
        assert alert.kind.is_urgent
        assert "drawdown 12%" in alert.body

    def test_re_arming_does_not(self, clock: ManualClock) -> None:
        """A human did that one deliberately, having typed a phrase to do it."""
        event = events_for(clock).kill_switch_changed(
            KillSwitchState.ARMED, "re-armed with typed confirmation", actor="cli"
        )

        assert evaluate(event, RuleState()) is None

    def test_a_halted_basket_alerts(self, clock: ManualClock) -> None:
        event = events_for(clock).basket_status_changed(
            "demo", BasketStatus.HALTED, "3 consecutive failed cycles"
        )

        alert = evaluate(event, RuleState())
        assert alert is not None
        assert (alert.kind, alert.scope) == (AlertKind.BASKET_HALTED, "demo")

    def test_un_halting_does_not(self, clock: ManualClock) -> None:
        event = events_for(clock).basket_status_changed(
            "demo", BasketStatus.ACTIVE, "un-halted by cli"
        )

        assert evaluate(event, RuleState()) is None

    def test_only_an_unexplained_reconciliation_alerts(self, clock: ManualClock) -> None:
        events = events_for(clock)
        alerting = [
            classification
            for classification in ReconcileClass
            if evaluate(
                events.reconciled(
                    _Report(venue="sim", classification=classification, observed_at=clock.now())
                ),
                RuleState(),
            )
            is not None
        ]

        assert alerting == [ReconcileClass.MISMATCH]


class TestProviderFailure:
    def _degrade(self, clock: ManualClock, state: RuleState, times: int) -> list[object]:
        events = events_for(clock)
        return [
            evaluate(events.cycle_completed(CycleOutcome.PANEL_DEGRADED, Decimal(0)), state)
            for _ in range(times)
        ]

    def test_one_degraded_cycle_is_not_an_outage(self, clock: ManualClock) -> None:
        state = RuleState()

        assert self._degrade(clock, state, 2) == [None, None]

    def test_the_streak_limit_alerts_exactly_once(self, clock: ManualClock) -> None:
        """A panel that stays down would otherwise alert on every cycle for hours."""
        state = RuleState()

        alerts = self._degrade(clock, state, 5)

        fired = [alert for alert in alerts if alert is not None]
        assert len(fired) == 1
        assert fired[0].kind is AlertKind.PROVIDER_FAILURE  # type: ignore[attr-defined]

    def test_a_healthy_cycle_clears_the_streak(self, clock: ManualClock) -> None:
        state = RuleState()
        self._degrade(clock, state, 2)

        evaluate(events_for(clock).cycle_completed(CycleOutcome.ORDERS_PLACED, Decimal(0)), state)

        assert state.degraded_streak == 0
        assert self._degrade(clock, state, 2) == [None, None]

    def test_the_streak_survives_being_carried_between_polls(self, clock: ManualClock) -> None:
        """The dispatcher persists it, so a restart mid-outage cannot forgive two cycles."""
        self._degrade(clock, first := RuleState(), 2)

        resumed = RuleState(first.degraded_streak)
        alert = self._degrade(clock, resumed, 1)[0]

        assert alert is not None


class TestDefensiveReading:
    def test_an_unreadable_outcome_leaves_the_streak_exactly_as_it_was(
        self, clock: ManualClock
    ) -> None:
        """It is not evidence the providers recovered, so it must not silence the next alert."""
        event = events_for(clock).cycle_completed(CycleOutcome.PANEL_DEGRADED, Decimal(0))
        garbled = event.model_copy(update={"payload": {**event.payload, "outcome": "who knows"}})
        state = RuleState(degraded_streak=2)

        assert evaluate(garbled, state) is None
        assert state.degraded_streak == 2

    def test_an_untailed_event_type_yields_nothing(self, clock: ManualClock) -> None:
        assert evaluate(events_for(clock).cycle_started((), "sim"), RuleState()) is None
