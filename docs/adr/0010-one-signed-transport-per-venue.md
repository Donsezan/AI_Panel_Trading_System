# ADR 0010 — One signed transport per venue, sharing the read path's rate budget

**Status:** accepted (2026-07-30) · **Phase:** 5 · **Supersedes:** nothing

## Context

Phase 5 adds order submission to a system that already reads market data from Binance. Three
questions had to be answered before any adapter could be written, and each of them is expensive to
revisit later.

**Where do venue credentials live in the object graph?** The Phase 3 transport *asserts it holds no
credentials* — a data client that could sign an order is a data client that might. Adding signing to
it would delete that control.

**Whose rate budget do orders spend?** Binance bans an IP and a key, not a code path (PLAN §3.1). A
market-data burst and a submit burst therefore have to be metered together, or the budget is a
fiction.

**How does the system reach Alpaca**, whose API is nothing like a crypto exchange's and whose
equities concerns (calendar, corporate actions, order classes, extended hours) have no ccxt
equivalent?

## Decision

### Two transport classes, one machinery, one limiter

`venues/ccxt_transport.py` holds both: `CcxtTransport` (public reads, asserts *no* credentials) and
`CcxtSignedTransport` (asserts credentials are present and that the resolved **private** host
matches the mode). They share budget accounting, the venue's used-weight header reconciliation, the
circuit breaker and the error taxonomy through a common base.

When the composition root builds both, it passes the data transport's `VenueRateLimiter` into the
trading transport, so one venue means one budget and one breaker. `is_order=True` additionally
charges the order-count windows, which market-data reads can then never exhaust.

The credential posture stays in the *type*: the read path cannot acquire signing by accident,
because acquiring it means constructing a different class that asserts the opposite.

### A new `tradebot/venues/` package

`interfaces/exchange.py` already described the transport as "one raw connection to one venue, shared
by data and execution". That was true and the file layout contradicted it — the only transport lived
under `marketdata/`. Moving it to `venues/` makes the seam physical and avoids an
`execution → marketdata` import edge that would have been the alternative.

Rejected: leaving it in `marketdata/` and importing upward from `execution/`. It would have made
`marketdata` a misnomer and put a venue's *order* plumbing in the package named for its prices.

### Alpaca speaks plain `httpx`, per ADR 0009's reasoning

`venues/alpaca_transport.py` is a small signed JSON client. Rejected: `alpaca-py` (a dependency tree
in a money-moving process, plus retry machinery that is actively harmful on an order-placing call),
and ccxt's `alpaca` adapter (crypto-centric; the calendar, corporate-action announcements and equity
order classes are not in its unified API, so they would have needed raw implicit calls anyway).

Owning the wire format means we can test it: the whole adapter runs through `httpx.MockTransport`,
so the contract suite asserts the exact URL, verb, headers and body Alpaca would receive.

### An ambiguous *placement* is `SUBMIT_UNKNOWN`; nothing else is

Both signed transports escalate a transient failure on an order-placing endpoint into
`SubmitUnknownError` carrying the `client_order_id` we sent. Everything else keeps its
classification, because each is a definite answer that nothing was placed:

| Venue outcome | Classification | Why not ambiguous |
|---|---|---|
| timeout, reset, 5xx after the request left | `SubmitUnknownError` | it may have matched |
| 429 / rate limited | `RateLimitedError` | refused before processing |
| insufficient funds, filter violation, 422 | `OrderRejectedError` | the venue said no |
| unknown order on a query | `OrderNotFoundError` | distinct from a rejection |
| 418 / IP ban | `VenueBannedError` | fatal; recovery adopts the order |
| `recvWindow` / `InvalidNonce` | `ConfigError` | the clock drifted; every call now fails |

Getting this table backwards in either direction is the incident: treating a rejection as ambiguous
halts a basket for nothing, and treating an ambiguity as retryable is how one decision becomes two
positions (R1).

### Credentials are read from mode-specific variable names

`BINANCE_TESTNET_API_KEY` for paper, `BINANCE_API_KEY` only for live; likewise
`ALPACA_PAPER_KEY_ID` versus `ALPACA_KEY_ID`. A live key present in the environment of a machine
running paper is therefore not merely unused — it is *unreachable*. Reaching live now takes more
than getting a boolean wrong (PLAN §2.4, R2).

## Consequences

* Adding a venue is a transport plus a broker plus one fixture set in the contract suite. Nothing
  above the transport changes.
* Binance's order plumbing depends on ccxt's exception hierarchy. That is pinned in the lock file
  and asserted by `tests/unit/test_signed_transports.py`, which fails if a future ccxt reclassifies
  an error we depend on distinguishing.
* Alpaca's wire format is ours to maintain. The mitigation is the opt-in smoke suite
  (`pytest -m smoke`): recorded fixtures prove we parse what we recorded, and only a real call
  proves the venue still speaks that way.
* Two transports mean two HTTP sessions to close. `Application.shutdown` owns both.
