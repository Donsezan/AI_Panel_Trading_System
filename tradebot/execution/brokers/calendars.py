"""Trading calendars for venues that have no calendar to fetch.

Crypto trades continuously, so "is it open" is always yes and a trading day is a UTC date. That
sounds too trivial to need a class, and the triviality is the point: the scheduler and the
daily-loss baseline both ask a `TradingCalendar`, so the crypto answer has to be *an
implementation* rather than a special case branched around at every call site (DESIGN §6.6, §6.1).

Failure semantics: no I/O, so nothing to fail.
"""

from __future__ import annotations

from datetime import UTC, datetime


class ContinuousCalendar:
    """`TradingCalendar` for a venue that never closes."""

    def __init__(self, venue_id: str) -> None:
        self.venue_id = venue_id

    async def is_open(self, at: datetime) -> bool:  # noqa: ARG002 — always open, by definition
        return True

    async def session_day(self, at: datetime) -> str:
        """UTC date. The crypto day boundary DESIGN §6.6 specifies for the daily-loss limit."""
        return at.astimezone(UTC).date().isoformat()

    async def next_open(self, after: datetime) -> datetime | None:  # noqa: ARG002 — never shut
        return None
