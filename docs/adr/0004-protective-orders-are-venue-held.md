# ADR 0004 — Protective exits are venue-held, and only linked ones come in pairs

**Status:** accepted · 2026-07-26 · implements DESIGN §6.7, PLAN §5 Phase 2a

## Context

The system decides on a cadence of minutes to hours. Between cycles nothing is watching: the
process may be restarting, the venue may be unreachable, the machine may be asleep. A stop-loss
that only exists as an intention in our own code is therefore not a stop-loss at all — it is a
plan to react, and the moments when it matters most are exactly the moments we are least able to.

This is not a detail. Tier-1 sizing computes `qty = risk_amount / (stop_multiple × ATR)` and
calls `risk_amount` "the amount at risk". That sentence is only true if something is actually
holding a stop at `stop_multiple × ATR`. If nothing is, the position's real risk is unbounded
and every position-sizing number in the system is a fiction.

## Decision

- **Every entry fill is immediately followed by venue-held protective legs.** The venue holds
  them; we do not. `BrokerCapabilities.protective_orders` declares whether a venue can.
- **Legs are sized to what actually filled, never to what was ordered.** A leg for the full
  order quantity after a half fill tries to sell what is not held.
- **A further partial fill replaces the legs**, because no venue permits editing a resting
  order's quantity. The replacement gets its own deterministic id via a revision counter.
- **Without venue-side OCO, only the stop is placed.** `BrokerCapabilities.oco_groups` is a
  separate capability from `protective_orders` for this reason. Two unlinked exit orders on one
  holding can both fill, and the second sells a position that is already gone — a short, in a
  long-only system. A take-profit is an optimisation; a double sell is an incident.
- **Both legs' limits sit *through* their triggers.** A stop must cross to escape a falling
  market; a take-profit must cross to realise the gain. A target that triggers and then rests
  unfilled is not a conservative exit, it is a missing one, and it leaves the group unresolved.
- **A leg that cannot be expressed at venue precision is reported, not skipped.** Below
  `min_qty` or `min_notional`, `plan_legs` returns the reason, the monitor emits an
  `unprotected_position` risk event, and the flag reaches the panel's context.

## Where the pieces live

Risk decides *where* the exits sit (`risk.tier1.protective_plan`), because the stop distance is
the same number that sized the trade. Execution decides *how* they are placed
(`execution.protective`, `execution.monitor`). The plan travels on the `OrderIntent` and the
`Order`, so recovery can rebuild a leg from durable data without recomputing ATR against a
market that has since moved.

## Consequences

- On a venue that cannot hold stops, Tier-1 applies the `unprotected_haircut_pct` instead. The
  position is smaller *and* flagged; the risk is priced and visible rather than denied.
- Protective legs outlive the cycle that created them, which is the point. They are adopted from
  the database by the startup sequence (DESIGN §8.2 step 3), not re-derived.
- `SimBroker` implements true OCO so the group logic is exercised by the default simulation
  rather than only by a test that mocks it.
