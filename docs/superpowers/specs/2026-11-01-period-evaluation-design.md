# Period evaluation — one seat set, one chosen period, from the dashboard

Status: approved by the operator on 2026-11-01. Extends
[the decision_lab design](2026-08-23-decision-lab-design.md); §10.4 (long exposure), §8 (regimes)
and §9 (scoring) are its parents. Wording in this document marked **[builder's call]** was decided
by the author and may be challenged; everything else came from the operator or from code read on
2026-11-01.

## 1. Why

The 2025 reference pass stopped at 380 of 1089 cycles, around May 10, on `max_consecutive_losses`.
A replay has no human to clear that rule, so most of a year goes unmeasured. Beyond that gap, the
operator wants a closed loop on one screen: choose a seat set and a period, press Run, then see how
the money moved over time, how each decision was graded, how the seats disagreed, and how they
behaved in market spikes.

## 2. Goals

1. Evaluate **only the selected period**: one month, one quarter (its three months) or one year
   (its twelve months).
2. Run it and read it from the decision_lab dashboard.
3. Show profit over time, with money and open positions carried month to month inside the period.
4. Keep measuring after a loss-streak pause: the rest of that month goes untraded, and the next
   month continues.
5. Let the operator drill from period to month to cycle and read each cycle's dispute, result, and
   what the market did next.
6. Compare seats on one shared time axis.
7. Show performance in spikes, with spike up and spike down always apart.
8. Reuse a finished result until the seat set changes.

## 3. Non-goals and anti-goals

Non-goals for this version:

- Comparing two different seat sets side by side on this page. The existing sweep does that.
- Profit attributed to a regime. Spike rows report decision accuracy, not dollars.
- Arbitrary date ranges.
- Joining a period across two dataset directories.

Anti-goals, which make it a failure even if it works:

- **Any write to a bot database or any change to `tradebot/`,** unless §5.7's test forces the seam
  approved there. The pause is lifted only in this tool's own workspace database.
  `test_separation.py` holds.
- **A lifted pause that the page doesn't show.** The bot's real rule needs a human. A result
  produced with it lifted must say so wherever its profit appears.
- **Blending profit and accuracy** into a score (§10.5), or pooling spike up with spike down (§8).
- **Dropping an ungraded decision.** Unscored stays a verdict with a reason (§9.4).
- **A partial figure for a frozen portfolio.** `UNVALUABLE` means no figure (§10.4).

## 4. What the operator does

Mockup: [2026-11-01-period-evaluation-mockup.html](2026-11-01-period-evaluation-mockup.html). All its
numbers are invented.

**BTC only, for now.** The operator's decision. `data/history-2025` also holds ETH, so the run's
basket names BTC/USDT alone rather than copying `dataset.instruments` as `longrun.run` does. One
cycle is therefore one decision. Identity already covers this through the basket's digest, so adding
coins later mints new runs rather than colliding.

**Controls:** dataset, seat set and candidate, *Evaluate one* (Month, Quarter or Year), the period,
start equity, cadence, budget, Run. The period list changes with the choice (`2025-03`, `2025-Q2`,
`2025`) and offers only calendar periods the chosen dataset fully covers. [builder's call]

**Results, top to bottom:**

1. **Period summary:** start and end equity, net profit after model costs, realized and unrealized,
   model costs, share of decisions graded correct with graded and unscored counts, coverage, and how
   many times the pause was lifted.
2. **Equity through the period:** one point per month end, with ⚠ on a paused month. A one-month
   period has one point, so it shows the start and end figures rather than a line. [builder's call]
3. **Performance in spikes:** see §7.
4. **Seats compared:** per seat, correct on the final vote, correct on the first vote, how often it
   changed its vote, and accuracy per quarter or per month, whichever fits the period.
5. **Months:** start and end equity, net profit, correct, cycles run of planned, how it ended.
6. **Inside a month:** one timeline with price above one row per seat plus the panel, sharing one
   axis, with spikes shaded, and the list of cycles.
7. **Inside a cycle:**
   - **Dispute:** each round, each seat's vote and argument. Blind round first.
   - **Result:** the vote count, the dissent, and whether an order followed and what happened to it.
   - **Market:** price from the decision onward, the grading band and the horizon.
   - **Grades:** per seat, first vote, final vote, verdict. The existing `decision.html` content is
     the starting point.

## 5. How a run works

### 5.1 One run, one database

A run is one workspace database at `workspace/period-<run_id>/`, built like a long run (§10.4), for
one candidate over one period. Months are folded out of that one log by time; they aren't separate
runs. That is what carries money and open positions, because the ledger and `SimBroker` simply
continue. Selecting March alone and selecting Q1 are different runs with different starting money,
and their March figures can legitimately differ.

**Identity:** dataset digest, `config_digest` of the candidate's whole basket, cadence, start equity,
period. The whole basket rather than `panel_digest`, because a changed risk limit must not reuse old
profit. [builder's call] The reference basket comes from `calibrate long`'s existing construction
(`cli.py:1018`), and its limits supply §5.3's configured limit. Budget isn't part of the identity.

In a multi-candidate matrix, editing candidate A leaves candidate B's result valid and shown, with
the gate's current state beside it. The gate is consulted before spend, never before reuse.
[builder's call]

A finished run is reused at its identity, and that includes one that ended early on the kill switch,
a halt or budget, since those are its result. Results are kept and never deleted.
**Retrying:** see §5.7.

### 5.2 Warm-up

The first cycle of a period must land at the period's start. `BacktestHarness` adds the indicator
warm-up to `start`, so the run requests `period_start − warmup_for(basket)` and the bars before the
period are read for indicators only. If the dataset has too little earlier history, the first
cycle comes later, and the page says so: `data/history-2025` begins Jan 1 and the existing pass
began Jan 3 03:00, so January and Q1 2025 start late. **[builder's call]** a late start is shown,
not refused, because it is minor against a whole period. Coverage counts the lost cycles.

### 5.3 Lifting the pause, and why clearing it isn't enough

Read from the code:

- The rule calls `watchdog.halt_basket` (`basket_runner.py:312-328`), which persists a basket
  status, and `BasketWorker._runnable` then stops the worker.
- `consecutive_losses` is **derived from `round_trips`** on each read, and only a win resets it
  (`ledger/history.py:96`). Clearing the status alone therefore achieves nothing: the next proposal
  sees the same streak, vetoes, and halts again. The basket can't trade, so it can't win, so the
  streak never ends.

At each month boundary, **only if** the basket stopped on `AUTO_PAUSE_RULE`, the run does two things
in its own workspace database:

1. clears that basket status;
2. publishes a new basket version with `max_consecutive_losses = current streak + the candidate's
   configured limit`.

So the basket gets its full allowance back, and no bot code or log row is altered. The new version
is pinned on each later cycle (ADR 0013), so the log shows exactly which limit each cycle ran under.
The run records each lift (when, streak, new limit) on its meta and as a `RISK_EVENT`.
[builder's call: this mechanism]

**Any other stop ends the run** and the page says why: the kill switch, a `FAILED`-cycle halt, a
poisoned group. Those are not what the operator agreed to lift, and §12.1 gives this tool no
authority to wave them through. Later months read *not run*. [builder's call]

### 5.4 Time keeps moving during a pause

`_replay` skips a stopped basket's ticks, and with them its order polling. A held position's stop or
take-profit would then not trigger until the next month, at whatever the price had become, and marks
would go stale so the month's end valued as `UNVALUABLE`. `SimBroker` matches resting orders only
when a tick reaches it through the stack's bridge, and `Application` exposes no public way to feed
one, so polling alone would never fire a stop.

**So the paused stretch runs as whole-basket quarantine cycles.** [builder's call] Right after a
pause, in the workspace database only, the run clears the basket status and publishes a version with
`quarantined` set. A quarantined cycle takes its snapshot, which feeds the bridge and refreshes the
marks, then ends as `CycleOutcome.QUARANTINED` with no panel call and no spend
(`basket_runner.py:205`). `QuarantineRule` guarantees no new order. At the month boundary a further
version removes the quarantine and applies §5.3's limit. Any loss in that stretch counts.

These cycles carry no decision, so they are never scored. They are excluded from "cycles run", which
is what makes coverage read 74 / 168, and the log shows exactly why each exists. Stops fire at the
cadence's resolution, as they do in any cycle. The first test must confirm a resting stop fires
across a quarantined cycle.

**The driver** [corrected while planning]: `BacktestHarness.run` calls `recover()` every time and
retires a stopped basket for good, so it can't express a month boundary. The run has its own loop in
`decision_lab`: `plan_ticks`, then `supervisor.worker_for(id).cycle()` and `monitor.poll()`, the same
calls `_replay` makes, with `recover()` once per process. `worker_for` replaces a retired worker, so
a cleared basket cycles again. A test asserts the driver and `BacktestHarness` produce the same fills
over one uninterrupted window, so the loop can't drift from the harness unnoticed.

### 5.5 Spending

- **Budget is required with no default**, as for `sweep`.
- Before spending, the page shows a projection from the calibrated cost per cycle, and a projection
  over budget refuses. Under `--skip-gate` there may be no calibrated cost: the run proceeds, and the
  page and report say no projection was available. [builder's call]
- During a run, spend is checked **at each month boundary**, and exceeding budget ends the run with
  months read *not run*. So a run can overshoot by up to one month's spend, and a one-month run has
  no mid-run ceiling. The page says that next to the field, and Stop remains. [builder's call]
- A running job holds the workspace lock (exit 7), as every job does.

### 5.6 Existing rules that apply unchanged

- **The gate:** refuses with exit 6 until both calibration halves have passed for this dataset,
  matrix and day set. `--skip-gate` exists and is stamped. The refusal on the page offers the
  calibration commands. The nine pinned days must exist for the chosen dataset (`dataset days`).
- **Unreachable provider:** refuses before spend.
- **Stub binding:** the whole page is stamped `PLUMBING CHECK`. News-blind runs carry `NEWS-BLIND`.
- **Substituted model:** a cycle contaminated by a fallback is never scored and is counted on the
  page (§7.7). Its trades still happened, so the profit includes them; the page shows how many
  cycles this affects. [builder's call]

### 5.7 Saved progress, and the two ways a run can end early

The operator's rule: **an error continues from the last good point; a rule ending the run is a
result.**

| What happened | Examples | What the run does | Page says |
|---|---|---|---|
| **Interrupted** | Crash, reboot, Stop pressed, budget reached, unexpected exception, `FAILED` cycle | Keeps the last month checkpoint; **Continue** resumes there | *Interrupted at <date>*, with Continue |
| **Ended by a rule** | Out of money, kill switch | Final and reused at its identity; no Continue | *Model failed: <rule> on <date>. Needs tuning.* Later months read *not run* |
| **Paused on losses** | `max_consecutive_losses` | Not an ending: §5.3 and §5.4 | ⚠ on the month |

**Out of money** is new: at a cycle, the run holds no position and its free cash is below the
instrument's `min_notional`, so no order can ever exist again. [builder's call: this definition,
since exactly $0 is unreachable once fees and minimums apply]

Continuing past a budget stop needs a larger budget. Continuing after a `FAILED` cycle retries from
the checkpoint; if the same defect recurs, it stops at the same place and says so.

**A checkpoint is written at each month end** [builder's call], atomically, in the run's directory:

- a verified copy of the database, through `maintenance.backup.take_backup`;
- the run's own meta so far.

Work since the last checkpoint is redone on Continue, so an interruption can repeat up to one month
of model spend. Previously spent budget still counts against the ceiling.

**Corrected while planning: no `tradebot` change is expected.** The operator approved a sim-only seam
on the belief that venue state could not be restored from outside. It can: `recover()` already replays
the ledger from the log and calls `SimBroker.restore` with balances, positions and open orders
(`control/startup.py:205`, `execution/brokers/sim.py:498`). Continue is therefore `build_sim(db_path=
<restored copy>, baskets=None, start_equity=<the same>)` followed by `recover()`.

`restore` loses two things: a protective order triggered but not yet filled comes back un-armed, and
the last ticks. The next tick re-feeds the second. The test comparing a continued run to an
uninterrupted one decides whether the first matters. **Only if it does** is the approved seam used,
under the slice E rules: sim only, naming no `decision_lab`, seam and guard tests as one change.

## 6. Changing a seat set

Saving an edit asks **Create new seat set** or **Update `<name>`**.

- **Create** keeps the old set and saves under a new name.
- **Update** replaces the set under its name.
- Either way the digest changes, so results no longer match, the seat set is uncalibrated, and the
  page says so at save time, not later as an exit 6.
- After an Update, old results stay on file, listed against the older digest. [builder's call]

## 7. Spikes

**The purpose, in the operator's words:** find the dates when the market moved with above-average
volatility, then match real news to its effect on the market, so we can see whether the seats read
the news and predicted the move. An accuracy table alone can't answer that, so spikes are a list of
**events**, each with its news and each seat's calls.

### 7.1 Finding the events

`regimes.py` labels each bar as today: spike at or above the 90th percentile of the trailing 30-bar
realised volatility, with its sign from the return (§8.1), against a threshold from the **whole
dataset**, so a label doesn't depend on the period selected. **[builder's call]** 90th percentile is
kept rather than a literal "above average", which would call a large share of all bars a spike and
produce events too numerous to read. The percentile is already a parameter.

**New: an event is a run of consecutive spike bars of one direction**, merged across gaps of up to
6 bars. It has a start, end, direction, price change and peak volatility. [builder's call: 6 bars]
A trailing window labels a bar only once volatility has risen, so a label lags the move. That lag is
why §7.3's lead-in exists.

Named windows in `regimes.toml` remain, as an operator's name attached to an overlapping event. Only
two 2024 episodes are named, so 2025 events are automatic until someone names them.

### 7.2 The summary

`NORMAL`, `SHOCK_UP`, `SHOCK_DOWN`, always all three rows, never pooled. Columns: the question each
asks, number of events, graded decisions, panel correct, each seat's correct, unscored. The long-only
truth table applies (§9): standing aside from a fall while flat is correct.

### 7.3 One event

A list of the period's events, largest move first, with a marker on the timeline. Opening one shows:

- **The move:** start, end, direction, size, on a price chart.
- **Each seat's calls through it**, in three stretches: **lead-in** (the 48 hours before the event's
  start, the same as the news window [builder's call]), **during**, and **after**. Lead-in is the one
  that answers "did it predict". A decision taken just before a spike is labelled `NORMAL` yet is
  graded on that spike, so it must be shown with the event or it disappears from the answer.
- **The news on a timeline against price**, split in two and never mixed:
  - **What the panel could see:** items whose `observed_at` was at or before that decision. These are
    the same items `ArchiveNewsFeed` served it.
  - **Published later:** items afterwards, for explaining the move. Labelled so they can't read as
    evidence the panel had. Merging them would be look-ahead, flattering hindsight.
- **Per seat:** whether it voted ahead of the move in the right direction, and the argument it gave,
  so the operator can see whether it cited the news.

**An event with no news is stated, not hidden.** A quiet window and a window outside coverage stay
distinct (§6.8.3).

### 7.4 The news this requires

Raw 2025 news already exists in `data/history-2025/news-raw`: about 12,000 articles, every month,
from `bitcoincom` and `cryptoslate`. It has **not been summarised or published**, and the panel only
reads a published archive. So:

- `evaluate period` takes `--archive`, feeding `build_sim(news_feed=…)` through the existing seam,
  and the archive digest joins the run's identity.
- A run without a real archive still works but is stamped `NEWS-BLIND` or `PLUMBING CHECK`, and the
  event page says *the seats had no news, so this says nothing about reading it*.
- **The summarising pass spends real money** and is the roadmap's D2 and Stage 5. It is a precondition
  for the purpose of this section, though not for building the page. The operator's decision: build
  first and summarise later, starting with a measured cost on a small sample.

Coverage: two crypto outlets, so a macro-driven move may have thin coverage. A move with no matching
news reads as that, never as proof that news didn't cause it.

## 8. Shape of the change

- **New module `decision_lab/period.py`:** parse a period, compute bounds and identity, drive the
  run, record lifts, month-end valuations and the meta. Mirrors `longrun.py`.
- **CLI:** `python -m decision_lab evaluate period --data … --configs … --candidate … --start-equity …
  --every … --period 2025-Q2 --budget …`. **A run from the page is a child of this command**, so a
  new entry in `routes/jobs.py`'s `BUILDERS`.
- **Read side:** extend `analysis.py`, the one read assembly, so the page, CLI report and notebook
  agree. Scoring reuses `records`, `scoring.score_records`, `seats.score_seats` and `by_regime`.
- **Dashboard:** a new route and templates. Charts follow `dashboard/chart.py`'s rule in the bot:
  floats only as coordinates. `test_discipline.py` forbids `float` in this package, so chart
  coordinates are computed as `Decimal` or with integers and rendered as strings. [builder's call]
- **Discipline:** both gates, `.\decision_lab\check.ps1` and `.\check.ps1`.

## 9. Tests that matter

- A forced loss streak across a month boundary: the pause lifts once, the limit in force is pinned on
  later cycles, money and position carry, and the lift appears on the page.
- A kill switch trip does **not** lift, and later months read *not run*.
- A held position's stop triggers during a paused stretch and its loss is counted, with no panel call.
- Quarantined stretch cycles are neither scored nor counted as cycles run.
- A paused month's end values rather than reading `UNVALUABLE`.
- Selecting a month runs only that month: no cycle outside its bounds.
- Same identity is reused with no spend; a changed seat mints a new run.
- All three spike rows render with zero decisions, and up and down are never pooled.
- Separation: no `tradebot/` diff unless §5.7's seam was forced, and nothing is written outside the
  workspace.
- Continue after a kill at a month boundary produces the same ledger and event outcomes as an
  uninterrupted run with a scripted panel.
- A crash mid-month resumes from the previous month end, and budget already spent still counts.
- Out of money and a kill switch trip both finish as *Model failed*, with no Continue.
- An event's "could see" list never holds an item with `observed_at` after the decision.
- A decision in the lead-in appears on its event even though it is labelled `NORMAL`.
- A basket built for BTC decides nothing about ETH.

## 10. Prerequisites

- The intra-cycle poll fix was merged to local `main` on 2026-11-01 as `437d3e3`.
- A real result on spikes needs §7.4's summarising pass.

## 11. Left to the builder

Template layout and pagination for a year's cycles, module boundaries inside `period.py`, how the
month driver is factored, and the exact wording of refusals, provided each names its reason.
