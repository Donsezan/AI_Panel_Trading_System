# 13. Configuration is versioned, and every cycle pins the versions it ran on

Date: 2026-07-31
Status: accepted

## Context

DESIGN §6.1 requires user-editable configuration to live in the database with a monotonically
increasing version per object, updates creating new versions rather than overwriting, and each
`DecisionCycle` recording the exact versions it used. Until Phase 6 the composition root held
configuration as arguments: `build_sim(basket=…, global_policy=…)`.

Three questions had to be answered before writing the store.

**What is a versioned document?** DESIGN §4 models `Basket` as referencing a `panel_config_id`
and a `risk_policy_id`. The code inlines both: `Basket.panel` is a `PanelConfig` and
`Basket.risk_policy` is a `RiskPolicy`. Normalising them into separately versioned rows would
mean a basket pins three versions instead of one, and a cycle's provenance becomes a join.

**Why does a cycle need pins at all?** The dashboard's decision drill-down is the core research
artifact (DESIGN §6.10). Reading a six-week-old decision against today's `min_conviction` would
misrepresent it — the panel was not gated on that number.

**How is a deletion represented?** A basket that is deleted still has cycles pointing at it.

## Decision

**Two kinds — `basket` and `global_risk` — and a basket carries its panel and Tier-1 policy
inside it.** One document is what the dashboard edits as one tree (DESIGN §6.10 describes exactly
that: a provider list and a seat list edited together), what `PanelConfig`'s validators already
check as one object, and what a cycle pins as one number. The Tier-2 policy is separate because it
belongs to no basket and outranks all of them. `ConfigStore` is generic over the kind, so a
`panel` kind — for Phase 7's shadow A/B harness, which needs to name two panels independently —
is a registry entry, not a redesign.

**A cycle records `{"basket:demo": 4, "global_risk:global": 2}`** on `CYCLE_STARTED` and in the
`cycles` projection. The runner is built from a pinned record and carries its refs, so the pin is
what the cycle actually ran on rather than what was current when it was read.

**Retirement is a version.** Retiring writes a new version carrying the last document with
`retired = 1`. The basket leaves `current()` immediately, and every version — including the
retiring one — still resolves for the cycles that pinned it.

**`config_versions` is not a projection.** `rebuild_projections` truncates and replays the log;
it must not touch this table, because the log's pins resolve *against* it. A rebuild that
truncated configuration would erase the meaning of the log it was rebuilding from.

**A document that any secret can be found in is refused.** `put` serializes the document and
compares it to `SECRETS.scrub(...)` of itself, which catches both values registered from the
environment at startup and known key *shapes*. `secret_ref` holds an environment variable name;
that indirection is only a control if something enforces it (PLAN §3.2).

**The row and its `CONFIG_CHANGED` event are one transaction**, via `EventStore.append_within`.

## Consequences

- The composition root *publishes* rather than holds configuration. Passing `baskets=` or
  `global_policy=` to `build_sim` writes a new version; a fresh database is seeded with the demo
  basket and default limits, and from then on the stored documents are the truth.
- Editing a shared panel means editing it in each basket that uses it. Acceptable in v1 (there is
  one basket by default) and revisited when Phase 7 needs named panels.
- The supervisor rebuilds a basket's runner when its version changes, which is what makes "runners
  pick up a change at their next cycle boundary" (DESIGN §6.10) true rather than aspirational. The
  Tier-2 *watchdog* outlives every cycle, so it is handed the new policy through
  `Watchdog.use_policy` instead — otherwise it would enforce the drawdown limit the process
  started with.
- Reads fail closed: an unparseable stored document raises `ConfigError`. A bot that falls back to
  a default risk policy is a bot that trades past a limit somebody set.
