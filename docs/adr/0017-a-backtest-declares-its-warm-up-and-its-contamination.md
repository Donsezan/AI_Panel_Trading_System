# 17. A backtest declares its warm-up and its contamination, and a future bar is a hard error

Date: 2026-07-31
Status: accepted

## Context

DESIGN §9 rung 4 and [L12] are unusually strict about what a backtest is: plumbing and risk
validation, "explicitly not evidence of alpha" over any period predating the models' knowledge
cutoffs, with a report banner stating which periods are pre- and post-cutoff. R8 names the risk
plainly — someone eventually quotes a backtest as a result.

Building the harness turned up two failure modes that would each have produced a *plausible*
report saying something false.

**The first run of a replay reported 120 consecutive `DATA_STALE` cycles.** Correctly: the window
began at the first bar of the dataset, and MACD cannot be computed from four bars. But a report
showing a wall of failures at the front of every backtest invites the reader to conclude the
system is broken, and invites the author to widen the staleness budget, which would be a genuine
loosening of a money-path control to fix a data-shaped problem.

**The first version of the scenario test passed for the wrong reason.** The dataset was loaded
with one clock and the application ran on another, so `ReplayMarketData` served the *whole*
series at every cycle — including bars from the replay's future. Nothing objected. The series was
not stale by any measure the system had: its content age was *negative*, which sailed through
`age(now) > max_age`. That is a look-ahead leak presenting as unusually fresh data, and it is
exactly the failure that makes a backtest quietly meaningless.

## Decision

**A future bar fails closed, in `core`.** `CandleSeries.require_fresh` now refuses a series whose
latest bar closes after the cycle's `now`:

```python
if self.latest.close_time > now:
    raise DataStaleError(... "a series from the future is a look-ahead leak, not fresh data")
```

Both providers already cut at `close_time <= cutoff`, so this can only fire when the series was
built against a different clock than the cycle deciding on it. It costs nothing in a live run and
turns a silent leak into an aborted cycle. This is a money-path rule, not a backtest one:
staleness has two directions and only one of them was checked.

**The window declares its warm-up.** `warmup_for` computes the longest indicator requirement on
the longest configured timeframe, the harness moves the first cycle forward by it, and the report
prints `requested from`, `indicator warm-up` and the effective window side by side. A window
shorter than its own warm-up refuses rather than producing a run of failures. The alternative —
starting where asked and letting the opening cycles abort — reports a property of the dataset as
a property of the system.

**Contamination is per model, with its source.** `validation/cutoffs.py` holds a table of
`(model, cutoff, source)` where `source` is either `vendor-published` or `estimate from release
date`, matched by longest family prefix after stripping OpenRouter's routing suffix (`:free` names
a billing lane, not another set of weights). Each model gets one of four verdicts — clean,
partial, contaminated, unknown — and the share of the window falling after its cutoff.

Two directions are deliberate. **Unknown reads as contaminated**, never as clean: an unproven
claim of freshness is worth less than an admitted gap. And **the source column is printed**,
because "the vendor says so" and "we guessed from the release date" are different qualities of
evidence, and few vendors publish a cutoff at all. The dates need verifying before a report is
quoted — the same standing caveat the model ids in `decision/presets.py` carry.

**Only models that could actually answer are analysed.** Bindings resolving to a `STUB` provider
are excluded, and a run on the offline panel says "no hosted model was contacted" rather than
reporting `stub-technical` as a model of unknown provenance. Fallback bindings *are* included: a
seat that spent the run on its backup was answered by that model.

**The banner is on every report, unconditionally**, including a clean-window one. A clean window
removes one known contaminant; it does not turn a plumbing test into a performance result.

## Consequences

- A backtest over a 1d-timeframe basket needs roughly two months of history before its first
  cycle. That is arithmetic, not policy, and it is stated in the report rather than discovered.
- The harness stops when every basket has stopped cycling. A loss streak auto-pauses the basket,
  no human exists inside a replay to clear it, and stepping the remaining months would add
  nothing but hours of "not cycling". The report shows the unused window as `cycles not run`.
- A replay wired to the wall clock now aborts every cycle instead of producing a confident,
  meaningless result. Loud is the correct failure mode here.
- The cutoff table is a maintenance burden that will drift. It is small, explicit, overridable per
  call, and its verdicts degrade towards "contaminated" as it ages — which is the safe direction.
