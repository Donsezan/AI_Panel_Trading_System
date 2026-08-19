---
name: trade-forensics
description: Use when asked why the trading bot did or did not do something — a decision that produced no order, an order that never filled, a basket that stopped trading, a blocked/halted/frozen/quarantined state, a veto whose rule name is unclear, an empty Orders or Fills table on the cycle drill-down, or what a number in the dashboard or event log actually means.
---

# Trade Forensics

## Overview

Explains what the bot did and why, from its own event log. **Read-only: this skill
diagnoses and names the lever. It never pulls it.**

Core principle: **the log already contains the answer.** Every gate the bot closed wrote an
event saying so. Forensics is reading them in order — never inferring a reason from an absence.

## When to Use

- "Why was no order placed / why is Orders empty?"
- "Why didn't this fill?" · "Why is this basket not trading?"
- "Why is it blocked / halted / frozen?" · "What does `venue_quantization` mean?"
- "What is `headroom` / `stop_distance` / conviction?"

**Not for:** changing configuration, clearing a halt, arming live. Those are human acts with
typed phrases. Name the lever and stop.

## Non-Negotiables

1. **Work on a copy of the database.** Never query `data/{mode}.db` directly — a live process
   owns it. See `references/reading-the-log.md`.
2. **Never mutate anything.** No config edits, no `risk rearm` / `unhalt` / `arm-live`, no
   writes to the copy that you then present as real. Diagnose, report, stop.
3. **The log is the authority, not the projections** (ADR 0016). `RISK_CHECKED` carries the
   actual reason. A halt and a kill-switch trip have no projector at all.
4. **Verify rule names against `tradebot/risk/` before asserting one.** The catalogue in
   `references/rules-and-blocks.md` is a map for orientation; the source is truth and drifts.
5. **If the log does not say, say that it does not say.** Never guess a reason on a money path.
   "No `RISK_CHECKED` was written for this instrument" is a finding, not a gap to fill.

## The Funnel

An order is the end of nine gates. Walk them **in order** and stop at the first one that closed —
that gate is the answer. Do not start at the bottom because the question mentioned orders.

| # | Gate | Where the evidence is |
|---|---|---|
| 1 | Did the cycle run? | `cycles` row exists; basket `status`; supervision stopped |
| 2 | Blocked before the panel? | outcome `blocked` (kill switch / halt), `quarantined` |
| 3 | Did market data arrive? | outcome `data_stale` |
| 4 | Did the panel answer? | outcome `panel_degraded`; `SEAT_RESPONDED` count |
| 5 | What was decided? | `decisions.action` — **`WAIT`/`HOLD` are not tradable** |
| 6 | Did Tier-1 approve? | first `RISK_CHECKED` → the rule whose `decision` is `veto` |
| 7 | Did Tier-2 approve? | second `RISK_CHECKED`, at submit |
| 8 | Did it reach the venue? | `orders.state`; `submit_unknown` is ambiguous, not failed |
| 9 | Did it fill? | `fills`; protective legs stay `open` by design |

Two traps this ordering exists to prevent:

- **A `risk_vetoed` cycle is not a broken cycle.** Gates 6-7 refusing is the system working
  (fail-closed). Report which rule, its limit, and the observed value.
- **`outcome` is basket-wide, per-instrument results are not.** A cycle reading `orders_placed`
  can contain an instrument that was vetoed. Always check the per-instrument `RISK_CHECKED`
  before saying an instrument traded.

## Reporting

State, in this order: **the gate that closed**, the **rule or condition**, the **observed value
against its limit**, and the **config field or act that would change it**. Then stop.

> Gate 6, Tier-1 `min_conviction`: conviction `0.25` against floor `0.6`, so no order existed to
> fill. The lever is `risk_policy.min_conviction` on the basket.

Distinguish **correct-and-refused** from **wrong**. Most "why no order" questions end at the
first; say so plainly rather than implying a defect.

## References

- `references/rules-and-blocks.md` — every veto rule, cycle outcome and blocking layer, with the
  config lever for each. Read when a rule name or outcome needs explaining.
- `references/reading-the-log.md` — safe db-copy recipes, exact schemas, and the metric glossary.
  Read before writing any query.

## Common Mistakes

| Mistake | Correct |
|---|---|
| Querying the live `.db` while the bot runs | Copy it first |
| Reading `cycles.outcome` as the per-instrument result | Read that instrument's `RISK_CHECKED` |
| Calling a veto a bug | Fail-closed is the design; report the rule |
| Guessing a schema or JSON shape | The exact shapes are in `reading-the-log.md` |
| Proposing a config change as the finding | Name the lever; the human decides |
| "Orders is empty so something failed" | Empty is expected when a gate above 8 closed |
