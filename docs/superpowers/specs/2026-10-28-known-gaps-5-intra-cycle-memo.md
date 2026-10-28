# KNOWN_GAPS §5, the intra-cycle half: decision memo

Branch `decision-lab-slice-e` at `56038a1`. This was a read-only investigation. The trace below
comes from reading the code. I did not re-run the failing seeds. Where a claim comes from
documented venue semantics and not from observed behaviour, it says so.

---

## 0. Summary

1. **The failure happens at booking, not in risk.** Risk passes the SELL, because the ledger agrees
   with itself when the SELL is sized. The cycle books its own SELL against the full, stale holding.
   The very next monitor poll then reads the stop fill that the venue matched during the snapshot,
   and `Ledger._apply_sell` refuses it (`portfolio.py:145-149`). It raises
   `ReconciliationMismatchError`, which is a `FailClosedError`.
2. **Inside the cycle the error is classified correctly**: the cycle is `FAILED` and the basket is
   halted at once. That is one strike, not three. **But the fill is never consumed.** Every later
   `monitor.poll()` re-reads the same fill and raises again. In a replay the harness's own unguarded
   poll lets it escape, so the corpus build dies. In a supervised process it **wedges the shared
   monitor for every basket.** Section 2 has the cascade.
3. **The race only produces an oversell because `SimBroker` lets free base go negative.** Binance
   spot and Alpaca both reserve the coins or shares behind a resting protective SELL. On those
   venues the cycle's SELL would be *rejected* and nothing would corrupt (derived from venue docs;
   this is §6's premise). The defect lives in the sim stack, and that stack is also **the primary
   paper-soak venue**.
4. **Correction to KNOWN_GAPS §5.** It says "A running process is largely covered by the 30-second
   sweep having already been round". That is not true on a `SimBroker` stack. The sim venue matches
   resting orders **only when someone reads `stack.prices`**, and only cycle snapshots and manual
   closes do that. The sweep reads `read_only_prices`. So a stop in sim or paper can *only* fill
   inside a cycle's snapshot read, and the sweep can never book it before that cycle sizes. A paper
   soak is exactly as exposed as a replay.
5. **Recommendation: Option A.** Add one scoped poll inside the cycle, after the snapshot's market
   reads and before its position reads. Do the same in `ManualCloser`. Ship it with Option D-lite,
   so that one unbookable fill can no longer wedge the monitor for every basket. This closes the
   race deterministically in sim, paper and replay with the same code live runs. It also makes the
   frozen snapshot truthful about what is held. It lets the `STUB_SEED` pin go, once the seven
   measured seeds pass. Option E (sim refuses an oversell, as the venues do) is the right *next*
   decision. It cannot ship without §6.

---

## 1. The call path, with file:line

### 1.1 The cycle

| Step | Where | What happens |
|---|---|---|
| Worker | `tradebot/control/supervisor.py:152-175` `BasketWorker.cycle` | Refreshes marks (`:172`, through `PortfolioWatch` on `read_only_prices`, so it **does not** touch the sim venue), then `_cycle`. |
| Guard | `supervisor.py:191-210` | `runner.run_once()`. Anything escaping is caught at `:202`, counted, and backed off (`_failed`, `:290-303`, halts at 3). An outcome of `FAILED` is counted too (`:206`). |
| Cycle | `tradebot/control/basket_runner.py:163-184` `run_once` | `FailClosedError` → `watchdog.halt_basket` **immediately** (`:181-184`) → outcome `FAILED`. |
| Snapshot | `basket_runner.py:196` → `tradebot/control/context_builder.py:105-122` `build` | Per instrument (`:158-181`): `series_for` → `get_candles` (`:154`), then `get_quote` (`:166`), **then** `ledger.position` (`:167`). |
| Bridge | `tradebot/app.py:518` (`prices=SimulatedMarket(source, broker)`), wired into the runner's `ContextBuilder` at `app.py:690-691` | `tradebot/execution/brokers/sim.py:551-565`: every `get_candles` / `get_quote` calls `broker.observe(tick)` (`:559`, `:564`). |
| Match | `sim.py:215-242` `observe` / `_match` | The new bar's low crosses the stop → the leg arms and fills (`_fill`, `:300-326`). The venue's position drops, and the OCO sibling is cancelled inside the venue (`:328-338`). **Nothing books it on our side.** |
| Stale position | `context_builder.py:167` | Reads `ledger.position` *after* the match, so the frozen snapshot tells the panel it holds coins the venue has already sold. |
| Panel | `basket_runner.py:210` | Deliberates on that snapshot and decides SELL. |
| Size | `basket_runner.py:270` → `_build_proposal` `:331-362`, position at `:344` | Reads the ledger again: still the full holding. `tradebot/risk/sizing.py:71-77` `_size_sell` = fraction × held. `tradebot/risk/rules.py:67-100` `LongOnlyRule` passes, because qty ≤ held. **No veto: risk is consistent with a ledger that is wrong.** |
| Submit | `basket_runner.py:293` → `tradebot/execution/service.py:76-98` | The record goes in first (`:82`). `SimBroker.submit` (`sim.py:246-262`) accepts: `_reserve` (`:435-444`) lets free base go **negative**, and the order is marketable so it fills at once (`_match_on_submit`, `:276-288`). Then `service.submit` → `sync` (`:98`) books the SELL: ledger qty = held − sold. |
| Settle | `basket_runner.py:230` → `_settle` `:246-259` → `monitor.poll()` `:256` | |
| Poll | `tradebot/execution/monitor.py:166-179` | Syncs each group's legs → `_sync` (`:181-189`) → `ExecutionService.sync/apply` (`service.py:147-167`) → `_book_fill` (`:262-275`) → `Ledger.apply_fill` (`tradebot/ledger/portfolio.py:122-129`). |
| **Raise** | `portfolio.py:143-149` `_apply_sell` | `sell of <leg qty> exceeds holding <held − sold>` → `ReconciliationMismatchError` (`tradebot/core/errors.py:65`, subclass of `FailClosedError`). It raises **before** any mutation, so the ledger and log are untouched, and `group.legs[cid]` is never reassigned (`monitor.py:172`). The leg stays "open" with the fill unbooked. |

This matches the evidence recorded in KNOWN_GAPS §4 *Closed*: the refused quantity is the leg's
full size, and the `orders` row still shows the original leg `open`.

### 1.2 The replay

`tradebot/validation/backtest.py:205-232` `_replay`. It sets the clock (`:217`), calls
`worker.cycle()` (`:222`), which ends `FAILED` with the basket halted, and then calls
**`self._application.monitor.poll()` (`:223`) unguarded.** That poll re-reads the same unbooked fill
and raises the same error. It escapes `_replay` → `run` → `decision_lab/corpus.py:237-243`, so the
build dies and the `finally` shuts the application down. The *cycle* already failed closed. The
*replay* crashes because of its own second poll.

### 1.3 The between-cycle poll (efe316b)

* `supervisor.py:410-419` `serve`: `_check_drift` → **`_poll_orders`** → `_sweep_portfolio` → `sync`,
  every `DEFAULT_RESYNC_SECONDS = 30` (`:56`).
* `supervisor.py:437-460` `_poll_orders`: `monitor.poll()` inside `except Exception` → log only.
* `tradebot/control/manual_close.py:242-262` `_settle`: guarded poll after a close.
* The valuation sweep reads `read_only_prices` (`app.py:980-984`), so on a sim stack nothing between
  cycles advances the simulated venue. **The sweep can only book what a cycle's read already matched,
  and by then that cycle has already sized** (point 4 of §0).

### 1.4 The same race in manual close

`manual_close.py:297-302`: `_proposal` reads `ledger.position` (`:298`) **before**
`self._prices.get_quote` (`:302`), and `_prices` is `stack.prices`, the bridge (`app.py:1060`). An
operator close on a sim or paper stack can therefore match the stop and then sell the full
pre-match holding. `_settle` swallows the raise (`:258-262`). The page says the close succeeded
and the monitor is now wedged (see §2).

---

## 2. Crash or veto?

**Neither cleanly. It is a correctly classified fail-closed error that is never consumed.**

| Context | What happens |
|---|---|
| Inside the cycle | The `FailClosedError` is caught at `basket_runner.py:181`. The basket is **halted on the first occurrence**. The three-strikes ladder (`supervisor.py:290-303`) never comes into it, because the halt is immediate. |
| Replay | The cycle is recorded `FAILED`, then `backtest.py:223` re-raises → **process-level crash** of the corpus build. |
| Supervised sim/paper (`serve`) | (1) The originating basket is halted. (2) Every 30s `_poll_orders` raises on the poisoned group, logs it and carries on. Because `poll()` has **no per-group isolation** (`monitor.py:169-172`), groups after it in dict order are never synced, and `_targets`/`_maintain` (`:177-179`) never run for anyone. That means no fills booked, no bot-side TTL enforced, and no leg resizing, **for every basket**. (3) The next time **any other basket** places an order, its `_settle` poll raises the same error → that basket is `FAILED` and halted too. One stop-versus-SELL coincidence ends up halting every basket that trades, one at a time, while the log blames each of them for the first one's fill. (Derived by reading; not executed. The restart path was not traced.) |
| Manual close | Same wedge, and the operator is told the close went through. |
| Live (Binance/Alpaca) | While protective legs rest, the venue reserves the base asset, so the cycle's SELL is **rejected** (§6). The stop fill is then booked against the unreduced ledger with no corruption. Oversell needs a venue that accepts a SELL beyond free balance, and on spot no venue does. **Alpaca margin accounts are the exception in principle**: with no resting legs, an oversell opens a short. But with no resting leg there is also no concurrent fill to race against. (From venue docs; live has never run.) |

### Adjacent defects found along the way (each needs its own entry; not fixed by any option below)

* **`ExecutionService.cancel` ignores `ack.cancelled=False`** (`service.py:191-212`) and advances the
  order to `CANCELLED`, which is terminal. `_sync` never re-reads a terminal order
  (`monitor.py:182`). `BinanceSpotBroker.cancel`'s docstring (`binance.py:320-325`) promises that "the
  monitor's next sweep books the fill". **It cannot.** A stop that fills between a poll's sync and
  `_replace_legs`'s cancel loses its fill, and only the reconciler finds it later as a `MISMATCH`.
  Any §6 fix (cancel legs before exit) widens this window and has to sync after cancel.
* **Sim overstates live**: `SimBroker` has never refused a SELL for insufficient free base, so every
  discretionary partial exit in soak evidence and in `decision_lab` corpora happened against
  reserved coins. Live would refuse each one of them (§6). That is a fidelity gap in ADR 0020's
  sense, not just this race.

---

## 3. Options

Rate-budget inputs: `RateBudget.weight_per_minute = 600` (`tradebot/core/ratelimit.py:51`);
`poll_interval` assumes pollers get 10% of it (`:69-78`). Binance weights (`binance.py:56-67`):
`queryOrder` 4, plus `myTrades` 20 once an order has executed quantity (`:540-560`). A protected
position is one OCO group = 2 legs = **8 weight per poll per held instrument**, plus 24 on the poll
that discovers a fill.

### Option A: a scoped poll between the snapshot's market reads and its position reads *(recommended)*

**Change.** Split `ContextBuilder.build` into two passes: market reads for every instrument, then
(through an injected `settle` hook) `monitor.poll(instrument_keys=basket's)`, then the position
reads. The runner passes the hook in. Add `ExecutionMonitor.poll(scope=...)` so it syncs only that
basket's groups (ADR 0026 means no other basket holds them). Apply the same reorder to
`ManualCloser._proposal`: quote → poll → position.

**What changes in live.** One extra venue read per cycle, placed before the decision: about 8
weight per held, protected instrument. A two-instrument basket at 1h cadence costs about 16 weight
per hour, roughly 0.05% of the budget. Five baskets × two instruments at a 1-minute cadence would
be 80/min, 13%, which is the same order as the 30s sweep already spends. A poll failure is a
`RetryableError` that propagates to the worker's backoff, the same as a failed submit today: **no
trade, fail closed.** The poll can run `_maintain`, so it can resize legs mid-cycle. The sweep
already does exactly that.

**Non-negotiables.** Strengthens #3 (the venue is the source of truth; the ledger is reconciled
before it is read for sizing). #4 is untouched, because the poll writes no order intent. #1 holds:
a poll error means no trade.

**Why the snapshot placement and not "before sizing".** Two reasons. The sim venue only matches
during the reads, so polling straight after them closes the sim race exactly. And the **frozen
snapshot then tells the truth about the position**. Today the panel can be told "holding 0.03 ETH,
+4%" about coins the venue sold during that read. `decision_lab` scores holding versus flat
differently, because its truth table is long-only aware, so a stale `PositionView` mislabels exactly
the cycles this race touches.

**What it does not close.** In live, the venue can still fill a stop *during deliberation*, after
the poll. On Binance/Alpaca that ends in a rejected SELL, not corruption (see §2). If a residual
window matters later, B can be added on top.

**Tests.**
* `tests/scenario/test_protective_resize.py`. Add a third rung-3 scenario next to the two already
  there, reusing `crashing_market`: buy, then on the next cycle a crash bar crosses the stop **and**
  the scripted panel says SELL. Assert the cycle is not `FAILED`, the ledger is flat, the SELL is
  vetoed `long_only` "SELL while flat" (or sized from the post-stop residual), and
  `snapshot.instruments[0].position is None`. It must fail on current code with `exceeds holding`.
* `tests/unit/test_context_builder.py`: the hook runs after every market read and before every
  position read, and a raising hook ends the build.
* `tests/unit/test_monitor.py`: a scoped poll touches only the named groups.
* `tests/scenario/test_backtest.py`: a replay over a crash series with a SELL-scripted panel runs
  to `planned_cycles`.
* `tests/unit/test_dashboard_control.py`: a manual close on a bar that crosses the stop sells
  nothing that the stop already sold.
* `tests/scenario/test_full_cycle.py`: the hook is the process's own monitor. That is the same
  "which monitor" trap the efe316b entry calls out.

**Size.** Small to medium: about 60–100 lines of source across `context_builder.py`,
`basket_runner.py`, `monitor.py`, `manual_close.py` and `app.py`, plus about 5 tests.

**STUB_SEED pin.** **Removable.** Once the stop is booked before sizing, nothing else in a sim cycle
matches (`_match_on_submit` matches only the new order; new legs post-date the bar). Verify by
running `test_slice_b_end_to_end.py` unpinned over the 7 measured seeds (ideally about 20) before
deleting it. A pass may still end early on `max_consecutive_losses`. That is an auto-pause, not a
crash, and the test's 228/228 assertion would need to account for it on whichever seed does it.

### Option B: poll immediately before sizing (after deliberation)

**Change.** Run the same scoped poll in `BasketRunner._run`, after `deliberate` (`:210`) and before
the first `_act` (`:224`).

**What changes in live.** The same cost as A. It shrinks the live window from "snapshot to submit"
to "poll to submit", which keeps LLM latency out of it. That is the best placement if the target
is a venue that does *not* reserve.

**Its weakness.** The panel still deliberates on a snapshot claiming a holding that is gone. The
log then records a SELL decision on 0.03 ETH, vetoed as "SELL while flat", which reads as a panel
error when it is not one. It is also a poor fit for `decision_lab`'s scoring.

**Variant to reject outright:** clamping in `_apply_sell`, or clamping the SELL to "whatever is
there". That makes the ledger absorb a venue disagreement, which is the reconciler's job (ADR 0006).
It breaks #3 and hides real corruption.

**Tests, size, pin.** Same as A, a little smaller (no `ContextBuilder` split). Pin removable.

### Option C: replay-only. The harness advances the venue before the cycle

**Change.** In `_replay`, after `clock.set` (`backtest.py:217`): feed the new bar to the sim venue
(read it through `stack.prices` for the basket's instruments), then `monitor.poll()`, then
`worker.cycle()`. The cycle's own reads then observe a tick that has already been matched. A
variant is to make `SimulatedMarket` idempotent per bar.

**What changes in live.** Nothing.

**Non-negotiables.** None touched. It does bend ADR 0020: the replay order now differs from the
running process, so a replay no longer exercises the thing a soak runs.

**Weakness.** **Paper soaks keep the race**, and they are the primary evidence base. A sibling
"sim venue ticker" on the supervisor sweep would narrow it for paper, but not close it, because a
bar can still close between the sweep and the cycle. The stale `PositionView` problem is fixed only
for replay.

**Tests.** `tests/scenario/test_backtest.py` (crash series + SELL panel completes) and the
decision_lab slice-B test unpinned.

**Size.** Small: about 20–30 lines in `backtest.py`.

**Pin.** Removable for decision_lab. Note that `decision_lab` treats `BacktestHarness` as
"unchanged", and this changes it.

### Option D: contain it. One unbookable fill must not wedge everything; plus `--seed`

**Change.**
1. `ExecutionMonitor.poll` isolates failures per group: sync all groups, collect errors, and still
   run `_targets`/`_maintain` for the healthy ones.
2. A group whose fill cannot be booked is marked poisoned, with a `RISK_EVENT` (`rule:
   unbookable_fill`, naming the order, the quantity and the holding). It raises **only to callers
   scoped to its basket**, so sibling baskets stop inheriting the halt.
3. The harness's `:223` poll treats a halted basket's poison as already recorded, and the replay
   ends as a short pass instead of a crash.
4. `corpus build --seed` (and `calibrate long --seed`), threaded through `StubLLMProvider`'s `rng`.

**What changes in live.** No money-path ordering change, no venue cost. The supervised cascade from
§2 becomes a single-basket halt with a named reason.

**Non-negotiables.** Keeps #1, because the basket still halts and nothing is cleared automatically.
Must **not** drop the fill: the group stays tracked and keeps being reported until a human acts or
a reconcile resolves it.

**Weakness.** It does not fix the race. Passes still end early, now as `ran_cycles < planned`
instead of as a crash. `--seed` gives reproducibility, not correctness.

**Tests.**
* `tests/unit/test_monitor.py`: a poisoned group does not stop a sibling group's fill being booked,
  and the error reaches only the scoped caller.
* `tests/unit/test_supervisor.py`: the sweep keeps polling other baskets.
* `tests/scenario/test_chaos.py`: a new "unbookable fill halts one basket, not all".

**Size.** Medium: about 80–120 lines.

**Pin.** **Not removable.** The test asserts a full pass. A seed would make the pin explicit rather
than a monkeypatch, nothing more.

### Option E (beyond the brief): make `SimBroker` refuse what Binance refuses, together with §6

**Change.** `SimBroker.submit` rejects a SELL whose quantity exceeds free base, as Binance -2010
and Alpaca 403 "insufficient qty" do. That removes the oversell outright. Alone, it would reject
**every** discretionary exit while legs rest, so it has to ship with the §6 fix: release the
instrument's legs before a discretionary or manual exit, then *sync after cancel* (adjacent defect
in §2), then submit, and let `_maintain` re-arm the remainder.

**What changes in live.** §6's fix changes the live exit path, and opens an unprotected window
between the cancel and the exit filling (the same window `_replace_legs` already opens).

**Non-negotiables.** It serves ADR 0020 and #3 best, because sim stops accepting what no venue
would accept.

**Weakness.** It is large. It changes the trading behaviour of every existing sim/paper soak and
corpus, so it is a re-baselining event.

**Tests.** `tests/contract/test_broker_contract.py` (an oversell is rejected by every adapter, which
is the right home for it), `tests/unit/test_sim_broker.py`, and new scenarios for §6.

**Size.** Large.

**Pin.** Removable, but the corpus contents shift.

---

## 4. Recommendation

**Ship A, together with D parts 1–2.** Then open E plus §6 as its own decision.

* **A is the only option that closes the race everywhere it actually occurs.** That is sim, paper
  on `SimBroker` (the evidence base for `report promotion`), replay, and manual close. It uses one
  mechanism, the same code in every mode, as ADR 0020 asks. C fixes only the tool that noticed.
  B fixes sizing but leaves the panel and the log reasoning about a holding that is gone, which
  `decision_lab` would then score as a judgement error.
* **The live cost is negligible and the ordering change is in the fail-closed direction.** About 8
  weight per held instrument per cycle against a 600/min budget. The read is placed before the
  decision, and a failure means no trade. The KNOWN_GAPS entry treated "changes the live money
  path's ordering" as the blocker. But a reconciling *read* ahead of sizing is what non-negotiable
  #3 already asks for, and it adds no write and no submit.
* **D's isolation is needed whatever happens to the race.** As things stand, *any* fill that cannot
  be booked (this race, the cancel-loses-fill defect, a §9 adopted group) silently disables order
  polling for the whole process and halts each basket in turn as it trades. That is the same
  "nobody is polling working orders" failure efe316b was written to end, arriving by another route.
* **Pin:** delete `STUB_SEED` after A lands **and** the slice-B test passes unpinned across the
  seven previously measured seeds. Update KNOWN_GAPS §5's *What the fix does not reach* paragraph,
  including the correction that the 30s sweep does not cover `SimBroker` stacks.
* **Tell the operator separately:** soak and corpus evidence so far includes discretionary exits
  that live Binance would have refused (§6 plus the sim fidelity gap). Whether that evidence still
  counts is a call to make, in the same way Phase 12 Piece 1 asked about the drawdown gate.
