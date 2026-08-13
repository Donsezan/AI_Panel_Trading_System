# ADR 0026 — An instrument belongs to exactly one basket

*Status: accepted. Extends [ADR 0025](0025-instrument-trading-rules-are-venue-reference-data.md)
and DESIGN §3 principle 7.*

## Context

DESIGN §3 principle 7 has said since Phase 0 that *"a basket's runner is the only thing that
trades that basket's assets … no concurrent mutation of the same position from two code
paths."* Nothing enforced it. `Basket._check_instruments`
([core/config.py:566-567](../../tradebot/core/config.py)) refuses a duplicate *within* one
basket's own instrument list — it has no visibility into any other basket, so two baskets could
each validate cleanly while both naming `binance:BTC/USDT`.

That gap is not theoretical. Baskets cycle as concurrent `asyncio` tasks, one per basket, started
by the supervisor's `sync()` (`self._tasks[basket_id] = asyncio.create_task(...)`,
[supervisor.py:404](../../tradebot/control/supervisor.py)) and never coordinated with each other.
What they would collide over is real: `Ledger.position` is keyed by `instrument_key` alone
([portfolio.py:98](../../tradebot/ledger/portfolio.py)) — there is no `basket_id` column in the
position map, because positions are the venue portfolio's, not a basket's (DESIGN §4). Two baskets
holding the same instrument key are, structurally, two writers reaching for the one row principle
7 says must have one.

## Decision

**At most one basket in service may hold a given `Instrument.key`.** Enforced in
[`control/reference.store_basket`](../../tradebot/control/reference.py), the one path that writes
a basket, over the same set of instruments an edit *changed*; re-checked by `DriftWatch` at
startup and on the supervisor's resync sweep, over every basket in service, whether or not the
venue can be reached.

### 1. The same exemption ADR 0025 already made, reused rather than reinvented

`store_basket` runs two checks over one `changed()` set: `verify_publish` (ADR 0025 — does the
venue still agree with the changed rules?) and `exclusive_findings` (does any other basket already
hold what changed?). Sharing the set is deliberate, not incidental: an instrument identical to the
one in the current version was already checked for exclusivity the publish that introduced it, so
re-checking it on every unrelated edit would spend nothing and catch nothing.

That is also why it is not a loophole. An operator's way out of an overlap is to edit one of the
two baskets down to not holding the instrument, or to pause or quarantine the offending basket
while they sort it out — and a pause or a quarantine toggle changes no instrument, so `changed()`
is empty and the publish that fixes the overlap, or buys time around it, is never blocked by the
overlap it is trying to address. A check that refused the fix would turn a configuration mistake
into one nothing could clear without a database edit — the same "safety mechanism becomes a safety
hazard" failure ADR 0025 already named for the venue check, on the same exemption.

### 2. The runtime check does not depend on the venue answering

`DriftWatch.check()` computes `overlaps()` and `_drift_for()` as two independent passes before
either is reported. Only `_drift_for` calls the catalogue, and only it can fail; `overlaps` reads
nothing but the basket records already loaded for the sweep. A basket sharing an instrument with
another must halt whether or not the venue is reachable that second — a transport failure has no
bearing on whether two documents in the same database name the same key, and letting one silence
the other would mean a venue outage doubles as a window in which two baskets can overlap
undetected for as long as it lasts.

### 3. Every mode halts — this is not `HALTS_ON_DRIFT`

ADR 0025's drift table lets sim keep cycling on a rules disagreement, because a `SimCatalogue`
mismatch is a human forgetting to edit a committed file, not something a running system could
have caused — there is nothing there for the check to be *for*. An instrument overlap has no such
excuse in sim: two baskets can name the same key in `Mode.SIM` exactly as easily as in live, the
fault is purely two documents in `ConfigStore`, and its damage lands in the read model regardless
of venue — the same `round_trips.basket_id` and `TradingHistory` corruption described below is
what `report promotion` folds into its evidence. So `EXCLUSIVITY_RULE` halts unconditionally
(`halts=True` regardless of `self._mode`), while `DRIFT_RULE` still keys off `HALTS_ON_DRIFT`. A
basket that has both faults at once gets one halt naming both — `DriftWatch._reason` appends each
clause only when it applies, so the operator fixes both in one pass rather than clearing one halt
into another.

### 4. What this does not change: Tier-2's per-instrument cap is still a real backstop

`GlobalRiskPolicy.max_instrument_exposure_pct` already reads *"across all baskets"*
([core/config.py:177-178](../../tradebot/core/config.py)) because Tier-1's per-basket sizing
cannot see a sibling basket. This ADR does not make that comment obsolete: it stops two baskets
from *owning* the same instrument key, not from two baskets each holding correlated exposure to
different instruments of the same name, asset, or venue concentration. `InstrumentExposureRule`
([risk/tier2.py:95-110](../../tradebot/risk/tier2.py)) is still the only thing bounding one
instrument's share of total equity when several baskets are each sized correctly on their own and
still add up to too much of one book. Exclusivity removes one failure mode; it is not a
replacement for the portfolio-level one.

## Consequences

The four failures this closes, each already possible before this ADR and each requiring nothing
more than two baskets independently choosing the same `Instrument.key`:

1. `LongOnlyRule.evaluate` caps a SELL at `proposal.position.qty` — the *portfolio* holding, not a
   per-basket share (`risk/rules.py:83-99`). Two baskets each holding the same instrument would
   each pass reduce-only at the *full* quantity, and both selling would reach the venue for 2× the
   position. Binance rejects the second order outright; Alpaca has no such floor and opens a
   margin short with unlimited-loss semantics nothing here models — R13.
2. Each basket's entry places its own venue-held protective leg (ADR 0004). Two baskets on one
   instrument means two independently-placed stops over one position; when either basket exits,
   the other's stop is left resting over a holding that is now gone, ready to fire against
   whatever the position becomes next.
3. The open round trip is keyed by instrument alone
   (`self._trips.setdefault(fill.instrument_key, _OpenTrip())`,
   [portfolio.py:163](../../tradebot/ledger/portfolio.py)), and the projector stamps
   `round_trips.basket_id` from the *closing* event
   (`"basket_id": event.basket_id`, [projections.py:210](../../tradebot/persistence/projections.py)).
   Whichever basket happens to place the fill that flattens the shared position claims the entire
   realized PnL for a trip that was actually run by both — and `report promotion` reads that table
   as the evidence for whichever basket it credits.
4. `HistoryReader.for_instrument` filters `cycles_since_trade`, `trades_today` and
   `consecutive_losses` by `basket_id` (`history.py:49-64`, corrected from the brief's cited
   49-62 — the function's closing `)` is line 64). Two baskets sharing an instrument each start
   that instrument's cooldown, daily cap and loss streak from zero in their own history, so the
   same instrument gets two independent cooldowns, two independent daily allowances and two
   independent loss-streak counters where the rules intend exactly one.

Two more consequences are how the guarantee above is actually made airtight, not what it prevents:

- **`store_basket`'s read-check-write is one unit, not three.** `configs.latest()` and
  `configs.baskets()` are plain reads outside `SingleWriter`'s lock, which only serializes `put`
  itself. Without more, two concurrent publishes of two *different* baskets each taking the same
  currently-free instrument could each read before either commits, each pass `exclusive_findings`
  against a snapshot that does not yet include the other's write, and both land — reproducing the
  exact overlap this ADR exists to prevent, just moved from "an operator misconfigured two
  baskets" to "two publishes raced." `ConfigStore.publishing()`, a per-instance `asyncio.Lock`,
  closes that window: `store_basket` holds it across the whole read-check-write, so the second
  publish to reach the lock sees the first one's write and its own `exclusive_findings` correctly
  refuses. One process, one event loop (DESIGN §5), so an `asyncio.Lock` is sufficient; there is no
  second process that could also be writing.
- **A database written before this ADR can already hold an overlap.** The startup preflight and
  the supervisor's resync sweep are what find it, halting every basket the overlap touches — both
  sides, because there is no way to tell which basket is the mistake, and leaving one cycling
  would mean it keeps trading an instrument whose history is already contaminated by the other.
  Cleared exactly as a fresh attempt to create the overlap would be refused: remove the instrument
  from all but one basket and re-publish, which re-resolves and re-verifies both checks.
