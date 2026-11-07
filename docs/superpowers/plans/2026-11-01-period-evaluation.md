# Period Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the decision_lab dashboard, evaluate one seat set over one chosen month, quarter or year on BTC, and read its profit over time, each graded decision, the seats' dispute, and its behaviour around market spikes with their news.

**Architecture:** One run is one workspace database folded by month. `decision_lab/period.py` drives the bot's public `Application` members with its own month loop, lifts a loss-streak pause in that database only, and checkpoints each month end so an interrupted run continues. `spikes.py` turns regime labels into events, `analysis.py` assembles the read, and the page starts runs as a child of the new CLI command.

**Tech Stack:** Python 3.11+, FastAPI + Jinja2 (server-rendered, no new JS dependency), inline SVG, pytest, existing `tradebot` and `decision_lab` modules.

**Spec:** [docs/superpowers/specs/2026-11-01-period-evaluation-design.md](../specs/2026-11-01-period-evaluation-design.md). Mockup: [2026-11-01-period-evaluation-mockup.html](../specs/2026-11-01-period-evaluation-mockup.html) (invented numbers).

## Global Constraints

- Base: local `main` at `437d3e3`. Work on a new branch `period-evaluation`.
- No change under `tradebot/`, unless Task 5's equivalence test forces spec §5.7's seam. Then stop and report before writing it.
- Nothing under `tradebot/` names `decision_lab`. Nothing writes to a bot database.
- No `float` anywhere in `decision_lab`, including chart coordinates (`test_discipline.py`).
- Money is `Decimal`; time comes from an injected `Clock`; errors are classified; docstrings state failure semantics at module level; comments explain why and cite the spec.
- A page start is a child of the CLI; every refusal is an exit code the command has (`EXIT_DATASET` 3, `EXIT_CANDIDATE` 4, `EXIT_BUDGET` 5, `EXIT_GATE` 6, `EXIT_BUSY` 7).
- `SHOCK_UP` and `SHOCK_DOWN` are never pooled; all three regime rows always render. Unscored is a verdict with a reason, never dropped. A frozen portfolio shows `UNVALUABLE` and no figure.
- A lifted pause is shown wherever that run's profit appears. A stub binding stamps `PLUMBING CHECK`; no archive stamps `NEWS-BLIND`.
- Before each commit: `.\decision_lab\check.ps1`. At the last task also `.\check.ps1`.
- Tests use the helpers in `decision_lab/tests/factories.py` (`write_dataset`, `walk`, `shocked_walk`, `instrument`, `stub_matrix_file`) and a seeded stub as in `test_slice_b_end_to_end.py`, since `varied-*` seats are otherwise unseeded.

## Review Focus

1. **A period the dataset only partly covers, or a typed period that doesn't exist** (`2025-13`, `2024` on a Jan–Jun dataset): refuse with exit 3, naming what is covered. Tested in Task 1.
2. **Pressing Continue when the seat set changed since the run began**: the identity no longer matches, so it starts a new run and never resumes under different seats. Tested in Task 5.
3. **Two consecutive pauses, or a pause in the period's last month**: each lifts once, with the streak computed fresh; a final-month pause has no lift and still values at the end. Tested in Task 3.
4. **A run still going while its page is open**: a half-written meta or a live database renders as *running* with finished months only, never a 500. Tested in Task 9.
5. **An event overlapping the period's edge, or a period with no spikes**: lead-in clipped at the first decision and labelled; no events reads *No spikes in this period*, with all three rows still showing. Tested in Tasks 7 and 9.

---

### Task 1: Period parsing and coverage

**Files:**
- Create: `decision_lab/period.py`
- Test: `decision_lab/tests/test_period_parse.py`

**Interfaces:**
- Produces: `class Period(DomainModel)` with `label: str`, `start: UtcDatetime`, `end: UtcDatetime` (exclusive), `months: tuple[Period, ...]` as a property. `parse_period(text: str) -> Period` raising `ConfigError`. `covered_periods(first: datetime, last: datetime) -> dict[str, tuple[str, ...]]` keyed `"month"`, `"quarter"`, `"year"`. `require_covered(period, first, last) -> None` raising `ConfigError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_month_is_one_month() -> None:
    p = parse_period("2025-03")
    assert (p.start, p.end) == (utc(2025, 3, 1), utc(2025, 4, 1))
    assert [m.label for m in p.months] == ["2025-03"]

def test_a_quarter_holds_three_months() -> None:
    assert [m.label for m in parse_period("2025-Q2").months] == ["2025-04", "2025-05", "2025-06"]

def test_a_year_holds_twelve_and_december_ends_at_new_year() -> None:
    p = parse_period("2025")
    assert len(p.months) == 12 and p.end == utc(2026, 1, 1)

@pytest.mark.parametrize("bad", ["2025-13", "2025-Q5", "25", "2025-3", "", "last year"])
def test_an_unreadable_period_refuses(bad: str) -> None:
    with pytest.raises(ConfigError, match=bad or "period"):
        parse_period(bad)

def test_only_fully_covered_periods_are_offered() -> None:
    offered = covered_periods(utc(2024, 1, 1), utc(2024, 6, 30, 23))
    assert offered["year"] == ()
    assert offered["quarter"] == ("2024-Q1", "2024-Q2")
    assert offered["month"][-1] == "2024-06"

def test_require_covered_names_what_exists() -> None:
    with pytest.raises(ConfigError, match="2024-01-01"):
        require_covered(parse_period("2024"), utc(2024, 1, 1), utc(2024, 6, 30, 23))
```

- [ ] **Step 2: Run:** `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_period_parse.py -q`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement the four names above** with the module docstring stating failure semantics. "Covered" means the last bar is in the period's final hour.
- [ ] **Step 4: Run the tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision_lab): a period is a month, a quarter or a year`.

### Task 2: The month driver, one uninterrupted run

**Files:**
- Modify: `decision_lab/period.py`
- Test: `decision_lab/tests/test_period_run.py`

**Interfaces:**
- Consumes: `Period`; `longrun.profit_of`, `longrun.Profit`, `longrun.veto_breakdown`, `longrun.VetoRow`; `tradebot.validation.backtest.plan_ticks`, `warmup_for`; `Evidence.gather(store)`; `build_sim`, `dataset_catalogue`; `corpus.config_digest`.
- Produces:
  - `class RunEnding(StrEnum)`: `RUNNING`, `COMPLETED`, `INTERRUPTED`, `RULE_ENDED`.
  - `class MonthResult(DomainModel)`: `label`, `start_equity`, `end_equity`, `unvaluable: bool`, `planned_cycles`, `ran_cycles`, `paused_at: UtcDatetime | None`, `cost_usd`.
  - `class PauseLift(DomainModel)`: `at`, `streak: int`, `new_limit: int`.
  - `class PeriodMeta(DomainModel)`: `run_id`, `period`, `dataset_directory`, `dataset_digest`, `candidate_id`, `basket_digest`, `panel_digest`, `archive_digest`, `cadence_seconds`, `start_equity`, `budget`, `first_cycle_at`, `months: tuple[MonthResult, ...]`, `lifts: tuple[PauseLift, ...]`, `profit: Profit`, `vetoes`, `incidents`, `ending: RunEnding`, `ending_reason: str`, `gate_skipped: bool`, `plumbing_check: bool`, `news_blind: bool`.
  - `period_dir(run_id, *, workspace=None) -> Path` (`workspace/period-<run_id>/`), and constants `PERIOD_DB = "period.db"`, `PERIOD_META = "period.json"`.
  - `period_basket(candidate: Candidate, dataset: ReplayDataset, cadence_seconds: int) -> Basket`: copies `candidate.basket` with `basket_id="period"`, instruments = BTC/USDT from the dataset only, refusing with `ConfigError` if BTC/USDT is absent.
  - `async def run(*, data_dir, candidate, period, cadence_seconds, start_equity, budget, wall_clock, news_feed=None, archive_digest="", gate_skipped=False, plumbing_check=False, workspace=None) -> PeriodMeta`.

- [ ] **Step 1: Write the failing tests** over a verified dataset built with `f.write_dataset`, spanning 3 months plus warm-up.

```python
async def test_only_the_selected_month_runs(...) -> None:
    meta = await run_period("2024-03")
    stamps = cycle_stamps(meta)
    assert stamps and all(utc(2024, 3, 1) <= s < utc(2024, 4, 1) for s in stamps)
    assert meta.first_cycle_at < utc(2024, 3, 1) + cadence  # warm-up read from before the period

async def test_a_short_history_starts_late_and_says_so(...) -> None:
    meta = await run_period("2024-01")  # dataset starts Jan 1
    assert meta.first_cycle_at > utc(2024, 1, 1) and meta.months[0].ran_cycles < full_month

async def test_money_and_position_carry_between_months(...) -> None:
    meta = await run_period("2024-Q1")
    assert meta.months[1].start_equity == meta.months[0].end_equity
    assert meta.months[0].end_equity == meta.start_equity + month_profit_from_log(meta, 0)

async def test_btc_only_on_a_two_coin_dataset(...) -> None:
    assert decided_instruments(await run_period("2024-03")) == {"binance:BTC/USDT"}

async def test_the_driver_matches_the_harness_on_one_window(...) -> None:
    """The loop must not drift from BacktestHarness (spec §5.4). Scripted panel, same window."""
    assert fills_of(await run_period("2024-03")) == fills_from_backtest_harness("2024-03")

async def test_the_same_identity_is_reused_without_spend(...) -> None: ...
async def test_a_changed_risk_limit_mints_a_new_run(...) -> None: ...
async def test_an_interrupted_database_with_no_meta_refuses(...) -> None: ...
```

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement `period_basket`, identity and `run`.** Identity is `registry.run_id(scenario="evaluate-period", ...)` over dataset digest, `config_digest(basket)`, cadence, start equity, period label, archive digest; budget excluded. Requested start = `period.start − warmup_for((basket,))`. Build with `build_sim(clock=, db_path=, baskets=(basket,), start_equity=, market_data=dataset.market_data, catalogue=dataset_catalogue(dataset), news_sources=(), news_feed=)`.
  Loop:

```python
await application.recover()  # once per process; a halted recovery raises FailClosedError
for month in period.months:
    for moment, basket_id in plan_ticks(baskets, start=max(month.start, start), end=month.end, limit=...):
        clock.set(moment)
        worker = application.supervisor.worker_for(basket_id)
        await worker.cycle()
        await application.monitor.poll()
    # Application.valuation() at month end → MonthResult. Task 3 adds the pause branch here.
```

  `plan_ticks` includes `end`, so drop a tick at exactly `month.end` (it belongs to the next month). Copy `longrun.run`'s reuse and refuse-if-interrupted structure and its comments' reasons. Write `period.json` atomically (tmp + replace) after every month, with `ending=RUNNING`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision_lab): drive one basket through a period, month by month`.

### Task 3: A paused month, lifted at the boundary

**Files:**
- Modify: `decision_lab/period.py`
- Test: `decision_lab/tests/test_period_pause.py`

**Interfaces:**
- Consumes: `tradebot.risk.rules.AUTO_PAUSE_RULE`; `application.states.set_basket_status(id, BasketStatus.ACTIVE, reason)`, `.status_of`; `application.configs.put(id, basket, actor=, note=)`, `.latest`; `Ledger`'s round trips via the store; `RiskPolicy.quarantined`, `.max_consecutive_losses`.
- Produces: `async def stopped_on_loss_streak(application, basket_id) -> bool`, `async def quarantine_stretch(application, basket) -> None`, `async def lift_pause(application, basket, *, configured_limit: int, at) -> PauseLift`, `ACTOR = "decision_lab.period"`.

- [ ] **Step 1: Write the failing tests**, scripting panel votes and prices to force four losing round trips in month 1.

```python
async def test_a_pause_stops_panel_calls_but_lets_a_held_stop_fire(...) -> None:
    meta = await run_with_streak_and_open_position()
    assert panel_calls_between(meta, paused_at, next_month) == 0
    assert stop_fill_between(meta, paused_at, next_month) is not None
    assert meta.months[0].end_equity < equity_before_the_stop

async def test_the_quarantined_stretch_is_neither_scored_nor_counted(...) -> None:
    meta = await run_with_streak()
    assert meta.months[0].ran_cycles == cycles_before_pause
    assert all(r.outcome != "quarantined" for r in scored_records(meta))

async def test_a_paused_month_end_values_rather_than_unvaluable(...) -> None:
    assert not (await run_with_streak()).months[0].unvaluable

async def test_the_next_month_trades_with_the_allowance_restored(...) -> None:
    meta = await run_with_streak()
    lift = meta.lifts[0]
    assert lift.new_limit == lift.streak + configured_limit
    assert meta.months[1].ran_cycles == meta.months[1].planned_cycles
    assert limit_pinned_on_cycles_after(lift) == lift.new_limit
    assert risk_event_recorded(meta, rule="decision_lab.pause_lifted")

async def test_two_pauses_lift_twice_from_the_fresh_streak(...) -> None: ...
async def test_a_pause_in_the_last_month_has_no_lift_and_still_values(...) -> None: ...
async def test_a_pause_on_a_month_without_later_months_ends_completed(...) -> None: ...
```

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** After `worker.cycle()`, if `worker.stopped`: `stopped_on_loss_streak` reads the newest `RISK_EVENT` with `rule == AUTO_PAUSE_RULE` and `action == "basket_paused"` for this basket, and requires the kill switch armed. If true, `quarantine_stretch` clears the status and `configs.put`s a copy with `risk_policy.quarantined=True`; remaining ticks of the month run through the same loop, tallied as stretch cycles rather than `ran_cycles`, and `worker_for` supplies a fresh worker. At the next month's start, if one exists, `lift_pause` `configs.put`s a copy with `quarantined=False` and `max_consecutive_losses = streak + configured_limit`, using `configs.latest` for the base document and `TradingHistory`'s streak source for `streak`, and appends a `RISK_EVENT` naming the lift. `configured_limit` comes from the candidate's basket, not the current version, so limits don't compound.
- [ ] **Step 4: Run them.** Expected: PASS. **If a resting stop doesn't fire across a quarantined cycle, stop and report**: the design's mechanism is wrong.
- [ ] **Step 5: Commit** `feat(decision_lab): a loss-streak pause ends the month, not the run`.

### Task 4: Endings: rules, out of money, budget

**Files:**
- Modify: `decision_lab/period.py`
- Test: `decision_lab/tests/test_period_endings.py`

**Interfaces:**
- Produces: `out_of_money(application, basket) -> bool`; `class BudgetReached(FailClosedError)`; `run` setting `ending`/`ending_reason`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_kill_switch_trip_is_final_and_names_the_rule(...) -> None:
    meta = await run_with_drawdown_trip()
    assert meta.ending is RunEnding.RULE_ENDED and "kill switch" in meta.ending_reason
    assert not meta.lifts
    assert not_run_months(meta)

async def test_out_of_money_ends_the_run_as_a_rule(...) -> None:
    meta = await run_with_cash_below_min_notional_and_flat()
    assert meta.ending is RunEnding.RULE_ENDED and "out of money" in meta.ending_reason

async def test_low_cash_while_holding_is_not_out_of_money(...) -> None: ...

async def test_budget_is_checked_at_month_end_and_is_resumable(...) -> None:
    meta = await run_with_budget_exhausted_in_first_month()
    assert meta.ending is RunEnding.INTERRUPTED and "budget" in meta.ending_reason
    assert len(meta.months) == 1

async def test_a_failed_cycle_halt_is_interrupted_not_a_verdict(...) -> None:
    assert (await run_with_scripted_failure()).ending is RunEnding.INTERRUPTED

async def test_a_rule_ended_run_is_reused_at_its_identity(...) -> None: ...
```

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Dispatch table on the stop's cause: kill switch tripped → `RULE_ENDED`; `AUTO_PAUSE_RULE` → Task 3; anything else halting the basket, including three `FAILED` cycles → `INTERRUPTED`. `out_of_money`, checked after each cycle: no position in `application.ledger` and free quote below `instrument.min_notional`. Budget compares `Evidence.gather(store).cost_usd` to `meta.budget` at each month end. Any unexpected exception out of the loop writes `INTERRUPTED` with its reason, then re-raises for the CLI's exit code.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision_lab): a rule ending a run is a verdict; an error is not`.

### Task 5: Checkpoints and Continue

**Files:**
- Modify: `decision_lab/period.py`
- Test: `decision_lab/tests/test_period_resume.py`

**Interfaces:**
- Consumes: `tradebot.maintenance.backup.take_backup(engine, destination, *, mode, clock)`; `open_database(path)`, which doesn't migrate; `recover()`.
- Produces: `CHECKPOINT_DIR = "checkpoints"`; `write_checkpoint(directory, month_label, *, clock) -> Path`; `restore_latest(directory) -> PeriodMeta | None`; `run(..., resume: bool = False)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_continuing_equals_an_uninterrupted_run(...) -> None:
    """Spec §5.7's deciding test. Scripted panel; killed between months 2 and 3."""
    whole = await run_period("2024-Q1", workspace=a)
    await run_period_until("2024-Q1", kill_after="2024-02", workspace=b)
    resumed = await run_period("2024-Q1", workspace=b, resume=True)
    assert ledger_outcomes(resumed) == ledger_outcomes(whole)
    assert fills_of(resumed) == fills_of(whole)
    assert resumed.months == whole.months

async def test_a_trade_with_a_triggered_unfilled_stop_survives_a_continue(...) -> None:
    """`SimBroker.restore` comes back un-armed. Must equal the uninterrupted run, or the seam is needed."""

async def test_a_crash_mid_month_redoes_only_that_month(...) -> None:
    await crash_in("2024-02")
    resumed = await continue_run()
    assert [m.label for m in resumed.months] == ["2024-01", "2024-02", "2024-03"]
    assert no_cycle_from_abandoned_work(resumed)

async def test_spent_budget_still_counts_after_continue(...) -> None: ...
async def test_continue_with_no_checkpoint_restarts_from_the_start_keeping_the_old_db(...) -> None: ...
async def test_a_rule_ended_run_refuses_continue(...) -> None: ...
async def test_continue_after_a_seat_change_starts_a_new_run(...) -> None:
    """Review Focus 2: identity differs, so the old directory is untouched."""
```

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** After each month: shut nothing down; open a second engine with `open_database(db)`, `take_backup` it into `checkpoints/<label>/`, write the meta beside it, then replace atomically. On `resume`: move the live `period.db` to `period.db.abandoned-<stamp>` (kept), copy the newest checkpoint back, `build_sim(db_path=, baskets=None, start_equity=meta.start_equity, …)`, `clock.set` to the checkpoint's month end, `recover()`, continue with the remaining months. Abandoned spend stays counted: add the difference from the abandoned database's cost to the meta as `abandoned_cost_usd`, included in budget checks and the net.
- [ ] **Step 4: Run them.** Expected: PASS. **If the equivalence tests can't pass because of `restore`, stop and report.** Spec §5.7's approved seam applies, and it goes in as its own change with guard tests.
- [ ] **Step 5: Commit** `feat(decision_lab): checkpoint each month so an interrupted run continues`.

### Task 6: The CLI command

**Files:**
- Modify: `decision_lab/cli.py`, `decision_lab/registry.py`, `decision_lab/render.py`
- Test: `decision_lab/tests/test_cli_period.py`

**Interfaces:**
- Consumes: `period.run`; `calibrate_long`'s construction of the reference basket, gate and reachability refusals (`cli.py:1014-1055`); `load_feed(data, sources, policy)`.
- Produces: `python -m decision_lab evaluate period --data --configs --candidate --start-equity --every --period --budget [--news-source …] [--policy …] [--skip-gate] [--continue] [--out]`; `rd.PeriodReport`, `rd.write_period`; registry scenario `"evaluate-period"`.

- [ ] **Step 1: Write the failing tests.** Exit codes: unreadable period → 2; uncovered → 3; unknown candidate → 2; missing `--budget` → 2; gate unsatisfied → 6, before any provider check; workspace held → 7; projection over budget → 5, nothing spent; `--skip-gate` with no calibration runs, and the report and registry row say both *gate skipped* and *no projection*. A stub matrix stamps `PLUMBING CHECK`, no `--news-source` stamps `NEWS-BLIND`. The report prints each lift and each ending.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Factor the shared opening of `calibrate_long` into `_load_candidate(args, clock) -> (matrix, candidate, record) | exit code`, with both commands calling it, and `test_cli_calibrate` unchanged. Projection = gate record's `cost_per_cycle` × planned cycles.
- [ ] **Step 4: Run them, and `test_cli_calibrate.py`.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision_lab): evaluate period, a command with its own report`.

### Task 7: Spike events

**Files:**
- Create: `decision_lab/spikes.py`
- Test: `decision_lab/tests/test_spikes.py`

**Interfaces:**
- Consumes: `RegimeIndex.labels`, `BarLabel`, `RegimeLabel`; `ScoredDecision`; `CycleRecord.snapshot.news`; `PublishedArchive.read`, `ArchiveItem`.
- Produces:
  - `MERGE_GAP_BARS: Final = 6`, `LEAD_IN: Final = timedelta(hours=48)`.
  - `class SpikeEvent(DomainModel)`: `start`, `end`, `direction: RegimeLabel`, `move_pct: Money`, `peak_volatility: Money`, `window_name: str = ""`.
  - `find_events(bars: Sequence[BarLabel], *, windows=()) -> tuple[SpikeEvent, ...]`.
  - `class EventStretches`: `lead_in`, `during`, `after`, each a tuple of `ScoredDecision`; `clipped: bool`.
  - `stretches(event, decisions) -> EventStretches`.
  - `class EventNews`: `could_see: tuple[NewsItemView, ...]`, `published_later: tuple[ArchiveItem, ...]`, `covered: bool`.
  - `news_for(event, records, archive_items, *, coverage) -> EventNews`.

- [ ] **Step 1: Write the failing tests**

```python
def test_consecutive_shock_bars_of_one_direction_are_one_event() -> None: ...
def test_a_gap_of_six_merges_and_seven_splits() -> None: ...
def test_an_opposite_direction_never_merges() -> None: ...

def test_a_decision_labelled_normal_before_the_spike_is_in_its_lead_in() -> None:
    event = event_at(utc(2025, 3, 10), SHOCK_DOWN)
    d = scored(as_of=utc(2025, 3, 9), regime=Pool.NORMAL)
    assert stretches(event, [d]).lead_in == (d,)

def test_a_lead_in_clipped_by_the_period_edge_says_so() -> None:
    event = event_at(utc(2025, 3, 1, 6), SHOCK_DOWN)
    assert stretches(event, [scored(as_of=utc(2025, 3, 1, 2))]).clipped

def test_could_see_is_only_what_the_snapshots_served() -> None:
    n = news_for(event, records_with_news(), items, coverage=True)
    assert all(item.observed_at <= last_decision for item in n.could_see)

def test_published_later_never_appears_in_could_see() -> None:
    n = news_for(event, records_with_news(), items, coverage=True)
    assert {i.url_hash for i in n.published_later}.isdisjoint(served_hashes(n.could_see))

def test_no_coverage_and_a_quiet_window_differ() -> None:
    assert not news_for(event_outside_archive, (), (), coverage=False).covered
    assert news_for(event_inside_archive, (), (), coverage=True).covered
```

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `could_see` reads `snapshot.news`, the items actually served, never a re-selection. `published_later` is archive items with `observed_at` after the event's last decision and at or before `event.end + LEAD_IN`, de-duplicated by `url_hash`, excluding anything served.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision_lab): spikes are dated events, with what the panel could see`.

### Task 8: The read assembly

**Files:**
- Modify: `decision_lab/analysis.py`
- Test: `decision_lab/tests/test_analysis_period.py`

**Interfaces:**
- Consumes: `period.PeriodMeta`, `period_dir`; `records.records_from_store`; `scoring.score_records`, `by_regime`; `seats.score_seats`; `spikes`.
- Produces: `class PeriodAnalysis` holding `meta`, `records`, `scored`, `seats: tuple[SeatMetrics, ...]`, `regimes` (always all three), `events`, `by_month: Mapping[str, tuple[ScoredDecision, ...]]`, `seat_trend: Mapping[str, Mapping[str, Decimal | None]]`, `contaminated: int`; `analyse_period(run_id, scoring: Scoring, *, panel, archive_items=(), workspace=None) -> PeriodAnalysis`; `running: bool` when `meta.ending is RUNNING`.

- [ ] **Step 1: Write the failing tests:** all three regime rows exist with zero decisions; quarantined cycles appear in none of `scored`, `by_month` or `seats`; a contaminated cycle is unscored and counted; `seat_trend` buckets by month for a quarter or less and by quarter for a year; a seat with no scored decisions in a bucket is `None`, not 0; a frozen meta carries no profit figure; the same page, CLI report and notebook call path gives one answer, by asserting `render.write_period` reads `PeriodAnalysis`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** using the module's existing patterns; open the database as `records.load` does, with `open_database`, which never migrates.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(decision_lab): one read assembly for a period run`.

### Task 9: The page

**Files:**
- Create: `decision_lab/dashboard/routes/periods.py`, `decision_lab/dashboard/charts.py`, `decision_lab/dashboard/templates/periods.html`, `period_detail.html`, `_period_month.html`, `_period_cycle.html`, `_period_event.html`
- Modify: `decision_lab/dashboard/app.py:61-63`, `decision_lab/dashboard/routes/jobs.py` (`BUILDERS`), `decision_lab/dashboard/templates/base.html` (nav)
- Test: `decision_lab/tests/test_lab_dashboard_periods.py`, `decision_lab/tests/test_lab_dashboard_charts.py`

**Interfaces:**
- Consumes: `analyse_period`; `covered_periods`; `jobs.start`; `state.cache`.
- Produces: routes `GET /periods` (form and list), `GET /periods/{run_id}`, `GET /periods/{run_id}/months/{label}`, `GET /periods/{run_id}/cycles/{cycle_id}`, `GET /periods/{run_id}/events/{index}`, `POST /periods/{run_id}/continue`; builder `"evaluate-period"`; `charts.line_svg(points, *, width, height) -> str`, `charts.lanes_svg(prices, seats, spikes) -> str`, `charts.after_svg(prices, *, band, decision_at) -> str`, all using `Decimal` coordinates rendered as strings.

- [ ] **Step 1: Write the failing tests**

```python
def test_auth_covers_every_new_route(lab_client) -> None: ...  # as test_lab_dashboard_auth does
def test_the_form_offers_only_covered_periods(lab_client) -> None: ...
def test_budget_has_no_default_and_is_required(lab_client) -> None:
    assert 'name="budget"' in page and 'required' in budget_input and 'value=' not in budget_input

def test_run_builds_the_cli_argv(lab_client) -> None:
    assert started_argv()[:2] == ["evaluate", "period"]

def test_a_refusal_rerenders_with_the_reason(lab_client) -> None: ...
def test_a_lift_is_on_the_page_beside_the_profit(lab_client) -> None: ...
def test_all_spike_rows_render_with_no_events(lab_client) -> None:
    assert all(x in page for x in ("Normal", "Spike up", "Spike down", "No spikes in this period"))

def test_model_failed_has_no_continue_and_interrupted_does(lab_client) -> None: ...
def test_a_running_job_with_half_written_meta_is_not_a_500(lab_client) -> None:
    """Review Focus 4."""

def test_could_see_and_published_later_are_separate_blocks(lab_client) -> None: ...
def test_a_cycle_shows_dispute_result_market_and_grades(lab_client) -> None: ...
def test_news_blind_page_says_the_seats_had_no_news(lab_client) -> None: ...
def test_chart_output_has_no_float_module(tmp) -> None: ...  # `charts.py` passes test_discipline
```

- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Match the mockup's order and spec §4; drill-downs are `<details>` holding `hx-get` fragments, loaded on open, so a year doesn't render 2,000 cycles at once. Selection is in the URL. BTC only. `decision.html`'s seat-vote tables are the base for `_period_cycle.html`, with rounds in order. The page's cost note says *a run can overshoot by up to a month's spend, and a one-month run has no ceiling while it runs*.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Look at it.** Run the dashboard over a stub run per the `run` skill and screenshot it. Check labels don't collide on a year's chart.
- [ ] **Step 6: Commit** `feat(decision_lab): evaluate a period from the dashboard`.

### Task 10: Create or update on save, and the record

**Files:**
- Modify: `decision_lab/dashboard/routes/matrices.py`, `decision_lab/dashboard/templates/matrix_edit.html`, `CLAUDE.md`, `docs/superpowers/plans/2026-10-27-roadmap-and-agent-plan.md`
- Create: `docs/adr/0031-a-period-run-lifts-a-loss-pause-in-its-own-database.md`
- Test: `decision_lab/tests/test_lab_dashboard_edit.py`, `decision_lab/tests/test_slice_period_end_to_end.py`

**Interfaces:**
- Consumes: `matrices.save`, `matrices.names`; every earlier task.

- [ ] **Step 1: Write the failing tests:** saving a changed set asks **Create new seat set** or **Update `<name>`**, with the new name required for Create and an existing name refused; Update makes a new version and Create leaves the old set unchanged; both say *uncalibrated until the nine days run again*; a save that changes nothing doesn't ask; every field in a stored document still round-trips through the prompt, including `instruction`. End to end: verify a dataset, run `2024-Q1` on a stub panel from the page's argv, open the month, cycle and event pages.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The prompt re-renders the form with every submitted value intact in hidden fields, so nothing is lost on the second click. Write the ADR, add the rules easy to get backwards to `CLAUDE.md`'s decision_lab section, its command list, and the roadmap's status.
- [ ] **Step 4: Run all gates.** `.\decision_lab\check.ps1` and `.\check.ps1`. Expected: both pass. `git diff --stat main -- tradebot/` empty.
- [ ] **Step 5: Commit** `feat(decision_lab): a seat set change asks whether it is new, and period evaluation is recorded`.
