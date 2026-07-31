"""The venue transport seam: one raw connection to one venue, shared by data and execution.

This is the layer where venue peculiarities are allowed to exist — kline array layouts, filter
names, weight tables, sandbox hostnames. Above it, `MarketDataProvider` and (from Phase 5)
`BrokerAdapter` are venue-agnostic. Below it, nothing else in the system knows what a
`X-MBX-USED-WEIGHT-1M` header is.

The split matters for one specific reason: **a venue ban applies to the IP and key, not to a
code path**. Market data and order submission must therefore share one gateway, one rate
budget, and one circuit breaker (PLAN §3.1).

Numbers cross this boundary as `Decimal`, converted from the venue's *string* fields. Venue
JSON carries prices as strings precisely so they survive exactly; a client that parses them to
float — as every unified crypto library does — has already lost the guarantee the money layer
exists to provide (PLAN §2.1).

Failure semantics for every implementation: transient transport failures raise `VenueError`;
rate limiting raises `RateLimitedError` with the venue's `Retry-After`; a hard ban raises
`VenueBannedError`, which is fatal and trips the kill switch. An unknown symbol or timeframe
raises `ConfigError` — asking for an instrument the venue does not list is a configuration
defect, not a market condition.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from tradebot.core.market import Candle
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.interfaces.market_data import DataCapabilities


@runtime_checkable
class VenueTransport(Protocol):
    """Raw request/response to a venue, with the rate budget and error taxonomy applied.

    Split out from `VenueGateway` so that the venue's *parsing* — the part that turns wire
    strings into the decimals an order is sized from — is unit-testable with no HTTP client, no
    recorded cassettes, and no library-specific mocking. Endpoints are named symbolically
    (`"klines"`, `"time"`); the implementation maps them to whatever its client calls them.
    """

    venue_id: str

    async def get(self, endpoint: str, params: Mapping[str, Any], *, weight: int) -> Any:
        """Perform an unauthenticated read, charging `weight` against the venue's budget."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class TradingTransport(Protocol):
    """Signed request/response to a venue, for the calls that can move money.

    Separate from `VenueTransport` for one reason: that transport *asserts it holds no
    credentials*, because a data client that could sign an order is a data client that might. The
    split makes the capability visible in the type, so the read path cannot acquire it by
    accident (PLAN §2.4, §3.2).

    Both transports share **one** `VenueRateLimiter` and **one** circuit breaker, because a venue
    bans an IP and a key, not a code path — a burst of candle reads and a burst of submits spend
    the same budget (PLAN §3.1). `is_order` additionally charges the venue's order-count windows,
    which market data must never be able to exhaust.
    """

    venue_id: str

    async def call(
        self, endpoint: str, params: Mapping[str, Any], *, weight: int, is_order: bool = False
    ) -> Any:
        """Perform a signed call, charging `weight` (and the order windows) against the budget.

        Raises `SubmitUnknownError` when an order-placing call's outcome cannot be determined:
        a timeout, a reset, or a 5xx after the request left. The caller may then only query the
        venue by `client_order_id` — there is no resubmission path (PLAN §2.3).
        """
        ...

    async def close(self) -> None: ...


class TopOfBook(DomainModel):
    """Best bid/ask and last trade, without the system-wide instrument key.

    The gateway does not know how instrument keys are composed, so the provider stamps it. That
    keeps key formatting in exactly one place (`Instrument.key`).
    """

    bid: Money
    ask: Money
    last: Money
    observed_at: UtcDatetime


class VenueMarket(DomainModel):
    """Trading rules for one symbol, as the venue publishes them.

    Fetched, never hand-configured. A lot size typed into config drifts from the venue's on the
    day the venue changes it: orders start getting rejected, or — worse — a stale `min_notional`
    lets through an order the risk layer sized against the wrong floor.
    """

    symbol: str
    base_currency: str
    quote_currency: str
    lot_size: Money
    tick_size: Money
    min_qty: Money
    min_notional: Money
    #: Venues keep delisted symbols in their metadata. Trading one is a guaranteed reject.
    tradable: bool = True


@runtime_checkable
class VenueGateway(Protocol):
    """One venue's raw transport. Venue-specific by design; the only such layer."""

    venue_id: str

    async def fetch_bars(
        self, symbol: str, timeframe: str, limit: int, *, end: datetime | None = None
    ) -> tuple[Candle, ...]:
        """Closed bars, oldest first, with exact decimal prices.

        Only *closed* bars: a forming bar's close moves, and an indicator computed on it would
        differ between two reads of the same instant, which destroys replay (DESIGN [L12]).
        """
        ...

    async def fetch_top_of_book(self, symbol: str) -> TopOfBook: ...

    async def fetch_markets(self) -> tuple[VenueMarket, ...]:
        """Every tradable symbol's precision and minimums."""
        ...

    async def server_time(self) -> datetime:
        """The venue's clock, for the startup skew check (PLAN §3.1)."""
        ...

    def capabilities(self) -> DataCapabilities: ...

    async def close(self) -> None:
        """Release the transport. Safe to call more than once."""
        ...
