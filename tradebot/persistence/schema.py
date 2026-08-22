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
    Connection,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.sqlite import insert
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


#: The audit trail. Append-only, with **exactly one exception**: `maintenance/compaction.py`
#: updates `payload_json` for two event types once their day has been archived and verified,
#: dropping a seat's verbatim completion and a frozen snapshot's body (ADR 0028). No code anywhere
#: deletes a row. Nothing else may update one — what licenses that exception is an invariant
#: asserted directly, that a projection rebuild after compaction is identical to one before it.
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
    #: `{"basket:demo": 4, "global_risk:global": 2}` — the exact config versions this cycle ran
    #: on, so a decision is re-read against the limits that produced it (DESIGN §6.1).
    Column("config_versions_json", Text, default="{}"),
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
    Column("role", String(16), nullable=False, default="entry"),
    Column("group_id", String(64), nullable=False, default=""),
    Column("qty", DecimalText, nullable=False),
    Column("limit_price", DecimalText),
    Column("stop_price", DecimalText),
    Column("state", String(24), nullable=False),
    Column("venue_order_id", String(64)),
    Column("filled_qty", DecimalText, nullable=False),
    Column("avg_fill_price", DecimalText),
    Column("expires_at", UtcText),
    Column("created_at", UtcText, nullable=False),
    Column("updated_at", UtcText, nullable=False),
    Index("ix_orders_state", "state"),
    Index("ix_orders_cycle", "cycle_id"),
    Index("ix_orders_group", "group_id"),
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
    Column("opened_at", UtcText),
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

#: What the dashboard's bell shows: one row per alert the rules produced, dismissed or not.
#:
#: A true projection, folded from `NOTIFICATION_RAISED` and `ALERT_DISMISSED` and listed in
#: `PROJECTION_TABLES`, so a rebuild reproduces the notification history *and* its dismissals.
#: `alert_id` is deterministic — `"{event_seq}:{kind}"`, or `"summary:{day}"` for the daily line —
#: which is what makes recording idempotent: a retry folds onto the row that already exists.
notifications = Table(
    "notifications",
    metadata,
    Column("alert_id", String(96), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("severity", String(8), nullable=False),
    #: When the thing *happened*, not when the tail noticed it.
    Column("at", UtcText, nullable=False),
    Column("scope", String(128), nullable=False, default=""),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False, default=""),
    #: The event that justified it, for the drill-down. Zero for the daily summary, which the
    #: clock produces rather than anything in the log.
    Column("event_seq", Integer, nullable=False, default=0),
    #: Null until cleared. `dismissed_by` is `dashboard` for an operator's click and `system`
    #: for a `MAINTENANCE_OK` superseded by the next day's (spec §5.4).
    Column("dismissed_at", UtcText),
    Column("dismissed_by", String(32)),
    Index("ix_notifications_open", "dismissed_at"),
)

#: Closed positions. The unit the consecutive-loss rule counts, and the tax artifact.
round_trips = Table(
    "round_trips",
    metadata,
    Column("event_seq", Integer, primary_key=True),
    Column("basket_id", String(64), nullable=False),
    Column("instrument_key", String(128), nullable=False),
    Column("qty", DecimalText, nullable=False),
    Column("entry_price", DecimalText, nullable=False),
    Column("exit_price", DecimalText, nullable=False),
    Column("realized_pnl", DecimalText, nullable=False),
    Column("opened_at", UtcText),
    Column("closed_at", UtcText, nullable=False),
    Index("ix_round_trips_instrument", "instrument_key"),
)

reconciliations = Table(
    "reconciliations",
    metadata,
    Column("event_seq", Integer, primary_key=True),
    Column("venue", String(32), nullable=False),
    Column("classification", String(32), nullable=False),
    Column("observed_at", UtcText, nullable=False),
    Column("detail", Text, default=""),
    Index("ix_reconciliations_class", "classification"),
)

#: Risk state that must survive a restart (DESIGN §8.2 step 4). Written directly rather than
#: projected: it is *current posture*, not a fold of history, and the startup sequence reads it
#: before any event has been replayed. Every change also emits an event for the audit trail.
risk_state = Table(
    "risk_state",
    metadata,
    Column("scope", String(32), primary_key=True),
    Column("kill_switch", String(16), nullable=False),
    Column("reason", Text, default=""),
    Column("high_water_mark", DecimalText, nullable=False),
    Column("day_start_equity", DecimalText, nullable=False),
    Column("day_started_on", String(10), default=""),
    Column("updated_at", UtcText, nullable=False),
)

#: The live-arming row (PLAN §2.4). Live mode requires a row here saying `armed`, carrying a
#: notional cap, and naming the human who set it — one of four independent preconditions, none of
#: which can be satisfied by a default, an env var or a typo. It lives in the database because the
#: other three are transient: a flag in a config file survives a reboot nobody authorised.
live_arming = Table(
    "live_arming",
    metadata,
    Column("scope", String(32), primary_key=True),
    Column("armed", Integer, nullable=False, default=0),
    #: Largest notional a single live order may carry. Enforced as a Tier-2 rule; live refuses to
    #: start without it, because "unlimited" is not a cap someone chose.
    Column("max_live_notional", DecimalText),
    Column("armed_by", String(64), default=""),
    Column("note", Text, default=""),
    Column("updated_at", UtcText, nullable=False),
)

#: User-editable configuration, versioned (DESIGN §6.1). An update inserts a new version rather
#: than overwriting one, because a cycle pins the versions it ran on and a replay has to be able
#: to resolve them — an overwritten row would make every past decision unauditable. Retirement is
#: a version too: a deleted basket must still resolve for the cycles that ran it.
config_versions = Table(
    "config_versions",
    metadata,
    Column("kind", String(32), primary_key=True),
    Column("config_id", String(64), primary_key=True),
    Column("version", Integer, primary_key=True),
    #: The whole document as canonical JSON. Secrets are referenced by env-var *name* and the
    #: store refuses a document any secret value can be found in (PLAN §3.2).
    Column("document_json", Text, nullable=False),
    Column("retired", Integer, nullable=False, default=0),
    Column("actor", String(64), default=""),
    Column("note", Text, default=""),
    Column("created_at", UtcText, nullable=False),
)

#: Normalized news, point-in-time. `observed_at` is *our* stamp and the only field a replayed
#: cycle may filter on — `published_at` is the publisher's claim and cannot order a replay
#: (DESIGN §6.4). Only title + excerpt + link are retained, never article bodies (PLAN §3.3).
news_items = Table(
    "news_items",
    metadata,
    Column("item_id", String(64), primary_key=True),
    Column("source_id", String(64), nullable=False),
    Column("url", Text, nullable=False),
    Column("url_hash", String(64), nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("excerpt", Text, nullable=False, default=""),
    Column("published_at", UtcText, nullable=False),
    Column("observed_at", UtcText, nullable=False),
    Index("ix_news_items_observed", "observed_at"),
    Index("ix_news_items_source", "source_id"),
)

#: Embeddings for dedup and historical retrieval, one row per stored document. Separate from
#: `news_items` because it implements the generic `VectorStore` seam: swapping in a real
#: embedding model, or Chroma, replaces this table and leaves the news rows untouched.
news_vectors = Table(
    "news_vectors",
    metadata,
    Column("doc_id", String(64), primary_key=True),
    Column("text", Text, nullable=False),
    Column("metadata_json", Text, nullable=False, default="{}"),
    Column("vector_json", Text, nullable=False),
    Column("observed_at", UtcText, nullable=False),
    Index("ix_news_vectors_observed", "observed_at"),
)

#: How far ops alerting has read the log (ADR 0019). Not a projection and not risk state: it is a
#: *delivery* position, written after a batch has actually been sent, so a restart resumes at the
#: last alert that reached a human rather than replaying weeks of them or skipping the one that
#: mattered. `degraded_streak` is here for the same reason the risk baselines are persisted — a
#: streak counted in memory is a streak a restart forgives.
alert_cursor = Table(
    "alert_cursor",
    metadata,
    Column("scope", String(32), primary_key=True),
    #: How far **delivery** has got. Advanced only after a sink has taken the notification,
    #: which is what makes delivery at-least-once (ADR 0019). Indexes `NOTIFICATION_RAISED`.
    Column("last_seq", Integer, nullable=False, default=0),
    #: How far **recording** has got, over the alert source types. A separate cursor because the
    #: two fail differently: a dead webhook must not withhold what the operator could already see
    #: on screen, and a retry must not append a second notification (spec 5.2).
    Column("recorded_seq", Integer, nullable=False, default=0),
    #: The session day the last daily summary covered. Empty means none has been sent, which is
    #: how the first poll of a fresh database avoids summarising a day it only saw the end of.
    Column("last_summary_day", String(10), default=""),
    Column("degraded_streak", Integer, nullable=False, default=0),
    Column("stale_streak", Integer, nullable=False, default=0),
    Column("updated_at", UtcText, nullable=False),
)

basket_status = Table(
    "basket_status",
    metadata,
    Column("basket_id", String(64), primary_key=True),
    Column("status", String(16), nullable=False),
    Column("reason", Text, default=""),
    Column("updated_at", UtcText, nullable=False),
)

#: Derived from the log and rebuilt by a replay. `risk_state`, `basket_status` and `alert_cursor`
#: are excluded deliberately: truncating the first two during a rebuild would un-halt a halted
#: system, and truncating the third would re-send every alert in the log. `news_items`
#: and `news_vectors` are excluded too, for the opposite reason: they are *observations*, not a
#: fold of our own events, and a rebuild cannot re-fetch what a publisher has since taken down.
#: `config_versions` is excluded for the first reason and then some: it is what the log's pinned
#: versions *resolve against*, so a rebuild that truncated it would erase the meaning of the log
#: it was rebuilding from.
PROJECTION_TABLES: tuple[Table, ...] = (
    cycles,
    decisions,
    orders,
    fills,
    positions,
    risk_events,
    round_trips,
    reconciliations,
    notifications,
)


def upsert(connection: Connection, table: Table, values: dict[str, Any], keys: list[str]) -> None:
    """Insert or update on the table's natural key.

    Shared by the projectors and the risk-state store so that "write a row" means exactly one
    thing, and so replay and live application cannot diverge on conflict handling.
    """
    statement = insert(table).values(**values)
    updates = {key: value for key, value in values.items() if key not in keys}
    connection.execute(
        statement.on_conflict_do_update(index_elements=keys, set_=updates)
        if updates
        else statement.on_conflict_do_nothing(index_elements=keys)
    )
