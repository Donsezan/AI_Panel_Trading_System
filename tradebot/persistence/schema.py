"""Database schema: one append-only event log plus the projections derived from it.

The log is the truth and the compliance artifact; projections are a disposable cache that
exists so the dashboard can answer questions cheaply. Anything in a projection must be
reconstructable by replaying the log — `rebuild_projections` proves it on demand.

**Money is stored as TEXT, never as a numeric column.** SQLite's `NUMERIC` affinity converts
through IEEE-754 double, which would silently corrupt the exact decimals the entire money layer
exists to preserve. Same for datetimes: SQLite has no timezone-aware type, so instants are
stored as ISO-8601 UTC strings and parsed back through the UTC-enforcing converter.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.engine import Dialect

from tradebot.core.clock import ensure_utc
from tradebot.core.money import to_decimal

metadata = MetaData()


class DecimalText(TypeDecorator[Decimal]):
    """Exact decimal storage. TEXT in, TEXT out, no float anywhere in between.

    Accepts the string form too, because projectors bind values straight from JSON event
    payloads. `to_decimal` still refuses a `float`, so this convenience cannot become a hole
    in the money-path ban (PLAN §2.1).
    """

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: Decimal | str | int | None, _dialect: Dialect
    ) -> str | None:
        return None if value is None else str(to_decimal(value))

    def process_result_value(self, value: str | None, _dialect: Dialect) -> Decimal | None:
        return None if value is None else to_decimal(value)


class UtcText(TypeDecorator[datetime]):
    """ISO-8601 UTC instants. Naive values are rejected on the way in and on the way out.

    Also accepts the ISO string form, for the same reason `DecimalText` accepts strings: event
    payloads are JSON. A naive string is still rejected by `ensure_utc`.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | str | None, _dialect: Dialect) -> str | None:
        if value is None:
            return None
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        return ensure_utc(moment).isoformat()

    def process_result_value(self, value: str | None, _dialect: Dialect) -> datetime | None:
        return None if value is None else ensure_utc(datetime.fromisoformat(value))


#: The audit trail. Append-only: no code updates or deletes a row here.
events = Table(
    "events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("ts", UtcText, nullable=False),
    Column("type", String(48), nullable=False),
    Column("aggregate_id", String(128), nullable=False),
    Column("basket_id", String(64)),
    Column("cycle_id", String(64)),
    Column("payload_json", Text, nullable=False),
    Index("ix_events_aggregate", "aggregate_id"),
    Index("ix_events_cycle", "cycle_id"),
    Index("ix_events_type", "type"),
)

cycles = Table(
    "cycles",
    metadata,
    Column("cycle_id", String(64), primary_key=True),
    Column("basket_id", String(64), nullable=False),
    Column("started_at", UtcText, nullable=False),
    Column("completed_at", UtcText),
    Column("outcome", String(32)),
    Column("snapshot_id", String(64)),
    Column("snapshot_digest", String(64)),
    Column("cost_usd", DecimalText),
    Index("ix_cycles_basket", "basket_id"),
)

decisions = Table(
    "decisions",
    metadata,
    Column("cycle_id", String(64), primary_key=True),
    Column("instrument_key", String(128), primary_key=True),
    Column("action", String(16), nullable=False),
    Column("conviction", DecimalText, nullable=False),
    Column("size_hint", String(16), nullable=False),
    Column("reasoning_summary", Text, default=""),
    Column("dissent_json", Text, default="[]"),
    Column("flags_json", Text, default="[]"),
    Column("decided_at", UtcText, nullable=False),
)

#: `client_order_id` is the primary key, which is what makes a duplicate submit a loud
#: integrity error rather than a second position (PLAN §2.2).
orders = Table(
    "orders",
    metadata,
    Column("client_order_id", String(64), primary_key=True),
    Column("basket_id", String(64), nullable=False),
    Column("cycle_id", String(64), nullable=False),
    Column("instrument_key", String(128), nullable=False),
    Column("side", String(8), nullable=False),
    Column("order_type", String(24), nullable=False),
    Column("qty", DecimalText, nullable=False),
    Column("limit_price", DecimalText),
    Column("state", String(24), nullable=False),
    Column("venue_order_id", String(64)),
    Column("filled_qty", DecimalText, nullable=False),
    Column("avg_fill_price", DecimalText),
    Column("created_at", UtcText, nullable=False),
    Column("updated_at", UtcText, nullable=False),
    Index("ix_orders_state", "state"),
    Index("ix_orders_cycle", "cycle_id"),
)

fills = Table(
    "fills",
    metadata,
    Column("fill_id", String(64), primary_key=True),
    Column("client_order_id", String(64), nullable=False),
    Column("instrument_key", String(128), nullable=False),
    Column("side", String(8), nullable=False),
    Column("qty", DecimalText, nullable=False),
    Column("price", DecimalText, nullable=False),
    Column("fee", DecimalText, nullable=False),
    Column("fee_currency", String(16), default=""),
    Column("filled_at", UtcText, nullable=False),
    Index("ix_fills_order", "client_order_id"),
)

positions = Table(
    "positions",
    metadata,
    Column("instrument_key", String(128), primary_key=True),
    Column("qty", DecimalText, nullable=False),
    Column("avg_entry", DecimalText, nullable=False),
    Column("realized_pnl", DecimalText, nullable=False),
    Column("held_cycles", Integer, nullable=False, default=0),
    Column("updated_at", UtcText, nullable=False),
)

risk_events = Table(
    "risk_events",
    metadata,
    Column("event_seq", Integer, primary_key=True),
    Column("ts", UtcText, nullable=False),
    Column("tier", String(16), nullable=False),
    Column("rule", String(64), nullable=False),
    Column("scope", String(128), nullable=False),
    Column("action_taken", String(32), nullable=False),
    Column("detail", Text, default=""),
)

#: Every table except `events`. Truncated and rebuilt by a replay.
PROJECTION_TABLES: tuple[Table, ...] = (cycles, decisions, orders, fills, positions, risk_events)


def as_row(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop `None` values so an update never overwrites a known column with nothing."""
    return {key: value for key, value in mapping.items() if value is not None}
