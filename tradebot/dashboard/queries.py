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
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, Row, Select, select

from tradebot.core.config import ConfigRef
from tradebot.core.enums import ConfigKind, OrderState
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO, divide
from tradebot.persistence.schema import (
    cycles,
    decisions,
    fills,
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
    def snapshot(self) -> dict[str, Any] | None:
        """The frozen input packet, as stored. `None` if the cycle ended before it was built."""
        frozen = self.events_of(EventType.SNAPSHOT_FROZEN)
        return frozen[-1].payload.get("snapshot") if frozen else None


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

    # ------------------------------------------------------------------ internals

    def _rows(self, query: Select[Any]) -> Rows:
        with self._engine.connect() as connection:
            return tuple(connection.execute(query).all())

    def _one(self, query: Select[Any]) -> Row[Any] | None:
        with self._engine.connect() as connection:
            return connection.execute(query).one_or_none()


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
