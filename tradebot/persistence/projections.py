"""Derives the read model from the event log.

Dispatch is a table, not a chain of `if`s: an event type either has a projector or it doesn't,
and adding one cannot disturb another. Event types with no projector are intentional — they are
audit-only records that no dashboard query needs (seat responses, risk-check provenance).

Every projector must be **idempotent under replay**: `rebuild_projections` replays the whole
log into a truncated read model and must land on exactly the state incremental application
produced. A scenario test asserts that equivalence, because a projector that only works
forwards silently destroys the audit guarantee.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, delete, select, update
from sqlalchemy.dialects.sqlite import insert

from tradebot.core.events import Event, EventType
from tradebot.persistence.schema import (
    PROJECTION_TABLES,
    cycles,
    decisions,
    events,
    fills,
    orders,
    positions,
    risk_events,
)

Projector = Callable[[Connection, Event], None]


def _upsert(connection: Connection, table: Any, values: dict[str, Any], keys: list[str]) -> None:
    """Insert or update on the table's natural key."""
    statement = insert(table).values(**values)
    update_columns = {k: v for k, v in values.items() if k not in keys}
    connection.execute(
        statement.on_conflict_do_update(index_elements=keys, set_=update_columns)
        if update_columns
        else statement.on_conflict_do_nothing(index_elements=keys)
    )


def _project_cycle_started(connection: Connection, event: Event) -> None:
    _upsert(
        connection,
        cycles,
        {"cycle_id": event.cycle_id, "basket_id": event.basket_id, "started_at": event.ts},
        ["cycle_id"],
    )


def _project_snapshot_frozen(connection: Connection, event: Event) -> None:
    connection.execute(
        update(cycles)
        .where(cycles.c.cycle_id == event.cycle_id)
        .values(
            snapshot_id=event.payload["snapshot_id"],
            snapshot_digest=event.payload["digest"],
        )
    )


def _project_decision_made(connection: Connection, event: Event) -> None:
    decision = event.payload["decision"]
    _upsert(
        connection,
        decisions,
        {
            "cycle_id": event.cycle_id,
            "instrument_key": decision["instrument_key"],
            "action": decision["action"],
            "conviction": decision["conviction"],
            "size_hint": decision["size_hint"],
            "reasoning_summary": decision["reasoning_summary"],
            "dissent_json": json.dumps(decision["dissent"]),
            "flags_json": json.dumps(decision["flags"]),
            "decided_at": event.ts,
        },
        ["cycle_id", "instrument_key"],
    )


def _project_order_submitted(connection: Connection, event: Event) -> None:
    order = event.payload["order"]
    _upsert(
        connection,
        orders,
        {
            "client_order_id": order["client_order_id"],
            "basket_id": order["basket_id"],
            "cycle_id": order["cycle_id"],
            "instrument_key": order["instrument_key"],
            "side": order["side"],
            "order_type": order["order_type"],
            "qty": order["qty"],
            "limit_price": order["limit_price"],
            "state": order["state"],
            "venue_order_id": order["venue_order_id"],
            "filled_qty": "0",
            "created_at": order["created_at"],
            "updated_at": order["updated_at"],
        },
        ["client_order_id"],
    )


def _project_order_state_changed(connection: Connection, event: Event) -> None:
    connection.execute(
        update(orders)
        .where(orders.c.client_order_id == event.aggregate_id)
        .values(
            state=event.payload["state"],
            venue_order_id=event.payload.get("venue_order_id"),
            updated_at=event.payload["updated_at"],
        )
    )


def _project_fill_received(connection: Connection, event: Event) -> None:
    fill = event.payload["fill"]
    _upsert(
        connection,
        fills,
        {
            "fill_id": fill["fill_id"],
            "client_order_id": fill["client_order_id"],
            "instrument_key": fill["instrument_key"],
            "side": fill["side"],
            "qty": fill["qty"],
            "price": fill["price"],
            "fee": fill["fee"],
            "fee_currency": fill["fee_currency"],
            "filled_at": fill["filled_at"],
        },
        ["fill_id"],
    )
    connection.execute(
        update(orders)
        .where(orders.c.client_order_id == fill["client_order_id"])
        .values(
            state=event.payload["order_state"],
            filled_qty=event.payload["filled_qty"],
            avg_fill_price=event.payload["avg_fill_price"],
            updated_at=fill["filled_at"],
        )
    )


def _project_position_updated(connection: Connection, event: Event) -> None:
    position = event.payload["position"]
    _upsert(
        connection,
        positions,
        {
            "instrument_key": position["instrument_key"],
            "qty": position["qty"],
            "avg_entry": position["avg_entry"],
            "realized_pnl": position["realized_pnl"],
            "held_cycles": position["held_cycles"],
            "updated_at": event.ts,
        },
        ["instrument_key"],
    )


def _project_risk_event(connection: Connection, event: Event) -> None:
    _upsert(
        connection,
        risk_events,
        {
            "event_seq": event.seq,
            "ts": event.ts,
            "tier": event.payload["tier"],
            "rule": event.payload["rule"],
            "scope": event.payload["scope"],
            "action_taken": event.payload["action_taken"],
            "detail": event.payload["detail"],
        },
        ["event_seq"],
    )


def _project_cycle_completed(connection: Connection, event: Event) -> None:
    connection.execute(
        update(cycles)
        .where(cycles.c.cycle_id == event.cycle_id)
        .values(
            completed_at=event.ts,
            outcome=event.payload["outcome"],
            cost_usd=event.payload["cost_usd"],
        )
    )


#: Audit-only event types are absent by design, not by omission.
PROJECTORS: dict[EventType, Projector] = {
    EventType.CYCLE_STARTED: _project_cycle_started,
    EventType.SNAPSHOT_FROZEN: _project_snapshot_frozen,
    EventType.DECISION_MADE: _project_decision_made,
    EventType.ORDER_SUBMITTED: _project_order_submitted,
    EventType.ORDER_STATE_CHANGED: _project_order_state_changed,
    EventType.FILL_RECEIVED: _project_fill_received,
    EventType.POSITION_UPDATED: _project_position_updated,
    EventType.RISK_EVENT: _project_risk_event,
    EventType.CYCLE_COMPLETED: _project_cycle_completed,
}


def apply_event(connection: Connection, event: Event) -> None:
    """Fold one event into the read model. A type with no projector is a no-op by design."""
    projector = PROJECTORS.get(event.type)
    if projector is not None:
        projector(connection, event)


def rebuild_projections(connection: Connection) -> int:
    """Truncate the read model and replay the entire log into it.

    Used by the startup sequence to verify projections against the log (DESIGN §8.2), and by
    the test that proves the log alone can reconstruct system state.
    """
    for table in reversed(PROJECTION_TABLES):
        connection.execute(delete(table))

    replayed = 0
    for row in connection.execute(select(events).order_by(events.c.seq)):
        apply_event(
            connection,
            Event(
                event_id=row.event_id,
                seq=row.seq,
                ts=row.ts,
                type=EventType(row.type),
                aggregate_id=row.aggregate_id,
                basket_id=row.basket_id,
                cycle_id=row.cycle_id,
                payload=json.loads(row.payload_json),
            ),
        )
        replayed += 1
    return replayed
