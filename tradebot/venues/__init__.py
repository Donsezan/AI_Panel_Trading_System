"""Venue transports: one raw connection per venue, shared by market data and execution.

The split this package exists to make physical is stated in `interfaces/exchange.py`: a venue's
transport is *one* seam, not one per consumer, because **a venue bans an IP and a key, not a code
path**. Market data and order submission therefore share a `VenueRateLimiter` and a circuit
breaker (PLAN §3.1) — which they can only do if both are built from the same place.

```
venues/ccxt_transport.py    Binance: public reads (unauthenticated) + signed calls
venues/alpaca_transport.py  Alpaca: signed JSON over httpx, paper and live hosts
```

Above this package, `MarketDataProvider` and `BrokerAdapter` are venue-agnostic. Below it,
nothing knows what an `X-MBX-USED-WEIGHT-1M` header is.
"""
