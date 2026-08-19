"""One contract suite, run against every `MarketDataProvider` (rung 2, PLAN §7).

Two things are proven here, and the second is Phase 3's exit criterion.

**Every provider honours identical semantics.** Point-in-time cutoff, `observed_at` stamping,
limit slicing, and failing closed on no data are properties of the *interface*, not of any
implementation. A provider that diverges fails CI, which is what makes a paper result predictive
of live behaviour rather than a result from a parallel implementation (DESIGN §5).

**Replay and live-fetch produce byte-identical snapshots for the same bars.** Given the same
candles, the recorded path and the venue path must build the same `ContextSnapshot` — same
summaries, same indicator readings, same digest. If they did not, every backtest and every paper
run would be measuring a different system from the one that trades.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tests.doubles import FakeGateway

from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.enums import AssetClass
from tradebot.core.errors import DataStaleError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.snapshot import ContextSnapshot
from tradebot.indicators.library import DEFAULT_INDICATORS, compute_readings
from tradebot.interfaces.exchange import TopOfBook
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.cache import CachingMarketData
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.marketdata.synthetic import SyntheticMarketData
from tradebot.marketdata.venue import VenueMarketData

pytestmark = pytest.mark.contract

#: Every timeframe ends at this instant, so a snapshot can be built without one of them being
#: legitimately stale. Series that merely share a *start* diverge by weeks at the daily scale.
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
TIMEFRAMES = ("1h", "4h", "1d")
BARS = 260
H1_START = NOW - timedelta(hours=BARS)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def venue_instrument() -> Instrument:
    return Instrument(
        symbol="BTC/USDT",
        venue="binance",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )


def recorded(timeframe: str) -> tuple[Candle, ...]:
    """One deterministic series per timeframe, shared by both providers.

    Identical inputs are the whole point: any difference in the output is then provably the
    code under test and not the data.
    """
    return synthetic_candles(
        start=NOW - timeframe_interval(timeframe) * BARS,
        timeframe=timeframe,
        count=BARS,
        open_price=Decimal("50000"),
        step=Decimal("25"),
    )


class MultiTimeframeGateway(FakeGateway):
    """`FakeGateway` holds one series; the contract needs one per timeframe."""

    def __init__(self) -> None:
        super().__init__(())
        self._by_timeframe = {timeframe: recorded(timeframe) for timeframe in TIMEFRAMES}

    async def fetch_bars(
        self, symbol: str, timeframe: str, limit: int, *, end: datetime | None = None
    ) -> tuple[Candle, ...]:
        bars = self._by_timeframe[timeframe]
        visible = [bar for bar in bars if end is None or bar.close_time <= end]
        return tuple(visible[-limit:])


@pytest.fixture
def replay(clock: ManualClock, venue_instrument: Instrument) -> ReplayMarketData:
    return ReplayMarketData(
        {(venue_instrument.key, timeframe): recorded(timeframe) for timeframe in TIMEFRAMES},
        clock,
    )


@pytest.fixture
def venue_gateway() -> MultiTimeframeGateway:
    return MultiTimeframeGateway()


@pytest.fixture
def venue(clock: ManualClock, venue_gateway: MultiTimeframeGateway) -> VenueMarketData:
    return VenueMarketData(venue_gateway, clock)


@pytest.fixture(params=["replay", "venue", "cached_venue", "synthetic"])
def provider(
    request: pytest.FixtureRequest,
    replay: ReplayMarketData,
    venue: VenueMarketData,
    clock: ManualClock,
) -> MarketDataProvider:
    """Every provider in the system, including the caching decorator wrapping one.

    The synthetic venue is here on the same terms as the rest. It fabricates rather than serves,
    which is exactly why the point-in-time rules have to hold for it too: it is what the whole
    simulation decides on, and `inception` is what gives it the "nothing before the series
    starts" refusal the others get from having run out of recorded bars.
    """
    return {
        "replay": replay,
        "venue": venue,
        "cached_venue": CachingMarketData(venue, clock),
        "synthetic": SyntheticMarketData(clock, inception=H1_START),
    }[request.param]


class TestProviderContract:
    async def test_only_closed_bars_are_visible(
        self, provider: MarketDataProvider, venue_instrument: Instrument
    ) -> None:
        cutoff = H1_START + timedelta(hours=10)
        series = await provider.get_candles(venue_instrument, "1h", 100, end=cutoff)
        assert all(candle.close_time <= cutoff for candle in series.candles)
        assert len(series) == 10

    async def test_a_forming_bar_is_never_returned(
        self, provider: MarketDataProvider, venue_instrument: Instrument
    ) -> None:
        """The look-ahead test (PLAN §7): mid-bar, the forming bar must not appear."""
        mid_bar = H1_START + timedelta(hours=10, minutes=30)
        series = await provider.get_candles(venue_instrument, "1h", 100, end=mid_bar)
        assert series.latest.close_time == H1_START + timedelta(hours=10)

    async def test_the_clock_is_the_cutoff_when_end_is_omitted(
        self, provider: MarketDataProvider, venue_instrument: Instrument, clock: ManualClock
    ) -> None:
        clock.set(H1_START + timedelta(hours=5))
        series = await provider.get_candles(venue_instrument, "1h", 100)
        assert len(series) == 5

    async def test_observed_at_is_the_cutoff(
        self, provider: MarketDataProvider, venue_instrument: Instrument
    ) -> None:
        cutoff = H1_START + timedelta(hours=30)
        series = await provider.get_candles(venue_instrument, "1h", 50, end=cutoff)
        assert series.observed_at == cutoff

    async def test_limit_takes_the_most_recent_bars(
        self, provider: MarketDataProvider, venue_instrument: Instrument
    ) -> None:
        cutoff = H1_START + timedelta(hours=50)
        series = await provider.get_candles(venue_instrument, "1h", 5, end=cutoff)
        assert len(series) == 5
        assert series.latest.close_time == cutoff

    async def test_candles_are_ordered_oldest_first(
        self, provider: MarketDataProvider, venue_instrument: Instrument
    ) -> None:
        series = await provider.get_candles(
            venue_instrument, "1h", 20, end=H1_START + timedelta(hours=50)
        )
        times = [candle.open_time for candle in series.candles]
        assert times == sorted(times)

    async def test_no_data_before_the_series_starts_fails_closed(
        self, provider: MarketDataProvider, venue_instrument: Instrument
    ) -> None:
        """Fail closed: deciding on no data is the fail-open behaviour the design forbids."""
        with pytest.raises(DataStaleError):
            await provider.get_candles(venue_instrument, "1h", 10, end=H1_START - timedelta(days=1))

    async def test_capabilities_declare_point_in_time_support(
        self, provider: MarketDataProvider
    ) -> None:
        assert provider.capabilities().supports_point_in_time
        assert "1h" in provider.capabilities().timeframes


class TestReplayAndVenueAgree:
    """Phase 3 exit criterion: the same bars must build the same snapshot on both paths."""

    async def test_the_series_are_identical_bar_for_bar(
        self, replay: ReplayMarketData, venue: VenueMarketData, venue_instrument: Instrument
    ) -> None:
        cutoff = NOW - timedelta(hours=1)
        for timeframe in TIMEFRAMES:
            from_replay = await replay.get_candles(venue_instrument, timeframe, 200, end=cutoff)
            from_venue = await venue.get_candles(venue_instrument, timeframe, 200, end=cutoff)
            assert from_replay == from_venue, timeframe

    async def test_the_indicator_readings_match(
        self, replay: ReplayMarketData, venue: VenueMarketData, venue_instrument: Instrument
    ) -> None:
        """The evidence the panel judges must not depend on where the bars came from."""
        replayed = await replay.get_candles(venue_instrument, "1h", 200)
        fetched = await venue.get_candles(venue_instrument, "1h", 200)
        assert compute_readings(replayed, DEFAULT_INDICATORS) == compute_readings(
            fetched, DEFAULT_INDICATORS
        )

    async def test_the_snapshots_are_byte_identical(
        self,
        replay: ReplayMarketData,
        venue: VenueMarketData,
        venue_gateway: MultiTimeframeGateway,
        venue_instrument: Instrument,
        clock: ManualClock,
        ledger: Ledger,
    ) -> None:
        """Same candles and same quote in, same digest out — everything between is shared code."""
        basket = _basket(venue_instrument)
        quote = await replay.get_quote(venue_instrument)
        venue_gateway.book = TopOfBook(
            bid=quote.bid, ask=quote.ask, last=quote.last, observed_at=quote.observed_at
        )

        from_replay = await _snapshot(replay, ledger, clock, basket)
        from_venue = await _snapshot(venue, ledger, clock, basket)

        assert from_replay.instruments == from_venue.instruments
        assert _canonical(from_replay) == _canonical(from_venue)


def _basket(instrument: Instrument) -> Basket:
    return Basket(
        basket_id="contract",
        name="contract basket",
        instruments=(instrument,),
        panel=PanelConfig(
            panel_id="p",
            seats=(SeatConfig(seat_id="s", role="Technical", provider_id="stub", model="stub"),),
        ),
    )


async def _snapshot(
    provider: MarketDataProvider, ledger: Ledger, clock: ManualClock, basket: Basket
) -> ContextSnapshot:
    return await ContextBuilder(provider, ledger, clock, timeframes=TIMEFRAMES).build(basket)


def _canonical(snapshot: ContextSnapshot) -> str:
    """The snapshot minus its random id, the only field that may legitimately differ."""
    return snapshot.model_copy(update={"snapshot_id": "fixed"}).digest
