"""How far alerting has read the log, and what it has to remember between restarts.

Two positions, because recording and delivering fail differently (spec 5.2). `last_seq` is
written **after** delivery, never before: a crash between sending and saving repeats an alert,
and a crash between saving and sending would lose one — of the two, only the second can leave a
tripped kill switch unannounced (ADR 0019). `recorded_seq` is written after the notification is
appended, so a dead webhook stalls delivery without withholding what the dashboard could show.

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
    #: Recording's own position. Ahead of `last_seq` whenever a destination is down or absent.
    recorded_seq: int = 0
    last_summary_day: str = ""
    degraded_streak: int = 0
    stale_streak: int = 0

    @property
    def started(self) -> bool:
        """Whether alerting has ever run against this database.

        `recorded_seq` counts, and on a machine with no destination it is the *only* one that
        moves — reading only the delivery cursor would re-anchor on every poll and record the
        same notification forever.
        """
        return bool(self.last_seq or self.recorded_seq or self.last_summary_day)


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
            recorded_seq=row.recorded_seq,
            last_summary_day=row.last_summary_day or "",
            degraded_streak=row.degraded_streak,
            stale_streak=row.stale_streak,
        )

    async def save(self, cursor: AlertCursor) -> AlertCursor:
        values: dict[str, object] = {
            "scope": _SINGLETON,
            "last_seq": cursor.last_seq,
            "recorded_seq": cursor.recorded_seq,
            "last_summary_day": cursor.last_summary_day,
            "degraded_streak": cursor.degraded_streak,
            "stale_streak": cursor.stale_streak,
            "updated_at": self._clock.now(),
        }
        await self._writer.run(
            lambda connection: upsert(connection, alert_cursor, values, ["scope"])
        )
        return cursor
