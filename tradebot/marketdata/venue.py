"""The venue-agnostic market-data provider: one `MarketDataProvider` over any `VenueGateway`.

Everything venue-specific lives below this in the gateway. What lives here is the part that
must behave identically for every venue, and that the replay provider must match bar for bar:

* **the point-in-time cutoff** — a bar is visible only once it has closed at or before the
  cutoff, which is `end` when given and the injected clock's `now` otherwise. Identical rule to
  `ReplayMarketData`, which is what makes a live snapshot and a replayed snapshot of the same
  bars byte-identical (DESIGN §6.2 exit criterion, [L12]);
* **`observed_at` stamping** — the cutoff, not the moment the network call returned, so the
  same inputs produce the same series whether they arrived over the wire or from a file;
* **gaps stay holes** — a missing bar is reported, never interpolated;
* **venue matching** — an instrument is only ever fetched from its own venue.

Failure semantics: an unsupported timeframe or a foreign instrument raises `ConfigError` (a
configuration defect, refuse rather than guess). No bars at or before the cutoff raises
`DataStaleError`, which aborts the cycle as `DATA_STALE` with no trade. Transport failures
propagate from the gateway with their retry classification intact.
"""

from __future__ import annotations

from datetime import datetime

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.enums import AssetClass
from tradebot.core.errors import ConfigError, DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.market import Candle, CandleSeries, Quote
from tradebot.interfaces.exchange import VenueGateway
from tradebot.interfaces.market_data import DataCapabilities
from tradebot.marketdata.catalogue import VenueCatalogue, instrument_of

logger = get_logger(__name__)


class VenueMarketData:
    """Normalized candles and quotes from one venue gateway."""

    def __init__(
        self, gateway: VenueGateway, clock: Clock, *, asset_class: AssetClass = AssetClass.CRYPTO
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        #: The same gateway's published rule set. Symbol resolution is a catalogue question, and
        #: answering it here as well would be a second opinion about what a venue lists.
        self.catalogue = VenueCatalogue(gateway, clock, asset_class=asset_class)
        self.provider_id = gateway.venue_id

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        self._assert_own_instrument(instrument)
        self._assert_timeframe(timeframe)
        cutoff = ensure_utc(end) if end is not None else self._clock.now()
        bars = await self._gateway.fetch_bars(
            instrument.symbol, timeframe, self._clamp(limit), end=cutoff
        )
        visible = self._visible(bars, cutoff)
        if not visible:
            raise DataStaleError(
                f"no {timeframe} candles closed on or before {cutoff.isoformat()} "
                f"for {instrument.key}"
            )
        series = CandleSeries(
            instrument_key=instrument.key,
            timeframe=timeframe,
            candles=visible[-limit:],
            observed_at=cutoff,
        )
        self._report_gaps(series)
        return series

    async def get_quote(self, instrument: Instrument) -> Quote:
        self._assert_own_instrument(instrument)
        book = await self._gateway.fetch_top_of_book(instrument.symbol)
        return Quote(
            instrument_key=instrument.key,
            bid=book.bid,
            ask=book.ask,
            last=book.last,
            observed_at=book.observed_at,
        )

    def capabilities(self) -> DataCapabilities:
        return self._gateway.capabilities()

    async def instruments(self, *symbols: str) -> tuple[Instrument, ...]:
        """Resolve symbols against the venue's own precision and minimums.

        One catalogue fetch serves the whole list: the first call populates its cache and the rest
        read it, so recording a ten-symbol dataset still spends one `exchangeInfo` weight.
        """
        return tuple([await instrument_of(self.catalogue, symbol) for symbol in symbols])

    async def close(self) -> None:
        await self._gateway.close()

    @staticmethod
    def _visible(bars: tuple[Candle, ...], cutoff: datetime) -> tuple[Candle, ...]:
        """Closed-at-or-before-cutoff bars only. A forming bar's close is not a fact yet."""
        return tuple(bar for bar in bars if bar.close_time <= cutoff)

    def _clamp(self, limit: int) -> int:
        if limit < 1:
            raise ConfigError(f"candle limit must be positive, got {limit}")
        return min(limit, self.capabilities().max_history)

    def _assert_own_instrument(self, instrument: Instrument) -> None:
        if instrument.venue != self._gateway.venue_id:
            raise ConfigError(
                f"{instrument.key} belongs to venue {instrument.venue!r}, not "
                f"{self._gateway.venue_id!r}; fetching it here would price it off the wrong book"
            )

    def _assert_timeframe(self, timeframe: str) -> None:
        supported = self.capabilities().timeframes
        if timeframe not in supported:
            raise ConfigError(
                f"{self.provider_id} does not serve {timeframe!r}; available: {sorted(supported)}"
            )

    @staticmethod
    def _report_gaps(series: CandleSeries) -> None:
        """Gaps are legitimate (halts, outages, sessions) but must never pass unnoticed."""
        gaps = series.gaps
        if gaps:
            logger.warning(
                "candle series has gaps",
                extra={
                    "instrument": series.instrument_key,
                    "timeframe": series.timeframe,
                    "gaps": len(gaps),
                    "first_gap_from": gaps[0][0].isoformat(),
                },
            )
