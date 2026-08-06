"""One contract suite, run against every `InstrumentCatalogue` (rung 2, PLAN §7).

The claim under test is Phase 11 decision 2: **the simulated venue publishes a catalogue exactly
as a real venue does.** Sim simulates a venue; it is not a mode with a second data path. Without
this suite that is an intention — someone adds a convenience to `SimCatalogue`, or lets it accept
what Binance would refuse, and the thing a soak validated stops being the thing that trades
(ADR 0020, ADR 0025).

So every implementation is driven through the same parametrized cases: an unknown identifier, a
delisted symbol, case handling, a blank identifier, an ISIN, and the exact error type each raises.
Each catalogue is built from a wire-level fake for its own source — Binance's `exchangeInfo` JSON
for the venue one, a recorded snapshot for the simulated one — so what is being compared is
behaviour, not a shared helper.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.doubles import FakeGateway, symbol_entry

from tradebot.core.clock import ManualClock
from tradebot.core.enums import AssetClass
from tradebot.core.errors import ConfigError, VenueError
from tradebot.interfaces.exchange import IdType, VenueMarket
from tradebot.marketdata.binance import parse_market
from tradebot.marketdata.catalogue import (
    DEFAULT_CATALOGUE_TTL,
    Catalogue,
    SimCatalogue,
    UnavailableCatalogue,
    VenueCatalogue,
    instrument_of,
    sim_catalogue,
)

pytestmark = pytest.mark.contract

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

#: The same three symbols every catalogue below publishes: one ordinary pair, one delisted pair,
#: and a second live pair so "unknown" is distinguishable from "empty".
ENTRIES = (
    symbol_entry(),
    symbol_entry("ETHUSDT", "ETH"),
    symbol_entry("LUNAUSDT", "LUNA", status="BREAK"),
)


def _venue_catalogue(clock: ManualClock) -> VenueCatalogue:
    """A real venue's: Binance's own `exchangeInfo` entries, through the gateway parsing them."""
    gateway = FakeGateway([], markets=[parse_market(entry) for entry in ENTRIES])
    return VenueCatalogue(gateway, clock)


def _sim_catalogue(clock: ManualClock) -> SimCatalogue:
    """The simulated venue's: the same rules, arriving from a recorded capture instead of a wire."""
    return SimCatalogue(
        [parse_market(entry) for entry in ENTRIES], source="test capture", as_of=NOW
    )


CATALOGUES: dict[str, Callable[[ManualClock], Catalogue]] = {
    "venue": _venue_catalogue,
    "sim": _sim_catalogue,
}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture(params=sorted(CATALOGUES), ids=sorted(CATALOGUES))
def catalogue(request: pytest.FixtureRequest, clock: ManualClock) -> Catalogue:
    return CATALOGUES[request.param](clock)


class TestOneContract:
    """What every catalogue must answer identically, whatever it reads."""

    async def test_it_lists_what_the_venue_publishes_including_the_delisted(
        self, catalogue: Catalogue
    ) -> None:
        """Delisted symbols are *listed*, not hidden. Omitting them would turn "we stopped
        trading this" into "we never heard of it", and the operator needs the difference."""
        markets = await catalogue.list_markets()
        assert {market.symbol for market in markets} == {"BTC/USDT", "ETH/USDT", "LUNA/USDT"}

    async def test_it_resolves_a_listed_symbol_to_the_venues_own_precision(
        self, catalogue: Catalogue
    ) -> None:
        market = await catalogue.resolve("BTC/USDT")
        assert market.lot_size == Decimal("0.00001")
        assert market.min_notional == Decimal("5.00000000")
        assert market.quote_currency == "USDT"

    async def test_an_unknown_identifier_is_a_config_error_naming_the_venue(
        self, catalogue: Catalogue
    ) -> None:
        with pytest.raises(ConfigError, match=f"{catalogue.venue_id} does not list 'DOGE/USDT'"):
            await catalogue.resolve("DOGE/USDT")

    async def test_a_delisted_symbol_is_refused_as_delisted_not_as_unknown(
        self, catalogue: Catalogue
    ) -> None:
        with pytest.raises(ConfigError, match="delisted"):
            await catalogue.resolve("LUNA/USDT")

    @pytest.mark.parametrize("typed", ["btc/usdt", "  BTC/USDT  ", "Btc/Usdt"])
    async def test_case_and_surrounding_space_do_not_change_the_answer(
        self, catalogue: Catalogue, typed: str
    ) -> None:
        assert (await catalogue.resolve(typed)).symbol == "BTC/USDT"

    async def test_the_wire_symbol_is_suggested_and_never_silently_resolved(
        self, catalogue: Catalogue
    ) -> None:
        """`BTCUSDT` is the venue's own wire form and an operator will type it. It is *not*
        resolved: the separator-free form is ambiguous in principle, and the thing being
        identified decides what gets bought. A hint costs nothing; a guess costs an order."""
        with pytest.raises(ConfigError, match="did you mean 'BTC/USDT'"):
            await catalogue.resolve("BTCUSDT")

    async def test_a_blank_identifier_is_refused_rather_than_matching_something(
        self, catalogue: Catalogue
    ) -> None:
        with pytest.raises(ConfigError, match="no instrument identifier"):
            await catalogue.resolve("   ")

    async def test_a_resolved_market_becomes_an_instrument_stamped_by_the_catalogue(
        self, catalogue: Catalogue
    ) -> None:
        """`venue` and `asset_class` come from whoever answered, never from whoever typed."""
        instrument = await instrument_of(catalogue, "ETH/USDT")
        assert instrument.key == f"{catalogue.venue_id}:ETH/USDT"
        assert instrument.asset_class is AssetClass.CRYPTO
        assert instrument.trading_rules.min_notional == Decimal("5.00000000")


class TestIsinIsDeclaredAndUnserved:
    """Decision 6: designed for, deliberately not implemented, and refused in the venue's terms."""

    async def test_a_mistyped_isin_is_caught_before_the_limitation_is_reported(
        self, catalogue: Catalogue
    ) -> None:
        """Otherwise "this venue has no ISIN mapping" would hide the typo, and the operator would
        go looking for the wrong problem."""
        with pytest.raises(ConfigError, match="fails its own check digit"):
            await catalogue.resolve("US0378331004", IdType.ISIN)

    @pytest.mark.parametrize("typed", ["US037833100", "0S0378331005", "US03783310AZ"])
    async def test_something_that_is_not_an_isin_is_refused_as_such(
        self, catalogue: Catalogue, typed: str
    ) -> None:
        with pytest.raises(ConfigError, match="is not an ISIN"):
            await catalogue.resolve(typed, IdType.ISIN)

    async def test_a_well_formed_isin_is_refused_with_the_venues_actual_limitation(
        self, catalogue: Catalogue
    ) -> None:
        """Apple's real ISIN. No venue here publishes a mapping, and guessing one would invent
        the identity of a tradable thing."""
        with pytest.raises(ConfigError, match="publishes no ISIN→symbol mapping"):
            await catalogue.resolve("US0378331005", IdType.ISIN)


class _FlakyGateway(FakeGateway):
    """A gateway whose next `fetch_markets` raises, as a venue outage would."""

    def __init__(self) -> None:
        super().__init__([], markets=[parse_market(symbol_entry())])
        self.fail_next = True

    async def fetch_markets(self) -> tuple[VenueMarket, ...]:
        if self.fail_next:
            self.fail_next = False
            raise VenueError("binance is unreachable")
        return await super().fetch_markets()


class TestVenueCatalogueSpendsTheBudgetOnce:
    """The one behaviour that cannot be shared, because only a fetched catalogue has a cost."""

    async def test_repeat_reads_are_served_from_memory(self, clock: ManualClock) -> None:
        gateway = FakeGateway([], markets=[parse_market(entry) for entry in ENTRIES])
        catalogue = VenueCatalogue(gateway, clock)

        await catalogue.resolve("BTC/USDT")
        await catalogue.resolve("ETH/USDT")
        await catalogue.list_markets()

        assert gateway.market_fetches == 1

    async def test_the_cache_expires_so_a_changed_filter_is_seen(self, clock: ManualClock) -> None:
        """The whole point of the drift check is that a venue can change a filter under a running
        system. A catalogue that never re-read would make that check permanently blind."""
        gateway = FakeGateway([], markets=[parse_market(entry) for entry in ENTRIES])
        catalogue = VenueCatalogue(gateway, clock)

        await catalogue.list_markets()
        clock.advance(DEFAULT_CATALOGUE_TTL.total_seconds() + 1)
        await catalogue.list_markets()

        assert gateway.market_fetches == 2

    async def test_a_failed_fetch_is_not_cached(self, clock: ManualClock) -> None:
        """A cached failure would make the next caller inherit an outage it could have retried —
        and, at publish time, would turn one bad second into a basket nobody can edit."""
        gateway = _FlakyGateway()
        catalogue = VenueCatalogue(gateway, clock)

        with pytest.raises(VenueError):
            await catalogue.resolve("BTC/USDT")

        assert (await catalogue.resolve("BTC/USDT")).symbol == "BTC/USDT"


class TestUnavailableCatalogueRefusesByName:
    """A venue nothing can ask. Held rather than made optional, so "every mode has a catalogue"
    stays a fact about the type instead of a promise in a comment."""

    async def test_it_names_the_limitation_rather_than_reporting_an_empty_venue(self) -> None:
        catalogue = UnavailableCatalogue(
            "alpaca", AssetClass.EQUITY, "there is no equity VenueGateway in v1"
        )
        with pytest.raises(ConfigError, match="no equity VenueGateway"):
            await catalogue.resolve("AAPL")


class TestTheCommittedSimRuleSet:
    """The file the simulated venue actually serves. It is a capture, and it is checked in."""

    async def test_it_loads_and_carries_its_provenance(self) -> None:
        catalogue = sim_catalogue()
        assert catalogue.venue_id == "sim"
        assert catalogue.as_of is not None
        assert "binance" in catalogue.source

    async def test_the_demo_instruments_are_listed(self) -> None:
        """`app.demo_basket` resolves these on a fresh database; an absent one is a broken start."""
        for symbol in ("BTC/USDT", "ETH/USDT"):
            assert (await sim_catalogue().resolve(symbol)).symbol == symbol

    async def test_every_rule_is_a_positive_decimal(self) -> None:
        """A zero `min_notional` would silently disable the Tier-2 minimum check, and a zero lot
        size would divide by zero in quantization. A capture is only trustworthy if it is whole."""
        for market in await sim_catalogue().list_markets():
            assert market.lot_size > 0, market.symbol
            assert market.tick_size > 0, market.symbol
            assert market.min_qty > 0, market.symbol
            assert market.min_notional > 0, market.symbol
