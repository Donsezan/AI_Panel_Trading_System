# ADR 0005 — Risk state and trading history live in the database, never in memory

**Status:** accepted · 2026-07-26 · implements DESIGN §6.6, §8.2, PLAN §5 Phase 2e/2f

## Context

Several controls exist specifically to survive the situation that breaks them. A cooldown stops
a panel re-deciding the same thesis every cycle; a daily trade cap stops a decision loop that
will not settle; a kill switch stops everything. All three are defeated by the same event: the
process restarts.

A crash-looping bot with in-memory limits is not a bot with limits. Each restart clears the
counters, so the crash loop becomes an unmetered trading loop — the failure mode is worst
exactly when the system is least healthy.

## Decision

**Risk posture is written to the database and read back on every start.**

- `risk_state` holds the kill switch, its reason, the high-water mark and day-start equity.
  `basket_status` holds halted baskets. Neither is a projection: they are current posture, read
  by the startup sequence before any event has been replayed.
- **An absent or unreadable row reads as `TRIPPED`.** Fail closed: "we do not know" is never
  "go ahead". Only a genuinely uninitialised database — no persisted state *and* no high-water
  mark — is armed automatically, and that state only exists before anything has ever traded.
- Every change also emits `KILL_SWITCH_CHANGED` / `BASKET_STATUS_CHANGED`, so the tables are a
  cache of facts the log already holds.

**Trading history is derived from the read model, not counted in memory.**

- `cycles_since_trade`, `trades_today`, `consecutive_losses` and `orders_last_hour` are queries
  against `orders`, `cycles` and `round_trips`.
- The holding period a position reports to the panel is derived the same way, from
  `positions.opened_at` and the cycle log. It was a counter; a counter told the panel that a
  position held for days had been opened this cycle, every time the process came back.
- Only **entry** orders count as trades. Counting protective legs would exhaust a six-trade
  daily cap in two decisions.

**Round trips are the unit a loss is counted in.** A loss is a position that opened from flat
and closed back to flat with negative realized PnL, fees included on both legs, partial fills
aggregated. Counting fills instead would auto-pause a basket for a scratch exit that happened to
be split across three of them.

## Baselines are flow-adjusted

Deposits and withdrawals move the high-water mark and day-start equity by the exact amount of
the flow, never by re-deriving from current equity. Without this a withdrawal reads as a
drawdown and trips the kill switch, and a deposit masks a real loss (R16). Re-deriving would
launder a genuine loss into the baseline, which is the same bug wearing a disguise.

## Re-arming

Clearing a tripped switch or a halted basket requires a typed phrase (`RE-ARM TRADING`) and is
recorded with its actor. Until the dashboard exists (Phase 6) the surface is
`tradebot risk rearm|unhalt`. Re-arming resets the baselines to current equity — the operator is
asserting they have looked at what happened and accept this as the new starting point; keeping
the old mark would simply re-trip on the next sweep.

## Consequences

- A restart never silently un-halts anything. The startup sequence asserts this directly.
- Nothing automatic can re-arm. There is no code path that supplies the phrase.
- The daily-loss limit is deliberately *not* a kill: it halts new orders for the day and lets
  existing protective legs keep working. Two different severities, two different responses.
