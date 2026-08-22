"""Read-only projection queries — the only place SQL for the UI lives.

The dashboard reads **projections, never the log** for its lists and totals (DESIGN §6.9): the
log is the audit artifact and the projections exist precisely so the UI can ask cheap questions
of it. The one deliberate exception is the decision drill-down, where seat responses, risk-check
provenance, frozen snapshots and protective placements have no projector because nothing but the
audit view ever reads them — those come from `EventStore.read_cycle`, scoped to one cycle.

Rows are returned as SQLAlchemy `Row`s rather than re-wrapped in per-view models. The schema is
already the contract, and a second set of field names is a second place for it to drift. Money
arrives as `Decimal` through `DecimalText` and instants as UTC-aware `datetime` through
`UtcText`, so a template renders exact values without a conversion step that could reintroduce a
float (PLAN §2.1).

The **equity curve is computed here on read** rather than projected: it is a running total of
`round_trips.realized_pnl`, which a rebuild reproduces by definition. A projection would be
another table a replay must reproduce byte-identically, and a drift there is a silently wrong
research artifact. Note what the curve is and is not — no historical *marks* are persisted, so
this is the realized curve, and unrealized PnL appears only as the current mark-to-market figure
beside it.

Failure semantics: this module only reads and never writes. An absent row is `None` and an empty
result is an empty tuple — a view that shows nothing is correct when there is nothing, and is
always preferable to a view that invents a zero.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Engine, Row, Select, func, select

from tradebot.core.config import ConfigRef
from tradebot.core.enums import ConfigKind, OrderRole, OrderState
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO, divide
from tradebot.dashboard.scope import Scope
from tradebot.maintenance.compaction import MARKER_KEY
from tradebot.persistence.schema import (
    cycles,
    decisions,
    fills,
    notifications,
    orders,
    positions,
    reconciliations,
    risk_events,
    round_trips,
)
from tradebot.persistence.store import EventStore

#: How many rows a list view shows before the operator has to narrow it. Generous enough that a
#: day of a ten-minute basket fits on one page.
DEFAULT_LIMIT = 100

#: What every list query returns. Projection rows carry their own column names, which are the
#: schema's — a per-view model would be a second set of names for one contract to drift between.
Rows = tuple[Row[Any], ...]

_OPEN_STATES = tuple(state.value for state in OrderState if state.is_open)

#: What the operation log shows of a decision beside its cycle. Named rather than `decisions.*`
#: because `cycle_id` appears on both sides of the join and one row may not carry it twice.
_DECISION_COLUMNS = (
    decisions.c.instrument_key,
    decisions.c.action,
    decisions.c.conviction,
    decisions.c.size_hint,
    decisions.c.reasoning_summary,
)


def _decision_join(scope: Scope | None) -> ColumnElement[bool]:
    """The join condition for `activity`, narrowed to one instrument when one is selected."""
    condition = decisions.c.cycle_id == cycles.c.cycle_id
    if scope is None or scope.instrument_key is None:
        return condition
    return condition & (decisions.c.instrument_key == scope.instrument_key)


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One closed round trip and the running realized total after it."""

    closed_at: datetime
    instrument_key: str
    realized_pnl: Decimal
    cumulative: Decimal


@dataclass(frozen=True, slots=True)
class CostRow:
    """One basket's deliberation spend. `$/decision` is what makes panels comparable."""

    basket_id: str
    cycle_count: int
    total_cost: Decimal

    @property
    def per_cycle(self) -> Decimal:
        return divide(self.total_cost, Decimal(self.cycle_count)) if self.cycle_count else ZERO


@dataclass(frozen=True, slots=True)
class CycleDetail:
    """Everything the decision drill-down shows for one cycle — the core research artifact.

    Carries the raw events alongside the projections because that is the point of the view: the
    exact snapshot the panel saw, what each seat said, why risk approved or refused, and what
    reached the venue, all resolvable to the configuration versions that produced them.
    """

    cycle: Row[Any]
    decisions: tuple[Row[Any], ...]
    orders: tuple[Row[Any], ...]
    fills: tuple[Row[Any], ...]
    events: tuple[Event, ...]
    pins: tuple[ConfigRef, ...]

    def events_of(self, *types: EventType | str) -> tuple[Event, ...]:
        """This cycle's events of the given types, in log order.

        Accepts the names as strings so a template can ask for `"SEAT_RESPONDED"` without
        importing the enum. `EventType` is a `StrEnum`, so the comparison is the same one.
        """
        return tuple(event for event in self.events if event.type in types)

    @property
    def instruments(self) -> tuple[str, ...]:
        """The instruments this cycle deliberated on — what the drill-down's filter may offer.

        Decisions and the transcript only, because those are the two sections `narrowed_to`
        hides. An instrument reaching Orders without either would get a checkbox that changes
        nothing on screen, and a control that does nothing is one an operator has to test to
        understand.
        """
        keys = {row.instrument_key for row in self.decisions}
        keys.update(key for event in self.events if (key := seat_instrument(event)) is not None)
        return tuple(sorted(keys))

    def narrowed_to(self, instruments: Collection[str]) -> CycleDetail:
        """This cycle with its *deliberation* narrowed to the named instruments.

        Only the decisions and the debate transcript narrow. What was in force and what happened
        — the configuration pins, risk checks, orders, fills and the frozen snapshot — stay whole:
        a portfolio-wide veto such as `max_gross_exposure` is recorded against one instrument but
        is the condition that shaped every other instrument's decision in the same cycle, so a
        filter that hid it would show a clean flow with the reason for it missing. The snapshot is
        the packet every seat saw, for all of them at once.

        That falls out of the event rule rather than being a list of exemptions: only a seat
        response names an instrument *here*, so every other event type passes through untouched.

        An empty selection narrows nothing. Unticking every box sends no parameter at all, and
        that must read as "all" — a page narrowed to nothing looks like a cycle that deliberated
        on nothing, which is the one thing this view must never say by accident.
        """
        if not instruments:
            return self
        keys = frozenset(instruments)
        return replace(
            self,
            decisions=tuple(row for row in self.decisions if row.instrument_key in keys),
            events=tuple(
                event
                for event in self.events
                if (spoke_for := seat_instrument(event)) is None or spoke_for in keys
            ),
        )

    @property
    def snapshot(self) -> dict[str, Any] | None:
        """The frozen input packet, as stored. `None` if the cycle ended before it was built."""
        frozen = self.events_of(EventType.SNAPSHOT_FROZEN)
        return frozen[-1].payload.get("snapshot") if frozen else None

    @property
    def snapshot_archive(self) -> dict[str, Any] | None:
        """Where a compacted snapshot went, or `None` if it is still here (spec §3.6).

        The body being absent has two very different causes — the cycle was blocked before one
        was built, or retention moved it to a file — and the page must not read the second as the
        first. This is what tells them apart.
        """
        frozen = self.events_of(EventType.SNAPSHOT_FROZEN)
        return frozen[-1].payload.get(MARKER_KEY) if frozen else None


class Queries:
    """Read-only views over the projections for one wired application."""

    def __init__(self, store: EventStore) -> None:
        self._engine: Engine = store.engine
        self._store = store

    # ------------------------------------------------------------------ cycles

    def cycles(self, *, basket_id: str | None = None, limit: int = DEFAULT_LIMIT) -> Rows:
        query = select(cycles).order_by(cycles.c.started_at.desc()).limit(limit)
        if basket_id is not None:
            query = query.where(cycles.c.basket_id == basket_id)
        return self._rows(query)

    def cycle(self, cycle_id: str) -> CycleDetail | None:
        """One cycle with its decisions, orders, fills and audit events. `None` if unknown."""
        row = self._one(select(cycles).where(cycles.c.cycle_id == cycle_id))
        if row is None:
            return None
        cycle_orders = self._rows(
            select(orders).where(orders.c.cycle_id == cycle_id).order_by(orders.c.created_at)
        )
        return CycleDetail(
            cycle=row,
            decisions=self._rows(
                select(decisions)
                .where(decisions.c.cycle_id == cycle_id)
                .order_by(decisions.c.instrument_key)
            ),
            orders=cycle_orders,
            fills=self._rows(
                select(fills)
                .where(fills.c.client_order_id.in_([o.client_order_id for o in cycle_orders]))
                .order_by(fills.c.filled_at)
            )
            if cycle_orders
            else (),
            events=self._store.read_cycle(cycle_id),
            pins=parse_pins(row.config_versions_json),
        )

    def cost_by_basket(self) -> tuple[CostRow, ...]:
        """What each basket has spent on deliberation — the `$/decision` view (DESIGN §6.10).

        Totalled **in Python, not in SQL**. Money is stored as TEXT precisely because SQLite's
        numeric affinity rounds through an IEEE-754 double, and `SUM` over a TEXT column does
        exactly that conversion — it would hand back a float for the one layer that must never
        see one (PLAN §2.1, `persistence/schema.py`).
        """
        totals: dict[str, tuple[int, Decimal]] = {}
        for row in self._rows(select(cycles.c.basket_id, cycles.c.cost_usd)):
            count, spent = totals.get(row.basket_id, (0, ZERO))
            totals[row.basket_id] = (count + 1, spent + (row.cost_usd or ZERO))
        return tuple(
            CostRow(basket_id=basket_id, cycle_count=count, total_cost=spent)
            for basket_id, (count, spent) in sorted(totals.items())
        )

    def latest_decisions(self) -> Rows:
        """The most recent decision for each instrument, per basket — the blotter's rows.

        Partitioned by basket *and* instrument, not by instrument alone: two baskets may hold the
        same instrument, and each row must show what *its* panel last concluded. Ranked in SQL so
        one query returns one row per blotter line, rather than reading the whole decision history
        to throw most of it away.

        `cycle_id` breaks a tie on `decided_at`. Two decisions can only share an instant when the
        clock did not move between cycles — a replay or a frozen test clock — and then there is no
        fact about which is newer. A total order at least makes the blotter show the *same* row on
        every render, rather than one that changes under a refresh.
        """
        joined = (
            select(decisions, cycles.c.basket_id)
            .join_from(decisions, cycles, decisions.c.cycle_id == cycles.c.cycle_id)
            .subquery()
        )
        ranked = select(
            joined,
            func.row_number()
            .over(
                partition_by=(joined.c.basket_id, joined.c.instrument_key),
                order_by=(joined.c.decided_at.desc(), joined.c.cycle_id.desc()),
            )
            .label("recency"),
        ).subquery()
        return self._rows(
            select(ranked)
            .where(ranked.c.recency == 1)
            .order_by(ranked.c.basket_id, ranked.c.instrument_key)
        )

    def activity(self, scope: Scope | None = None, *, limit: int = DEFAULT_LIMIT) -> Rows:
        """Cycles with the decision each reached, newest first — the operation log's rows.

        An **outer** join, and the instrument filter sits in the join rather than the `WHERE`.
        That is what keeps a cycle that decided nothing — `DATA_STALE`, `QUARANTINED`, a degraded
        panel — in the log with an empty decision, instead of vanishing from it. A basket that
        stops appearing is a basket nobody can audit (ADR 0022), and the same reasoning applies to
        the operator's own view of it.
        """
        query = (
            select(cycles, *_DECISION_COLUMNS)
            .select_from(cycles.outerjoin(decisions, _decision_join(scope)))
            .order_by(cycles.c.started_at.desc())
            .limit(limit)
        )
        if scope is not None:
            query = query.where(cycles.c.basket_id == scope.basket_id)
        return self._rows(query)

    # ------------------------------------------------------------------ portfolio

    def positions(self) -> Rows:
        return self._rows(select(positions).order_by(positions.c.instrument_key))

    def open_orders(self) -> Rows:
        return self._rows(
            select(orders).where(orders.c.state.in_(_OPEN_STATES)).order_by(orders.c.created_at)
        )

    def orders(self, *, limit: int = DEFAULT_LIMIT) -> Rows:
        return self._rows(select(orders).order_by(orders.c.created_at.desc()).limit(limit))

    def round_trips(self, *, limit: int = DEFAULT_LIMIT) -> Rows:
        return self._rows(select(round_trips).order_by(round_trips.c.event_seq.desc()).limit(limit))

    def day_realized(self, day_start: datetime) -> Decimal:
        """Realized PnL booked since the daily-loss boundary — the workspace's "today".

        Takes the boundary rather than computing one, so the number the operator watches is
        measured from the same flow-adjusted instant the daily-loss rule uses (DESIGN §6.6). A
        figure with its own idea of when the day started would drift from the limit it is meant to
        preview.

        Totalled **in Python, not in SQL**, for the reason `cost_by_basket` documents: `SUM` over a
        money TEXT column rounds through an IEEE-754 double.
        """
        rows = self._rows(
            select(round_trips.c.realized_pnl).where(round_trips.c.closed_at >= day_start)
        )
        return sum((row.realized_pnl for row in rows), ZERO)

    def cycles_since(self, moment: datetime) -> dict[str, int]:
        """Cycles started per basket since `moment` — the blotter's "cycles today".

        `COUNT` in SQL rather than in Python, unlike every money total here: a count is an
        integer, and integers do not round through a double.
        """
        return self._counts(
            select(cycles.c.basket_id, func.count())
            .where(cycles.c.started_at >= moment)
            .group_by(cycles.c.basket_id)
        )

    def entry_orders_since(self, moment: datetime) -> dict[str, int]:
        """Entry orders placed per basket since `moment` — the blotter's "trades today".

        Entries only, and that is the same rule `HistoryReader` meters the daily cap with: a
        protective leg belongs to the decision that placed it, not to a second trade. A blotter
        counting legs would show a basket at its cap while the rule still let it trade.
        """
        return self._counts(
            select(orders.c.basket_id, func.count())
            .where(orders.c.role == OrderRole.ENTRY.value, orders.c.created_at >= moment)
            .group_by(orders.c.basket_id)
        )

    # ------------------------------------------------------------------ chart windows

    def decisions_in(self, scope: Scope, *, since: datetime) -> Rows:
        """One instrument's decisions from `since`, for the basket that made them.

        Scoped by basket as well as instrument: two baskets may hold the same instrument, and a
        chart of one basket's reasoning must not carry the other's marks.
        """
        return self._rows(
            select(decisions)
            .join_from(decisions, cycles, decisions.c.cycle_id == cycles.c.cycle_id)
            .where(
                decisions.c.instrument_key == scope.instrument_key,
                cycles.c.basket_id == scope.basket_id,
                decisions.c.decided_at >= since,
            )
            .order_by(decisions.c.decided_at)
        )

    def orders_in(self, instrument_key: str, *, since: datetime) -> Rows:
        """One instrument's orders from `since` — the chart's decision and cancellation marks."""
        return self._rows(
            select(orders)
            .where(orders.c.instrument_key == instrument_key, orders.c.created_at >= since)
            .order_by(orders.c.created_at)
        )

    def fills_in(self, instrument_key: str, *, since: datetime) -> Rows:
        """One instrument's fills from `since` — marks at the price that actually happened."""
        return self._rows(
            select(fills)
            .where(fills.c.instrument_key == instrument_key, fills.c.filled_at >= since)
            .order_by(fills.c.filled_at)
        )

    def equity_curve(self, *, opening_equity: Decimal = ZERO) -> tuple[EquityPoint, ...]:
        """Realized PnL accumulated over closed round trips, oldest first.

        Starts from `opening_equity` so the curve reads in account terms rather than as a bare
        PnL series. Deliberately *not* mark-to-market: no historical marks are persisted, and
        interpolating them would put a number on the research artifact that nothing observed.
        """
        running = opening_equity
        points = []
        for row in self._rows(select(round_trips).order_by(round_trips.c.event_seq)):
            running += row.realized_pnl
            points.append(
                EquityPoint(
                    closed_at=row.closed_at,
                    instrument_key=row.instrument_key,
                    realized_pnl=row.realized_pnl,
                    cumulative=running,
                )
            )
        return tuple(points)

    # ------------------------------------------------------------------ risk

    def risk_events(self, *, limit: int = DEFAULT_LIMIT) -> Rows:
        return self._rows(select(risk_events).order_by(risk_events.c.event_seq.desc()).limit(limit))

    def reconciliations(self, *, limit: int = DEFAULT_LIMIT) -> Rows:
        return self._rows(
            select(reconciliations).order_by(reconciliations.c.event_seq.desc()).limit(limit)
        )

    # ---------------------------------------------------------- notifications

    def notification_counts(self) -> dict[str, int]:
        """Undismissed notifications per severity. One grouped count, on every page render.

        Absent severities are absent here; the template supplies the zeros, because a count the
        *query* invented would be indistinguishable from one it measured.
        """
        return self._counts(
            select(notifications.c.severity, func.count())
            .where(notifications.c.dismissed_at.is_(None))
            .group_by(notifications.c.severity)
        )

    def open_notifications(self) -> Rows:
        """Every undismissed notification, newest first — deliberately not a time window.

        An unacknowledged alert that scrolls out of existence by itself is the one behaviour this
        list must not have (spec §5.8). What bounds it is dismissal and supersession, both of
        which are acts with a record, rather than a `LIMIT` that hides a row nobody answered for.
        """
        return self._rows(
            select(notifications)
            .where(notifications.c.dismissed_at.is_(None))
            .order_by(notifications.c.at.desc(), notifications.c.alert_id.desc())
        )

    def notification_is_open(self, alert_id: str) -> bool:
        """Whether this notice exists and is still undismissed.

        Asked before recording a dismissal, so the log holds an act that actually changed
        something: a second browser tab, or a page left open while the notice was superseded,
        would otherwise append an `ALERT_DISMISSED` that projects onto nothing and reads in the
        audit trail as a dismissal that never happened.
        """
        return (
            self._one(
                select(notifications.c.alert_id).where(
                    notifications.c.alert_id == alert_id,
                    notifications.c.dismissed_at.is_(None),
                )
            )
            is not None
        )

    # ------------------------------------------------------------------ internals

    def _rows(self, query: Select[Any]) -> Rows:
        with self._engine.connect() as connection:
            return tuple(connection.execute(query).all())

    def _counts(self, query: Select[Any]) -> dict[str, int]:
        """A grouped count as `key → n`. Absent keys are absent, never a fabricated zero."""
        with self._engine.connect() as connection:
            return dict(connection.execute(query).all())  # type: ignore[arg-type]

    def _one(self, query: Select[Any]) -> Row[Any] | None:
        with self._engine.connect() as connection:
            return connection.execute(query).one_or_none()


def seat_instrument(event: Event) -> str | None:
    """The instrument one transcript row spoke for; `None` for an event that is not a seat's.

    The only place that knows where a seat response keeps its instrument key. `RISK_CHECKED`
    carries one of its own at the top of its payload and is deliberately not read here — `None`
    is what keeps it out of the narrowing (`CycleDetail.narrowed_to`).
    """
    if event.type is not EventType.SEAT_RESPONDED:
        return None
    key = event.payload.get("response", {}).get("instrument_key")
    return key if isinstance(key, str) else None


def parse_pins(config_versions_json: str | None) -> tuple[ConfigRef, ...]:
    """`{"basket:demo": 4}` → the refs a cycle ran on, so `configs.at(ref)` can resolve them.

    An unreadable pin yields no ref rather than raising: it makes one cycle's configuration
    unresolvable, and the rest of that cycle's audit trail is still worth showing (DESIGN §6.1).
    """
    try:
        pins = json.loads(config_versions_json or "{}")
    except ValueError:
        return ()
    return tuple(ref for key, version in sorted(pins.items()) if (ref := _ref(key, version)))


def _ref(key: str, version: object) -> ConfigRef | None:
    kind, _, config_id = key.partition(":")
    if not config_id or not isinstance(version, int) or kind not in set(ConfigKind):
        return None
    return ConfigRef(kind=ConfigKind(kind), config_id=config_id, version=version)
