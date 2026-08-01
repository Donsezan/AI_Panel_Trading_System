# 16. A validation report is folded from the event log, and never signs itself off

Date: 2026-07-31
Status: accepted

## Context

PLAN Phase 7 asks for promotion gates "enforced in code/report" and exits on "a promotion report
generated from the event log that a human signs off on". DESIGN §9 rung 5 states the gates: ≥200
cycles, zero unhandled risk events, every reconciliation clean with venue resets excluded, then
human review.

Three things had to be decided before any of that could be counted.

**What the report reads.** The obvious source is the projections — they exist so the dashboard
can ask cheap questions. But the two facts a promotion decision most turns on, a tripped kill
switch and a halted basket, have **no projector at all** (`projections.py` lists the audit-only
types deliberately). Reading projections would mean either adding projectors for the report's
benefit, or counting incidents from `risk_events`, which records the *rule* that fired rather
than the state change it produced.

**What counts as an "unhandled risk event".** Taken literally, a Tier-1 veto is a risk event, and
a soak that vetoed anything could never be promoted. That reading makes the gate unreachable and
punishes the system for working.

**Which cycles count.** DESIGN §9 is explicit that live data through `SimBroker` is the evidence
base and that Binance testnet and Alpaca paper run alongside as adapter integration checks — but
both write to the same `data/paper.db`, so nothing in the log distinguished them.

## Decision

**The report is a fold over the log, narrowed by event type.** `EventStore.read_types` selects
the eleven types a report needs and `Evidence.gather` folds them into counters. Frozen snapshots
and seat transcripts — the two largest payloads, and most of a soak's log by volume — are never
loaded. The log is the compliance artifact (PLAN §3.3); a report derived from a table that a
rebuild regenerates would be a report about a derivation.

**An incident is something that needed a human**, and there are exactly five kinds:

| Kind | Read from |
|---|---|
| `kill_switch_tripped` | `KILL_SWITCH_CHANGED` → tripped (a re-arm is not one) |
| `basket_halted` | `BASKET_STATUS_CHANGED` → halted (an un-halt is not one) |
| `cycle_failed` | `CYCLE_COMPLETED` with outcome `failed` |
| `recon_mismatch` | `RECONCILED` classified `mismatch` |
| `order_stranded` | an order whose last recorded state is `SUBMIT_UNKNOWN` or `FAILED` |

A veto, a `DATA_STALE` abort, a degraded panel and a daily-loss halt are **not** incidents. Each
is the deterministic shell doing the thing it was built to do, and a gate that counted them would
select for a soak in which the risk layer never engaged.

`order_stranded` is judged at the window's close rather than when the state was entered:
`SUBMIT_UNKNOWN` is a normal transient that recovery resolves (PLAN §2.3), and only one that is
*still* unresolved is a fact about the system.

**Cycles carry their venue.** `CYCLE_STARTED` gained a `venue` field, stamped by the runner from
the broker it is wired to. The gate counts only cycles on the evidence venues (default `sim`) and
the report shows the rest by name as adapter checks. A cycle recorded without a venue is
`unknown` and never counted: a cycle that cannot say where it would have traded cannot
substantiate a promotion decision. This is a payload addition only — no schema change, no
migration, and the existing projector ignores it.

**A missing fact fails its gate.** An empty reconciliation history is reported as "no
reconciliation recorded" and **fails** the clean-reconciliation gate, rather than passing
vacuously. Silence is never taken as consent.

**The report cannot sign itself off.** `passed` means every *automatic* gate passed, and the
rendered report ends in a checklist of the five things no gate can check — jurisdiction, key
permissions, the notional cap, alerting that reaches a person, and an actual read of the decision
drill-down — over a signature line. `tradebot report promotion` exits 5 on failure so a script can
gate on it, and 0 on success, which means only "worth a human's time".

## Consequences

- Adding a fact to a report means reading one more event type, not adding a projection and a
  migration. The read model stays the dashboard's.
- The report's cost is proportional to the *number* of events of the interesting types, not to
  the size of the log. A soak's snapshots dominate on disk and are never touched.
- `Evidence` is shared by the promotion report and the backtest report, so both count the same
  things the same way. A divergence between "what the backtest says happened" and "what the gates
  measured" is structurally impossible.
- Venue attribution is only as good as the wiring: a cycle is stamped with
  `stack.broker.venue_id`, so a future venue that mislabels itself would be miscounted. The
  default evidence set is a single name, which makes that visible rather than silent.
- Cycles recorded before this change count as `unknown` and are excluded. There are none in
  practice — no soak has run — and the fail-closed direction is the right one regardless.
