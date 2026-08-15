# Phase 12 Piece 1 — mark-to-market portfolio valuation

> Implementation design for [PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md](../../PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md)
> Piece 1. That document says *what is wrong and why it matters*; this one says *what is built,
> in what shape, and what "done" checks*. Authoritative specs remain [DESIGN.md](../../../DESIGN.md)
> and [IMPLEMENTATION_PLAN.md](../../../IMPLEMENTATION_PLAN.md).
>
> **Status: approved, nothing built.** Written 2026-08-16, before any code change, per the standing
> rule that a change touching the money path gets a design pass first.

This is a **defect fix**, not a feature. Findings 1–4 are live in the running crypto-only paper
soak; the most severe of them disables the drawdown kill switch. Piece 2 is out of scope and is
blocked on this landing.

---

## Table of contents

- [1. Decisions taken](#1-decisions-taken)
- [2. What the implementation audit added](#2-what-the-implementation-audit-added)
- [3. The design](#3-the-design)
  - [3.1 One function](#31-one-function)
  - [3.2 `Marks` — the shared price cache](#32-marks--the-shared-price-cache)
  - [3.3 Valuing cash](#33-valuing-cash)
  - [3.4 The freeze](#34-the-freeze)
  - [3.5 The consumers](#35-the-consumers)
  - [3.6 `PortfolioWatch` — the sweep](#36-portfoliowatch--the-sweep)
  - [3.7 Flows and baselines](#37-flows-and-baselines)
  - [3.8 Concurrency](#38-concurrency)
  - [3.9 The basis change on existing databases](#39-the-basis-change-on-existing-databases)
  - [3.10 Telling the operator](#310-telling-the-operator)
- [4. Rules that are easy to get backwards](#4-rules-that-are-easy-to-get-backwards)
- [5. Tests](#5-tests)
- [6. Order of work](#6-order-of-work)
- [7. Definition of Done](#7-definition-of-done)
- [8. Risks](#8-risks)
- [9. Deliberately out of scope](#9-deliberately-out-of-scope)

---

## 1. Decisions taken

Four questions the phase document left open, settled by the operator on 2026-08-16.

### D1 — the sweep ships with the fix

Slices 1, 2 and 3 land together. The phase document offered Slice 3 (a continuous watchdog sweep)
as deferrable; it is not, and the reason is structural rather than a preference.

A strict staleness rule needs a mark for every **held** instrument. Cycles only mark the instruments
of the basket that is cycling. Without a shared sweep:

- pausing, quarantining or halting a basket that holds a position would leave that position
  unmarkable, freeze the portfolio, and block **every other basket** — a system-wide denial caused
  by a routine operator action;
- an hourly basket's marks would be an hour old when a five-minute basket gates, so the tolerance
  would have to exceed the slowest cadence in the process to avoid constant freezing;
- the first cycle after any restart has no marks at all, and the gate runs *before* the snapshot —
  freeze, therefore no snapshot, therefore no marks, therefore freeze, permanently.

The sweep is what makes mark freshness independent of basket cadence, and therefore what makes the
staleness tolerance a real limit instead of a number chosen to avoid tripping.

### D2 — resolve, then freeze

A balance the system cannot value freezes the aggregate, but only after the valuation has actually
tried. In order: the notional currency at face value; a `USD_STABLECOINS` member at par, subject to
the peg check; the base asset of a configured instrument as an already-counted position; anything
else resolved against its `{CUR}/{notional}` market if the catalogue lists one, and marked like any
other instrument. Only what survives all four and is non-zero freezes, naming the currency.

Strict freeze-on-anything-unknown was rejected on operational grounds: dust in stray coins is
routine on a real spot account, and a rule that stops a live account trading on a residual balance
is fail-useless rather than fail-closed. A declared ignore threshold was rejected because it writes
a fabricated zero into the money path.

### D3 — measure the truth; `risk rearm` is the remedy

No automatic re-baseline. On the first start after the change, startup records one `RISK_EVENT`
naming the stored cost-basis high-water mark, the newly computed mark-to-market equity and the
drawdown that follows, and logs a warning. If that drawdown trips the switch, it is a real loss that
has been open and unmeasured, and the operator clears it with the existing typed-phrase
`tradebot risk rearm` — which already resets both baselines to current equity.

An automatic re-baseline would silently forgive whatever unrealized loss happened to be open at the
moment of the upgrade. That is precisely the laundering `Watchdog.record_flow`'s docstring warns
against, arriving through a migration instead of through a flow.

### D4 — mark the boundary in the promotion report

`report promotion` gains a line naming the point at which valuation changed basis, and counts
cycles either side of it separately. Whether the earlier evidence still counts stays the operator's
call; the report makes it a recorded decision rather than an oversight.

---

## 2. What the implementation audit added

Two findings beyond the four in the phase document, discovered while pinning down signatures. Both
share Finding 2's root cause — a portfolio-wide question answered with a basket-scoped input — and
both are fixed by the same change.

### Finding 5 — the freeze contract has never run

`PortfolioAggregate.frozen` has no consumer anywhere in `tradebot/`. `aggregate()` is called from
exactly one place ([`basket_runner._build_proposal`](../../../tradebot/control/basket_runner.py#L317)),
which reads `gross_exposure` and `exposure_of` and ignores `frozen_reason`. `stablecoin_prices` is
never passed, so `_peg_check` receives an empty map, `peg_deviation_pct` returns zero for every
unquoted currency, and the depeg guard has never fired in production.

`BasketRunner`'s own module docstring lists "a frozen portfolio aggregate → `BLOCKED`" among its
failure semantics. That is not true of the code today. Piece 1 makes it true.

Consequence for the plan: §1.4's "the existing `PortfolioAggregate.frozen` contract, reused rather
than re-invented" is optimistic. The model field is reusable; the enforcement is new.

### Finding 6 — `max_gross_exposure` is enforced against one basket

[`aggregate()`](../../../tradebot/risk/aggregate.py#L98) computes each `VenueSlice.exposure` over the
`instruments` tuple it is handed, and sums those slices into `gross_exposure`. The single caller
passes `self._basket.instruments`. So the limit `GrossExposureRule` documents as "everything
deployed at once, across all baskets and instruments" is in fact this basket's exposure alone, and
every sibling basket's positions are invisible to it.

With one basket in service the two are equal, which is why nothing has surfaced. With two, the
portfolio's gross exposure ceiling can be breached by an arbitrary factor — the exact
Tier-2-cannot-see-a-sibling failure Tier-2 exists to prevent, and the same failure ADR 0026 reasons
about for positions.

The same reasoning applies to `cluster_exposure`: `cluster_members(instrument, self._basket.instruments)`
scopes a cross-basket concentration bucket to one basket. `InstrumentExposureRule`'s docstring
("caps one instrument across *all* baskets") is likewise not currently true.

Fix: every portfolio-wide input is computed over the **configured instrument universe**, not over
the cycling basket's slice. `basket_exposure` stays basket-scoped, because that one is a question
about the basket.

---

## 3. The design

### 3.1 One function

`risk/aggregate.py` becomes the single valuation function. It is extended rather than joined by a
peer, because it already computes equity, gross exposure, per-instrument exposure, per-venue slices
and a freeze reason; adding a second summing path on the money path is exactly the second
implementation DoD 1 forbids.

```python
def aggregate(
    ledgers: Mapping[str, Ledger],
    universe: tuple[Instrument, ...],      # every configured instrument, never one basket's
    marks: Marks,
    policy: GlobalRiskPolicy,
    *,
    as_of: UtcDatetime,
    notional_currency: str,
) -> PortfolioAggregate: ...
```

`PortfolioAggregate` keeps its shape and gains meaning:

| field | change |
|---|---|
| `equity` | cash valued in the notional currency **plus** Σ(qty × mark). Never cost basis. |
| `gross_exposure` | over `universe`, so it spans baskets |
| `per_instrument` | over `universe` |
| `venues` | unchanged; `VenueSlice` already exists and Piece 2 needs it |
| `frozen_reason` | now populated by three causes, and now actually consumed |
| `cash` | **new**: the notional-valued cash total, so the dashboard can show the split |

Deleted from `Ledger`, which stops having an opinion about prices it was never given:

- `Ledger.equity` — every caller moves to `aggregate`;
- `Ledger.unrealized_pnl` — **dead code**, no production caller (`context_builder` uses
  `Position.unrealized_pnl_pct` directly);
- the `prices.get(key, position.avg_entry)` fallback in `Ledger.exposure`, which takes a strict
  `Mapping[str, Decimal]` and raises on a key it was not given. The caller is `aggregate`, which
  has already resolved every mark or frozen.

`Ledger` keeps `positions`, `balance`, `snapshot` and its mutations. It remains the only thing that
knows what is held; it stops being one of the things that knows what it is worth.

### 3.2 `Marks` — the shared price cache

New module `tradebot/ledger/marks.py`.

```python
@dataclass(frozen=True, slots=True)
class Mark:
    price: Decimal
    observed_at: UtcDatetime


class Marks:
    """Current prices for everything the portfolio holds, in the notional currency."""

    def observe(self, key: str, price: Decimal, at: UtcDatetime) -> None: ...
    def observe_quote(self, quote: Quote) -> None: ...          # keyed by quote.instrument_key
    def price_of(self, key: str, *, now: UtcDatetime, tolerance: timedelta) -> Decimal | None: ...
    def age_of(self, key: str, *, now: UtcDatetime) -> timedelta | None: ...
```

`price_of` returns `None` for a key that is absent **or** older than `tolerance`. There is no other
outcome; there is no fallback.

One namespace holds instrument marks and currency marks. Instrument keys are `venue:symbol` and
always contain a colon; currency codes never do. That is what makes one map unambiguous, and it is
stated in the module docstring because a future key format without a colon would silently collide.

`Marks` holds no money authority. It cannot adjust a position, a balance or a baseline, and it has
no write path to the database. It is shared mutable state read on the money path, and the staleness
rule plus the freeze are the only things keeping it honest.

**Staleness tolerance** is a new `GlobalRiskPolicy` field, `mark_staleness_seconds`, default `300`.
It is policy rather than a constant for the reason every other limit is: it is read from the
database, versioned, editable in the dashboard, and a restart cannot clear it. The model validates
it positive.

The *other* validation — that it comfortably exceeds the supervisor's resync interval, since a
tolerance below the sweep cadence is a guaranteed permanent freeze — cannot live in the model:
`core/` depends on nothing, and `DEFAULT_RESYNC_SECONDS` belongs to `control/supervisor.py`. It is
asserted where both numbers are known, at `PortfolioWatch` construction in the composition root,
and refuses to wire rather than discovering it at 03:00.

No Alembic migration: `GlobalRiskPolicy` is a versioned JSON document in `ConfigStore`, not a table.
An already-stored policy simply gains the pydantic default on read, and the next dashboard publish
writes it explicitly as a new version.

### 3.3 Valuing cash

One function, consulted by both `aggregate` and `Watchdog.record_flow`, so a deposit is converted
by exactly the rule that values the balance it lands in.

```python
def value_cash(
    currency: str,
    amount: Decimal,
    marks: Marks,
    *,
    notional_currency: str,
    position_currencies: frozenset[str],
    now: UtcDatetime,
    tolerance: timedelta,
) -> Decimal | None:      # None = no admissible valuation
```

The ladder, in order, first match wins:

1. `currency == notional_currency` → face value.
2. `currency in USD_STABLECOINS` → par (`amount`), subject to `_peg_check`, which now receives real
   `stablecoin_prices` from `Marks` and can therefore freeze for the first time.
3. `currency in position_currencies` → **zero**. This is the base asset of a configured instrument
   and is already counted as a position; valuing it here would double-count it. `position_currencies`
   is `{i.base_currency for i in universe}`, the same set
   [`Reconciler._diff`](../../../tradebot/ledger/reconciler.py#L173) computes as `held_as_positions`,
   extracted to one shared helper so the two cannot drift.
4. a mark for `currency` exists and is fresh → `amount × mark`. Populated by the sweep, which
   resolves `{CUR}/{notional}` against the catalogue for any held currency that reaches this rung.
5. otherwise → `None`, and a non-zero `amount` freezes.

Rung 3 before rung 4 matters: `BTC` is both a configured instrument's base asset and a currency
with a `BTC/USDT` market. Reaching rung 4 first would value the holding twice.

### 3.4 The freeze

Three causes, one reason string, one consumer:

- a **non-flat position** whose mark is absent or stale;
- a **non-zero balance** with no admissible valuation (rung 5 above);
- a **depegged stablecoin** beyond `stablecoin_peg_tolerance_pct` — the existing check, now fed.

A **flat portfolio never freezes.** Pure cash in the notional currency needs no marks, so a fresh
database, the seeded demo basket and `run --once` on a flat ledger are untouched and cost no venue
call. This is what keeps the fix from breaking the zero-configuration path.

`Watchdog.check` takes the aggregate rather than a bare `Decimal`:

```python
async def check(self, valuation: PortfolioAggregate) -> WatchdogVerdict: ...
```

When `valuation.frozen`, the watchdog:

- returns a verdict whose `may_trade` is `False`, carrying `frozen=True` and the reason, so a
  caller can distinguish "we do not know" from "we are halted";
- does **not** trip the kill switch — the switch is for breaches, not for ignorance;
- does **not** raise the high-water mark, does **not** roll the day, does **not** write state. A
  freeze spanning midnight leaves `day_start_equity` at yesterday's value, which measures the daily
  loss from an older and generally higher baseline. That is the conservative direction, and it is
  stated rather than incidental.

`BasketRunner._gate` already turns `not verdict.may_trade` into `BLOCKED` with the reason recorded,
so the runner's docstring becomes true with no new branch.

**The gate stays before the snapshot.** Startup seeding and the sweep guarantee marks exist there,
so a `BLOCKED` cycle still costs nothing — no market data, no panel call.

**A freeze never traps an operator.** Every Tier-1 and Tier-2 rule that reads `equity` or
`basket_budget` already stands aside on `Side.SELL` (`MaxPositionSizeRule`, `MaxBasketAllocationRule`,
`GrossExposureRule`, `InstrumentExposureRule`, `ClusterExposureRule`), and `_size_sell` clamps to the
holding rather than to a budget. A manual close therefore proceeds with equity unknown by
construction, not by a new exemption — consistent with ADR 0015 and with "the switch stops the bot
trading, not a human getting out". Asserted by a test rather than left to inspection.

### 3.5 The consumers

| Call site | Passes today | After |
|---|---|---|
| [`_gate` → `Watchdog.check`](../../../tradebot/control/basket_runner.py#L181) | `{key: avg_entry}` | the aggregate; frozen → `BLOCKED` |
| [`_build_proposal`](../../../tradebot/control/basket_runner.py#L316) | this basket's quotes | the same aggregate, built once per cycle over the universe |
| [`Application.equity()`](../../../tradebot/app.py#L244) | `{key: avg_entry}` | the aggregate |
| [`startup._reconcile`](../../../tradebot/control/startup.py#L206) | `{}` | the aggregate |
| [`startup._arm_first_run`](../../../tradebot/control/startup.py#L321) | `{key: avg_entry}` | the aggregate |
| [`manual_close._proposal`](../../../tradebot/control/manual_close.py#L264) | one fresh quote | that quote pushed to `Marks`, then the aggregate |
| [`risk rearm`](../../../tradebot/__main__.py#L857) + [Control](../../../tradebot/dashboard/routes/control.py#L293) | `{key: avg_entry}` | the aggregate; **refuses while frozen** |
| `PortfolioWatch` | — | the aggregate, every sweep |

`Application.equity()` is **renamed `Application.valuation()`** and returns the aggregate rather
than a `Decimal`, so the dashboard can render either the figure or the reason it has none. A method
still called `equity` that returns a composite would invite exactly the `.equity()` → bare-number
usage this fix exists to remove. Its four callers move with it —
[`monitor.py:83`](../../../tradebot/dashboard/routes/monitor.py#L83),
[`workspace.py:234`](../../../tradebot/dashboard/routes/workspace.py#L234) and `:310`,
[`control.py:293`](../../../tradebot/dashboard/routes/control.py#L293) and
[`__main__.py:857`](../../../tradebot/__main__.py#L857) — and the workspace's risk-control and
portfolio panes gain the freeze state (`_rc.html`, `_portfolio.html`).

`risk rearm` refusing while frozen is deliberate and new: re-arming writes both baselines from
current equity, and doing that from a number the system has just said it cannot compute would
persist a fiction that outlives the outage.

**The instrument universe.** Portfolio-wide inputs need every configured instrument, read fresh
because a basket published mid-run changes it. `app._instruments_of` already computes exactly this
from `ConfigStore` records; it is promoted to a shared helper (`control/reference.py`, which already
reasons about instruments across baskets) and read per cycle at the boundary, like every other
configuration read. `cluster_members` is called with the universe for the same reason.

### 3.6 `PortfolioWatch` — the sweep

New module `tradebot/control/valuation.py`, deliberately shaped like `DriftWatch`.

```
PortfolioWatch.sweep()
  1. resolve the held set: non-flat positions, plus balances reaching rung 4 of value_cash
  2. refresh each through the shared CachingMarketData (single-flight, cached per bar interval)
  3. observe every result into Marks; a failure leaves the previous mark to age out
  4. build the aggregate and hand it to Watchdog.check
```

Two resolution details decide whether a thing can be marked at all:

- **A position needs an `Instrument`** to fetch a quote, and one held in an instrument no longer in
  any basket has none. It cannot be marked, so it freezes. The remedy is to keep the instrument
  configured or close it by hand — the same constraint
  [`manual_close.closable()`](../../../tradebot/control/manual_close.py#L139) already imposes for
  the same reason, and a precedent rather than a new rule.
- **A currency at rung 4 needs a synthetic instrument**, built from the catalogue's
  `{CUR}/{notional}` market through the existing `instrument_for`. That costs one `list_markets`
  call, which is TTL-cached on the catalogue, not one call per currency. A currency the catalogue
  does not list falls to rung 5 and freezes.

It hangs off `Supervisor.serve`'s existing resync tick beside `_check_drift`, guarded the same way
— a raising sweep logs and supervision continues, because a dead supervisor leaves working orders
with nobody polling them and a price refresh is not worth that.

It is exposed on `Application`, like `monitor`, so the backtest harness can drive it between cycles
exactly as it already drives `monitor.poll()`
([`backtest._replay`](../../../tradebot/validation/backtest.py#L220)). Without that, a replay
stepping an hour per tick would freeze on its second cycle.

This is what DESIGN §5 and §6.6 already describe — a watchdog that "monitors the reconciled
portfolio continuously" and can act "without waiting for a cycle" — and it closes the case where the
last basket is paused, halted or between cycles while the market moves.

**Startup seeds marks** before the first cycle, best-effort, over every held instrument, so
`Application.valuation()`, the reconciler's mismatch-tolerance denominator and the first-run baseline
are mark-to-market from the first moment. A venue that will not answer leaves marks absent — which
is a freeze, not a startup failure. An unreachable venue must not halt the process; it must stop it
opening risk, which is what the freeze does.

### 3.7 Flows and baselines

`ExternalFlow` already carries a currency;
[`startup.py:202`](../../../tradebot/control/startup.py#L202) stops discarding it.

```python
async def record_flow(self, flow: ExternalFlow) -> RiskState: ...
```

The watchdog converts through `value_cash` — the same ladder `aggregate` uses — and raises
`FailClosedError` on a currency it cannot value, leaving both baselines untouched. `startup._reconcile`
already converts a `TradebotError` into a recorded failure and leaves the process **up and halted**,
so no new failure path is needed. A baseline adjusted by a number in the wrong unit is worse than no
adjustment, and this is the compound half of the guaranteed spurious trip in Findings 3+4.

The watchdog therefore gains `marks` and `notional_currency` at construction — both are fixed for
the process's life in Piece 1, since `_quote_currency` is derived once in `_assemble` and cannot
move without a restart. The tolerance comes from the policy it already holds.

`position_currencies` is **not** captured at construction. It moves whenever a basket adds an
instrument, and a set fixed at boot is the same defect ADR 0021 fixed for the Tier-2 cap: the
watchdog would convert a flow against a universe the dashboard had already changed. It is refreshed
through a `use_universe(instruments)` setter, called wherever `use_policy` already is
(`RunnerBuilder.build`) and on every sweep — the existing idiom for a long-lived object whose
configuration can move under it.

### 3.8 Concurrency

`Watchdog.check` is read-modify-write over one `risk_state` row: load, compare, save. Under `serve`
it is already called from N concurrent basket tasks, so two cycles raising the high-water mark can
already lose one update. The sweep adds an `N+1`th caller on a fixed cadence and makes the
interleaving routine rather than incidental.

`check`, `trip`, `rearm` and `record_flow` are serialized behind one `asyncio.Lock` on the watchdog.
`SingleWriter` serializes the *write*; it does not make the read-compare-write atomic, and the state
this row holds is the kill switch.

This is a pre-existing latent defect that the sweep would otherwise make likely. It is fixed here
rather than filed, because it is three lines and it is the kill switch.

### 3.9 The basis change on existing databases

No schema migration: nothing about the change alters a table. What changes is the meaning of a
number already stored.

On the first start after the upgrade, `StartupSequence` compares the persisted `high_water_mark`
against the newly computed mark-to-market equity and, when they differ, appends one `RISK_EVENT`
(`rule="valuation_basis"`, `action="recorded"`) naming both figures and the implied drawdown, and
logs a warning. It changes no state. Detected from the log rather than from a flag column, so
nothing new is persisted to remember that it happened — consistent with "anything derivable from the
log is derived".

If the resulting drawdown exceeds the limit, the very next check trips the switch. That is correct
behaviour reporting a real, previously invisible loss, and the operator clears it with
`tradebot risk rearm`. The migration note in `docs/OPERATIONS.md` says exactly this, and says to
read the event before re-arming.

A run against a copy of the existing soak database is part of the DoD, not a suggestion.

### 3.10 Telling the operator

A freeze blocks all new orders. A soak sitting frozen for hours with nobody told is the failure mode
alerting exists for, so it alerts.

- The freeze emits a `RISK_EVENT` **once per transition**, not once per sweep — a per-sweep event
  would flood the log at the resync cadence and bury the transition that matters.
- `ops/rules.py` gains one row: `EventType.RISK_EVENT` joins `ALERT_TYPES` with a narrow rule
  matching the freeze action, and a new `AlertKind.VALUATION_FROZEN`.
- `validation/evidence.py` gains the matching `IncidentKind`, because that module's stated invariant
  is that "what needed a human" has one definition in this codebase rather than an alerting one and
  a reporting one that drift apart. A portfolio that cannot be valued needed a human.

The alternative — a new `CycleOutcome` — was rejected: it would change the vocabulary the promotion
report counts, for a state that is already fully described by `BLOCKED` plus a reason.

---

## 4. Rules that are easy to get backwards

Destined for CLAUDE.md's Phase 12 section on landing.

- **A stale mark is not a mark.** Valuing a position at a four-hour-old price is not more
  conservative than valuing it at cost; it is differently wrong, in whichever direction the market
  moved. The fallback is a freeze, never cost. Re-introducing a fallback anywhere as a "safe
  default" reinstates Finding 1, which is the entire defect.
- **Equity is one function.** A second implementation is a bug by construction, and the reason six
  call sites disagreed is that there were six maps.
- **Freezing blocks new orders; it does not trip the kill switch.** The switch is for breaches, not
  for ignorance. An unvaluable portfolio means stop opening risk and tell the operator.
- **Understating equity is not "conservative".** It tightens percentage ceilings, which reads as
  safe, and simultaneously fabricates drawdown against the high-water mark, which trips the switch.
  There is no safe direction to be wrong in.
- **The high-water mark must move on the same basis it is measured against.** Mixing bases produces
  a phantom drawdown on the first cycle after the change, which is why the transition is announced
  rather than absorbed.
- **Marks are a cache; the ledger is the truth.** Nothing in `Marks` may adjust a position, a
  balance or a baseline.
- **A base asset is a position, not cash.** Rung 3 precedes rung 4 in `value_cash` for that reason;
  reversing them double-counts every holding on a spot venue.
- **A portfolio-wide limit reads the configured universe, never one basket's instruments.** That is
  Finding 6, and `gross_exposure`, `per_instrument` and `cluster_members` all take the universe;
  only `basket_exposure` stays basket-scoped.
- **A freeze never blocks a reduce-only operator exit**, and does so by construction — every rule
  that reads equity already stands aside on `SELL`.

---

## 5. Tests

The seam is what failed, so these are seam tests. Unit-testing `Watchdog.check` harder would not
have caught any of the six findings, because its arithmetic was never wrong.

From the phase document §1.5. It enumerates **ten**, though its DoD item 5 calls them eleven; the
list below is the ten as written, and the count in that DoD is corrected when it is restated here:

1. an **unrealized** 15% loss driven through `BasketRunner` trips the drawdown kill switch
2. an unrealized 4% loss driven through `BasketRunner` halts orders for the day
3. an unrealized *gain* raises the high-water mark; a later give-back is measured against it
4. two baskets in service compute the **same** equity for one instant, equal to the mark-to-market
   total (Finding 2) — present by name
5. a ledger holding 1,000 USDT and 9,000 USDC values at 10,000 (Finding 3)
6. a 9,000 USDC deposit raises the baselines **and** equity by 9,000 — net drawdown zero, no trip
   (Findings 3+4, the compound case) — present by name
7. a balance with no admissible valuation freezes the aggregate, blocks new orders, and does **not**
   trip the switch
8. a mark older than the tolerance freezes rather than falling back to cost
9. `Application.valuation()`, the dashboard figure and the watchdog's input are the **same number**
   for one instant, asserted directly
10. property: for any set of fills and any mark map, `equity == cash_in_notional + Σ(qty × mark)`;
    no branch returns cost basis

Added by this design:

11. **the boundary test** (DoD 2), in the manner `test_dashboard_chart.py` asserts the float
    boundary: no module in `ledger/` or `risk/` uses a position's cost as a price fallback. A
    structural assertion, because a comment saying "do not add a fallback" is not enforcement.
12. a **paused** basket holding a position does not freeze the portfolio, and the other baskets keep
    cycling (D1 — the coupling that made the sweep mandatory)
13. a **halted** and a **quarantined** basket, same assertion
14. a manual close succeeds while the aggregate is frozen (ADR 0015 preserved)
15. `risk rearm` refuses while frozen, and the baselines are unchanged
16. `record_flow` refuses a currency it cannot value, leaves both baselines untouched, and leaves
    the process up and halted
17. a flat portfolio never freezes, and takes no venue call to value
18. an unreachable venue at startup freezes rather than failing startup; the process comes up, the
    dashboard renders the reason, and the freeze clears on its own when the venue answers
19. two baskets over one venue: `gross_exposure` spans both, and a Tier-2 gross ceiling is enforced
    against the portfolio total (Finding 6)
20. `Watchdog.check` under concurrent callers does not lose a high-water-mark raise (§3.8)
21. a backtest over recorded history completes without freezing (the harness drives the sweep)
22. the basis-change `RISK_EVENT` is emitted once, changes no state, and a run against a copy of the
    existing soak database is exercised (DoD 7)

---

## 6. Order of work

Each step leaves a working system and is independently reviewable.

**Step 1 — `Marks` and `value_cash`.** New `ledger/marks.py`, the cash ladder, the shared
`base_currencies_of` helper, `mark_staleness_seconds` on `GlobalRiskPolicy` with its positive-value
validator. Pure addition; nothing consumes it yet. Tests 5, 10, 17.

**Step 2 — `aggregate` becomes the valuation.** Extend `aggregate`, delete `Ledger.equity` and
`Ledger.unrealized_pnl`, make `Ledger.exposure` strict, feed `_peg_check`. Every caller breaks until
Step 3 moves them, which is the intended forcing function. Tests 7, 8.

**Step 3 — the consumers.** All eight call sites, `Watchdog.check`'s new parameter, the freeze
verdict, `Application.valuation()` and its four callers, the dashboard panes, the universe helper.
Tests 1, 2, 3, 4, 9, 14, 15, 19. *Closes Findings 1, 2, 3, 5, 6.*

**Step 4 — flows.** `record_flow(flow)`, currency-aware, refusing; `use_universe`. Tests 6, 16.
*Closes Finding 4.*

**Step 5 — the sweep.** `PortfolioWatch`, startup seeding, the supervisor tick, the backtest hook,
the wiring-time tolerance-versus-cadence assertion, the watchdog lock. Tests 12, 13, 18, 20, 21.

**Step 6 — operator surfaces and records.** The basis-change event, the alert row, the incident
kind, the promotion-report boundary line, `docs/OPERATIONS.md`, DESIGN §6.6 and §8.1, ADR 0027,
CLAUDE.md, and the `IMPLEMENTATION_PLAN.md` risk-register rows. Test 22.

---

## 7. Definition of Done

The phase document's §1.6, restated with what this design adds, plus the standing per-module DoD in
[PLAN §6](../../../IMPLEMENTATION_PLAN.md#6-definition-of-done-applies-to-every-module).

1. **One function answers "what is the portfolio worth".** `grep` finds no second implementation,
   and every consumer in §3.5 calls it.
2. **No code path values a position at `avg_entry` as a fallback**, enforced by test 11.
3. **Every balance is valued, counted as a position, or freezes the aggregate.** No balance is
   silently worth zero.
4. **`record_flow` is currency-aware** and refuses rather than guessing.
5. **All twenty-two tests in §5 pass**, with tests 4, 6, 11, 12 and 19 present by name.
6. **Coverage gates hold**: `core/`, `risk/`, `execution/`, `ledger/` ≥ 95%; everything else ≥ 80%.
7. **A run against a copy of the existing soak database is exercised**, and the basis-change event
   is present in its log.
8. **DESIGN §6.6's "mark-to-market" claim is true of the code**, and §8.1 gains a row for
   "portfolio cannot be valued → aggregate frozen, no new orders, no trip".
9. **`BasketRunner`'s docstring claim about a frozen aggregate is true** (Finding 5).
10. **`GrossExposureRule` and `InstrumentExposureRule`'s "across all baskets" claims are true**
    (Finding 6).
11. **ADR 0027 records the decision**, including the freeze-on-unvaluable rule and its deliberate
    divergence from fallback-to-cost.
12. **`IMPLEMENTATION_PLAN.md` R16, R17 and R18 move from "none today" to the shipped mitigation**,
    and R19/R20 are added for Findings 5 and 6.
13. **CLAUDE.md gains the Phase 12 Piece 1 section** with §4's rules.
14. `.\check.ps1` clean.

---

## 8. Risks

- **`Marks` is shared mutable state read on the money path.** The staleness rule and the freeze are
  what keep it honest; a "helpful" fallback added later is how Finding 1 returns. Test 12 is the
  structural defence, and it is a DoD item rather than a nicety.
- **The tolerance and the sweep cadence are coupled.** A tolerance below the resync interval is a
  permanent freeze. Refused at publish by the policy validator, but the coupling is real and is
  documented at both ends.
- **Re-baselining on the existing soak database.** A soak currently holding unrealized losses may
  legitimately trip on the first sweep. The operator is told before it runs; see §3.9.
- **The soak's existing promotion evidence was gathered with the drawdown gate ineffective.** D4
  makes the boundary visible; whether the earlier cycles count is the operator's recorded call.
- **`aggregate` gains callers on the request path** (the dashboard, every sweep). It is pure
  in-memory arithmetic over the ledger and a dict, with no I/O, so cost is bounded by position count
  — but the venue refresh inside `PortfolioWatch` is I/O and belongs to the sweep, never to a page
  render. Phase 10's "only the chart data route awaits the venue" stands.
- **Deleting `Ledger.equity` touches every test that used it.** Expected and intended; a large test
  diff here is the fix reaching the seam that had no test.

---

## 9. Deliberately out of scope

Piece 2 and its prerequisites are untouched. In particular this design does **not**:

- change `app._quote_currency` — the notional currency stays *derived* and single-valued. Declaring
  USD as notional is Piece 2 Stage C;
- introduce a second venue, a second ledger, or a second stack;
- add an equity market-data provider or an Alpaca catalogue (Piece 2 Stage A);
- add FX, non-USD notional currencies, or dynamic correlation clusters;
- relax ADR 0026, which is unaffected and still correct.

`aggregate` already takes `Mapping[str, Ledger]` and already emits a `VenueSlice` per venue. It is
called with one entry today and stays that way here; Piece 2 supplies the second.
