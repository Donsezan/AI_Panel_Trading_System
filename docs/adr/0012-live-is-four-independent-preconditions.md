# ADR 0012 — Live mode is four independent preconditions, one of which outlives the process

**Status:** accepted (2026-07-30) · **Phase:** 5 · **Supersedes:** nothing

## Context

PLAN §2.4 treats mode confusion as a catastrophic failure class and lists what live must require:
the mode flag, a typed confirmation phrase, a `live_armed` config row, and a non-null
`max_live_notional` cap. Phase 5 is where those stop being a list in a document, because it is the
phase that produces adapters capable of reaching a real exchange.

The failure being defended against is not one missing check. It is an operator who satisfied three
conditions and assumed the fourth.

## Decision

### Four preconditions, in four different places

| # | Precondition | Where it lives | Why there |
|---|---|---|---|
| 1 | `--mode live` | the command line | required argument, no default |
| 2 | the typed phrase | the same invocation | transient by design |
| 3 | an `armed` row | the **live** database | must survive a reboot nobody authorised |
| 4 | a positive notional cap | that same row | "unlimited" is not a cap anyone chose |

The third is the one that had to be persisted. A flag in a file or an env var arms a machine after a
restart nobody authorised; a row is per-mode (paper and live never share a database), is set by
`tradebot risk arm-live --confirm "…"`, records who set it, and shows up next to the kill switch in
`risk status`.

`assert_live_preconditions` reports **every** unmet condition at once. An operator fixing them one
refusal at a time is an operator who stops reading the refusals.

### The cap is enforced as an ordinary Tier-2 rule

The arming row's `max_live_notional` becomes `GlobalRiskPolicy.max_order_notional`, enforced by
`OrderNotionalRule` — which shrinks rather than vetoes, exactly like every other Tier-2 limit, and
lets the exchange-minimum machinery turn an unusable shrink into a veto.

That rule is inert in sim and paper (no cap configured) but *present*, so the code path that
enforces the live cap is the one every test run already exercises. A limit only the live path
evaluates is a limit nobody has tested.

### Two further preconditions belong to the adapter

Only the transport knows what host it resolved, and only the venue can report what its own key may
do. So `venues/` asserts the endpoint matches the mode at construction, and `control/preflight.py`
asserts at startup that a live key cannot withdraw (Binance `apiRestrictions`, PLAN §3.2), that the
clock skew is inside tolerance, and that the venue can be queried by our own `client_order_id` —
without which `SUBMIT_UNKNOWN` has no safe resolution and the adapter must not trade live at all.

A venue that will not answer the withdrawal question (Alpaca has no such endpoint; the spot testnet
has no `sapi`) is recorded as a warning and documented as a human precondition. It is never treated
as a pass.

### Live still refuses, and that is the point

`build(Mode.LIVE)` evaluates every precondition and then raises anyway: live *wiring* is Phase 8's
deliverable, delivered with `docs/OPERATIONS.md` and armed by a human, never by me. The gate is
built and tested now so that an operator working towards live meets the whole list at once, from the
same refusal they will see when the wiring lands.

## Consequences

* `tests/unit/test_arming.py` is the phase's second exit criterion: the process refuses on each
  §2.4 case individually, and lists all four when none are met.
* Migration `0004` adds `live_arming`. It is not a projection — nothing replays into it, because it
  records a human decision, so a projection rebuild must leave it alone.
* Credentials are read from mode-specific variable names ([ADR 0010](0010-one-signed-transport-per-venue.md)),
  which makes a live key unreachable from a paper run — a fifth barrier, structural rather than
  procedural.
