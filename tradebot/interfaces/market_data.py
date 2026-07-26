"""Market data provision.

Failure semantics for every implementation: a fetch that fails transiently raises
`VenueError`; data older than its `max_age` is the *consumer's* concern, enforced by
`CandleSeries.require_fresh`. Providers never interpolate a missing bar — a fabricated candle
feeds a fabricated indicator into a real order.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from tradebot.core.instrument import Instrument
from tradebot.core.market import CandleSeries, Quote
from tradebot.core.schema import DomainModel


class DataCapabilities(DomainModel):
    """What a provider can actually serve, so callers never assume."""

    timeframes: tuple[str, ...]
    max_history: int
    #: Typical publication delay. Feeds the staleness budget: a 15-minute-delayed feed cannot
    #: back a 5-minute cycle, and the ContextBuilder must be able to tell.
    delay: timedelta = timedelta(0)
    supports_point_in_time: bool = False


@runtime_checkable
class MarketDataProvider(Protocol):
    """Normalized candles and quotes for one asset class or venue."""

    provider_id: str

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        """Candles up to `end` (exclusive), oldest first.

        `end` is what makes replay honest: a point-in-time slice must never include a bar that
        closed after the moment being replayed (DESIGN [L12]).
        """
        ...

    async def get_quote(self, instrument: Instrument) -> Quote:
        """Current top of book."""
        ...

    def capabilities(self) -> DataCapabilities: ...
