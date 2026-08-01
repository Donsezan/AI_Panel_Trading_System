"""How far alerting has read the log, and what it has to remember between restarts.

Written **after** delivery, never before. That ordering is the whole design: a crash between
sending and saving repeats an alert, and a crash between saving and sending would lose one. Of
the two, only the second can leave a tripped kill switch unannounced (ADR 0019).

Failure semantics: an absent row reads as "nothing delivered yet", and the dispatcher starts a
fresh database at the log's *end* rather than replaying it — see `AlertDispatcher.start`.
"""

from __future__ import annotations

from sqlalchemy import Engine, select

from tradebot.core.clock import Clock
from tradebot.core.schema import DomainModel
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import alert_cursor, upsert

_SINGLETON = "alerts"


class AlertCursor(DomainModel):
    """The delivery position, plus the state the streak and summary rules carry."""

    last_seq: int = 0
    last_summary_day: str = ""
    degraded_streak: int = 0
    stale_streak: int = 0

    @property
    def started(self) -> bool:
        """Whether alerting has ever run against this database."""
        return bool(self.last_seq or self.last_summary_day)


class AlertCursorStore:
    """Reads and writes the cursor through the single writer that owns the database."""

    def __init__(self, engine: Engine, writer: SingleWriter, clock: Clock) -> None:
        self._engine = engine
        self._writer = writer
        self._clock = clock

    def load(self) -> AlertCursor:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(alert_cursor).where(alert_cursor.c.scope == _SINGLETON)
            ).one_or_none()
        if row is None:
            return AlertCursor()
        return AlertCursor(
            last_seq=row.last_seq,
            last_summary_day=row.last_summary_day or "",
            degraded_streak=row.degraded_streak,
            stale_streak=row.stale_streak,
        )

    async def save(self, cursor: AlertCursor) -> AlertCursor:
        values: dict[str, object] = {
            "scope": _SINGLETON,
            "last_seq": cursor.last_seq,
            "last_summary_day": cursor.last_summary_day,
            "degraded_streak": cursor.degraded_streak,
            "stale_streak": cursor.stale_streak,
            "updated_at": self._clock.now(),
        }
        await self._writer.run(
            lambda connection: upsert(connection, alert_cursor, values, ["scope"])
        )
        return cursor
