# 27. Portfolio equity is mark-to-market in one notional currency

Date: 2026-08-16

## Status

Accepted. Implements Piece 1 of
[docs/PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md](../PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md).

## Context

`BasketRunner._equity()` built its price map as `{instrument_key: position.avg_entry}` and passed
it to `Ledger.equity`, whose own fallback for a missing price was *also* `avg_entry`. The result
was the cost basis by construction, always — and that number was the sole argument to
`Watchdog.check`, which had exactly one call site in the system.

So the drawdown kill switch could not see unrealized loss. A portfolio holding 0.1 BTC bought at
50,000 while BTC traded at 25,000 reported **0% drawdown** against a 10% limit. The switch measured
realized losses and fee bleed and nothing else, and the high-water mark only ever rose on realized
gains.

Five more defects travelled with it:

1. **Equity was basket-dependent.** `_build_proposal` mapped only the cycling basket's instruments
   while `Ledger.equity` iterated every position, falling back to cost for the rest. Two baskets
   computed two different portfolio equities for the same instant, and neither was correct.
2. **Non-quote cash was worth zero.** `equity` summed `balance(quote_currency)` alone, so 1,000
   USDT beside 9,000 USDC valued at 1,000 — routine on a real spot account.
3. **External flows were currency-blind.** `startup.py` dropped `ExternalFlow.currency` and added
   the bare amount to baselines denominated in the notional currency. Compounded with (2), a 9,000
   USDC deposit raised the high-water mark by 9,000 while contributing nothing to equity — a
   guaranteed spurious trip needing a typed phrase to clear.
4. **The freeze contract had no consumer.** `PortfolioAggregate.frozen` was read nowhere, and
   `stablecoin_prices` was never supplied, so the depeg guard had never fired in production.
5. **`max_gross_exposure` was enforced against one basket.** `aggregate` computed each venue
   slice's exposure over the instrument tuple it was handed, and its only caller handed it
   `self._basket.instruments` — so a limit documented as spanning all baskets omitted every sibling
   basket's positions.

DESIGN §6.6 already said the drawdown and daily-loss baselines are "computed on **mark-to-market
equity**". It was not true of the code.

## Decision

**One function answers "what is this portfolio worth", and every consumer reads it.**

`risk.aggregate.aggregate` is that function. `Ledger.equity` and `Ledger.unrealized_pnl` are
deleted; `Ledger.exposure` takes a strict price map and raises rather than substituting cost. The
ledger knows what is held, never what it is worth.

Equity is **cash valued in the notional currency, plus each position at its current mark**. Marks
come from `ledger.marks.Marks`, a shared cache written by four things — every cycle's snapshot, the
supervisor's sweep, startup, and a manual close — and read by the valuation.

### A missing or stale mark is a freeze, never a fallback

`Marks.price_of` returns `None` for a key that is absent or older than
`GlobalRiskPolicy.mark_staleness_seconds`, and there is no third outcome. A non-flat position with
no fresh mark **freezes the aggregate**, which blocks new orders.

Valuing a position at what it cost is not the conservative choice. It reports zero drawdown on a
portfolio that has halved, and it simultaneously fabricates drawdown after a re-baseline. There is
no safe direction to be wrong in; there is only correct, or an admission that we do not know.

### Freezing blocks new orders; it does not trip the kill switch

The switch is for breaches. A freeze is *ignorance* — the feed is down, or a balance is in a
currency nothing can price. So a frozen valuation:

- returns a verdict whose `may_trade` is false, which the cycle gate already turns into `BLOCKED`;
- trips nothing, moves no baseline, rolls no day, and **writes no state at all**;
- clears itself the moment marks return, with no operator action.

A freeze spanning midnight therefore leaves `day_start_equity` at yesterday's, measuring the daily
loss from an older and generally higher baseline. That is the conservative direction and it is
chosen, not incidental.

**A freeze never blocks a reduce-only operator exit**, and does so by construction rather than by a
new exemption: every Tier-1 and Tier-2 rule that reads `equity` or `basket_budget` already stands
aside on `Side.SELL`, and `_size_sell` clamps to the holding. This preserves ADR 0015 — the switch
stops the bot trading, not a human getting out.

### Every balance is valued, counted as a position, or freezes

`value_cash` is a four-rung ladder, first match wins, and **the order is load-bearing**:

1. the notional currency → face value;
2. a `USD_STABLECOINS` member → par, subject to the peg check, which now receives real prices;
3. the base asset of a configured instrument → **zero**, because it is already counted as a
   position;
4. otherwise → its `{CUR}/{notional}` market if the catalogue lists one, marked like any
   instrument;
5. anything left and non-zero → no admissible valuation, and the aggregate freezes naming it.

Rung 3 must precede rung 4: `BTC` is both a configured instrument's base asset *and* a currency
with a `BTC/USDT` market, so reaching rung 4 first would value every spot holding twice.

A **zero** balance in an unvaluable currency does not freeze. Dust already converted away has the
same value in every currency, and stopping a live account trading over a residual would be
fail-useless rather than fail-closed.

### Portfolio-wide questions read the configured universe

Gross exposure, per-instrument exposure and correlation-cluster membership are computed over every
configured instrument, never the cycling basket's slice. Only `basket_exposure` stays
basket-scoped, because that one is genuinely a question about the basket.

### The sweep is not optional

`control.valuation.PortfolioWatch` refreshes the marks and runs `Watchdog.check`. It runs in two
places, and both are needed:

- **`BasketWorker.cycle`**, before the gate. Every path that runs a cycle goes through it — `serve`,
  `run --once`, the backtest harness, the scenario suite — so this is what guarantees the gate has
  marks at all. The gate runs *before* the snapshot exists, so it cannot get them from the cycle.
- **The supervisor's resync tick.** This marks the positions of baskets that are **not** cycling.
  Without it, pausing, halting or quarantining a basket that holds a position would freeze the
  whole portfolio and block every other basket — a system-wide denial caused by a routine operator
  action.

It reads `read_only_prices`, never `prices`: in the sim stack the latter is a bridge that feeds the
tick to `SimBroker` and matches resting orders, and a valuation sweep must observe the market, never
move it.

`mark_staleness_seconds` must exceed the sweep cadence by a factor of three, asserted at wiring
where both numbers are known — `core/` may not import the supervisor's cadence.

### The basis change is announced, never absorbed

A high-water mark stored by an earlier version was recorded on cost basis. Mark-to-market equity is
a different number, and any open unrealized loss now shows as drawdown against it — correctly, and
possibly for the first time.

Startup records one `RISK_EVENT` naming both figures and the implied drawdown, and **changes
nothing**. There is deliberately no automatic re-baseline: resetting the mark would silently forgive
whatever loss happened to be open at the moment of the upgrade, which is exactly the laundering
`record_flow` refuses to do, arriving through a migration instead of through a flow. If it trips,
that is a real loss, and the operator clears it with the typed `risk rearm` — the mechanism that
already exists for precisely this decision.

## Consequences

- The drawdown kill switch enforces what it claims. **Soak evidence gathered before this landed was
  gathered under a gate that could not see unrealized loss**, and `report promotion` should record
  that boundary rather than let it pass unnoticed.
- `Marks` is shared mutable state read on the money path. The staleness rule and the freeze are what
  keep it honest; a "helpful" fallback added later is how the defect returns.
  `tests/unit/test_valuation_boundary.py` asserts structurally that none exists, in the manner
  `test_dashboard_chart.py` asserts the float boundary.
- A frozen portfolio stops all trading, so it alerts (`AlertKind.VALUATION_FROZEN`) and counts as an
  incident. The recovery alerts too — unlike a re-arm or an un-halt, which a human did on purpose,
  this one clears itself and an operator should not have to infer that from silence.
- `Watchdog.check`/`trip`/`rearm`/`record_flow` are serialized behind one lock. They are
  read-modify-write over the row holding the kill switch, `SingleWriter` serializes only the write,
  and the sweep added a caller on a fixed cadence.
- `Application.equity()` became `Application.valuation()` and returns the aggregate. A method called
  `equity` returning a number is what let six call sites each build their own price map.
- `risk rearm` refuses while frozen: baselines written from an equity nobody can compute would
  outlive the outage that caused it.
- Piece 2 (mixed crypto+equity baskets) is unblocked. `aggregate` already takes
  `Mapping[str, Ledger]` and emits a `VenueSlice` per venue; it is called with one entry today.
