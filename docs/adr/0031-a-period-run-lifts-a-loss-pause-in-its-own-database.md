# 31. A period run lifts a loss pause in its own database, and continues by re-execution

Date: 2026-11-04

## Status

Accepted. Implements the [period evaluation spec](../superpowers/specs/2026-11-01-period-evaluation-design.md)
(§5.1, §5.3, §5.4, §5.7). Adds the second sanctioned `tradebot` seam, `build_sim(deliberate=…)`,
beside slice E's `news_feed`.

## Context

`decision_lab evaluate period` runs one seat set over one calendar month, quarter or year as a
single run with one ledger, so money and open positions carry across months. The bot's own
`max_consecutive_losses` rule (a Tier-1 rule) halts a basket and needs a human to clear it. A
replay has no human, so left alone a run stops at the first bad streak and every later month reads
as nothing — a measurement of one unlucky week, not of the period.

Clearing the halt is not enough. `consecutive_losses` is derived from `round_trips` on every read
and only a win resets it (`ledger/history.py:96`); the basket cannot trade, so it cannot win, so
the next proposal sees the same streak, vetoes and halts again (spec §5.3).

A run is also long and paid. A crash, a reboot or a budget stop must not forfeit it, and a rule
that ends the run (out of money, the kill switch) is a result, not an error.

## Decision

**A loss pause is lifted inside the run's own database.** At a month boundary, and only if the
basket stopped on the loss-streak rule, the run clears the basket status and publishes a basket
version with `max_consecutive_losses = streak + L`, where L is the candidate's configured limit
(§5.3). When a win resets the streak the limit goes back to L, so a relaxed rule does not flatter
the rest of the run. Each lift is a `RISK_EVENT` and is recorded on the run's meta. No bot code is
changed and no bot database is touched. Because the bot would have stayed halted, the lift is
shown wherever that run's profit appears.

**The rest of a paused month runs as whole-basket quarantine cycles** (§5.4). Skipping a stopped
basket's ticks would leave a held position's stop unfired until the next month and the marks stale
(`UNVALUABLE`). A quarantined cycle takes its snapshot, which feeds the sim venue and refreshes the
marks, then ends `QUARANTINED` with no panel call and no spend. These cycles carry no decision, so
they are neither scored nor counted in coverage.

**Three endings** (§5.7):

| Ending | Cause | Treatment |
|---|---|---|
| `COMPLETED` | every month ran | reused at its identity |
| `INTERRUPTED` | crash, Stop, budget reached, an unexpected exception, a `FAILED` cycle | continuable |
| `RULE_ENDED` — "Model failed, requires tuning" | out of money, or the kill switch | final, reused, never continued |

Out of money counts **total** cash, free plus locked in a resting entry (the kill switch is the
bot's own, valued as it always is): locked cash can still become a position and the verdict is
final, so the reading errs toward letting a run go on.

**Continue re-executes from the period start; it does not restore.** It opens a fresh database,
re-drives the same loop, replays the panel's recorded answers up to the last checkpointed month
end, and asks live models after that. A replayed cycle compares its snapshot's market content with
the recorded one and refuses, naming the first cycle that differs; its recorded cost is carried
unchanged. A checkpoint is the month's meta only — no database copies.

Restoring was built first and rejected on evidence. `SimBroker.restore` lost a stop's armed state,
and recovery rebuilds a held position's group with no protective plan (KNOWN_GAPS §9), so a panel
sale of a position held across a checkpoint left its stop resting over a flat position; the run
diverged and halted. That is the restart defect recorded as KNOWN_GAPS §18, and closing §9 is a
bot-wide money-path change out of scope here. Re-execution rebuilds every piece of in-memory state
with the code that built it the first time, so there is nothing to restore wrongly.

**The seam.** `build_sim(deliberate=…)` is an optional callable replacing
`DecisionEngine.deliberate`. It is refused first in `_assemble` for any mode but sim, and
`BasketRunner` uses it only when `is not None`. Nothing under `tradebot/` names `decision_lab`; the
seam and its guard tests are one change. The earlier `armed_orders` seam was reverted, since
re-execution makes it unnecessary.

## Alternatives rejected

- **Loosening the risk rule for everyone.** Widening `max_consecutive_losses` globally changes what
  is being measured for every run and hides a real streak. The lift is per run, bounded to L more
  losses, restored on a win, and stated beside the profit.
- **Restoring state from a checkpoint** (database copy plus `recover()`). Unfaithful, as above; a
  year would also hold twelve growing copies (§5.7).
- **One long run per period with no month structure.** An interruption late in a year would forfeit
  months of paid spend, and there would be no unit to report, budget-check or continue at.

## Consequences

- A run's profit is what the panel made with the loss rule suspended between months. It is not what
  the bot would have done, and the page and report say so.
- Continue repeats up to one month of replay work and spends only after the last checkpoint. The
  cost of replaying a year is unmeasured and is to be measured on Stage 3 data.
- KNOWN_GAPS §9 and §18 remain open for the paper soak's restarts. Continue does not depend on
  them, because it never restarts a process into recovered state.
