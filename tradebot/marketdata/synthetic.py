"""The simulated venue's own price feed: a deterministic pseudo-market, generated on demand.

It differs from `ReplayMarketData` on one axis, and that axis is the whole reason they are two
classes. Replay serves *recorded* bars and refuses everything it was not given, because a
backtest that fabricated a series for an instrument its dataset never covered would be quietly
meaningless. This provider is the opposite promise: it **is** the venue, so it answers for every
instrument and every timeframe the engine knows, exactly as Binance answers for anything in its
`exchangeInfo`. Fabrication is the product here and a defect there.

That distinction is what the fixed map it replaces got wrong. A dict built once from the baskets
configured at wiring cannot answer for a basket published *afterwards* — which the dashboard
invites an operator to do, and which the supervisor's resync sweep is built to pick up — nor for
a timeframe the basket editor offers but the wiring did not enumerate. Both surfaced as
`DataStaleError: no replay series for …`, on the chart pane and on every cycle of the new basket.

Two properties make it a venue rather than a fixture:

* **Bars sit on the venue's grid**, aligned to the epoch, so a 1h bar opens on the hour and a 1d
  bar at midnight UTC like Binance's. The newest closed bar is therefore never more than one
  interval old and the staleness policy is *exercised*. A series anchored at process start
  instead ages past `require_fresh` about a bar later, which is what stopped every cycle of a
  `serve --mode sim` run left up for more than an hour.
* **A series is anchored once and only extended.** Bar *n* is generated once and never redrawn,
  so the history a pane refreshes into is the history the panel deliberated on. Extension resumes
  the same walk rather than reseeding it.

Timeframes are independent walks of one instrument, as they were before: a 4h series is not the
aggregate of its four 1h bars and will drift away from it. Aggregating would mean holding the
finest series over the coarsest series' span — 200 daily bars is 288 000 minutes — and the sim
has never claimed to be a market model. `QUOTE_TIMEFRAME` is fixed rather than "the shortest
available" so that the quote a fill happens at stays the series the workspace charts by default.

Failure semantics: a timeframe the engine does not know, and a cutoff before this venue began
publishing, both raise `DataStaleError`. A simulated venue has limits and states them rather than
inventing history backwards to meet a question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import blake2s

from tradebot.core.clock import Clock, ensure_utc
from tradebot.core.instrument import Instrument
from tradebot.core.market import (
    TIMEFRAME_INTERVALS,
    Candle,
    CandleSeries,
    Quote,
    timeframe_interval,
)
from tradebot.core.money import divide, multiply
from tradebot.interfaces.market_data import DataCapabilities
from tradebot.marketdata.replay import synthetic_walk

#: Bars kept per series. Comfortably above both readers — the chart's 200 and the context
#: builder's warm-up, which is at least 100 — and bounded so a process left running for a month
#: does not accumulate a month of bars per instrument per timeframe.
MAX_BARS = 512

#: The grid every bar is aligned to, so boundaries match a real venue's: hours on the hour, days
#: at midnight UTC. The epoch divides all six intervals exactly.
GRID_ORIGIN = datetime(1970, 1, 1, tzinfo=UTC)

#: Which series a quote comes from. Fixed rather than derived, because "the shortest one loaded"
#: would make the price a sim order fills at depend on which timeframe was charted first, and
#: because a fill marker has to land on the candles the workspace draws by default.
QUOTE_TIMEFRAME = "1h"

#: Opening level by base asset. Not a valuation — the walk has to start somewhere and the only
#: thing that number has to be is stable and of a plausible order of magnitude next to the
#: `min_notional` the simulated venue publishes.
OPEN_PRICES: Mapping[str, Decimal] = {"BTC": Decimal(50_000)}
DEFAULT_OPEN = Decimal(3_000)

#: Per-bar drift scale, in quote currency. Unchanged from the map this replaces.
STEP = Decimal(25)


@dataclass(slots=True)
class _Walk:
    """One instrument's series for one timeframe, and where its walk has got to.

    `price` and `state` are carried rather than recomputed so extension continues the sequence.
    Recomputing from the anchor would redraw bars an operator has already seen.
    """

    price: Decimal
    state: int
    candles: list[Candle] = field(default_factory=list)


class SyntheticMarketData:
    """A deterministic pseudo-market: any instrument, any timeframe, always up to the cutoff."""

    provider_id = "synthetic"

    def __init__(
        self,
        clock: Clock,
        *,
        inception: datetime | None = None,
        spread_pct: Decimal = Decimal("0.02"),
    ) -> None:
        self._clock = clock
        #: When this venue started publishing. Nothing before it exists, which is what gives the
        #: provider the "no data before the series starts" refusal every other one has. The
        #: default is far enough back that only `MAX_BARS` ever binds.
        self._inception = (
            ensure_utc(inception)
            if inception is not None
            else clock.now() - max(TIMEFRAME_INTERVALS.values()) * MAX_BARS
        )
        self._spread_pct = spread_pct
        self._walks: dict[tuple[str, str], _Walk] = {}

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        cutoff = ensure_utc(end) if end is not None else self._clock.now()
        return CandleSeries.point_in_time(
            instrument.key, timeframe, self._bars(instrument, timeframe, cutoff), cutoff, limit
        )

    async def get_quote(self, instrument: Instrument) -> Quote:
        """Derive a quote from the most recent *closed* bar of `QUOTE_TIMEFRAME`.

        A synthetic spread keeps the sim honest about crossing costs; SimBroker adds slippage and
        fees on top.
        """
        series = await self.get_candles(instrument, QUOTE_TIMEFRAME, limit=1)
        close = series.latest.close
        half_spread = divide(multiply(close, self._spread_pct), Decimal(200))
        return Quote(
            instrument_key=instrument.key,
            bid=close - half_spread,
            ask=close + half_spread,
            last=close,
            observed_at=series.observed_at,
        )

    def capabilities(self) -> DataCapabilities:
        return DataCapabilities(
            timeframes=tuple(TIMEFRAME_INTERVALS),
            max_history=MAX_BARS,
            supports_point_in_time=True,
        )

    def _bars(self, instrument: Instrument, timeframe: str, cutoff: datetime) -> tuple[Candle, ...]:
        """This series, grown forward to `cutoff`. An unknown timeframe fails closed here."""
        interval = timeframe_interval(timeframe)
        key = (instrument.key, timeframe)
        walk = self._walks.get(key)
        if walk is None:
            walk = _Walk(price=_open_for(instrument), state=_seed_for(instrument.key))
            self._walks[key] = walk
        self._grow(walk, timeframe, interval, _floor(cutoff, interval))
        return tuple(walk.candles)

    def _grow(self, walk: _Walk, timeframe: str, interval: timedelta, last_close: datetime) -> None:
        """Append every bar that has closed since this walk was last asked, and trim the tail."""
        start = walk.candles[-1].close_time if walk.candles else self._first_open(interval)
        count = (last_close - start) // interval
        if count <= 0:
            return
        if count > MAX_BARS:
            # A process resumed after a long sleep, or a clock stepped forward in bulk. Generating
            # the whole interval would cost bars nobody will read, so the window is re-opened at
            # the cutoff instead — the walk's price and state carry over, so the series stays one
            # continuous walk, and the bars that are dropped are ones `MAX_BARS` would have
            # discarded anyway.
            walk.candles.clear()
            start, count = last_close - interval * MAX_BARS, MAX_BARS
        for candle, state in synthetic_walk(
            start=start,
            timeframe=timeframe,
            count=count,
            open_price=walk.price,
            step=STEP,
            seed=walk.state,
        ):
            walk.candles.append(candle)
            walk.state = state
        walk.price = walk.candles[-1].close
        del walk.candles[:-MAX_BARS]

    def _first_open(self, interval: timedelta) -> datetime:
        """Where a series that has never been asked for begins."""
        return _ceil(self._inception, interval)


def _open_for(instrument: Instrument) -> Decimal:
    return OPEN_PRICES.get(instrument.base_currency, DEFAULT_OPEN)


def _seed_for(instrument_key: str) -> int:
    """Stable across processes — `hash()` is randomized per run, which would make the
    simulation irreproducible."""
    return blake2s(instrument_key.encode(), digest_size=2).digest()[0] + 1


def _floor(moment: datetime, interval: timedelta) -> datetime:
    """The close of the most recent bar to have closed at or before `moment`."""
    return GRID_ORIGIN + interval * ((moment - GRID_ORIGIN) // interval)


def _ceil(moment: datetime, interval: timedelta) -> datetime:
    """The first grid boundary at or after `moment`."""
    return GRID_ORIGIN + interval * -((GRID_ORIGIN - moment) // interval)
