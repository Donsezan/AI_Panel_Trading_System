"""The event store: append to the log and fold into the read model, in one transaction.

Atomicity is the whole point. If the event committed but the projection didn't, the dashboard
would show a state the audit trail contradicts; if the projection committed but the event
didn't, the state would be unreconstructable. Both are written together or neither is.

Failure semantics: an append that fails raises and writes nothing. Callers treat that as
`FailClosed` — an order whose intent could not be durably recorded must not be submitted
(PLAN §1.4).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, Select, func, select

from tradebot.core.clock import ensure_utc
from tradebot.core.events import Event, EventType
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.projections import apply_event, rebuild_projections
from tradebot.persistence.schema import events


class EventStore:
    """Append-only log plus its projections. The only writer of either."""

    def __init__(self, engine: Engine, writer: SingleWriter) -> None:
        self._engine = engine
        self._writer = writer

    @property
    def engine(self) -> Engine:
        """Read-only access for projection queries. Writes go through `append` only."""
        return self._engine

    async def append(self, *new_events: Event) -> tuple[Event, ...]:
        """Append events and project them atomically, returning them with their `seq`."""
        if not new_events:
            return ()
        return await self._writer.run(lambda connection: self._append(connection, new_events))

    def append_within(self, connection: Connection, *new_events: Event) -> tuple[Event, ...]:
        """Append inside a transaction the caller already owns.

        For stores that write a row of their own *and* its audit event: two `append` calls would
        be two transactions, and a crash between them would leave either a configuration nobody
        authorised or an authorisation of a configuration that was never stored.
        """
        return self._append(connection, new_events)

    @staticmethod
    def _append(connection: Connection, new_events: Sequence[Event]) -> tuple[Event, ...]:
        stored: list[Event] = []
        for event in new_events:
            result = connection.execute(
                events.insert().values(
                    event_id=event.event_id,
                    ts=event.ts,
                    type=event.type.value,
                    aggregate_id=event.aggregate_id,
                    basket_id=event.basket_id,
                    cycle_id=event.cycle_id,
                    payload_json=event.payload_json,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise RuntimeError(f"event {event.event_id} was inserted without a sequence")
            sequenced = event.sequenced(int(primary_key[0]))
            apply_event(connection, sequenced)
            stored.append(sequenced)
        return tuple(stored)

    async def rebuild(self) -> int:
        """Replay the log into a truncated read model. Returns the number of events replayed."""
        return await self._writer.run(rebuild_projections)

    # ------------------------------------------------------------------ reads
    # Reads bypass the writer: WAL lets them run concurrently with a cycle in flight.

    def read_all(self) -> tuple[Event, ...]:
        return self._read(select(events).order_by(events.c.seq))

    def read_cycle(self, cycle_id: str) -> tuple[Event, ...]:
        """One cycle's events, in order — the dashboard's decision drill-down.

        Scoped rather than filtered from `read_all`, because seat responses and frozen snapshots
        are the largest payloads in the log and a soak accumulates months of them: the drill-down
        must cost one cycle, not one database.
        """
        return self._read(
            select(events).where(events.c.cycle_id == cycle_id).order_by(events.c.seq)
        )

    def read_types(
        self,
        *types: EventType,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> tuple[Event, ...]:
        """The log, narrowed to the given types and an optional window, in order.

        What the validation reports are built from. They read the **log** rather than the
        projections because the facts a promotion decision turns on — a kill switch trip, a
        basket halt — are audit-only and have no projector at all (DESIGN §9, `projections.py`).
        Narrowing by type is what keeps that affordable: frozen snapshots and seat transcripts
        are the largest payloads in the log and no report needs them.
        """
        query = select(events).where(events.c.type.in_([type_.value for type_ in types]))
        if since is not None:
            query = query.where(events.c.ts >= ensure_utc(since))
        if until is not None:
            query = query.where(events.c.ts <= ensure_utc(until))
        return self._read(query.order_by(events.c.seq))

    def read_after(self, seq: int, *types: EventType, limit: int = 500) -> tuple[Event, ...]:
        """The next events of the given types after `seq`, oldest first.

        Ordered by **sequence, not timestamp**, because this is what ops alerting tails and a
        cursor has to be exact: two events sharing a timestamp must not let a restart skip one
        (ADR 0019). `limit` bounds the batch so a tailer starting on a months-old database
        delivers in chunks rather than loading the whole log into memory.
        """
        return self._read(
            select(events)
            .where(events.c.seq > seq, events.c.type.in_([type_.value for type_ in types]))
            .order_by(events.c.seq)
            .limit(limit)
        )

    def last_seq(self) -> int:
        """The log's high-water sequence. Zero on an empty log."""
        with self._engine.connect() as connection:
            return int(connection.execute(select(func.max(events.c.seq))).scalar() or 0)

    def _read(self, query: Select[Any]) -> tuple[Event, ...]:
        with self._engine.connect() as connection:
            rows = connection.execute(query).all()
        return tuple(
            Event(
                event_id=row.event_id,
                seq=row.seq,
                ts=row.ts,
                type=EventType(row.type),
                aggregate_id=row.aggregate_id,
                basket_id=row.basket_id,
                cycle_id=row.cycle_id,
                payload=json.loads(row.payload_json),
            )
            for row in rows
        )

    def event_types(self, cycle_id: str) -> tuple[EventType, ...]:
        """The ordered event chain for one cycle — what scenario tests assert against."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(events.c.type).where(events.c.cycle_id == cycle_id).order_by(events.c.seq)
            ).all()
        return tuple(EventType(row.type) for row in rows)

    def count(self) -> int:
        with self._engine.connect() as connection:
            return int(connection.execute(select(func.count()).select_from(events)).scalar_one())
