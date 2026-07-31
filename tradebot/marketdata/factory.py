"""Assembling a market-data stack from its four layers.

The layering is the point, so it is written down once here rather than repeated at every call
site:

```
CachingMarketData      — one venue call per bar interval, single-flight
  VenueMarketData      — venue-agnostic: point-in-time cutoff, observed_at, gap reporting
    BinanceSpotGateway — Binance wire format → exact decimals
      CcxtTransport    — HTTP, rate budget, circuit breaker, error taxonomy
```

Each layer is independently testable, and only the bottom two know which venue this is. Adding a
venue means a gateway and a transport; the two layers above are untouched.

Failure semantics: construction performs the endpoint and credential assertions, so a
misconfigured venue fails here rather than on the first fetch.
"""

from __future__ import annotations

from datetime import timedelta

from tradebot.core.clock import Clock
from tradebot.core.enums import AssetClass
from tradebot.core.ratelimit import RateBudget
from tradebot.interfaces.exchange import VenueTransport
from tradebot.marketdata.binance import BinanceSpotGateway
from tradebot.marketdata.cache import DEFAULT_QUOTE_TTL, CachingMarketData
from tradebot.marketdata.venue import VenueMarketData
from tradebot.venues.ccxt_transport import binance_spot_transport


def binance_spot_market_data(
    transport: VenueTransport,
    clock: Clock,
    *,
    quote_ttl: timedelta = DEFAULT_QUOTE_TTL,
) -> CachingMarketData:
    """Wrap an existing transport. The seam the tests use, with no HTTP client involved."""
    gateway = BinanceSpotGateway(transport, clock)
    provider = VenueMarketData(gateway, clock, asset_class=AssetClass.CRYPTO)
    return CachingMarketData(provider, clock, quote_ttl=quote_ttl)


def live_binance_spot(
    clock: Clock,
    *,
    sandbox: bool = False,
    budget: RateBudget | None = None,
    quote_ttl: timedelta = DEFAULT_QUOTE_TTL,
) -> tuple[CachingMarketData, VenueTransport]:
    """The real thing: public Binance spot data over ccxt.

    Returns the transport as well, because it owns an HTTP session that must be closed. Read-only
    and unauthenticated — this stack holds no credentials and cannot place an order.
    """
    transport = binance_spot_transport(clock, sandbox=sandbox, budget=budget)
    return binance_spot_market_data(transport, clock, quote_ttl=quote_ttl), transport
