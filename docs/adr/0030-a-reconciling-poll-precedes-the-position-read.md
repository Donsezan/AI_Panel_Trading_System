# 30. A reconciling poll runs between the snapshot's market reads and its position reads

Date: 2026-10-28

## Status

Accepted. Closes the intra-cycle half of [KNOWN_GAPS](../KNOWN_GAPS.md) §5, per
[the decision memo](../superpowers/specs/2026-10-28-known-gaps-5-intra-cycle-memo.md) (Option A,
with Option D parts 1–2).

## Context

On a `SimBroker` stack — sim, a replay, and paper, whose primary venue is `SimBroker`
(ADR 0020) — the venue matches resting orders **only when someone reads `stack.prices`**, and only
a cycle's snapshot and a manual close do. So a venue-held stop can fill *during* the snapshot's
own candle read. `ContextBuilder` then read `ledger.position` from a ledger that had not booked
it, the panel was told it held coins the venue had sold, `_size_sell` sized a SELL from the same
stale holding, `SimBroker` accepted it (free base may go negative), and the next poll refused the
stop's fill in `Ledger._apply_sell` as `sell of … exceeds holding …`.

That error was correctly classified (`ReconciliationMismatchError`, fail-closed) and never
consumed. The fill stayed unbooked, every later `poll()` re-read it and raised again, and because
`poll()` had no per-group isolation it stopped syncing every group after it — in a supervised
process, every basket's fills, TTLs and legs — and failed each sibling basket's next `_settle` in
turn. In a replay the harness's own unguarded poll crashed the build.

The 30-second supervisor sweep does not cover this on a `SimBroker` stack: it reads
`read_only_prices`, so it can only book what a cycle's read has already matched, by which time
that cycle has sized.

## Decision

- **`ContextBuilder.build` reads every instrument's market data, then awaits an injected `settle`
  hook, then reads every position.** The runner supplies the hook: a poll of the process's one
  `ExecutionMonitor`, scoped to the basket's instrument keys (ADR 0026 means no other basket's
  group sits on them). The frozen snapshot is then truthful about what is held.
- **`ManualCloser._proposal` does the same:** quote → scoped poll → position.
- **The same code runs in every mode, live included** (ADR 0020). A separate replay-only fix
  would mean a replay no longer exercises the thing a soak runs.
- **A poll failure is the worker's existing failure path.** A `RetryableError` propagates out of
  `run_once` to the worker's backoff, exactly as a failed submit does; a `FailClosedError` ends the
  cycle `FAILED` and halts the basket. Either way: no snapshot, no decision, no order.
- **`ExecutionMonitor.poll` isolates failures per group.** It syncs every group in scope, collects
  errors, still maintains the healthy groups, and re-raises the first error afterwards.
- **A group whose fill the ledger refuses is poisoned.** One `RISK_EVENT`
  (`rule: unbookable_fill`, naming the order, the quantity and the holding), the group stays
  tracked and is retried every poll, and the poison is raised only to a caller whose scope contains
  that group. An unscoped poll — the supervisor's sweep, the backtest harness — never raises for it.
  The fill is never dropped; it books on the first poll after the holding allows it. The ledger has
  no dedupe, so a multi-fill order whose poll is retried can re-book fills it had already booked;
  that is pre-existing behaviour, not one this ADR introduces.

## Consequences

- **One extra venue read per cycle, before the decision.** On Binance a protected position is one
  OCO group of two legs at `queryOrder` weight 4 each: about **8 weight per held, protected
  instrument per cycle**, plus `myTrades` (20) on the poll that discovers a fill. Against the
  600/min `RateBudget`: a two-instrument basket at 1h cadence is ~16 weight/hour (~0.05%); five
  baskets × two instruments at a 1-minute cadence is 80/min (~13%), the same order as the 30s sweep
  already spends. A basket holding nothing costs nothing: `poll` over no tracked group reaches no
  venue.
- **The ordering change is in the fail-closed direction.** A reconciling *read* now precedes
  sizing, which is what non-negotiable #3 asks for; it adds no order intent (#4) and an error means
  no trade (#1). The poll may resize or re-arm protective legs mid-cycle, exactly as the sweep does.
- **What it does not close:** in live, a venue can still fill a stop *during deliberation*, after
  the poll. On Binance spot and Alpaca the resting stop reserves the base asset, so the cycle's
  SELL is *rejected* rather than overselling, and the stop's fill then books cleanly (from venue
  documentation; live has never run). A poll placed immediately before sizing (the memo's Option
  B) could be added on top if that window ever matters.
- A pass of `decision_lab` that would have died on `exceeds holding` no longer reaches that state;
  if some other unbookable fill appears, it ends the pass short (basket halted) rather than
  crashing it.

## Follow-ups, deliberately not done here

- **`SimBroker` accepts a SELL beyond free base** (Option E). It must ship with KNOWN_GAPS §6's fix
  — release the legs before a discretionary exit — or it refuses every exit while legs rest. It
  re-baselines every soak and corpus.
- **`ExecutionService.cancel` ignores `ack.cancelled=False`** and advances to terminal `CANCELLED`,
  so a fill racing the cancel is lost to the monitor (KNOWN_GAPS §10).
- **The evidence question:** soak and corpus evidence so far contains discretionary exits live
  Binance would have refused. Whether it still counts is the operator's call.

## Rules that are easy to get backwards

- **The hook goes after *every* market read and before *every* position read**, not per
  instrument. Per instrument, the second instrument's candle read could match a stop after the
  first instrument's position was read.
- **Scope by instrument key, not by basket id.** A group's `basket_id` is where it came from; what
  the snapshot needs settled is every group that can move the holdings it is about to read.
- **Poison is raised only to scoped callers.** Raising it to the sweep is the wedge this ADR ends;
  swallowing it from the scoped caller is a basket sizing against a ledger known to be wrong.
- **Never clamp in `_apply_sell`.** Absorbing the disagreement into the ledger is the reconciler's
  job (ADR 0006) and would hide real corruption.
