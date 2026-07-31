# ADR 0008 — Venue calls are metered by a sliding window, not a token bucket

**Status:** accepted · 2026-07-28 · implements PLAN §3.1, DESIGN §6.2

## Context

Binance bans on request **weight** per minute, not request count, and a `418` is an IP-level auto
ban that lengthens every time you call during it. A banned key is an account we cannot flatten,
so rate limiting is a money-safety control (R4), not a politeness feature.

The obvious implementation is a refilling token bucket. It is wrong here, and the way it is wrong
is easy to miss: a full bucket permits an instant burst of the entire capacity, and the drip then
refills a further capacity's worth over the following window. Inside one 60-second interval that
is **up to twice the budget** — precisely the overspend the limiter exists to prevent. A bucket
sized to the venue's limit does not stay under the venue's limit.

## Decision

**Every venue call passes one `SlidingWindow` per budget, which bounds spend in *any* interval.**

- Charges are recorded with their timestamps and expire individually, so no 60-second window can
  exceed the budget. This is strictly stronger than Binance's own fixed window, which permits a
  double spend across its boundary.
- Weight budgets are set **below** the venue's published allowance. A research bot at
  minutes-scale cadence has no reason to approach it, and the headroom absorbs a miscounted
  endpoint weight without a ban.
- Endpoint weights are stated at or above the published figures. Overpaying weight is free;
  underpaying is how an IP gets banned.
- **The venue's own `X-MBX-USED-WEIGHT-1M` header wins whenever it is higher than our count.**
  Our weight table can be wrong — endpoint weights change and a client library's table lags. The
  header cannot be.
- Order-count windows are consulted only for order-placing calls, so a burst of market-data reads
  can never exhaust the allowance an order needs.
- A background poller's cadence is *derived* from the budget (`RateBudget.poll_interval`) rather
  than hardcoded, so tightening the budget slows the pollers instead of silently overspending it.

## Failing closed rather than waiting forever

A call that cannot fit within `max_wait_seconds` raises `RateLimitedError` instead of blocking.
An order delayed past its usefulness is a different decision from the one risk approved, so
waiting indefinitely is not the safe option it looks like.

## Escalation ladder

| Signal | Response |
|---|---|
| Budget exhausted | wait, then fail closed past the ceiling |
| `429` / `Retry-After` | serve the penalty before any further call |
| N consecutive failures | circuit opens; calls are refused without being attempted |
| `418` (IP ban) | `VenueBannedError` — fatal, latched, requires a human |

The circuit breaker exists because continuing to call a venue that has failed five times running
is how a soft failure becomes a ban. Half-open lets exactly one probe through.

## Consequences

- One limiter instance per venue, shared by market data and (from Phase 5) the broker: a ban
  applies to the IP and key, not to a code path.
- ccxt's own `enableRateLimit` stays on as a *floor*. It counts requests; we count weight.
- The guarantee is tested as a property — no 60-second window over a 50-call burst may exceed the
  budget — rather than as a mechanism, so a future reimplementation still has to honour it. A
  token bucket fails that test, which is how the defect above was caught.
