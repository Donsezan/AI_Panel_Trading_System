# Phase 12 — one portfolio, valued in one notional currency

> Authoritative specs remain [DESIGN.md](../DESIGN.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
> This records what was asked for, what an audit of the current code found, how it should be
> solved, and what "done" means. Conventions that outlive it move to [CLAUDE.md](../CLAUDE.md);
> decisions move to `docs/adr/`. Written before any code changes, per the standing rule that a
> change touching the money path gets a design pass first.

**Status: proposed. Nothing here is built.** The audit findings in Piece 1 are **live defects in the
running crypto-only system** and are independent of whether Piece 2 is ever built.

---

## Table of Contents

- [The idea](#the-idea)
- [Why this is two pieces](#why-this-is-two-pieces)
- [Piece 1 — Equity is mark-to-market in the notional currency](#piece-1--equity-is-mark-to-market-in-the-notional-currency)
  - [1.1 The idea](#11-the-idea)
  - [1.2 The problem](#12-the-problem)
  - [1.3 The solution](#13-the-solution)
  - [1.4 Rules that are easy to get backwards](#14-rules-that-are-easy-to-get-backwards)
  - [1.5 Tests](#15-tests-must-have-not-nice-to-have)
  - [1.6 Definition of Done](#16-definition-of-done)
- [Piece 2 — Crypto and equity in one basket and one portfolio](#piece-2--crypto-and-equity-in-one-basket-and-one-portfolio)
  - [2.1 The idea](#21-the-idea)
  - [2.2 The problem](#22-the-problem)
  - [2.3 The solution](#23-the-solution)
  - [2.4 Rules that are easy to get backwards](#24-rules-that-are-easy-to-get-backwards)
  - [2.5 Tests](#25-tests-must-have-not-nice-to-have)
  - [2.6 Definition of Done](#26-definition-of-done)
- [Slices](#slices)
- [Risks](#risks)
- [Recommendations beyond the ask](#recommendations-beyond-the-ask)
- [Open questions for the operator](#open-questions-for-the-operator)
- [Appendix — audit evidence](#appendix--audit-evidence)

---

## The idea

Stated by the operator, 2026-08-15:

> Crypto and equity could be in one basket for the purpose of **diversification and hedging**. So
> yes, it will affect risk control and portfolio aggregation. **All assets shall be evaluated in the
> notional currency (which is USD)**, so the portfolio shows value in notional and all calculations
> use it — it simplifies logic and minimises complexity from working with different
> currencies / markets / products.

Two claims are being made, and they are separable:

1. **A valuation claim.** There is exactly one number that means "what is this portfolio worth", it
   is denominated in USD, and every percentage-based risk limit is a share of *that* number. This is
   a simplification decision, and it is the right one: the alternative is per-currency equity
   buckets, per-currency drawdown baselines, and a kill switch that has to decide which currency it
   tripped in.
2. **A composition claim.** One basket may hold instruments from more than one asset class and more
   than one venue, because that is what makes a hedge expressible as a basket rather than as two
   baskets an operator has to keep in step by hand.

The audit that produced this document was asked to verify both. It found that **claim 1 is not true
of the current code**, in four distinct ways, and that **claim 2 is refused by the current code**, in
four distinct places.

## Why this is two pieces

The refusals against claim 2 are correct, layered, and fail closed. Nothing is silently wrong about
mixed baskets — they simply do not run. That is a *missing feature*.

The defects against claim 1 are a different thing entirely: they are wrong **now**, on the
crypto-only paper soak that is currently gathering promotion evidence, and the most severe of them
disables the drawdown kill switch. That is a *live defect*.

So the ordering is not a preference. Piece 1 must land first, because Piece 2 multiplies every
valuation path it touches — building multi-venue aggregation on top of an equity function that
cannot see unrealized PnL would bake the defect into four more call sites and make it far harder to
see.

| | Piece 1 | Piece 2 |
|---|---|---|
| Nature | Live defect | Missing feature |
| Affects today's soak | **Yes** | No |
| Blocks the other | Yes | No |
| Classification | Bounded change | Architectural |
| Needs new ADR | Yes (one) | Yes (two or three) |

---

# Piece 1 — Equity is mark-to-market in the notional currency

## 1.1 The idea

One function answers "what is this portfolio worth in USD right now", and **every** consumer reads
it: the Tier-2 watchdog's drawdown and daily-loss baselines, Tier-1's basket budget, Tier-2's
exposure ceilings, the dashboard's equity figure, `risk rearm`'s new high-water mark, and the
reconciler's mismatch tolerance. Its inputs are current marks for every position and the value of
every cash balance.

DESIGN already requires this. §6.6: the daily-loss boundary is "computed on **mark-to-market
equity**". §5: the Global Risk Manager "monitors the reconciled portfolio continuously".

## 1.2 The problem

Four findings, verified by running the real classes rather than by reading them. Evidence is in the
[appendix](#appendix--audit-evidence).

### Finding 1 (critical) — the drawdown kill switch cannot see unrealized loss

[`BasketRunner._equity()`](../tradebot/control/basket_runner.py#L186-L193) builds its price map as
`{instrument_key: position.avg_entry}` — every position valued at *exactly its own cost*.
[`Ledger.equity`](../tradebot/ledger/portfolio.py#L194-L207) then computes
`market_value(prices.get(key, avg_entry))`, so the fallback and the supplied value are the same
number. The result is the cost basis, by construction, always.

That number is the sole argument to `Watchdog.check`, and
[basket_runner.py:181](../tradebot/control/basket_runner.py#L181) is its **only** call site in the
system. `Supervisor` runs no independent sweep, so there is no second path where a real mark could
arrive.

Verified: 10,000 USDT, buy 0.1 BTC @ 50,000, BTC halves to 25,000.

```
equity @cost   : 10000.0     <- what the watchdog sees
equity @mkt/2  : 7500.0      <- the truth (-25%)
```

Drawdown reads **0%** against a 10% limit. The kill switch measures *realized* drawdown plus fee
bleed and nothing else. A portfolio 40% underwater in open positions keeps trading, and the
high-water mark only ever rises on realized gains.

The correct marks already exist eleven lines further down —
[`_build_proposal`](../tradebot/control/basket_runner.py#L315-L316) builds them from
`snapshot.instruments`. They are simply constructed *after* the gate has already run, and used only
for sizing.

The same cost-basis map appears in [`Application.equity()`](../tradebot/app.py#L243-L247), which
feeds the dashboard's equity figure, `risk rearm` and the Control page's re-arm — so re-arming after
an incident sets the new high-water mark to cost basis — and in
[`startup.py:206`](../tradebot/control/startup.py#L206), where `equity({})` is the reconciler's
mismatch-tolerance denominator, and [`startup.py:321-324`](../tradebot/control/startup.py#L321-L324),
which establishes the first-run baselines.

### Finding 2 (high) — equity is basket-dependent

[`_build_proposal`](../tradebot/control/basket_runner.py#L315) maps only *this basket's* instruments,
but `Ledger.equity` iterates **every** position in the ledger, falling back to `avg_entry` for the
rest. With two baskets in service, basket A and basket B compute different portfolio equity for the
same instant, and neither is correct.

Every Tier-1 and Tier-2 percentage is a share of that number: `basket_budget`, `max_gross_exposure`,
`max_instrument_exposure`, `max_cluster_exposure`. Tier-2 exists precisely to give the portfolio one
view that no individual basket has; this gives it N views, one per basket, each blind to the others'
marks.

### Finding 3 (medium; a live kill-switch hazard) — non-quote cash is invisible

`Ledger.equity` returns `balance(quote_currency) + holdings`. Any balance in another currency
contributes nothing. Verified: 1,000 USDT + 9,000 USDC → equity **1,000**.

On a real Binance account this is routine — USDC, FDUSD or BUSD alongside USDT, or a dust
conversion. A 9,000 USDT→USDC move on a 10,000 account reads as a 90% instant drawdown. Finding 1
does not mask this, because cash is not a position.

The knowledge to fix it is already in the codebase and unused by `equity`:
[`aggregate.USD_STABLECOINS`](../tradebot/risk/aggregate.py#L30) enumerates exactly the set that
should be valued at par, and `_peg_check` already reads the ledger's balances to falsify that
assumption.

### Finding 4 (high) — external-flow baselines are currency-blind

[`Reconciler.apply_external_flows`](../tradebot/ledger/reconciler.py#L372-L381) yields
`ExternalFlow(currency=d.scope, amount=d.delta, …)` where `scope` is a currency name.
[`startup.py:201-202`](../tradebot/control/startup.py#L201-L202) then calls
`record_flow(flow.amount, flow.reason)` — **the currency is dropped**, and
[`Watchdog.record_flow`](../tradebot/risk/watchdog.py#L199-L216) adds the bare amount to the
high-water mark and day-start equity, both of which are denominated in the notional currency.

Base currencies of configured instruments are excluded from the balance diff
([reconciler.py:173-176](../tradebot/ledger/reconciler.py#L173-L176)), so this cannot fire on BTC in
a BTC/USDT basket. It fires on everything else: a stablecoin the account holds but does not quote in,
or an airdropped asset with no configured instrument.

**Findings 3 and 4 compound into a guaranteed spurious trip.** A 9,000 USDC deposit raises the
high-water mark by 9,000 (Finding 4) while contributing 0 to equity (Finding 3). The very next
watchdog check sees a 9,000 drawdown that never happened, and trips the kill switch — which then
requires a typed phrase from a human to clear.

### Why the tests did not catch any of this

[`tests/unit/test_watchdog.py`](../tests/unit/test_watchdog.py) injects equity as a literal
(`await watchdog.check(Decimal("8500"))`). The watchdog's own arithmetic is correct and well tested.
Nothing anywhere asserts **what the caller passes it**. The defect lives entirely in the seam, and
the seam has no test.

## 1.3 The solution

**One valuation module, and every consumer reads it.** The shape below is deliberately small: it
mostly moves a map that already exists to where the other callers can see it, and teaches `equity`
about cash it already stores.

### 1.3.1 A `Marks` source, owned above the runner

Introduce one object that answers "the current mark for every instrument the portfolio holds, in the
notional currency". It is fed by the same snapshot quotes the runner already fetches, and it is
shared, because equity is a property of the portfolio and not of a basket (DESIGN §4 — the same
reason the ledger is shared).

```
Marks           instrument_key → last price, in the notional currency, with observed_at
  .update(snapshot)     every cycle, from the quotes that cycle already paid for
  .valuation()          the map Ledger.equity consumes
```

Three properties do the work:

- **It is written by every basket and read by all of them**, so basket A's cycle refreshes the mark
  basket B's ledger valuation uses. That is what closes Finding 2.
- **A mark carries `observed_at` and ages.** A mark older than a configured tolerance is *not* a
  mark — see the staleness rule in [§1.4](#14-rules-that-are-easy-to-get-backwards).
- **It holds no money authority.** It is a price cache, not a ledger; the ledger stays the only
  thing that knows what is held.

### 1.3.2 `Ledger.equity` values all cash, not one currency

Replace the `quote_currency: str` parameter with the notional currency plus a per-currency
valuation:

- a balance whose currency is in `USD_STABLECOINS` is valued at par, subject to the existing
  `_peg_check` — this is the same rule `aggregate.py` already applies, applied one layer lower;
- a balance whose currency is the base asset of a configured instrument is **already counted as a
  position** and must not be double-counted — the reconciler's `held_as_positions` set
  ([reconciler.py:173-176](../tradebot/ledger/reconciler.py#L173-L176)) is exactly this set and
  should be shared rather than recomputed;
- any other currency has no admissible valuation in v1 and **freezes the aggregate**, exactly as a
  depeg does. Fail closed: an unvaluable balance means "we do not know what this portfolio is
  worth", and the answer to that is never "trade anyway".

### 1.3.3 `record_flow` takes a currency

`ExternalFlow` already carries one; [startup.py:202](../tradebot/control/startup.py#L202) simply
stops discarding it. `Watchdog.record_flow` converts to notional through the same valuation used by
`equity`, and **refuses** — leaving the baselines untouched and halting — on a currency it cannot
value. A baseline adjusted by a number in the wrong unit is worse than no adjustment.

### 1.3.4 The watchdog gets a sweep of its own

DESIGN §5 and §6.6 both describe a watchdog that "monitors the reconciled portfolio continuously"
and can act "without waiting for a cycle". Today it is a per-cycle gate. With `Marks` shared, a sweep
on the supervisor's existing resync tick — the same loop
[`DriftWatch`](../tradebot/control/reference.py#L275) hangs off, per Phase 11's *What implementation
changed* — is a small addition and closes the case where the last basket is paused, halted, or
between cycles while the market moves.

This is the one part of Piece 1 that is genuinely new behaviour rather than a correction. It may be
deferred to its own slice, but it should not be dropped: without it, a fully halted system stops
measuring its own drawdown.

## 1.4 Rules that are easy to get backwards

- **A stale mark is not a mark.** Valuing a position at a price from four hours ago is not more
  conservative than valuing it at cost — it is differently wrong, and it is wrong in whichever
  direction the market moved. A stale or absent mark must **freeze the aggregate**, which blocks new
  orders, rather than silently falling back to `avg_entry`. The current fallback-to-cost is the
  entire mechanism of Finding 1, and re-introducing it as a "safe default" anywhere would reinstate
  the defect.
- **Freezing blocks new orders; it does not trip the kill switch.** The kill switch is for breaches,
  not for ignorance. An unvaluable portfolio means stop opening risk and tell the operator — the
  existing `PortfolioAggregate.frozen` contract, reused rather than re-invented.
- **Equity understating is not "conservative".** It tightens percentage ceilings, which reads as
  safe, and simultaneously fabricates drawdown against the high-water mark, which trips the switch.
  There is no safe direction to be wrong in; there is only correct.
- **The high-water mark must move on the same basis it is measured against.** Mixing bases — a mark
  raised on cost-basis equity and compared against mark-to-market equity, or vice versa — produces a
  phantom drawdown on the first cycle after the change. Any migration must re-baseline explicitly,
  and say so in the log.
- **Marks are a cache; the ledger is the truth.** Nothing in `Marks` may ever adjust a position, a
  balance, or a baseline.

## 1.5 Tests (must-have, not nice-to-have)

The seam is what failed, so the tests are seam tests. Unit-testing `Watchdog.check` harder would not
have caught any of the four findings.

- an **unrealized** 15% loss, driven through `BasketRunner`, trips the drawdown kill switch
- an unrealized 4% loss, driven through `BasketRunner`, halts orders for the day
- an unrealized *gain* raises the high-water mark; a subsequent give-back is measured against it
- two baskets in service compute the **same** equity for the same instant, and it equals the
  mark-to-market total (Finding 2)
- a ledger holding 1,000 USDT and 9,000 USDC values at 10,000 (Finding 3)
- a 9,000 USDC deposit adjusts the baselines by 9,000 **and** raises equity by 9,000 — net drawdown
  zero, no trip (Findings 3+4 together, the compound case)
- a balance in a currency with no admissible valuation freezes the aggregate and blocks new orders,
  and does **not** trip the switch
- a mark older than the staleness tolerance freezes the aggregate rather than falling back to cost
- `Application.equity()`, the dashboard figure, and the watchdog's input are the **same number** for
  the same instant — asserted directly, because three call sites drifting apart is what happened
- a property test: for any set of fills and any price map, `equity == cash_in_notional +
  Σ(qty × mark)`; no branch returns cost basis

## 1.6 Definition of Done

Piece 1 is done when all of the following hold, in addition to the standing per-module DoD in
[PLAN §6](../IMPLEMENTATION_PLAN.md#6-definition-of-done-applies-to-every-module).

1. **One function answers "what is the portfolio worth".** `grep` finds no second implementation, and
   every consumer named in [1.1](#11-the-idea) calls it — watchdog, Tier-1 budget, Tier-2 ceilings,
   dashboard, `risk rearm`, reconciler tolerance.
2. **No code path values a position at `avg_entry` as a fallback.** The fallback is a freeze. Enforced
   by a test, in the manner `test_dashboard_chart.py` enforces the float boundary.
3. **Every balance the ledger holds is either valued, counted as a position, or freezes the
   aggregate.** No balance is silently worth zero.
4. **`record_flow` is currency-aware** and refuses rather than guessing.
5. **All eleven tests in [1.5](#15-tests-must-have-not-nice-to-have) pass**, and the two-basket
   agreement test and the compound Findings 3+4 test are present by name.
6. **Coverage gates hold**: `ledger/` and `risk/` remain ≥ 95%.
7. **A migration note exists** for the high-water-mark basis change, and a run against an existing
   soak database is exercised: the first sweep after the change must not trip the switch on the
   basis change alone.
8. **DESIGN §6.6's "mark-to-market" claim is true of the code**, and §8.1 gains a row for "portfolio
   cannot be valued → aggregate frozen, no new orders".
9. **An ADR records it** — see [Recommendations](#recommendations-beyond-the-ask).
10. `.\check.ps1` clean.

---

# Piece 2 — Crypto and equity in one basket and one portfolio

## 2.1 The idea

An operator can put `BTC/USDT` and `AAPL` in one basket, because the hedge is the *relationship*
between them and a basket is the unit the panel deliberates over. The portfolio sums both in USD.
Tier-2 sees one gross exposure across both venues. Correlation clusters keep the crypto sleeve and
the equity sleeve in separate buckets, which is what makes the diversification claim checkable rather
than asserted.

## 2.2 The problem

### 2.2.1 It is blocked today, in four independent places

All four fail closed. Nothing is silently wrong; the feature simply does not exist.

| # | Gate | Where | Behaviour |
|---|---|---|---|
| 1 | Single quote currency per process | [app.py:785-798](../tradebot/app.py#L785-L798) | `ConfigError` at wiring if the instrument union spans more than one quote currency |
| 2 | One venue per process | [app.py:876-905](../tradebot/app.py#L876-L905) | Exactly one `VenueStack`, one `Ledger`, one `ExecutionService`, one `ExecutionMonitor`, one `Reconciler`, one calendar |
| 3 | Venue/catalogue match | [reference.py:185-190](../tradebot/control/reference.py#L185-L190) | Refuses any instrument whose `venue != catalogue.venue_id`, at publish and on every `DriftWatch` sweep |
| 4 | Equities are unwired | [app.py:553-590](../tradebot/app.py#L553-L590) | `_alpaca_stack` raises without an equity market-data provider — none exists — and its catalogue is `UnavailableCatalogue`, so every equity instrument is a drift finding and the basket halts |

Verified for gate 1:

```
_quote_currency(BTC/USDT, AAPL) REFUSED: ConfigError every basket in one process must
share a quote currency, found ['USD', 'USDT']
```

[`RunnerBuilder.calendar_for`](../tradebot/app.py#L671) carries the marker plainly:
`# noqa: ARG002 — one venue in v1`.

### 2.2.2 What is already right, and should not be rebuilt

The domain model anticipated this. Do not redesign these:

- `Instrument.asset_class` exists, and keys are `venue:symbol`, so a position already knows its venue.
- [`GlobalRiskPolicy.clusters`](../tradebot/core/config.py#L211-L216) already ships a `crypto` bucket
  and an `equities` bucket covering `EQUITY` and `INDEX_ETF`.
- [`risk/aggregate.py`](../tradebot/risk/aggregate.py#L72-L109) already takes
  `Mapping[str, Ledger]` and already emits `VenueSlice` per venue. **It was built for N venues and
  is only ever called with one.**
- `Scheduler` already resolves the earliest candidate that lands in an open session, for any
  calendar.
- ADR 0026's one-basket-per-instrument rule is unaffected and still correct.

### 2.2.3 What genuinely has to change

1. **N venue stacks, and routing by `instrument.venue`.** `ExecutionService`, `ExecutionMonitor` and
   `Reconciler` are each constructed around one broker. Each needs to resolve the broker from the
   instrument, or be instantiated per venue behind a router.
2. **N ledgers behind the aggregate.** One `Ledger` per venue portfolio, which is what `aggregate`
   already expects. Positions stay keyed by `instrument_key`, which already carries the venue.
3. **A notional currency that is declared, not derived.** `_quote_currency` derives the account
   currency from the instruments and refuses on disagreement. It becomes: the notional currency is
   USD; each instrument's quote currency must be *valuable* in USD; anything else refuses at publish.
   This is Piece 1's valuation, applied at configuration time.
4. **Per-instrument trading calendars inside one cycle.** Today a basket has one schedule and one
   calendar. A mixed basket needs BTC to keep cycling overnight while `AAPL`'s session is shut — see
   the [open question](#open-questions-for-the-operator), which the operator must settle.
5. **Tier-2's per-venue / cross-venue split, finally built.** DESIGN §6.6 specifies per-venue hard
   limits checked synchronously plus cross-venue aggregate rules enforced by the watchdog. With one
   venue the distinction is invisible; with two it is the whole point. `Tier2RiskEngine` is currently
   one instance with one policy reading a one-ledger aggregate.
6. **An equity market-data provider and an Alpaca catalogue.** This is Phase 3 work that was never
   done and is the hard prerequisite for everything else — without it, gate 4 stands and nothing in
   Piece 2 can be exercised end to end. `AlpacaMarketData` plus a real `VenueCatalogue` for Alpaca.
7. **Corporate actions become load-bearing.** `AlpacaAnnouncements` exists and the reconciler already
   classifies `CORPORATE_ACTION`, but no soak has ever exercised it. A split that the reconciler
   fails to match halts a basket.

## 2.3 The solution

Build it in the order that keeps the system working at every step, and **do not** start before
Piece 1 has landed.

**Stage A — equity market data.** `AlpacaMarketData` implementing `MarketDataProvider`, and an
Alpaca `VenueCatalogue` replacing `UnavailableCatalogue`, both under the existing contract suites
([`tests/contract/test_catalogue_contract.py`](../tests/contract/test_catalogue_contract.py)). This
removes gate 4 and is independently valuable: it makes an equity-only basket possible for the first
time, which is a far smaller step than a mixed one and proves the adapter before anything depends on
it.

**Stage B — multi-venue portfolio.** N stacks, N ledgers, a venue router in front of execution and
reconciliation, and `aggregate` called with the real map it was designed for. Tier-2 splits into
per-venue instances plus cross-venue watchdog rules. Removes gate 2.

**Stage C — the notional currency contract.** USD as declared notional; per-instrument quote-currency
admissibility checked at publish; removes gate 1 and gate 3's currency dimension. Gate 3's *venue*
dimension is not removed — it becomes "the catalogue for *this instrument's* venue", which is the
correct form of the same check.

**Stage D — mixed baskets.** Per-instrument calendars within a cycle, the panel told which
instruments are currently untradeable and why, and a Tier-1 rule that vetoes an order to a shut
venue. Only now does a basket holding both actually run.

## 2.4 Rules that are easy to get backwards

- **A closed venue is not a data fault.** An instrument whose venue is shut must be excluded from the
  actionable set *and named in the snapshot as excluded*, not dropped. A basket that silently shows
  the panel two instruments on Monday and one on Saturday is a basket whose decisions cannot be
  compared across days.
- **"Untradeable now" must be vetoed in Tier-1, not merely omitted upstream.** Omission is a
  presentation choice and one refactor away from being lost; a veto is a tested rule that records
  itself. Same reasoning as ADR 0022 for quarantine.
- **One instrument still belongs to exactly one basket** (ADR 0026). Mixed baskets do not relax it —
  they make it more important, because a hedge held by two baskets is two writers of one position.
- **Per-venue Tier-2 limits are not the aggregate's limits, and neither replaces the other.** A venue
  cannot block another venue's order synchronously; that is why DESIGN splits them. Collapsing them
  into one check is the tempting simplification and it is wrong in both directions.
- **The correlation cluster is what carries the diversification claim.** If crypto and equity share
  a bucket, the mixed basket is a concentration mechanism wearing a hedge's clothes. The default
  clusters already separate them; a publish that puts them in one bucket should be hard to do by
  accident.
- **USDT is not USD, it is USD-par-until-falsified.** The existing peg check is the falsification and
  must keep running once equity valuation depends on it more heavily, not less.

## 2.5 Tests (must-have, not nice-to-have)

- an equity-only basket runs a full cycle on Alpaca paper (Stage A exit)
- the catalogue contract suite passes for Alpaca exactly as for Binance and sim
- an aggregate over two venue ledgers equals the sum of the two, per instrument and in total
- a Tier-2 gross-exposure ceiling is enforced against the **cross-venue** total, not one venue's
- an order for `alpaca:AAPL` is routed to the Alpaca broker and never to Binance, and vice versa —
  asserted directly, because a mis-route is an order at the wrong venue
- a mixed basket cycles overnight, decides on its crypto leg, and **vetoes** its equity leg with a
  recorded reason naming the closed session
- the same basket at 15:00 UTC acts on both legs
- an instrument quoted in a currency with no admissible USD valuation is refused at publish, naming
  the currency
- a stock split during a soak is matched against announcements and adjusts the ledger without halting
- a venue outage on one venue does not stop cycling on the other, and does not freeze the aggregate
  where marks remain fresh

## 2.6 Definition of Done

In addition to [PLAN §6](../IMPLEMENTATION_PLAN.md#6-definition-of-done-applies-to-every-module):

1. **A basket holding `binance:BTC/USDT` and `alpaca:AAPL` can be created in the dashboard, published,
   and run** — the end-to-end criterion, asserted in `tests/scenario/`.
2. **One equity figure spans both venues**, and the dashboard shows the USD total with a per-venue
   breakdown (`VenueSlice` already exists for this).
3. **Tier-2 enforces per-venue limits synchronously and cross-venue limits through the watchdog**,
   and a test proves an order is shrunk by the *aggregate* ceiling and not only its own venue's.
4. **No module outside `app.py` names a broker, a calendar or a catalogue.** Routing is by
   `instrument.venue`, resolved from a map built at composition.
5. **Gate 3 survives in its correct form**: every instrument is still verified against the catalogue
   for *its own* venue, at publish and on every `DriftWatch` sweep.
6. **A mixed basket's closed-venue behaviour is a recorded Tier-1 veto**, visible in the event log and
   on the workspace, never a silent omission.
7. **All ten tests in [2.5](#25-tests-must-have-not-nice-to-have) pass.**
8. **DESIGN §4, §6.1, §6.6 and §8.1 are updated** to describe the multi-venue portfolio as built,
   including a §8.1 row for "instrument's venue is closed".
9. **CLAUDE.md gains the Phase 12 section** with the rules from [2.4](#24-rules-that-are-easy-to-get-backwards).
10. **A paper soak of at least one full week runs a mixed basket** before any consideration of live,
    with `report promotion` clean. Live remains disarmed.
11. `.\check.ps1` clean; coverage gates hold.

---

## Slices

Ordered so that each leaves a working system, and so that correctness lands before capability.

**Slice 1 — valuation core.** `Marks`, `Ledger.equity` over all balances, the freeze path, and every
consumer moved onto one function. Closes Findings 1, 2 and 3. *Exit: an unrealized 15% loss trips the
drawdown switch, and two baskets agree on equity.*

**Slice 2 — flows and baselines.** Currency-aware `record_flow`, the migration note, and the
re-baselining run against an existing soak database. Closes Finding 4. *Exit: a 9,000 USDC deposit
raises equity and the baselines together, net zero drawdown.*

**Slice 3 — the watchdog sweep.** The continuous sweep DESIGN §5 describes, on the supervisor's
existing resync tick. *Exit: a fully paused system still measures its drawdown.*

**Slice 4 — equity market data.** `AlpacaMarketData` and the Alpaca catalogue, under the existing
contract suites. *Exit: an equity-only basket completes a cycle on Alpaca paper.*

**Slice 5 — multi-venue portfolio.** N stacks, N ledgers, venue routing, the Tier-2 split. *Exit: two
single-asset-class baskets on two venues share one equity figure and one gross-exposure ceiling.*

**Slice 6 — mixed baskets.** Notional-currency contract, per-instrument calendars, the closed-venue
veto. *Exit: the DoD 2.6.1 criterion.*

Slices 1–3 are Piece 1 and should be treated as a defect fix, not a feature. Slice 4 is independently
valuable and can be scheduled on its own merits. Slices 5–6 are the feature.

## Risks

- **Re-baselining the high-water mark on an existing soak database.** Switching from cost basis to
  mark-to-market changes the number the mark is compared against. On a soak currently holding
  unrealized losses, the first sweep after the change could legitimately trip the switch — correctly,
  but startlingly. The migration must re-baseline explicitly and log that it did, and the operator
  should be told before it runs.
- **The soak's existing promotion evidence was gathered with the drawdown gate ineffective.** Cycles
  already counted toward `report promotion` ran under a kill switch that could not see unrealized
  loss. Whether that evidence still counts is the operator's call, but it should be a deliberate call
  and not an oversight. Recording the decision in the report is worth more than the cycles.
- **Marks introduce shared mutable state between baskets.** It is a cache, but it is read on the money
  path. The staleness rule and the freeze are what keep it honest; a "helpful" fallback added later is
  how Finding 1 comes back.
- **Stage A is real integration work with no equity data provider today**, and Alpaca's catalogue
  shape is unproven against `InstrumentCatalogue`. It may reveal that the protocol needs widening —
  better discovered in Slice 4 than in Slice 6.
- **A mixed basket's panel sees two asset classes in one deliberation.** Nothing in the prompt or the
  seat roles anticipates that. It is not a correctness risk — risk still gates every order — but the
  decision quality is unstudied, and the shadow-panel harness (ADR 0018) is the right way to find out
  before it trades.
- **Corporate actions become load-bearing and have never run in a soak.** An unmatched split halts a
  basket. Worth a deliberate rehearsal against a known historical split.

## Recommendations beyond the ask

1. **ADR 0027 — portfolio equity is mark-to-market in one notional currency.** Piece 1 changes where a
   Tier-2 input comes from and what "equity" means system-wide; that is exactly what an ADR is for. It
   should record the freeze-on-unvaluable rule and its deliberate divergence from the
   fallback-to-cost the code has today.
2. **ADR 0028 — the portfolio spans venues; the notional currency is declared, not derived.** For
   Stage C, replacing `_quote_currency`'s derivation with a declared USD notional.
3. **ADR 0029 — an instrument's venue calendar gates the order, not the cycle.** For Stage D, if the
   operator confirms the union-of-sessions answer to the open question below.
4. **CLAUDE.md conventions**, one line each, beside the existing Phase 11 notes: *"a stale mark is
   not a mark — the fallback is a freeze, never cost"*, and *"equity is one function; a second
   implementation is a bug by construction"*.
5. **Deferred, deliberately**: non-USD notional currencies, real FX rates, multi-venue same-instrument
   (DESIGN §12 already excludes it), and dynamic correlation clusters. Each is a real want; none
   blocks the two problems this phase exists to fix.

## Open questions for the operator

1. **When does a mixed basket cycle?** Recommendation: on the **union** of its venues' open sessions,
   so the crypto leg keeps reacting overnight — which is the point of holding the hedge. The
   consequence is that a decision to act on the equity leg at 03:00 cannot execute, and must be
   recorded as a veto rather than carried forward as a pending intent. The alternative — the
   intersection — makes the basket cycle only during US market hours and defeats the hedge.
2. **Does the existing soak evidence still count** after the drawdown gate is fixed? See Risks.
3. **Is USD-only notional acceptable for v1**, refusing any instrument not quoted in USD or a USD
   stablecoin at publish? This is what your simplification implies, and it keeps FX entirely out of
   scope. Confirming it lets Stage C be small.

---

## Appendix — audit evidence

Produced 2026-08-15 by driving the real `Ledger`, `Instrument` and `app._quote_currency` — not by
reading. Reproduction: 10,000 USDT opening balance; one BUY fill of 0.1 BTC @ 50,000, zero fee.

```
positions      : [('binance:BTC/USDT', '0.1', '50000')]
USDT balance   : 5000.0
equity @cost   : 10000.0        # {key: avg_entry} — what BasketRunner._equity() passes
equity @mkt/2  : 7500.0         # {key: 25000}     — the truth
equity @empty  : 10000.0        # {}               — what startup.py:206 passes

USDT+USDC held : 1000 9000
equity(USDT)   : 1000           # 9,000 USDC contributes nothing

_quote_currency(BTC/USDT, AAPL) REFUSED: ConfigError every basket in one process must
share a quote currency, found ['USD', 'USDT']
_quote_currency(BTC/USDT) -> USDT
```

Call-site census for equity, at the time of the audit:

| Call site | Prices passed | Basis |
|---|---|---|
| [basket_runner.py:186](../tradebot/control/basket_runner.py#L186) → `Watchdog.check` | `{key: avg_entry}` | cost |
| [basket_runner.py:316](../tradebot/control/basket_runner.py#L316) → Tier-1/Tier-2 | snapshot quotes, **this basket only** | partial mark |
| [app.py:244](../tradebot/app.py#L244) → dashboard, `risk rearm` | `{key: avg_entry}` | cost |
| [startup.py:206](../tradebot/control/startup.py#L206) → recon tolerance | `{}` | cost |
| [startup.py:321](../tradebot/control/startup.py#L321) → first-run baseline | `{key: avg_entry}` | cost |
| [manual_close.py:264](../tradebot/control/manual_close.py#L264) → operator exit | one fresh quote | partial mark |

`grep` for `watchdog.check(` returns exactly one call site:
[basket_runner.py:181](../tradebot/control/basket_runner.py#L181). `Supervisor` runs no independent
watchdog sweep.
