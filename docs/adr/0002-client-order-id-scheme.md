# ADR 0002 — Deterministic `client_order_id` as the idempotency key

**Status:** accepted · 2026-07-26 · implements PLAN §2.2, DESIGN §6.7

## Context

The failure that dominates practitioner incident reports is the duplicate order after a retry:
a submit times out, the bot retries, and the venue now holds two positions. Any defence that
depends on remembering state written *after* the network call is defeated by a crash between
the two.

## Decision

```
{prefix}-{base32(blake2s(basket_id|cycle_id|instrument|seq, digest_size=10))}
```

- **Deterministic.** The same logical order always yields the same id, so a resumed submit
  reaches the venue's existing order instead of creating a second one.
- **Recomputable from durable data.** Recovery can query the venue by id even if the id string
  itself was never persisted. It is persisted anyway, with a uniqueness constraint.
- **Per-mode prefix** (`sim` / `pap` / `liv`) so a paper id can never be mistaken for a live
  one, and so the reconciler can adopt "our" orders by prefix while leaving a human's manual
  orders alone.
- **Venue-safe by construction.** 20 characters from `[a-z0-9-]` ∪ base32's `A-Z2-7`, inside
  Binance spot's `^[\.A-Z\:/a-z0-9_-]{1,36}$` and inside Alpaca's limit. `assert_venue_safe`
  runs at generation time, so a bad id fails in our process rather than in a venue rejection.

The human-readable tuple survives only as hash *input*: it cannot be used directly because it
exceeds 36 characters for realistic basket and symbol names.

## Consequences

- `seq` must distinguish every order the same cycle places for the same instrument (an entry
  and its protective legs). Reusing a `seq` for a *different* order is a defect that would
  collide two orders onto one id; the DB uniqueness constraint turns that into a loud failure.
- There is no code path that resubmits blindly. `SUBMIT_UNKNOWN` can only be resolved by
  querying the venue or, after a bounded window, by failing the order and halting the basket.
