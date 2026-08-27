# Protective legs track the position, not the entry order

> Implementation design for [KNOWN_GAPS.md §4](../../KNOWN_GAPS.md). That document says *what is
> wrong, what was observed, and what it costs*; this one says *what is built, in what shape, and
> what "done" checks*. Authoritative specs remain [DESIGN.md](../../../DESIGN.md) and
> [IMPLEMENTATION_PLAN.md](../../../IMPLEMENTATION_PLAN.md).
>
> **Status: approved, nothing built.** Written 2026-08-28, before any code change, per the standing
> rule that a change touching the money path gets a design pass first.

This is a **defect fix**, not a feature. It is reachable from ordinary automated trading in every
mode, and it blocks the `decision_lab` corpus rebuild and therefore slice B: the six-month
reference pass on `--reference-panel sim` raises `ReconciliationMismatchError` and stops, which is
how the defect was found.

Everything here is inside `tradebot/execution/`. Four adjacent findings surfaced while designing it
and are recorded as [KNOWN_GAPS.md §5–§8](../../KNOWN_GAPS.md) rather than fixed — see
[§8](#8-deliberately-out-of-scope).

---

## Table of contents

- [1. Decisions taken](#1-decisions-taken)
- [2. The design](#2-the-design)
  - [2.1 State: what is remembered, what is derived](#21-state-what-is-remembered-what-is-derived)
  - [2.2 The allocation rule](#22-the-allocation-rule)
  - [2.3 When the target cannot be met](#23-when-the-target-cannot-be-met)
  - [2.4 Ordering within a poll](#24-ordering-within-a-poll)
- [3. Rules that are easy to get backwards](#3-rules-that-are-easy-to-get-backwards)
- [4. Tests](#4-tests)
- [5. Order of work](#5-order-of-work)
- [6. Definition of Done](#6-definition-of-done)
- [7. Risks](#7-risks)
- [8. Deliberately out of scope](#8-deliberately-out-of-scope)

---

## 1. Decisions taken

Four questions settled with the operator on 2026-08-27 and 2026-08-28.

### D1 — monitor-side only

The seam has three defects in it. Only one is fixed here.

`ExecutionMonitor` gains a view of the position and keeps the protective legs matched to it. It does
**not** gain a poll of its own (§5), and the discretionary exit path does **not** learn to release
protection before it sells (§6). Both are recorded with their evidence and left open.

The consequence is stated rather than hidden: on a real venue this fix may never be reached, because
§6 means the reducing order is itself rejected while a full-size stop holds the coins. What it does
fix is every mode whose venue does not enforce reservations — which is sim, and paper, whose primary
venue **is** `SimBroker` (ADR 0020). That is exactly the evidence base `report promotion` reads and
the corpus slice B will score, so it is the blocking half.

### D2 — the venue answers for the orders; the ledger answers for the holding

Non-negotiable 3 says the venue is the source of truth. It applies to one half of this and
deliberately not to the other.

**Orders: the venue, and it costs nothing.** `poll` already calls `fetch_order` on every leg every
sweep ([service.py:136](../../../tradebot/execution/service.py#L136)), so by the time the size check
runs the monitor is holding the venue's own answer about each protective order. `protected_qty` — the
local counter whose drift *is* this defect — is therefore redundant beside a fresher fact. It is
deleted and the quantity derived. This also picks up three things a counter cannot express: a leg
partially filled at the venue, a leg the venue cancelled itself, and an operator cancelling an order
by hand in the venue's own UI.

**The holding: our own books, on purpose.** `fetch_positions_and_balances` could answer this, and the
monitor must not ask it. If the venue said 0.02 where the ledger says 0.10, that is the single most
serious condition in the system, and there is already a component whose entire job is to catch it,
classify it, and escalate — `Reconciler`, ADR 0006. A monitor that saw the same difference and quietly
resized its legs down to 0.02 would **absorb the alarm**: tidy screen, nothing told, discrepancy
invisible. That is the failure §1 already describes from the arithmetic side. One component, one
opinion about venue-versus-us — the rule the instrument catalogue follows (ADR 0025).

The holding therefore comes from `Ledger.position`, reached through a new
`ExecutionService.held(instrument_key) -> Decimal`. `ExecutionService` already owns the ledger and
already calls `apply_fill` on it, so this adds no dependency edge and **no constructor changes
anywhere** — `app.py`, `tests/scenario/harness.py`, `tests/unit/test_startup.py` and all four
`ExecutionMonitor(...)` sites in `tests/unit/test_monitor.py` are untouched.

### D3 — allocation is ranked by stop trigger price, not by age

When the holding falls below the total protected quantity, something has to give up cover. The first
proposal was newest-entry-first; it is wrong and was corrected before approval.

Which *group* keeps its cover is bookkeeping. What matters is which **trigger prices** rest against
the holding and at what size. For a long position the tightest protection is the **highest** stop,
because it fires first on the way down; the stop most affordable to lose is the one that would only
have fired after a larger loss.

Age is a proxy for that in a rising market and an exactly inverted one in a falling market — which is
the case that matters. Entry at 100 with a stop at 95, then a later entry at 90 with a stop at 85:
newest-first keeps the 85 and trims the 95, so nothing protects the holding until it has fallen a
further ten.

So groups are ranked by `entry.protective.stop_price`, tightest first. No market price and no venue
call are needed, because the ranking is of candidate stops against each other rather than against the
market. Direction is side-dependent and lives in a `dict[Side, ...]` keyed on the exit side, matching
`_EXIT_SIDE` and `_OFFSET_SIGN` in [protective.py](../../../tradebot/execution/protective.py). v1 is
long-only so only one row is ever taken; the table keeps the module honest, as those two already do.

### D4 — cancel-then-place stays, and its failure becomes visible

The resize window is not a design choice. On a shrink the smaller leg cannot be placed first, because
the coins are reserved against the larger one still resting; on a growth it cannot either, because two
legs would momentarily reserve more than is held. Cancel-then-place is what the venue permits in both
directions, and `self._polling` already bounds the window against an interleaved poll. Binance's
`cancelReplace` would genuinely close it but is single-order rather than order-list and absent from
`BrokerAdapter`; adding it is a change across three adapters and the contract suite, and is out of
scope.

On a shrink, being caught mid-window costs nothing. What rests before the cancel is an oversized
order: if it triggered, the venue would reject it for insufficient balance, so the position is
unprotected *anyway* — with a record claiming otherwise. Cancelling first trades a false protection
for an honest absence of one.

What does change is that a failure of the second half is recorded. Today an exception from
`submit_group` propagates and no `unprotected_position` event is written at all, so the log shows a
cancellation followed by silence and the state has to be inferred from an absence.

---

## 2. The design

One file changes materially — [execution/monitor.py](../../../tradebot/execution/monitor.py) — plus a
one-method addition to [execution/service.py](../../../tradebot/execution/service.py). `protective.py`
is unchanged: `plan_legs` already sizes to a quantity it is given and already reports its own refusal
reason.

### 2.1 State: what is remembered, what is derived

`_Tracked` loses one field and gains one.

```python
@dataclass(slots=True)
class _Tracked:
    order: Order
    instrument: Instrument
    revision: int = 0
    legs: dict[str, Order] = field(default_factory=dict)
    #: The target last reported as unguardable, so the report fires once per target rather than
    #: once per poll. A de-duplication marker; nothing reasons from it.
    unprotected_at: Decimal | None = None

    @property
    def resting_qty(self) -> Decimal:
        """How much of the holding the venue is currently guarding for this group."""
```

`protected_qty` is deleted (D2).

`resting_qty` is a **`max` over the open protective legs' remaining quantity, never a sum.** With OCO
the stop and the take-profit rest at the same size and the venue's order list reserves the coins once,
not twice, so the guarded amount is one leg's worth. Remaining is `qty - filled_qty`, so a leg that
partially filled counts at what is left.

`revision` stays. It is an id-derivation counter, not a fact about the world: two replacements at the
same size must not collide on `client_order_id`, so it cannot be derived.

`ExecutionService` gains:

```python
def held(self, instrument_key: str) -> Decimal:
    """What the ledger says is held. Not the venue's answer — see the design's D2."""
    return self._ledger.position(instrument_key).qty
```

### 2.2 The allocation rule

`poll` gains one step between the sync loop and maintenance: group the tracked entries by instrument
and compute a target per group.

```
for each instrument:
    budget = max(ZERO, held − our other working sells)
    for each group, tightest stop first:
        target  = min(entry.filled_qty, budget)
        budget -= target
```

- **`held`** — `execution.held(instrument_key)`, per D2.
- **our other working sells** — the remaining quantity of every *tracked* order that is a SELL, still
  open, and not a protective leg: a discretionary exit or a manual close still resting. Subtracting it
  stops the total committed quantity exceeding the holding while an exit is in flight. Hold 0.10, a
  discretionary sell for 0.03 resting, legs for 0.10 — locally that looks fine, but 0.13 is committed
  against 0.10 and a real venue rejects one of them.
- **tightest stop first** — `entry.protective.stop_price`, ordered by D3's side table, ties broken on
  `created_at` then `client_order_id`, because startup adopts orders from the database in arbitrary
  order and the rule must not depend on it.
- **`budget` clamps at zero.** If our own resting sells already exceed the holding, every target is
  zero and all legs are cancelled. Adding protective commitments on top of an over-commitment is the
  wrong direction.

Then per group, `target != resting_qty` is the whole trigger for a replacement. `_maintain(group)`
becomes `_maintain(group, target)`.

### 2.3 When the target cannot be met

| Target vs. `resting_qty` | Action | Recorded |
|---|---|---|
| Equal | none | nothing |
| Zero, legs resting | cancel them | the cancellation, reason `released_to_position` |
| Different, expressible | cancel, then place at the new size | `PROTECTIVE_PLACED`, as today |
| Above zero, below `min_qty` / `min_notional` | **cancel first**, then report | `unprotected_position` |
| Expressible, `submit_group` fails | report, **then** propagate | `unprotected_position`, venue's reason |

**Zero is not "unprotected".** That event means money is at risk with no stop behind it. A group whose
holding is gone guards nothing and risks nothing; filing the same event for both would train an
operator to ignore the one that matters. The group then falls to `prune` unchanged.

**Cancel before reporting.** Today the `not plan.protected` branch of `_replace_legs` records the
reason and returns with the legs still resting. Harmless when the entry grew; on a shrink it would
leave an oversized order at the venue *and* file a report saying the position is unguarded — both
statements false, in opposite directions.

**A group whose entry carries no `ProtectivePlan` is skipped entirely** — a discretionary SELL, or a
buy on a venue holding no protective orders (`protective_plan` returns `None` for both,
[tier1.py:110](../../../tradebot/risk/tier1.py#L110)). Today such a group reaches `plan_legs`, gets
`"no protective plan on the entry"`, and files an `unprotected_position` risk event and a
*"position left without a venue-held stop"* warning for an order that **is** the exit. Confirmed in
the operator's `data/sim.db` at seq 2232, `sim:LTC/USDT`, from a dashboard manual close.

**`unprotected_at` is cleared on a successful placement**, or a position that becomes unguardable,
recovers, and becomes unguardable again at the same size is reported once and never again.

### 2.4 Ordering within a poll

Targets are computed **after** the sync loop, never during it. `_sync` books fills into the ledger as
it reads them, so a stop that filled at the venue this sweep has already reduced the position by the
time the budget is computed. Computing targets per group inside the sync loop would size some groups
against a pre-fill holding and others against a post-fill one.

---

## 3. Rules that are easy to get backwards

- **The invariant is per instrument, and a per-group clamp makes it worse.** The observed log has two
  live groups on ETH/USDT — 0.0351 and 0.0852 against a position of 0.1116. "If the position is below
  what this group guards, resize to the position" gives group A 0.1116 *and* leaves B at 0.0852:
  0.1968 resting against 0.1116.
- **The rule subsumes the old one; there is no growth branch.** "The entry filled further" is `target`
  rising because `filled_qty` rose. One comparison, both directions. The existing protective-group
  tests passing unchanged is the executable form of this.
- **It never invents protection.** `target` is capped by the group's own `filled_qty`, so quantity no
  tracked group covers — adopted at startup, held from before the process — gets no legs. We only cap.
- **At most one group is partially funded.** The pass fully funds groups until the budget runs out;
  everything after gets zero. So quantization strands at most one remainder, and that remainder is by
  definition below `min_qty` — dust by the venue's own measure. This is the cost of an ordered
  allocation over a proportional one, and it is bounded. Proportional was rejected for the opposite
  reason: it replaces *every* group's legs on *every* reduction and manufactures many sub-minimum
  fractions, turning one reduction into several unprotected reports.
- **`resting_qty` is a `max`, not a `sum`.** Summing an OCO pair double-counts and would shrink every
  group to half its correct size on the first poll after arming.
- **`protected_qty` did two jobs, and both replacements must land together.** It recorded the guarded
  size *and*, by being written in `_record_unprotected` too, suppressed the unprotected report from
  firing on every subsequent poll. Deriving the size alone would leave that report with nothing to
  de-duplicate against — `resting_qty` is zero precisely when the report is warranted — so an
  unguardable position would file one every sweep. `resting_qty` takes the first job and
  `unprotected_at` the second; deleting the field without adding both is a regression, not a
  refactor.

---

## 4. Tests

**Rung 1** — `tests/unit/test_monitor.py`. The existing protective-group tests must pass
**unchanged**. New:

- A reduction from outside the group shrinks its legs — the gap's own case.
- A reduction to zero cancels the legs and records *no* `unprotected_position`.
- Two groups on one instrument, holding cut: the **lower** stop is trimmed, the higher keeps full
  cover. This pins D3, and fails under both age orderings.
- A shrink below `min_qty` cancels the legs *and* reports.
- A tracked discretionary SELL reduces the budget; its own group records nothing.
- `submit_group` failing after the cancel reports, then raises.
- The report fires once per target, not once per poll; a successful placement re-arms it.
- **Property test** over arbitrary sequences of fills and reductions: `Σ resting_qty ≤ held` for every
  instrument, always.

**Rung 2** — nothing new. This is monitor logic, not adapter semantics; the contract suite already
covers `fetch_order` reporting `filled_qty` on a protective leg, which is what `resting_qty` reads.

**Rung 3** — `tests/scenario/`, the test the gap names as its own absence: an entry fills, a partial
discretionary exit fills, then a bar through the original stop. It must use a panel that actually
takes partial exits — `stub` never does, which is why this survived hundreds of backtests — so it
drives `varied-*` seats or a scripted panel through the real loop against `SimBroker`. Asserts no
`ReconciliationMismatchError`, and that the stop which fires is sized to the holding at the moment it
fires.

---

## 5. Order of work

1. `ExecutionService.held`, with its docstring naming D2.
2. `_Tracked`: delete `protected_qty`, add `resting_qty` and `unprotected_at` **together**. The two
   replacements are not separable — see §3's last rule. Existing tests stay green; this step is
   behaviour-preserving on its own.
3. The rung-1 tests, failing.
4. Target computation and the `_maintain` signature change.
5. §2.3's table, including the two corrections and the `ProtectivePlan` skip.
6. The rung-3 scenario.
7. Re-run the `decision_lab` reference pass.
8. Move §4 to *Closed* in `KNOWN_GAPS.md`, with what was observed and what it now does.

---

## 6. Definition of Done

- `.\check.ps1` green, `execution/` still ≥ 95%.
- Every test in §4 present and passing; the pre-existing protective-group tests unmodified.
- The six-month `decision_lab` reference pass on `--reference-panel sim` runs to completion.
- No `unprotected_position` event in that pass attributable to a discretionary SELL.
- `KNOWN_GAPS.md` §4 moved to *Closed* with its evidence; §5–§8 left open.
- `git diff --stat` touches only `tradebot/execution/`, the two test files, and the two docs.

---

## 7. Risks

- **R1 — the ordering is wrong in a regime not considered.** Mitigated by it being one sort key with
  a test that pins it, and by the ranking being of stops against each other rather than against a
  market state that can change.
- **R2 — more frequent replacement raises venue call volume.** Every reduction now costs up to one
  cancel-plus-submit per affected group, against a shared rate budget (ADR 0008). Bounded by groups
  per instrument, which is small, and reductions are rare relative to polls.
- **R3 — the fix acts at the next poll, which §5 says may be hours away.** Accepted and recorded.
- **R4 — on a real venue §6 may reject the reducing order before any of this is reached.** Accepted
  and recorded; it does not affect the modes this unblocks.

---

## 8. Deliberately out of scope

Four findings from this design pass, recorded as [KNOWN_GAPS.md §5–§8](../../KNOWN_GAPS.md):

- **§5** — the monitor has no poll of its own; `poll()` has two production callers and `settle()` has
  none. Fixing it means a tick in `Supervisor.serve`, which is a control-plane change.
- **§6** — nothing releases protective legs before a discretionary exit, so on a venue that reserves
  the base asset the exit is itself rejected. Fixing it touches `basket_runner`, `manual_close` and
  the unprotected window between the cancel and the sell.
- **§7** — the `RECON_MISMATCH` alert body names no instrument and no quantity.
- **§8** — the kill-switch reason joins the *explained* diff lines and carries no absolute figures.

Also out of scope: `cancelReplace` at the adapter level (D4), and any change to `plan_legs`, which
already does its job.
