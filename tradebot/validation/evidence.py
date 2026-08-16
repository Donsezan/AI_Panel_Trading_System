"""The event log, folded into the facts a validation report is made of.

Read from the **log**, never from the projections. Two reasons, and both matter:

* the facts a promotion decision turns on are audit-only — a kill switch trip and a basket halt
  have no projector at all, because nothing in the dashboard queries them (`projections.py`);
* the log is the compliance artifact (PLAN §3.3). A report derived from a table that a rebuild
  regenerates is a report about a derivation; this one is about what happened.

Only the types a report needs are read, so the two largest payloads in the log — frozen
snapshots and seat transcripts — are never loaded. A soak's log is mostly those.

An **incident** is the thing this module exists to count: something that needed a human. A veto
is not an incident (the system did exactly what it was built to do); a halt, a trip, a failed
cycle, an unexplained reconciliation and an order left stranded in `SUBMIT_UNKNOWN` are, because
each one ends with a person deciding something (DESIGN §9).

Failure semantics: this module only reads. A payload that has drifted from its producer yields a
fact with an empty field rather than raising — a report that omits one number is more useful than
a report that cannot be produced, and every gate treats a missing number as a failure to prove
the gate rather than as a pass.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from tradebot.core.enums import CycleOutcome, KillSwitchState, OrderState, ReconcileClass
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO, to_decimal
from tradebot.persistence.store import EventStore
from tradebot.validation.payload import money, nested, text

#: A cycle recorded before venues were stamped on the log, or by a component that had no venue
#: to name. Never counted as evidence: a cycle that cannot say where it would have traded cannot
#: substantiate a promotion decision.
UNKNOWN_VENUE = "unknown"

#: The event types a report reads. Deliberately not "all of them".
REPORT_TYPES: tuple[EventType, ...] = (
    EventType.CYCLE_STARTED,
    EventType.CYCLE_COMPLETED,
    EventType.DECISION_MADE,
    EventType.ORDER_SUBMITTED,
    EventType.ORDER_STATE_CHANGED,
    EventType.FILL_RECEIVED,
    EventType.ROUND_TRIP_CLOSED,
    EventType.RISK_EVENT,
    EventType.RECONCILED,
    EventType.KILL_SWITCH_CHANGED,
    EventType.BASKET_STATUS_CHANGED,
)

#: Order states that mean the order's fate was never established. Both require a human: the
#: first is PLAN §2.3's bounded window having expired, the second a submission nobody resolved.
STRANDED_STATES = frozenset({OrderState.SUBMIT_UNKNOWN, OrderState.FAILED})


class IncidentKind(StrEnum):
    """What kind of human attention an incident needed."""

    KILL_SWITCH = "kill_switch_tripped"
    BASKET_HALTED = "basket_halted"
    CYCLE_FAILED = "cycle_failed"
    RECON_MISMATCH = "recon_mismatch"
    ORDER_STRANDED = "order_stranded"
    #: The portfolio could not be valued, so trading stopped until someone fixed the feed or the
    #: balance. Counted because `ops/rules.py` alerts on it, and the two vocabularies must not
    #: drift — "what needed a human" has one definition in this codebase (ADR 0027).
    VALUATION_FROZEN = "valuation_frozen"


@dataclass(frozen=True, slots=True)
class CycleFacts:
    """One decision cycle, as the log describes it."""

    cycle_id: str
    basket_id: str
    venue: str
    started_at: datetime
    completed_at: datetime | None = None
    outcome: CycleOutcome | None = None
    cost_usd: Decimal = ZERO

    @property
    def completed(self) -> bool:
        """Whether the cycle reached a recorded outcome. An interrupted one never counts."""
        return self.outcome is not None


@dataclass(frozen=True, slots=True)
class Incident:
    """Something that needed a human — the thing the promotion gates count."""

    kind: IncidentKind
    at: datetime
    scope: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReconcileFacts:
    """One reconciliation pass and how it was classified."""

    at: datetime
    venue: str
    classification: ReconcileClass

    @property
    def excluded(self) -> bool:
        """A testnet wiping itself is an operational event, not evidence about this system.

        Binance's spot testnet resets to a blank state roughly monthly without notice (R15), so
        DESIGN §9 excludes those passes from gate accounting. They are still reported.
        """
        return self.classification is ReconcileClass.VENUE_RESET


@dataclass(frozen=True, slots=True)
class RoundTripFacts:
    """One closed position and what it realized."""

    at: datetime
    basket_id: str
    instrument_key: str
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the log says about a window, in the shape a report renders."""

    since: datetime | None
    until: datetime | None
    cycles: tuple[CycleFacts, ...] = ()
    incidents: tuple[Incident, ...] = ()
    reconciliations: tuple[ReconcileFacts, ...] = ()
    round_trips: tuple[RoundTripFacts, ...] = ()
    actions: Mapping[str, int] = field(default_factory=dict)
    order_states: Mapping[OrderState, int] = field(default_factory=dict)
    risk_events: Mapping[str, int] = field(default_factory=dict)
    fills: int = 0

    @classmethod
    def gather(
        cls, store: EventStore, *, since: datetime | None = None, until: datetime | None = None
    ) -> Evidence:
        """Fold the log's report-relevant events into one immutable summary."""
        fold = _Fold()
        for event in store.read_types(*REPORT_TYPES, since=since, until=until):
            fold.apply(event)
        return fold.finish(since, until)

    def for_venues(self, venues: frozenset[str]) -> tuple[CycleFacts, ...]:
        """Completed cycles that ran against one of `venues` — the evidence base."""
        return tuple(c for c in self.cycles if c.completed and c.venue in venues)

    @property
    def cycles_by_venue(self) -> Mapping[str, int]:
        return Counter(cycle.venue for cycle in self.cycles if cycle.completed)

    @property
    def outcomes(self) -> Mapping[str, int]:
        return Counter(c.outcome.value for c in self.cycles if c.outcome is not None)

    @property
    def cost_usd(self) -> Decimal:
        """What deliberation cost over the window. Totalled in Python, never in SQL."""
        return sum((cycle.cost_usd for cycle in self.cycles), start=ZERO)

    @property
    def realized_pnl(self) -> Decimal:
        return sum((trip.realized_pnl for trip in self.round_trips), start=ZERO)

    @property
    def losing_trips(self) -> int:
        return sum(1 for trip in self.round_trips if trip.realized_pnl < ZERO)

    @property
    def unclean_reconciliations(self) -> tuple[ReconcileFacts, ...]:
        """Passes that neither matched nor were explained, excluding venue resets."""
        return tuple(
            pass_
            for pass_ in self.reconciliations
            if not pass_.classification.is_clean and not pass_.excluded
        )


class _Fold:
    """Accumulates one pass over the log. Dispatch is a table, never a chain of `if`s."""

    def __init__(self) -> None:
        self._cycles: dict[str, CycleFacts] = {}
        self._orders: dict[str, OrderState] = {}
        self._incidents: list[Incident] = []
        self._reconciliations: list[ReconcileFacts] = []
        self._round_trips: list[RoundTripFacts] = []
        self._actions: Counter[str] = Counter()
        self._risk_events: Counter[str] = Counter()
        self._fills = 0
        self._handlers: dict[EventType, Callable[[Event], None]] = {
            EventType.CYCLE_STARTED: self._cycle_started,
            EventType.CYCLE_COMPLETED: self._cycle_completed,
            EventType.DECISION_MADE: self._decision_made,
            EventType.ORDER_SUBMITTED: self._order_submitted,
            EventType.ORDER_STATE_CHANGED: self._order_state_changed,
            EventType.FILL_RECEIVED: self._fill_received,
            EventType.ROUND_TRIP_CLOSED: self._round_trip_closed,
            EventType.RISK_EVENT: self._risk_event,
            EventType.RECONCILED: self._reconciled,
            EventType.KILL_SWITCH_CHANGED: self._kill_switch_changed,
            EventType.BASKET_STATUS_CHANGED: self._basket_status_changed,
        }

    def apply(self, event: Event) -> None:
        handler = self._handlers.get(event.type)
        if handler is not None:
            handler(event)

    def finish(self, since: datetime | None, until: datetime | None) -> Evidence:
        return Evidence(
            since=since,
            until=until,
            cycles=tuple(self._cycles.values()),
            incidents=tuple(sorted((*self._incidents, *self._stranded()), key=lambda i: i.at)),
            reconciliations=tuple(self._reconciliations),
            round_trips=tuple(self._round_trips),
            actions=dict(self._actions),
            order_states=dict(Counter(self._orders.values())),
            risk_events=dict(self._risk_events),
            fills=self._fills,
        )

    def _stranded(self) -> tuple[Incident, ...]:
        """Orders whose fate the window never established (PLAN §2.3).

        Counted at the end rather than when the state was entered: `SUBMIT_UNKNOWN` is a normal
        transient that recovery resolves, and only one that is *still* unresolved when the window
        closes is an incident.
        """
        last = max((cycle.started_at for cycle in self._cycles.values()), default=None)
        return tuple(
            Incident(
                kind=IncidentKind.ORDER_STRANDED,
                at=last or _EPOCH,
                scope=client_order_id,
                detail=f"order left in {state.value}; its fate was never established",
            )
            for client_order_id, state in sorted(self._orders.items())
            if state in STRANDED_STATES
        )

    # ------------------------------------------------------------------ handlers

    def _cycle_started(self, event: Event) -> None:
        cycle_id = event.cycle_id or event.aggregate_id
        self._cycles[cycle_id] = CycleFacts(
            cycle_id=cycle_id,
            basket_id=event.basket_id or text(event, "basket_id"),
            venue=text(event, "venue") or UNKNOWN_VENUE,
            started_at=event.ts,
        )

    def _cycle_completed(self, event: Event) -> None:
        cycle = self._cycles.get(event.cycle_id or event.aggregate_id)
        if cycle is None:
            return
        outcome = CycleOutcome(text(event, "outcome"))
        self._cycles[cycle.cycle_id] = CycleFacts(
            cycle_id=cycle.cycle_id,
            basket_id=cycle.basket_id,
            venue=cycle.venue,
            started_at=cycle.started_at,
            completed_at=event.ts,
            outcome=outcome,
            cost_usd=money(event, "cost_usd"),
        )
        if outcome is CycleOutcome.FAILED:
            self._incidents.append(
                Incident(
                    kind=IncidentKind.CYCLE_FAILED,
                    at=event.ts,
                    scope=cycle.basket_id,
                    detail=f"cycle {cycle.cycle_id} failed closed",
                )
            )

    def _decision_made(self, event: Event) -> None:
        self._actions[str(nested(event, "decision", "action"))] += 1

    def _order_submitted(self, event: Event) -> None:
        self._orders[event.aggregate_id] = _order_state(nested(event, "order", "state"))

    def _order_state_changed(self, event: Event) -> None:
        self._orders[event.aggregate_id] = _order_state(event.payload.get("state"))

    def _fill_received(self, event: Event) -> None:
        self._fills += 1
        self._orders[event.aggregate_id] = _order_state(event.payload.get("order_state"))

    def _round_trip_closed(self, event: Event) -> None:
        self._round_trips.append(
            RoundTripFacts(
                at=event.ts,
                basket_id=event.basket_id or "",
                instrument_key=event.aggregate_id,
                realized_pnl=to_decimal(nested(event, "round_trip", "realized_pnl") or 0),
            )
        )

    def _risk_event(self, event: Event) -> None:
        self._risk_events[f"{text(event, 'rule')}/{text(event, 'action_taken')}"] += 1

    def _reconciled(self, event: Event) -> None:
        report = event.payload.get("report")
        if not isinstance(report, dict):
            return
        classification = ReconcileClass(str(report.get("classification")))
        venue = str(report.get("venue", ""))
        self._reconciliations.append(
            ReconcileFacts(at=event.ts, venue=venue, classification=classification)
        )
        if classification is ReconcileClass.MISMATCH:
            self._incidents.append(
                Incident(
                    kind=IncidentKind.RECON_MISMATCH,
                    at=event.ts,
                    scope=venue,
                    detail="the ledger and the venue disagreed in a way nothing explained",
                )
            )

    def _kill_switch_changed(self, event: Event) -> None:
        if KillSwitchState(text(event, "state")) is not KillSwitchState.TRIPPED:
            return
        self._incidents.append(
            Incident(
                kind=IncidentKind.KILL_SWITCH,
                at=event.ts,
                scope=text(event, "actor"),
                detail=text(event, "reason"),
            )
        )

    def _basket_status_changed(self, event: Event) -> None:
        if text(event, "status") != "halted":
            return
        self._incidents.append(
            Incident(
                kind=IncidentKind.BASKET_HALTED,
                at=event.ts,
                scope=text(event, "basket_id"),
                detail=text(event, "reason"),
            )
        )


#: Stands in when a stranded order cannot be dated because no cycle was recorded at all.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _order_state(value: Any) -> OrderState:
    """A state the log recorded. An unreadable one is `FAILED`: unknown means not established."""
    try:
        return OrderState(str(value))
    except ValueError:
        return OrderState.FAILED
