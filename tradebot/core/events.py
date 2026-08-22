"""The append-only event log's vocabulary — the system's audit trail (DESIGN §6.9).

Every state change emits an event. The rule is strict: the event log *alone* must be able to
reconstruct the system's state, because that is what makes a past decision replayable and what
makes tax and compliance reconstruction possible (PLAN §3.3).

Events are facts that already happened, so they are past tense and never mutated. Projections
are derived and disposable; the log is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import Field

from tradebot.core.clock import Clock
from tradebot.core.config import ConfigRef
from tradebot.core.decision import Decision, SeatResponse
from tradebot.core.enums import (
    BasketStatus,
    CycleOutcome,
    KillSwitchState,
    OrderState,
    RiskTier,
)
from tradebot.core.ids import new_uuid
from tradebot.core.orders import Fill, Order, RiskCheckResult
from tradebot.core.portfolio import Position, RoundTrip
from tradebot.core.schema import DomainModel, UtcDatetime, canonical_json
from tradebot.core.snapshot import ContextSnapshot


class EventType(StrEnum):
    """Persisted event vocabulary. Values are on-disk contract — never rename, only add."""

    CYCLE_STARTED = "CYCLE_STARTED"
    SNAPSHOT_FROZEN = "SNAPSHOT_FROZEN"
    SEAT_RESPONDED = "SEAT_RESPONDED"
    DECISION_MADE = "DECISION_MADE"
    #: A challenger panel's verdict on the same snapshot, for the record only (ADR 0018).
    SHADOW_EVALUATED = "SHADOW_EVALUATED"
    RISK_CHECKED = "RISK_CHECKED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_STATE_CHANGED = "ORDER_STATE_CHANGED"
    PROTECTIVE_PLACED = "PROTECTIVE_PLACED"
    FILL_RECEIVED = "FILL_RECEIVED"
    POSITION_UPDATED = "POSITION_UPDATED"
    ROUND_TRIP_CLOSED = "ROUND_TRIP_CLOSED"
    CYCLE_COMPLETED = "CYCLE_COMPLETED"
    RISK_EVENT = "RISK_EVENT"
    RECONCILED = "RECONCILED"
    EXTERNAL_CHANGE = "EXTERNAL_CHANGE"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    KILL_SWITCH_CHANGED = "KILL_SWITCH_CHANGED"
    BASKET_STATUS_CHANGED = "BASKET_STATUS_CHANGED"
    CONFIG_CHANGED = "CONFIG_CHANGED"
    #: One housekeeping pass: what it did, under which retention windows, and whether it failed.
    #: Also what answers "is a run due" — derived from the log, never counted in memory, so a
    #: restart can neither skip the day's backup nor take a second (spec §6.2).
    MAINTENANCE_RAN = "MAINTENANCE_RAN"


class Event(DomainModel):
    """One immutable fact.

    `seq` is assigned by the store on append and is the total order of the log; it is `None`
    until then. `aggregate_id` identifies what the event is about (a cycle, an order, a basket)
    and is what a replay groups on.
    """

    event_id: str = Field(default_factory=new_uuid)
    seq: int | None = None
    ts: UtcDatetime
    type: EventType
    aggregate_id: str
    basket_id: str | None = None
    cycle_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def payload_json(self) -> str:
        return canonical_json(self.payload)

    def sequenced(self, seq: int) -> Event:
        """Return this event stamped with its position in the log."""
        return self.model_copy(update={"seq": seq})


def _json(model: DomainModel) -> dict[str, Any]:
    """Domain object as JSON-safe primitives — decimals become strings, not floats."""
    return model.model_dump(mode="json")


class EventFactory:
    """Builds events for one cycle, so payload shapes live in exactly one place.

    Producer and projector read the same construction code; a payload key can therefore never
    drift between what is written and what is replayed.
    """

    def __init__(self, *, clock: Clock, basket_id: str, cycle_id: str) -> None:
        self._clock = clock
        self._basket_id = basket_id
        self._cycle_id = cycle_id

    def _event(self, type_: EventType, aggregate_id: str, **payload: Any) -> Event:
        return Event(
            ts=self._clock.now(),
            type=type_,
            aggregate_id=aggregate_id,
            basket_id=self._basket_id,
            cycle_id=self._cycle_id,
            payload=payload,
        )

    def cycle_started(self, config_refs: Sequence[ConfigRef] = (), venue: str = "") -> Event:
        """Open a cycle, pinning the configuration versions it will run on (DESIGN §6.1).

        `venue` names the venue that would have taken this cycle's orders. It is what lets the
        promotion report separate the evidence base — live data through `SimBroker` — from the
        testnet runs that sit beside it as adapter integration checks (DESIGN §9). An empty
        venue means *unknown*, which the report excludes from gate accounting rather than
        counting towards a promotion decision it cannot substantiate.
        """
        return self._event(
            EventType.CYCLE_STARTED,
            self._cycle_id,
            basket_id=self._basket_id,
            venue=venue,
            config_versions={ref.key: ref.version for ref in config_refs},
        )

    def config_changed(self, ref: ConfigRef, *, actor: str, note: str, retired: bool) -> Event:
        """A new configuration version was published. The audit record of who changed what."""
        return self._event(
            EventType.CONFIG_CHANGED,
            ref.key,
            kind=ref.kind.value,
            config_id=ref.config_id,
            version=ref.version,
            retired=retired,
            actor=actor,
            note=note,
        )

    def snapshot_frozen(self, snapshot: ContextSnapshot) -> Event:
        return self._event(
            EventType.SNAPSHOT_FROZEN,
            self._cycle_id,
            snapshot_id=snapshot.snapshot_id,
            digest=snapshot.digest,
            snapshot=_json(snapshot),
        )

    def seat_responded(self, response: SeatResponse) -> Event:
        return self._event(EventType.SEAT_RESPONDED, self._cycle_id, response=_json(response))

    def decision_made(self, decision: Decision) -> Event:
        return self._event(EventType.DECISION_MADE, self._cycle_id, decision=_json(decision))

    def shadow_evaluated(
        self,
        panel_id: str,
        decisions: Sequence[Decision],
        cost_usd: Decimal,
        *,
        error: str = "",
    ) -> Event:
        """What the challenger panel would have decided, on the champion's frozen snapshot.

        Its `cost_usd` is recorded *here* rather than on the cycle, so `$/decision` for the panel
        that actually traded stays a true figure — a shadow run is research spend, not the cost of
        a decision (ADR 0018). An `error` means the challenger failed and the champion's cycle was
        unaffected, which is the whole contract: recording the failure is how it stays visible.
        """
        return self._event(
            EventType.SHADOW_EVALUATED,
            self._cycle_id,
            panel_id=panel_id,
            decisions=[_json(decision) for decision in decisions],
            cost_usd=str(cost_usd),
            error=error,
        )

    def risk_checked(
        self, instrument_key: str, checks: tuple[RiskCheckResult, ...], approved: bool
    ) -> Event:
        return self._event(
            EventType.RISK_CHECKED,
            self._cycle_id,
            instrument_key=instrument_key,
            approved=approved,
            checks=[_json(check) for check in checks],
        )

    def order_submitted(self, order: Order) -> Event:
        return self._event(EventType.ORDER_SUBMITTED, order.client_order_id, order=_json(order))

    def order_state_changed(self, order: Order, previous: OrderState) -> Event:
        return self._event(
            EventType.ORDER_STATE_CHANGED,
            order.client_order_id,
            previous=previous.value,
            state=order.state.value,
            venue_order_id=order.venue_order_id,
            updated_at=order.updated_at.isoformat(),
        )

    def fill_received(self, fill: Fill, order: Order) -> Event:
        return self._event(
            EventType.FILL_RECEIVED,
            fill.client_order_id,
            fill=_json(fill),
            order_state=order.state.value,
            filled_qty=str(order.filled_qty),
            avg_fill_price=str(order.avg_fill_price),
        )

    def position_updated(self, position: Position) -> Event:
        return self._event(
            EventType.POSITION_UPDATED, position.instrument_key, position=_json(position)
        )

    def round_trip_closed(self, trip: RoundTrip) -> Event:
        return self._event(EventType.ROUND_TRIP_CLOSED, trip.instrument_key, round_trip=_json(trip))

    def protective_placed(self, entry: Order, legs: tuple[Order, ...], detail: str = "") -> Event:
        return self._event(
            EventType.PROTECTIVE_PLACED,
            entry.client_order_id,
            group_id=entry.group_id,
            legs=[_json(leg) for leg in legs],
            protected=bool(legs),
            detail=detail,
        )

    def reconciled(self, report: DomainModel) -> Event:
        return self._event(EventType.RECONCILED, self._basket_id, report=_json(report))

    def external_change(self, currency: str, amount: Decimal, reason: str) -> Event:
        return self._event(
            EventType.EXTERNAL_CHANGE,
            currency,
            currency=currency,
            amount=str(amount),
            reason=reason,
        )

    def corporate_action(self, instrument_key: str, detail: str, **fields: Any) -> Event:
        return self._event(EventType.CORPORATE_ACTION, instrument_key, detail=detail, **fields)

    def kill_switch_changed(self, state: KillSwitchState, reason: str, actor: str) -> Event:
        return self._event(
            EventType.KILL_SWITCH_CHANGED,
            "global",
            state=state.value,
            reason=reason,
            actor=actor,
        )

    def basket_status_changed(self, basket_id: str, status: BasketStatus, reason: str) -> Event:
        return self._event(
            EventType.BASKET_STATUS_CHANGED,
            basket_id,
            basket_id=basket_id,
            status=status.value,
            reason=reason,
        )

    def risk_event(
        self, *, tier: RiskTier, rule: str, scope: str, action: str, detail: str
    ) -> Event:
        return self._event(
            EventType.RISK_EVENT,
            scope,
            tier=tier.value,
            rule=rule,
            scope=scope,
            action_taken=action,
            detail=detail,
        )

    def cycle_completed(self, outcome: CycleOutcome, cost_usd: Decimal) -> Event:
        return self._event(
            EventType.CYCLE_COMPLETED,
            self._cycle_id,
            outcome=outcome.value,
            cost_usd=str(cost_usd),
        )
