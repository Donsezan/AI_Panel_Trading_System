"""Instrument reference data: what a venue lists, and the precision it lists it at.

`lot_size`, `tick_size`, `min_qty` and `min_notional` are what `quantize_order` rounds against and
what the Tier-2 minimum check compares to, so they decide whether an order is legal and — through
`min_notional` — whether it exists at all. They are the venue's numbers, and this module is the
only way the system obtains them (ADR 0025).

Three sources, one contract:

```
Catalogue              the resolution semantics, written exactly once
  VenueCatalogue       the venue's own exchangeInfo, fetched and briefly cached
  SimCatalogue         a recorded rule set served offline — the simulated venue, and a backtest
  UnavailableCatalogue a venue this system cannot ask; refuses by naming the limitation
```

`tests/contract/test_catalogue_contract.py` runs one suite over all of them, because "the
simulated venue publishes a catalogue exactly as a real venue does" is a claim that has to be
enforced rather than intended.

Failure semantics: every refusal is a `ConfigError` — an unlisted symbol, a delisted one, an
unresolvable id type, or a venue with no catalogue at all. A fetch that fails raises the
transport's own classified error and is **not** cached, so the next caller retries rather than
inheriting a failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from tradebot.core.clock import Clock
from tradebot.core.enums import AssetClass
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.interfaces.exchange import IdType, InstrumentCatalogue, VenueGateway, VenueMarket

#: The venue id the simulated venue publishes under. Its symbols are `sim:BTC/USDT`, and they are
#: a different tradable thing from `binance:BTC/USDT` — a different ledger, a different portfolio.
SIM_VENUE_ID = "sim"

#: The simulated venue's published rule set, recorded from a real venue and committed. See
#: `tradebot catalogue fetch` for how it is refreshed; it carries the instant it was captured.
SIM_MARKETS = Path(__file__).with_name("sim_markets.json")

#: How long a fetched catalogue is reused. `exchangeInfo` is weight 20 and three callers ask the
#: same question — publish, startup preflight, and the periodic drift check — while a venue
#: changes a filter on the scale of weeks. Short enough that the drift check still catches a
#: change within minutes; long enough that it costs a rounding error of the rate budget (ADR 0008).
DEFAULT_CATALOGUE_TTL = timedelta(minutes=5)

#: An ISIN is a two-letter country code, nine alphanumerics, and a check digit.
ISIN_LENGTH = 12

#: What `tradebot catalogue fetch` records unless told otherwise, and therefore what the simulated
#: venue lists. Thirty liquid pairs rather than the venue's whole listing of several thousand: the
#: file is a committed copy of somebody else's data and it ages, and a capture an operator can
#: actually read is one whose `min_notional` values get looked at. Every entry is a real pair, so
#: sim refuses anything outside it exactly as a venue refuses a symbol it does not list.
SIM_SYMBOLS: tuple[str, ...] = tuple(
    f"{base}/USDT"
    for base in (
        "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
        "LTC", "TRX", "ATOM", "UNI", "NEAR", "APT", "ARB", "OP", "FIL", "ETC",
        "XLM", "HBAR", "VET", "ICP", "INJ", "SUI", "TIA", "SEI", "RENDER", "AAVE",
    )
)  # fmt: skip


class MarketSnapshot(DomainModel):
    """A venue's published trading rules, captured at an instant, as a file.

    Provenance is part of the artifact: the rules are somebody else's data and they age, so a
    snapshot that has lost where and when it came from cannot be judged. `source_venue` is the
    venue the rules were *recorded from*, which is not the venue that serves them — the simulated
    venue publishes a real capture under its own id, and saying otherwise in the file would be a
    lie about provenance to save one field.
    """

    source_venue: str
    source: str
    as_of: UtcDatetime
    markets: tuple[VenueMarket, ...]


def instrument_for(market: VenueMarket, venue_id: str, asset_class: AssetClass) -> Instrument:
    """Build an `Instrument` from venue-published trading rules.

    Precision comes from the venue rather than from a config file, because a stale `min_notional`
    or `lot_size` silently changes what the risk layer is allowed to size.
    """
    if not market.tradable:
        raise ConfigError(f"{venue_id}:{market.symbol} is not tradable on this venue")
    return Instrument(
        symbol=market.symbol,
        venue=venue_id,
        asset_class=asset_class,
        base_currency=market.base_currency,
        quote_currency=market.quote_currency,
        lot_size=market.lot_size,
        tick_size=market.tick_size,
        min_qty=market.min_qty,
        min_notional=market.min_notional,
    )


def assert_isin(value: str) -> None:
    """Refuse a mistyped ISIN locally, before any venue is asked about it.

    An ISIN carries its own check digit, so a transposed pair of characters is detectable without
    a network call — and has to be detected here, because the refusal that follows ("this venue
    publishes no ISIN mapping") would otherwise hide a typo behind a limitation and send the
    operator looking for the wrong problem.
    """
    isin = value.strip().upper()
    if (
        len(isin) != ISIN_LENGTH
        or not isin[:2].isalpha()
        or not isin[2:].isalnum()
        or not isin[-1].isdigit()
    ):
        raise ConfigError(
            f"{value!r} is not an ISIN: twelve characters — a two-letter country code, nine "
            "alphanumerics, then a check digit"
        )
    if _check_digit(isin[:-1]) != int(isin[-1]):
        raise ConfigError(f"ISIN {isin} fails its own check digit, so it has been mistyped")


def _check_digit(payload: str) -> int:
    """The ISIN check digit: letters expanded to their two-digit values, then Luhn."""
    digits = "".join(str(int(character, 36)) for character in payload)
    total = 0
    for index, character in enumerate(reversed(digits)):
        doubled = int(character) * (2 if index % 2 == 0 else 1)
        total += doubled - 9 if doubled > 9 else doubled
    return (10 - total % 10) % 10


class Catalogue:
    """The resolution semantics every catalogue shares, written once.

    A subclass supplies only *where the markets came from*. Everything a caller can observe —
    how an unknown identifier is refused, how a delisted symbol is refused, how case is treated,
    and which error type comes out — lives here, which is what makes the contract suite an
    assertion about all implementations rather than about each in turn.
    """

    venue_id: str
    asset_class: AssetClass

    async def list_markets(self) -> tuple[VenueMarket, ...]:
        raise NotImplementedError

    async def resolve(self, identifier: str, id_type: IdType = IdType.SYMBOL) -> VenueMarket:
        """One symbol's published trading rules, or a refusal in the venue's own terms."""
        if id_type is IdType.ISIN:
            assert_isin(identifier)
            raise ConfigError(
                f"{self.venue_id} publishes no ISIN→symbol mapping, so {identifier.strip().upper()}"
                " cannot be resolved here. Name the venue's own symbol instead"
            )
        wanted = identifier.strip().upper()
        if not wanted:
            raise ConfigError(f"no instrument identifier was given for {self.venue_id}")
        markets = await self.list_markets()
        found = next((market for market in markets if market.symbol.upper() == wanted), None)
        if found is None:
            raise ConfigError(
                f"{self.venue_id} does not list {identifier.strip()!r}"
                f"{_did_you_mean(markets, wanted)}"
            )
        if not found.tradable:
            raise ConfigError(
                f"{self.venue_id} lists {found.symbol} as delisted; it is not tradable, and an "
                "order on it is a guaranteed reject"
            )
        return found


async def instrument_of(
    catalogue: InstrumentCatalogue, identifier: str, id_type: IdType = IdType.SYMBOL
) -> Instrument:
    """A named instrument as the system's own `Instrument`, venue and asset class stamped.

    A free function rather than a method on the protocol, so the protocol stays the two questions
    a venue can answer — what it lists, and what one symbol's rules are — while the derived
    operation everything actually calls exists exactly once. `venue` and `asset_class` come from
    the catalogue that answered, never from whoever typed the identifier: those two fields are
    where a free-text typo used to reach the risk layer (ADR 0025).
    """
    return instrument_for(
        await catalogue.resolve(identifier, id_type), catalogue.venue_id, catalogue.asset_class
    )


def _did_you_mean(markets: Sequence[VenueMarket], wanted: str) -> str:
    """A suggestion when the operator typed the venue's wire symbol instead of ours.

    Only ever a suggestion. `BTCUSDT` is *not* silently resolved to `BTC/USDT`, because the
    separator-free form is ambiguous in principle — two listed pairs can share it — and the thing
    being identified decides what gets bought. A hint costs nothing; a guess costs an order.
    """
    candidates = sorted(
        {market.symbol for market in markets if market.symbol.upper().replace("/", "") == wanted}
    )
    if len(candidates) != 1:
        return ""
    return f"; did you mean {candidates[0]!r}?"


class VenueCatalogue(Catalogue):
    """The venue's own `exchangeInfo`, fetched through the shared gateway and briefly cached.

    Cached because three callers ask the same question — a publish, the startup preflight, and the
    periodic drift check — and `exchangeInfo` is one of the heaviest reads Binance meters. The
    cache is on the same clock the rest of the system is injected with, so a test can age it.
    """

    def __init__(
        self,
        gateway: VenueGateway,
        clock: Clock,
        *,
        asset_class: AssetClass = AssetClass.CRYPTO,
        ttl: timedelta = DEFAULT_CATALOGUE_TTL,
    ) -> None:
        self.venue_id = gateway.venue_id
        self.asset_class = asset_class
        self._gateway = gateway
        self._clock = clock
        self._ttl = ttl
        self._markets: tuple[VenueMarket, ...] = ()
        self._expires_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def list_markets(self) -> tuple[VenueMarket, ...]:
        if self._fresh():
            return self._markets
        async with self._lock:
            # Re-checked under the lock: a caller that queued behind a fetch which has since
            # completed must use its result rather than issue a second identical call.
            if self._fresh():
                return self._markets
            markets = await self._gateway.fetch_markets()
            self._markets = markets
            self._expires_at = self._clock.now() + self._ttl
            return markets

    def _fresh(self) -> bool:
        return self._expires_at is not None and self._clock.now() < self._expires_at


class SimCatalogue(Catalogue):
    """A recorded rule set, served offline: the simulated venue, and a replayed dataset.

    **The numbers are a real capture, never invented**, and that is not fastidiousness.
    `min_notional` decides whether an order exists at all, because a Tier-2 shrink below it becomes
    a veto (DESIGN §6.6). Set it too high and everything vetoes, which is obvious the first time
    anyone looks; set it too low and the veto path is simply never exercised, which is not.
    """

    def __init__(
        self,
        markets: Sequence[VenueMarket],
        *,
        venue_id: str = SIM_VENUE_ID,
        asset_class: AssetClass = AssetClass.CRYPTO,
        source: str = "",
        as_of: datetime | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.asset_class = asset_class
        #: Where these rules were recorded from, and when. Rendered beside the resolved fields so
        #: an operator can see that a `min_notional` is somebody's published number and how old it
        #: is, rather than a value of unknown origin.
        self.source = source
        self.as_of = as_of
        self._markets = tuple(markets)

    async def list_markets(self) -> tuple[VenueMarket, ...]:
        return self._markets


class UnavailableCatalogue(Catalogue):
    """A venue this system cannot ask what it lists.

    Alpaca in v1: Phase 3 built the Binance stack only, so there is no equity `VenueGateway` and
    nothing to fetch from. Refusing by name is the honest answer — the alternative is accepting
    hand-typed trading rules for an equity, which is the one defect this module exists to remove.
    Held rather than made optional, so "every mode has a catalogue" stays a fact about the type.
    """

    def __init__(self, venue_id: str, asset_class: AssetClass, reason: str) -> None:
        self.venue_id = venue_id
        self.asset_class = asset_class
        self._reason = reason

    async def list_markets(self) -> tuple[VenueMarket, ...]:
        raise ConfigError(f"{self.venue_id} publishes no instrument catalogue here: {self._reason}")


def load_snapshot(path: Path = SIM_MARKETS) -> MarketSnapshot:
    """Read a committed rule set. Raises `ConfigError` when it is not one."""
    try:
        return MarketSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"the simulated venue's rule set is missing from {path}: {exc}") from exc


def sim_catalogue(path: Path = SIM_MARKETS) -> SimCatalogue:
    """The simulated venue's catalogue, from the committed capture."""
    snapshot = load_snapshot(path)
    return SimCatalogue(
        snapshot.markets,
        source=f"{snapshot.source} (recorded from {snapshot.source_venue})",
        as_of=snapshot.as_of,
    )


def replay_catalogue(instruments: Sequence[Instrument], *, source: str) -> Catalogue:
    """The rules a recorded dataset was captured under, served as its venue's catalogue.

    A dataset already stores the venue's trading rules beside its prices, precisely so a replay
    quantizes the way the venue did when the prices were published (`marketdata/recorder.py`).
    Serving them here means a backtest resolves instruments through the same seam every other mode
    does, rather than being the one path that reads rules from a document nobody verified.
    """
    venues = {instrument.venue for instrument in instruments}
    if len(venues) != 1:
        raise ConfigError(
            f"a dataset's instruments must share one venue, found {sorted(venues)}; a catalogue "
            "answers for exactly one venue"
        )
    return SimCatalogue(
        tuple(
            VenueMarket(
                symbol=instrument.symbol,
                base_currency=instrument.base_currency,
                quote_currency=instrument.quote_currency,
                lot_size=instrument.lot_size,
                tick_size=instrument.tick_size,
                min_qty=instrument.min_qty,
                min_notional=instrument.min_notional,
            )
            for instrument in instruments
        ),
        venue_id=venues.pop(),
        asset_class=instruments[0].asset_class,
        source=source,
    )
