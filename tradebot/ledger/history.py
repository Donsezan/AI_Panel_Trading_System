"""What a basket has already done, read from the projections rather than remembered.

The Tier-1 metering rules — cooldown, daily trade cap, consecutive losses — and the Tier-2 order
rate limit all need history. Keeping that history in memory would mean a restart resets it, and
a limit that a crash can clear is not a limit: a crash-looping process would trade without
bound. So every count here is a query against the read model, which is itself a fold of the
append-only log.

Only **entry** orders count as trades. A protective leg is part of one decision, not a second
one, and counting legs would exhaust a six-trade daily cap in two decisions.

Failure semantics: this module only reads, and it reads the most restrictive interpretation
available. `cycles_since_trade=None` means "never traded", which no cooldown blocks; an
unavailable database raises rather than returning zeros that would silently unlock every limit.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, func, select

from tradebot.core.clock import Clock
from tradebot.core.enums import OrderRole
from tradebot.interfaces.risk import TradingHistory
from tradebot.persistence.schema import cycles, orders, round_trips

_ENTRY = OrderRole.ENTRY.value


class HistoryReader:
    """Derives `TradingHistory` for one basket and instrument."""

    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def held_cycles(self, basket_id: str, opened_at: datetime | None) -> int:
        """Completed cycles since a position was opened.

        Derived rather than counted: an in-memory counter resets on every restart, and the panel
        would then be told that a position held for days was opened this cycle.
        """
        if opened_at is None:
            return 0
        with self._engine.connect() as connection:
            return _cycles_since(connection, basket_id, opened_at) or 0

    def for_instrument(self, basket_id: str, instrument_key: str) -> TradingHistory:
        now = self._clock.now()
        with self._engine.connect() as connection:
            last_traded = connection.execute(
                select(func.max(orders.c.created_at)).where(
                    orders.c.basket_id == basket_id,
                    orders.c.instrument_key == instrument_key,
                    orders.c.role == _ENTRY,
                )
            ).scalar_one_or_none()
            return TradingHistory(
                cycles_since_trade=_cycles_since(connection, basket_id, last_traded),
                trades_today=_count_orders(connection, _start_of_day(now), basket_id=basket_id),
                consecutive_losses=_consecutive_losses(connection, basket_id),
                orders_last_hour=_count_orders(connection, now - timedelta(hours=1)),
            )


def _cycles_since(connection: Connection, basket_id: str, since: datetime | None) -> int | None:
    """Completed cycles for this basket since it last entered this instrument."""
    if since is None:
        return None
    return int(
        connection.execute(
            select(func.count())
            .select_from(cycles)
            .where(
                cycles.c.basket_id == basket_id,
                cycles.c.started_at > since,
                cycles.c.completed_at.is_not(None),
            )
        ).scalar_one()
    )


def _count_orders(connection: Connection, since: datetime, *, basket_id: str | None = None) -> int:
    """Entry orders placed since `since`, for one basket or across all of them."""
    query = (
        select(func.count())
        .select_from(orders)
        .where(orders.c.role == _ENTRY, orders.c.created_at >= since)
    )
    if basket_id is not None:
        query = query.where(orders.c.basket_id == basket_id)
    return int(connection.execute(query).scalar_one())


def _consecutive_losses(connection: Connection, basket_id: str) -> int:
    """Losing round trips at the head of the basket's history. Any win resets the count."""
    rows = connection.execute(
        select(round_trips.c.realized_pnl)
        .where(round_trips.c.basket_id == basket_id)
        .order_by(round_trips.c.event_seq.desc())
    ).all()
    streak = 0
    for row in rows:
        if row.realized_pnl >= 0:
            break
        streak += 1
    return streak


def _start_of_day(now: datetime) -> datetime:
    """UTC day boundary. Equity sessions arrive with the trading calendars in Phase 5."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
