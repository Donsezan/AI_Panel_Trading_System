# 18. A challenger panel is evaluated on the champion's snapshot, and never trades

Date: 2026-08-01
Status: accepted

## Context

DESIGN §12 listed a panel A/B harness as future work and PLAN Phase 7 promoted it, for a reason
DESIGN §9 rung 5 states outright: a few weeks of forward PnL is statistically weak. It is worse
than weak for *comparing* panels. Run panel A in June and panel B in July and the difference you
measure is mostly the difference between June and July; the panels contribute a rounding error.

Two panels judged on the **same frozen snapshot**, every cycle, removes the market from the
comparison entirely. What remains is the only thing under test — how each panel reads one
identical packet of evidence. The snapshot already exists, is already immutable, and is already
hashed and recorded, so the comparison costs one extra deliberation and no new machinery.

The hazard is equally clear. A second panel inside the cycle loop is a second thing that can
raise, hang, spend money and place an order. It must be able to do exactly one of those.

## Decision

**The challenger lives on the `Basket` document** as `shadow_panel: PanelConfig | None`, so it is
versioned, pinned per cycle, GUI-editable, and off entirely when unset (ADR 0013). It is a
`PanelConfig` like any other — there is no second panel type and no second set of validators.

**It runs last, and only for the record.** The runner deliberates the champion, sizes it, sends
it through both risk tiers, submits, settles — and only then hands the same snapshot to
`ShadowEvaluator`. Placing it earlier would put a research record between a decision and the
order it justified. The result is one `SHADOW_EVALUATED` event and nothing else: no
`DECISION_MADE`, no `RISK_CHECKED`, no intent. There is no code path from a shadow decision to an
`OrderIntent`, in the same sense that there is no blind-resubmission path (PLAN §2.3).

**A shadow failure is not a cycle failure.** `evaluate` catches every exception — classified or
not — writes it into the event as `error`, and returns. The champion's cycle has already
completed by then and its outcome is whatever the champion made it. Recording the failure rather
than swallowing it is what keeps a challenger that quietly stopped being evaluated visible in the
comparison report instead of silently shrinking its denominator.

**Its cost is its own.** The engine builds a fresh `CycleBudget` from the challenger's own
`max_cost_usd_per_cycle`, and the cost is recorded on `SHADOW_EVALUATED` rather than added to
`CYCLE_COMPLETED`. `$/decision` for the panel that actually traded stays a true figure; research
spend is reported beside it, not inside it.

**Log-only.** A new event type, no projection (ADR 0016). The comparison is folded out of the log
like every other report, from four narrow types — nothing reads a snapshot or a transcript.

**Two configuration rules, enforced on the `Basket`, not at runtime.** The panels must carry
different ids, because the report names each side by its panel id and a comparison whose two
sides are both called `p1` cannot be read. And a provider id both panels declare must be declared
*identically*: one `ProviderPool` serves both, so two endpoints or two price lists under one id
would price one panel's tokens against the other's table.

**The dashboard edits both panels through one macro.** `_panel.html` is rendered twice, for
`panel` and for `shadow_panel`. This was not the original plan — the ladder doc deferred GUI
editing — but the form round-trips a basket through `draft_of` and `parse`, so a form rendering
only the champion would have **deleted a configured challenger the first time anyone edited the
basket**. A blank challenger section is dropped before validation, because a `<select>` always
submits something and an untouched section is *no challenger*, not an invalid one.

## Consequences

- A basket with a challenger costs roughly twice as much per cycle and takes roughly twice as
  long to deliberate. Both are visible: the cost in the report, the latency in the cycle. On free
  slots the cost is zero and the latency is the whole price.
- The comparison pairs only instruments **both** panels ruled on. A challenger that answered for
  fewer instruments contributes `unpaired`, counted and reported rather than paired against a
  guess.
- Cycles where the challenger failed are excluded from the arithmetic and listed by cycle id.
  Dropping them silently would overstate agreement exactly when the challenger was least reliable.
- A window containing two different challenger panel ids mixes two experiments into one set of
  totals. The report says so rather than refusing; narrowing the window is the operator's call.
- The backtest harness gets shadow evaluation for free, since it drives the real loop. A replay
  of a shadowed basket therefore deliberates twice per tick.
