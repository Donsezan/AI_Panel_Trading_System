# KNOWN_GAPS §5, the intra-cycle half: a scoped poll inside the snapshot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Book every venue fill a cycle's own market reads can cause *before* the cycle reads what is held, and stop one unbookable fill from wedging the shared `ExecutionMonitor` for every basket.

**Architecture:** Option A of the memo: `ContextBuilder.build` does all of a basket's market reads, then awaits an injected `settle` hook, then does the position reads. `BasketRunner` supplies the hook as a poll of the process's one `ExecutionMonitor`, scoped to the basket's instrument keys; `ManualCloser._proposal` does the same (quote → poll → position). Plus Option D parts 1–2: `ExecutionMonitor.poll` isolates failures per group, and a group whose fill the ledger refuses is *poisoned* — recorded once as a `RISK_EVENT` (`rule: unbookable_fill`), kept tracked, retried every poll, and raised only to a caller whose scope contains it.

**Tech Stack:** Python 3.11, asyncio, pydantic, SQLAlchemy/SQLite event store, pytest + pytest-asyncio.

**Spec:** [docs/superpowers/specs/2026-10-28-known-gaps-5-intra-cycle-memo.md](../specs/2026-10-28-known-gaps-5-intra-cycle-memo.md) — read all of it. The operator approved its §4 recommendation; do not reopen the choice.

## Global Constraints

- Python 3.11: no PEP 695 (`type X = ...`, `def f[T]`). Every module keeps `from __future__ import annotations`.
- Money is `Decimal`, never `float`, never `Decimal(some_float)` (ADR 0001). Tests may use `Decimal("0.5")` literals and `/` on `Decimal`.
- Time comes from the injected `Clock`; never `datetime.now()` in library code.
- Errors are classified: `RetryableError` / `FailClosedError` / `FatalError`. `except Exception` is allowed only where it records-or-collects and the error still reaches a caller; a bare swallow is a defect.
- Every state change emits an event; the poison `RISK_EVENT` is that event for this change.
- Layering: nothing outside `tradebot/app.py` imports a concrete adapter. `tradebot/core/errors.py` depends on nothing but the stdlib.
- Fail closed: a poll that fails inside the snapshot means **no snapshot, no decision, no order** for that cycle.
- The poll writes no order *intent* in the cycle's name (non-negotiable #4 untouched). It may run `_maintain`, which resizes or re-arms protective legs exactly as the supervisor's sweep already does.
- Nothing under `tradebot/` may name `decision_lab`.
- Gates: `.\check.ps1` (root) **and** `.\decision_lab\check.ps1` must both pass before the last commit.
- Every commit message ends with the trailer line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Work on branch `known-gaps-5-intra-cycle`. Commit only the files a task names.
- Comments explain *why* and cite `ADR 0030` / `KNOWN_GAPS §5`; they do not restate code.

## Where the current code differs from the memo (read before Task 1)

The memo was written at `56038a1` on `decision-lab-slice-e`. Verified against today's tree:

1. **The veto is `sizing`, not `long_only`.** `Tier1RiskEngine.approve` runs `base_quantity` first, and `_size_sell` (`tradebot/risk/sizing.py:71-77`) vetoes a flat SELL as rule `sizing`, detail `"SELL while flat would open a short; v1 is long-only"`, before `LongOnlyRule` is reached. The rung-3 test below asserts `sizing`.
2. **There is no `228/228` assertion.** `test_slice_b_end_to_end.py` asserts exit codes and rendered regime rows only; "228/228" is a comment. An auto-paused short pass already passes it.
3. **The pin reaches further than the memo says.** `seeded_stub` is imported by `decision_lab/tests/conftest.py` (`built_corpus_id`, `calibrated_corpus`, via `built_corpus`) and by `decision_lab/tests/test_slice_e_end_to_end.py:37,225`. Task 8 therefore measures the whole `decision_lab/tests` suite per seed, not one file.
4. **After D, a §5 recurrence in a replay no longer crashes — it ends the pass short.** The harness's poll is unscoped and will not raise for a poisoned group, and the cycle that hit it has already halted the basket. "Every seed passes" is therefore not enough evidence on its own; Task 8 reads each corpus's `cycles`/`risk_events` tables.
5. **D part 3 is unnecessary — because of D part 2, independently of A.** `BacktestHarness._replay` (`tradebot/validation/backtest.py:223`) polls unscoped; an unscoped poll never raises for a poisoned group, and the existing `worker.stopped` check (`:224`) already ends the pass short. A removes the known trigger; D2 removes the crash path for any other trigger.
6. Line drift only: `app.py:1060` is now `:1081` (`prices=stack.prices` for `ManualCloser`), `app.py:980-984` is now `:1005`.

## Review Focus

1. **The venue is down during the in-snapshot poll.** Expect: `run_once` raises the `VenueError` to the worker (its existing failure path), no `SNAPSHOT_FROZEN` and no `ORDER_SUBMITTED` are written for that attempt. Test: Task 4 Step 7.
2. **A basket with no tracked groups.** The scoped poll must reach the venue zero times — that is the live-cost claim in ADR 0030. Test: Task 2 Step 1, second test.
3. **A manual close against a poisoned group.** Expect a refusal naming the fill (`UnbookableFillError`, a `TradebotError`, which the route renders as "Not done"), never a 500 and never an order. Test: Task 7 Step 9.
4. **A poisoned group whose holding is later corrected** (a reconcile, or an operator's fix). Expect: the next poll books the fill and stops raising. Test: Task 7 Step 1, fourth test.
5. **A venue error on one group in a sweep.** Expect: every other group is still synced *and* maintained, and the error still reaches the caller after them. Test: Task 7 Step 1, fifth test.

---

### Task 1: ADR 0030

**Files:**
- Create: `docs/adr/0030-a-reconciling-poll-precedes-the-position-read.md`

**Interfaces:**
- Produces: the ADR number every later comment cites (`ADR 0030`).

- [ ] **Step 1: Write the ADR with exactly this content** (format follows ADR 0027 and 0029; replace `<today>` with `Get-Date -Format yyyy-MM-dd`)

````markdown
# 30. A reconciling poll runs between the snapshot's market reads and its position reads

Date: <today>

## Status

Accepted. Closes the intra-cycle half of [KNOWN_GAPS](../KNOWN_GAPS.md) §5, per
[the decision memo](../superpowers/specs/2026-10-28-known-gaps-5-intra-cycle-memo.md) (Option A,
with Option D parts 1–2).

## Context

On a `SimBroker` stack — sim, a replay, and paper, whose primary venue is `SimBroker`
(ADR 0020) — the venue matches resting orders **only when someone reads `stack.prices`**, and only
a cycle's snapshot and a manual close do. So a venue-held stop can fill *during* the snapshot's
own candle read. `ContextBuilder` then read `ledger.position` from a ledger that had not booked
it, the panel was told it held coins the venue had sold, `_size_sell` sized a SELL from the same
stale holding, `SimBroker` accepted it (free base may go negative), and the next poll refused the
stop's fill in `Ledger._apply_sell` as `sell of … exceeds holding …`.

That error was correctly classified (`ReconciliationMismatchError`, fail-closed) and never
consumed. The fill stayed unbooked, every later `poll()` re-read it and raised again, and because
`poll()` had no per-group isolation it stopped syncing every group after it — in a supervised
process, every basket's fills, TTLs and legs — and failed each sibling basket's next `_settle` in
turn. In a replay the harness's own unguarded poll crashed the build.

The 30-second supervisor sweep does not cover this on a `SimBroker` stack: it reads
`read_only_prices`, so it can only book what a cycle's read has already matched, by which time
that cycle has sized.

## Decision

- **`ContextBuilder.build` reads every instrument's market data, then awaits an injected `settle`
  hook, then reads every position.** The runner supplies the hook: a poll of the process's one
  `ExecutionMonitor`, scoped to the basket's instrument keys (ADR 0026 means no other basket's
  group sits on them). The frozen snapshot is then truthful about what is held.
- **`ManualCloser._proposal` does the same:** quote → scoped poll → position.
- **The same code runs in every mode, live included** (ADR 0020). A separate replay-only fix
  would mean a replay no longer exercises the thing a soak runs.
- **A poll failure is the worker's existing failure path.** A `RetryableError` propagates out of
  `run_once` to the worker's backoff, exactly as a failed submit does; a `FailClosedError` ends the
  cycle `FAILED` and halts the basket. Either way: no snapshot, no decision, no order.
- **`ExecutionMonitor.poll` isolates failures per group.** It syncs every group in scope, collects
  errors, still maintains the healthy groups, and re-raises the first error afterwards.
- **A group whose fill the ledger refuses is poisoned.** One `RISK_EVENT`
  (`rule: unbookable_fill`, naming the order, the quantity and the holding), the group stays
  tracked and is retried every poll, and the poison is raised only to a caller whose scope contains
  that group. An unscoped poll — the supervisor's sweep, the backtest harness — never raises for it.
  The fill is never dropped; it books on the first poll after the holding allows it.

## Consequences

- **One extra venue read per cycle, before the decision.** On Binance a protected position is one
  OCO group of two legs at `queryOrder` weight 4 each: about **8 weight per held, protected
  instrument per cycle**, plus `myTrades` (20) on the poll that discovers a fill. Against the
  600/min `RateBudget`: a two-instrument basket at 1h cadence is ~16 weight/hour (~0.05%); five
  baskets × two instruments at a 1-minute cadence is 80/min (~13%), the same order as the 30s sweep
  already spends. A basket holding nothing costs nothing: `poll` over no tracked group reaches no
  venue.
- **The ordering change is in the fail-closed direction.** A reconciling *read* now precedes
  sizing, which is what non-negotiable #3 asks for; it adds no order intent (#4) and an error means
  no trade (#1). The poll may resize or re-arm protective legs mid-cycle, exactly as the sweep does.
- **What it does not close:** in live, a venue can still fill a stop *during deliberation*, after
  the poll. On Binance spot and Alpaca the resting stop reserves the base asset, so the cycle's
  SELL is *rejected* rather than overselling, and the stop's fill then books cleanly (from venue
  documentation; live has never run). A poll placed immediately before sizing (the memo's Option
  B) could be added on top if that window ever matters.
- A pass of `decision_lab` that would have died on `exceeds holding` no longer reaches that state;
  if some other unbookable fill appears, it ends the pass short (basket halted) rather than
  crashing it.

## Follow-ups, deliberately not done here

- **`SimBroker` accepts a SELL beyond free base** (Option E). It must ship with KNOWN_GAPS §6's fix
  — release the legs before a discretionary exit — or it refuses every exit while legs rest. It
  re-baselines every soak and corpus.
- **`ExecutionService.cancel` ignores `ack.cancelled=False`** and advances to terminal `CANCELLED`,
  so a fill racing the cancel is lost to the monitor (KNOWN_GAPS §10).
- **The evidence question:** soak and corpus evidence so far contains discretionary exits live
  Binance would have refused. Whether it still counts is the operator's call.

## Rules that are easy to get backwards

- **The hook goes after *every* market read and before *every* position read**, not per
  instrument. Per instrument, the second instrument's candle read could match a stop after the
  first instrument's position was read.
- **Scope by instrument key, not by basket id.** A group's `basket_id` is where it came from; what
  the snapshot needs settled is every group that can move the holdings it is about to read.
- **Poison is raised only to scoped callers.** Raising it to the sweep is the wedge this ADR ends;
  swallowing it from the scoped caller is a basket sizing against a ledger known to be wrong.
- **Never clamp in `_apply_sell`.** Absorbing the disagreement into the ledger is the reconciler's
  job (ADR 0006) and would hide real corruption.
````

- [ ] **Step 2: Commit**

```powershell
git add docs/adr/0030-a-reconciling-poll-precedes-the-position-read.md
git commit -m "docs(adr): 0030 a reconciling poll precedes the position read" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `ExecutionMonitor.poll` takes a scope

**Files:**
- Modify: `tradebot/execution/monitor.py:166-179` (`poll`) and the module docstring's "Who calls it" (`:11-21`)
- Test: `tests/unit/test_monitor.py` (new class `TestScopedPoll`, after `TestBooking`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `ExecutionMonitor.poll(self, instrument_keys: frozenset[str] | None = None) -> None`. `None` means every tracked group (today's behaviour, used by the supervisor sweep and the backtest harness). A set means only groups whose `group.order.instrument_key` is in it are synced and maintained.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_monitor.py` (add `second_instrument: Instrument` from `tests/conftest.py`; reuse this file's `tick`, `submit_entry`, `PLAN`)

```python
class TestScopedPoll:
    """ADR 0030: a cycle settles its own basket's instruments, and nothing else's."""

    async def test_a_scoped_poll_touches_only_the_named_instruments(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        # Both entries rest below the market, then both markets trade through them.
        broker.observe(tick(instrument, clock, last="51000"))
        broker.observe(tick(second_instrument, clock, last="3000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument,
                           price="50000", ttl=None, plan=None)
        await submit_entry(monitor, broker, store, ledger, clock, second_instrument,
                           price="2900", qty="1", ttl=None, plan=None, coid="sim-ETH")
        clock.advance(60)
        broker.observe(tick(instrument, clock, last="49000"))
        broker.observe(tick(second_instrument, clock, last="2800"))

        await monitor.poll(frozenset({second_instrument.key}))

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert ledger.position(instrument.key).is_flat, "outside the scope: not synced"

        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.5")

    async def test_a_scope_holding_no_group_reaches_no_venue(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        second_instrument: Instrument,
    ) -> None:
        """The live-cost claim in ADR 0030: a basket holding nothing pays nothing."""
        broker.observe(tick(instrument, clock, last="51000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument,
                           price="50000", ttl=None, plan=None)
        calls: list[str] = []
        real = broker.fetch_order

        async def counted(ref: OrderRef) -> object:
            calls.append(ref.client_order_id)
            return await real(ref)

        broker.fetch_order = counted  # type: ignore[method-assign]
        await monitor.poll(frozenset({second_instrument.key}))

        assert calls == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -k TestScopedPoll -q`
Expected: FAIL with `TypeError: ExecutionMonitor.poll() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Implement `poll(self, instrument_keys: frozenset[str] | None = None) -> None`**

Filter `self._tracked.values()` to the in-scope groups once, under the existing `self._polling` lock, and use that list for both the sync loop and the `_maintain` loop. `_targets()` stays computed over everything (it reads only the ledger and tracked state, no venue call). Document the parameter in `poll`'s docstring; the module docstring's "Who calls it" is Task 4's, once the callers exist.

- [ ] **Step 4: Run the file**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q`
Expected: PASS (all, including the existing tests, which call `poll()` unscoped)

- [ ] **Step 5: Commit**

```powershell
git add tradebot/execution/monitor.py tests/unit/test_monitor.py
git commit -m "feat(execution): ExecutionMonitor.poll takes an instrument scope (ADR 0030)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `ContextBuilder.build` takes a `settle` hook between market and position reads

**Files:**
- Modify: `tradebot/control/context_builder.py:105-122` (`build`) and `:158-181` (`_build_instrument`, split in two); module docstring gains one failure-semantics line
- Test: `tests/unit/test_context_builder.py` (new class `TestSettleHook`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Settle = Callable[[], Awaitable[None]]` — module-level alias in `context_builder.py`.
  - `ContextBuilder.build(self, basket: Basket, *, news: tuple[NewsItemView, ...] | None = None, settle: Settle | None = None) -> ContextSnapshot`. `settle=None` behaves exactly as today minus the reorder (readiness and the contract suite pass nothing).
  - Private: `_MarketReads` (frozen, slotted dataclass: `summaries: tuple[tuple[str, str], ...]`, `readings: tuple[IndicatorReading, ...]`, `quote: Quote`); `_market_reads(self, instrument: Instrument) -> _MarketReads`; `_instrument_context(self, instrument: Instrument, basket_id: str, market: _MarketReads) -> InstrumentContext`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_context_builder.py`

```python
class RecordingMarketData:
    """Wraps a provider and writes every read into a shared trace."""

    provider_id = "recording"

    def __init__(self, inner: ReplayMarketData, trace: list[str]) -> None:
        self._inner = inner
        self._trace = trace

    async def get_candles(self, instrument: Instrument, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self._trace.append(f"candles:{instrument.key}")
        return await self._inner.get_candles(instrument, *args, **kwargs)  # type: ignore[arg-type]

    async def get_quote(self, instrument: Instrument):  # type: ignore[no-untyped-def]
        self._trace.append(f"quote:{instrument.key}")
        return await self._inner.get_quote(instrument)

    def capabilities(self) -> DataCapabilities:
        return self._inner.capabilities()


class RecordingLedger(Ledger):
    def __init__(self, clock: ManualClock, trace: list[str]) -> None:
        super().__init__(clock, venue="sim", balances={"USDT": Decimal(10_000)})
        self._trace = trace

    def position(self, instrument_key: str) -> Position:
        self._trace.append(f"position:{instrument_key}")
        return super().position(instrument_key)


@pytest.fixture
def two_markets(
    instrument: Instrument, second_instrument: Instrument, clock: ManualClock
) -> ReplayMarketData:
    bars = 260
    return ReplayMarketData(
        {
            (held.key, timeframe): synthetic_candles(
                start=NOW - timeframe_interval(timeframe) * bars,
                timeframe=timeframe,
                count=bars,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for held in (instrument, second_instrument)
            for timeframe in ("1h", "4h", "1d")
        },
        clock,
    )


class TestSettleHook:
    """ADR 0030: what a market read made the venue do is booked before anything reads a holding."""

    async def test_the_hook_runs_after_every_market_read_and_before_every_position_read(
        self, two_markets: ReplayMarketData, clock: ManualClock, basket: Basket,
        instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        trace: list[str] = []
        pair = basket.model_copy(update={"instruments": (instrument, second_instrument)})
        builder = ContextBuilder(
            RecordingMarketData(two_markets, trace), RecordingLedger(clock, trace), clock
        )

        async def settle() -> None:
            trace.append("settle")

        await builder.build(pair, settle=settle)

        assert trace.count("settle") == 1
        cut = trace.index("settle")
        before, after = trace[:cut], trace[cut + 1 :]
        assert {f"quote:{instrument.key}", f"quote:{second_instrument.key}"} <= set(before)
        assert not [read for read in before if read.startswith("position:")]
        assert after and all(read.startswith("position:") for read in after)

    async def test_the_snapshot_reports_what_the_hook_booked(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock,
        basket: Basket, instrument: Instrument,
    ) -> None:
        async def settle() -> None:
            ledger.apply_fill(
                Fill(
                    fill_id="booked-by-settle",
                    client_order_id="sim-SETTLE",
                    instrument_key=instrument.key,
                    side=Side.BUY,
                    qty=Decimal("0.1"),
                    price=Decimal("50000"),
                    filled_at=clock.now(),
                ),
                base_currency=instrument.base_currency,
                quote_currency=instrument.quote_currency,
            )

        snapshot = await ContextBuilder(market_data, ledger, clock).build(basket, settle=settle)

        position = snapshot.instruments[0].position
        assert position is not None and position.qty == Decimal("0.1")

    async def test_a_hook_that_raises_ends_the_build(
        self, market_data: ReplayMarketData, ledger: Ledger, clock: ManualClock, basket: Basket
    ) -> None:
        """Fail closed: no settled ledger, no snapshot, so no decision and no order."""

        async def settle() -> None:
            raise VenueError("503 from the venue")

        with pytest.raises(VenueError):
            await ContextBuilder(market_data, ledger, clock).build(basket, settle=settle)
```

New imports for this file: `from tradebot.core.enums import Side`, `from tradebot.core.errors import VenueError`, `from tradebot.core.orders import Fill`, `from tradebot.core.portfolio import Position`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_context_builder.py -k TestSettleHook -q`
Expected: FAIL with `TypeError: ContextBuilder.build() got an unexpected keyword argument 'settle'`

- [ ] **Step 3: Implement the split**

`build` does, in order: `as_of = clock.now()`, `_assert_feed_keeps_up`, one `_market_reads` per instrument (in `basket.instruments` order), `await settle()` if given, then one `_instrument_context` per instrument (`zip(..., strict=True)`), then `_news`. `_market_reads` holds today's candle/indicator loop and the `get_quote` call; `_instrument_context` holds today's `ledger.position` read and the `InstrumentContext` construction, unchanged. Comment the hook's placement with the "after every market read, not per instrument" reason from ADR 0030. Add to the module docstring's failure semantics: "whatever the `settle` hook raises propagates unchanged; the build produces no snapshot (ADR 0030)."

- [ ] **Step 4: Run the file**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_context_builder.py tests/contract/test_market_data_contract.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```powershell
git add tradebot/control/context_builder.py tests/unit/test_context_builder.py
git commit -m "feat(control): ContextBuilder settles between market and position reads (ADR 0030)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: The runner supplies the hook — the rung-3 race closes

**Files:**
- Modify: `tradebot/control/basket_runner.py:196` (`_run`'s build call), `:246-259` (`_settle`), module docstring's failure list (`:12-21`)
- Test: `tests/scenario/test_protective_resize.py` (new test; update one comment in the existing first test)
- Test: `tests/scenario/test_full_cycle.py` (`TestTheSimulatedVenue`, new test)
- Test: `tests/scenario/test_chaos.py` (`TestExecutionRows`, new test)

**Interfaces:**
- Consumes: `ExecutionMonitor.poll(instrument_keys)` (Task 2); `ContextBuilder.build(..., settle=)` (Task 3).
- Produces: `BasketRunner._scope(self) -> frozenset[str]` (the basket's instrument keys) and `async BasketRunner._settle_positions(self) -> None` (`await self._monitor.poll(self._scope())`). `_settle` polls `self._scope()` too.

- [ ] **Step 1: Write the rung-3 scenario** in `tests/scenario/test_protective_resize.py`, after the first test (reuses `crashing_market`, `HISTORY_DAYS`; add imports `from tradebot.core.events import EventType`)

```python
FULL_SELL = """{
  "action": "SELL", "conviction": 5, "size_hint": "full",
  "thesis": "Take the position off.", "key_risks": [], "invalidation": "n/a"
}"""


async def test_a_stop_matched_by_the_snapshot_is_booked_before_the_panel_sizes_a_sell(
    basket: Basket, clock: ManualClock, crashing_market: ReplayMarketData
) -> None:
    """KNOWN_GAPS §5's intra-cycle half, end to end (ADR 0030).

    The crash bar is first read by the cycle's own snapshot, which on the sim stack hands it to
    the venue and fills the stop right then. Before the fix the snapshot then read the stale
    holding, the panel's SELL was sized from it and filled, and the cycle's settle poll refused
    the stop's fill: FAILED, "sell of … exceeds holding 0 …", basket halted.
    """
    eager = basket.model_copy(
        update={"risk_policy": basket.risk_policy.model_copy(update={"cooldown_cycles": 0})}
    )
    harness = Harness(
        eager,
        clock,
        crashing_market,
        [DEFAULT_RESPONSE, FULL_SELL],
        policy=GlobalRiskPolicy(mark_staleness_seconds=int(timedelta(days=3).total_seconds())),
    )
    await harness.start()
    key = basket.instruments[0].key
    try:
        await harness.runner.run_once()
        assert harness.ledger.position(key).qty > ZERO, "the panel bought"

        clock.advance(timedelta(days=2).total_seconds())
        result = await harness.runner.run_once()

        assert result.outcome is not CycleOutcome.FAILED, result.detail
        assert result.outcome is CycleOutcome.RISK_VETOED
        assert harness.ledger.position(key).is_flat
        events = harness.store.read_cycle(result.cycle_id)
        (snapshot,) = [e.payload["snapshot"] for e in events if e.type is EventType.SNAPSHOT_FROZEN]
        assert snapshot["instruments"][0]["position"] is None, "the panel was told the truth"
        checks = [c for e in events if e.type is EventType.RISK_CHECKED for c in e.payload["checks"]]
        assert any(c["rule"] == "sizing" and "SELL while flat" in c["detail"] for c in checks)
        assert [
            order for order in harness.monitor.tracked
            if order.role is OrderRole.STOP_LOSS and order.state is OrderState.FILLED
        ], "the stop's fill is booked, not lost"
    finally:
        harness.close()
```

- [ ] **Step 2: Run it to verify it fails on current code**

Run: `.venv\Scripts\python.exe -m pytest tests/scenario/test_protective_resize.py -k snapshot_is_booked -q`
Expected: FAIL at `assert result.outcome is not CycleOutcome.FAILED`, message containing `exceeds holding`. If it fails any other way, stop and report — the scenario is not reproducing the race.

- [ ] **Step 3: Write the "which monitor" test** in `tests/scenario/test_full_cycle.py`, class `TestTheSimulatedVenue`, after `test_the_supervision_sweep_is_wired_to_the_one_monitor`

```python
    async def test_the_snapshot_and_the_close_settle_through_the_one_monitor(
        self, clock: ManualClock
    ) -> None:
        """ADR 0030's hook polls `runner._monitor` and the close polls `_monitor`; a second
        monitor would settle an empty set and look identical (the efe316b trap, again)."""
        application = await build_sim(clock=clock, db_path=None)
        try:
            await application.recover()
            record = application.configs.baskets()[0]
            runner = await application.supervisor._factory.build(record)
            assert runner._monitor is application.monitor
            assert application.manual_close._monitor is application.monitor
        finally:
            await application.shutdown()
```

- [ ] **Step 4: Run it**

Run: `.venv\Scripts\python.exe -m pytest tests/scenario/test_full_cycle.py -k one_monitor -q`
Expected: PASS (it is structural; it guards the wiring this task relies on)

- [ ] **Step 5: Implement in `basket_runner.py`**

`_run` calls `await self._context.build(self._basket, settle=self._settle_positions)`. Add `_scope` and `_settle_positions` per the Interfaces block. `_settle` calls `await self._monitor.poll(self._scope())` — scoped, so a sibling basket's group can never fail this cycle. In `tradebot/execution/monitor.py`'s module docstring, rewrite "Who calls it" to name the callers as they now are: the snapshot's settle hook and `BasketRunner._settle` (both scoped to the basket, ADR 0030), `ManualCloser` (scoped; Task 5), and `Supervisor._poll_orders` (unscoped). Add to the docstring failure list: `* a venue error while settling the snapshot → raised to the worker; no snapshot, no order (ADR 0030)` and amend "The cycle never raises past `run_once`" to "…except a `RetryableError`, which is the worker's to back off".

- [ ] **Step 6: Update the stale comment** in `test_a_partial_exit_resizes_the_legs_before_the_stop_fires` (`tests/scenario/test_protective_resize.py:170-172`): the third cycle's own snapshot now books the stop (ADR 0030); the following `harness.monitor.poll()` stays and is now a no-op confirmation. Assertions unchanged.

- [ ] **Step 7: Write the fail-closed test** in `tests/scenario/test_chaos.py`, class `TestExecutionRows`

```python
    async def test_a_venue_error_while_settling_the_snapshot_places_no_order(
        self, basket: Basket, clock: ManualClock, market_data: ReplayMarketData
    ) -> None:
        """ADR 0030: the in-snapshot poll failing is the worker's failure path — no trade."""
        harness = await make_harness(basket, clock, market_data, [DEFAULT_RESPONSE])
        try:
            await harness.runner.run_once()  # buys; legs rest, so the next snapshot must poll
            frozen = len([e for e in harness.store.read_all() if e.type is EventType.SNAPSHOT_FROZEN])
            sent = len([e for e in harness.store.read_all() if e.type is EventType.ORDER_SUBMITTED])

            async def down(_ref: object) -> None:
                raise VenueError("503 from the venue")

            harness.broker.fetch_order = down  # type: ignore[method-assign]
            with pytest.raises(VenueError):
                await harness.runner.run_once()

            after = harness.store.read_all()
            assert len([e for e in after if e.type is EventType.SNAPSHOT_FROZEN]) == frozen
            assert len([e for e in after if e.type is EventType.ORDER_SUBMITTED]) == sent
        finally:
            harness.close()
```

Guard check: temporarily remove `settle=self._settle_positions` from `_run`, run `.venv\Scripts\python.exe -m pytest tests/scenario/test_chaos.py -k settling_the_snapshot -q` — Expected: FAIL with `DID NOT RAISE` (without the hook the cycle polls nothing until it has placed an order) — then put the argument back and rerun (PASS).

- [ ] **Step 8: Run the touched files, then the scenario and unit suites**

Run: `.venv\Scripts\python.exe -m pytest tests/scenario/test_protective_resize.py tests/scenario/test_full_cycle.py tests/scenario/test_chaos.py -q`
Expected: PASS
Run: `.venv\Scripts\python.exe -m pytest tests -q`
Expected: PASS. If `tests/scenario/test_backtest.py::TestBacktest::test_a_basket_that_auto_pauses_ends_the_replay` changes outcome, stops now book one cycle earlier (inside the snapshot instead of the harness's after-cycle poll): read the new evidence before touching the assertion, and never weaken it to pass.

- [ ] **Step 9: Run the root gate**

Run: `.\check.ps1`
Expected: `all checks passed`

- [ ] **Step 10: Commit**

```powershell
git add tradebot/control/basket_runner.py tradebot/execution/monitor.py tests/scenario/test_protective_resize.py tests/scenario/test_full_cycle.py tests/scenario/test_chaos.py
git commit -m "fix(control): settle the basket's orders inside the snapshot (KNOWN_GAPS §5, ADR 0030)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `ManualCloser` settles between the quote and the position read

**Files:**
- Modify: `tradebot/control/manual_close.py:242-270` (`_settle`) and `:297-345` (`_proposal`); `_run`'s `_settle` call (`:233`)
- Test: `tests/unit/test_dashboard_control.py` (manual close section)

**Interfaces:**
- Consumes: `ExecutionMonitor.poll(instrument_keys)` (Task 2).
- Produces: `_proposal` order: cheap flat pre-check → `get_quote` (+ freshness) → `await self._monitor.poll(scope)` → re-read `ledger.position` → refuse with the same `ConfigError("nothing to close: no position is held in …")` if now flat → `marks.observe_quote` → proposal. `scope = frozenset(i.key for i in basket.instruments)`. `_settle(self, order: Order, scope: frozenset[str]) -> Order` polls that scope.

- [ ] **Step 1: Write the failing test** in `tests/unit/test_dashboard_control.py` (new imports: `from tradebot.core.clock import ManualClock`, `from tradebot.core.errors import ConfigError`, `from tradebot.execution.brokers.sim import Tick`, `OrderState` from `tradebot.core.enums`)

```python
def cross_the_stop(application: Application, instrument_key: str, clock: ManualClock) -> None:
    """The venue trades through every resting stop on this instrument, and nobody has looked.

    Exactly the state a sim-stack quote read leaves behind when its bar crosses the stop.
    """
    one = Decimal(1)
    application.monitor._broker.observe(  # type: ignore[attr-defined]
        Tick(
            instrument_key=instrument_key, bid=one, ask=one, last=one, high=one, low=one,
            covers_since=clock.now(), observed_at=clock.now(),
        )
    )


async def test_a_close_after_the_stop_sold_it_sells_nothing(
    cycled: Application, clock: ManualClock
) -> None:
    """ADR 0030 through the operator's door: quote, settle, *then* read the holding.

    Before: the close read the stale holding, sold all of it on top of the stop's sale, and its
    guarded `_settle` swallowed the "exceeds holding" the stop's fill then raised.
    """
    assert resting_stops(cycled, BTC)
    cross_the_stop(cycled, BTC, clock)

    with pytest.raises(ConfigError, match="nothing to close"):
        await cycled.manual_close.close("demo", BTC, actor="test")

    assert cycled.ledger.position(BTC).is_flat
    assert [
        o for o in cycled.monitor.tracked
        if o.role is OrderRole.STOP_LOSS and o.instrument_key == BTC and o.state is OrderState.FILLED
    ]
    actions = [e.payload["action_taken"] for e in risk_events(cycled, "manual_close")]
    assert "order_submitted" not in actions
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_control.py -k stop_sold_it -q`
Expected: FAIL with `DID NOT RAISE <class 'tradebot.core.errors.ConfigError'>`

- [ ] **Step 3: Implement the reorder** per the Interfaces block. Keep the first flat check (it answers "nothing to close" without a venue call, which `test_closing_when_flat_is_refused` relies on). A poll that raises propagates out of `close()`; the route already renders any `TradebotError` as a refusal, and the `requested` `RISK_EVENT` was appended first. Replace the `:298`/`:302` comment with the ADR 0030 reason.

- [ ] **Step 4: Run the manual close tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_control.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```powershell
git add tradebot/control/manual_close.py tests/unit/test_dashboard_control.py
git commit -m "fix(control): a manual close settles before it reads the holding (ADR 0030)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: A replay over a crash with a SELL-scripted panel runs every planned cycle

**Files:**
- Test: `tests/scenario/test_backtest.py` (new fixture `crash_dataset`, new test in `TestBacktest`)

**Interfaces:**
- Consumes: Task 4's runner change (the test is its replay-level regression).
- Produces: nothing.

- [ ] **Step 1: Write the fixture and the test** (new imports: `functools`; `from tradebot.core.market import Candle, timeframe_interval`; `from tradebot.decision.providers import DEFAULT_RESPONSE`; `from tradebot.decision.providers import registry`; `from tradebot.decision.providers.stub import StubLLMProvider`; `from tradebot.indicators.library import DEFAULT_INDICATORS, required_history`)

```python
CRASH_DEPTH = Decimal("6000")
FULL_SELL = """{
  "action": "SELL", "conviction": 5, "size_hint": "full",
  "thesis": "Take the position off.", "key_risks": [], "invalidation": "n/a"
}"""


def first_cycle_at() -> datetime:
    """Where `BacktestHarness` starts cycling over this file's timeframes: after the 4h warm-up,
    on the next hourly tick — `warmup_for` and `plan_ticks` restated, so the crash bar can be
    placed on the second cycle exactly."""
    warmup = timeframe_interval("4h") * (required_history(DEFAULT_INDICATORS) + 1)
    return HISTORY_START + warmup + timedelta(hours=1)


@pytest.fixture
async def crash_dataset(
    tmp_path: Path, history_instrument: Instrument, replay_clock: ManualClock
) -> ReplayDataset:
    """Rising hourly bars until the first cycle, then one bar through any stop, then rising again."""
    rising_hours = (first_cycle_at() - HISTORY_START) // timedelta(hours=1)
    before = synthetic_candles(start=HISTORY_START, timeframe="1h", count=rising_hours,
                               open_price=Decimal("50000"), step=Decimal("25"))
    last = before[-1]
    crash = Candle(
        open_time=last.close_time, close_time=last.close_time + timedelta(hours=1),
        open=last.close, high=last.close, low=last.close - CRASH_DEPTH,
        close=last.close - CRASH_DEPTH + Decimal(500), volume=Decimal(1),
    )
    after = synthetic_candles(start=crash.close_time, timeframe="1h",
                              count=BARS - rising_hours - 1, open_price=crash.close,
                              step=Decimal("25"))
    source = ReplayMarketData(
        {
            (history_instrument.key, "1h"): (*before, crash, *after),
            (history_instrument.key, "4h"): synthetic_candles(
                start=HISTORY_START, timeframe="4h", count=BARS,
                open_price=Decimal("50000"), step=Decimal("25"),
            ),
        },
        replay_clock,
    )
    return await record(
        source, (history_instrument,), TIMEFRAMES,
        start=HISTORY_START, end=HISTORY_START + timedelta(hours=BARS),
        directory=tmp_path / "crash", clock=replay_clock, source="crash fixture",
    )
```

```python
    async def test_a_stop_crossed_by_the_snapshot_does_not_end_the_replay(
        self, crash_dataset: ReplayDataset, tmp_path: Path, replay_clock: ManualClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KNOWN_GAPS §5 as decision_lab met it: buy, then a SELL on the cycle whose own read
        crosses the stop. Before ADR 0030 the cycle FAILED and the harness's own poll re-raised
        `exceeds holding`, killing the run."""
        monkeypatch.setattr(
            registry, "StubLLMProvider",
            functools.partial(StubLLMProvider, [DEFAULT_RESPONSE, FULL_SELL]),
        )
        basket = dataset_basket(crash_dataset, STUB_PANEL)
        basket = basket.model_copy(
            update={"risk_policy": basket.risk_policy.model_copy(update={"cooldown_cycles": 0})}
        )
        application = await build_sim(
            clock=replay_clock, db_path=backtest_database_path(tmp_path / "crash-run"),
            baskets=(basket,), market_data=crash_dataset.market_data,
        )
        try:
            start = crash_dataset.coverage[0]
            report = await BacktestHarness(
                application, replay_clock, start=start, end=first_cycle_at() + timedelta(hours=4)
            ).run()
        finally:
            await application.shutdown()

        assert report.ran_cycles == report.planned_cycles == 5
        assert CycleOutcome.FAILED.value not in report.evidence.outcomes
        assert report.evidence.fills > 0
```

- [ ] **Step 2: Run it**

Run: `.venv\Scripts\python.exe -m pytest tests/scenario/test_backtest.py -k crossed_by_the_snapshot -q`
Expected: PASS. If `planned_cycles` is not 5, the warm-up restatement in `first_cycle_at` no longer matches `warmup_for`/`plan_ticks`: fix the helper, not the assertion.

- [ ] **Step 3: Prove it guards Task 4** — delete `settle=self._settle_positions` from the build call in `tradebot/control/basket_runner.py`, rerun Step 2's command.
Expected: FAIL with `ReconciliationMismatchError: sell of … exceeds holding …` raised out of `BacktestHarness.run`.
Then restore: `git checkout -- tradebot/control/basket_runner.py` and rerun Step 2 (PASS).

- [ ] **Step 4: Commit**

```powershell
git add tests/scenario/test_backtest.py
git commit -m "test(validation): a replay survives a stop crossed by its own snapshot (KNOWN_GAPS §5)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Contain an unbookable fill — per-group isolation and poison (D parts 1–2)

**Files:**
- Modify: `tradebot/core/errors.py` (after `ReconciliationMismatchError`, `:65-66`)
- Modify: `tradebot/ledger/portfolio.py:143-149` (`_apply_sell` raises the new subclass, same message)
- Modify: `tradebot/execution/monitor.py` (`_Tracked`, `poll`, new `_sync_group`, new `_poison`, constant; docstring failure semantics)
- Modify: `tradebot/control/supervisor.py:437-460` (`_poll_orders` docstring only), `tradebot/validation/backtest.py:223` (comment only)
- Test: `tests/unit/test_monitor.py`, `tests/unit/test_supervisor.py`, `tests/scenario/test_chaos.py`, `tests/unit/test_dashboard_control.py`

**Interfaces:**
- Consumes: `poll(instrument_keys)` (Task 2); `cross_the_stop` helper (Task 5, `tests/unit/test_dashboard_control.py`).
- Produces:
  - `class UnbookableFillError(ReconciliationMismatchError)` with `__init__(self, message: str, *, client_order_id: str, instrument_key: str, qty: Decimal, held: Decimal) -> None`, each kwarg stored as an attribute of the same name (pattern: `SubmitUnknownError`).
  - `Ledger._apply_sell` raises `UnbookableFillError(<today's message>, client_order_id=fill.client_order_id, instrument_key=fill.instrument_key, qty=fill.qty, held=current.qty)`.
  - `UNBOOKABLE_FILL_RULE = "unbookable_fill"` in `tradebot/execution/monitor.py`.
  - `_Tracked.poisoned: UnbookableFillError | None = None`.
  - Private: `async ExecutionMonitor._sync_group(self, group: _Tracked) -> None` (today's two sync lines, entry then each leg, assigning as it goes); `async ExecutionMonitor._poison(self, group: _Tracked, error: UnbookableFillError) -> None`.
  - `poll` semantics below. Test helper `poisoned_pair(...)` in `tests/unit/test_monitor.py`, imported by `tests/unit/test_supervisor.py`.

- [ ] **Step 1: Write the failing monitor tests** in `tests/unit/test_monitor.py` (new imports: `from tradebot.core.errors import UnbookableFillError, VenueError`)

```python
ETH_PLAN = ProtectivePlan(stop_price=Decimal("2700"), take_profit_price=Decimal("3200"))


async def poisoned_pair(
    monitor: ExecutionMonitor, broker: SimBroker, store: EventStore, ledger: Ledger,
    clock: ManualClock, btc: Instrument, eth: Instrument, *, eth_plan: ProtectivePlan | None = None,
) -> None:
    """BTC: an armed 0.5 stop the venue has filled, against a ledger something else already cut
    to 0.2 — unbookable. ETH: an entry the venue has filled since — bookable. BTC tracked first."""
    broker.observe(tick(btc, clock, last="49000"))
    await submit_entry(monitor, broker, store, ledger, clock, btc, ttl=None)
    await monitor.poll()
    book(ledger, external_sell(btc, clock, qty="0.3"), btc)
    await submit_entry(monitor, broker, store, ledger, clock, eth, price="2900", qty="1",
                       ttl=None, plan=eth_plan, coid="sim-ETH")
    clock.advance(60)
    broker.observe(tick(btc, clock, last="46500", high="49000", low="46000"))
    broker.observe(tick(eth, clock, last="2800"))


def unbookable_events(store: EventStore) -> list[Event]:
    return [
        e for e in store.read_all()
        if e.type is EventType.RISK_EVENT and e.payload["rule"] == "unbookable_fill"
    ]


class TestAnUnbookableFillIsContained:
    """ADR 0030 / memo Option D: one fill the ledger refuses must not wedge every other group."""

    async def test_a_poisoned_group_does_not_stop_a_sibling_fill_being_booked(
        self, monitor: ExecutionMonitor, broker: SimBroker, store: EventStore, ledger: Ledger,
        clock: ManualClock, instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)

        await monitor.poll()  # unscoped: the sweep's call, and it must not raise

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert ledger.position(instrument.key).qty == Decimal("0.2")

    async def test_the_poison_reaches_only_a_caller_scoped_to_it(
        self, monitor: ExecutionMonitor, broker: SimBroker, store: EventStore, ledger: Ledger,
        clock: ManualClock, instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)

        with pytest.raises(UnbookableFillError, match="exceeds holding") as raised:
            await monitor.poll(frozenset({instrument.key}))
        assert raised.value.qty == Decimal("0.5") and raised.value.held == Decimal("0.2")
        await monitor.poll(frozenset({second_instrument.key}))

    async def test_it_is_recorded_once_and_the_fill_is_never_dropped(
        self, monitor: ExecutionMonitor, broker: SimBroker, store: EventStore, ledger: Ledger,
        clock: ManualClock, instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        for _ in range(3):
            await monitor.poll()

        (event,) = unbookable_events(store)
        assert event.payload["scope"] == instrument.key
        assert "0.5" in event.payload["detail"] and "0.2" in event.payload["detail"]
        assert [
            o for o in monitor.tracked
            if o.role is OrderRole.STOP_LOSS and o.instrument_key == instrument.key
            and o.state.is_open
        ], "still tracked, still unbooked — nothing pretends it never happened"

    async def test_a_poisoned_group_books_once_the_holding_allows_it(
        self, monitor: ExecutionMonitor, broker: SimBroker, store: EventStore, ledger: Ledger,
        clock: ManualClock, instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        await monitor.poll()
        book(ledger, Fill(fill_id="restored", client_order_id="sim-ELSEWHERE",
                          instrument_key=instrument.key, side=Side.BUY, qty=Decimal("0.3"),
                          price=Decimal("49000"), filled_at=clock.now()), instrument)

        await monitor.poll(frozenset({instrument.key}))  # no longer raises

        assert ledger.position(instrument.key).is_flat

    async def test_a_venue_error_on_one_group_still_maintains_the_others(
        self, monitor: ExecutionMonitor, broker: SimBroker, store: EventStore, ledger: Ledger,
        clock: ManualClock, instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        await submit_entry(monitor, broker, store, ledger, clock, second_instrument,
                           price="2900", qty="1", ttl=None, plan=ETH_PLAN, coid="sim-ETH")
        clock.advance(60)
        broker.observe(tick(second_instrument, clock, last="2800"))
        real = broker.fetch_order

        async def btc_down(ref: OrderRef) -> object:
            if ref.instrument_key == instrument.key:
                raise VenueError("503 from the venue")
            return await real(ref)

        broker.fetch_order = btc_down  # type: ignore[method-assign]
        with pytest.raises(VenueError):
            await monitor.poll()

        assert ledger.position(second_instrument.key).qty == Decimal("1")
        assert [
            o for o in monitor.tracked
            if o.role is OrderRole.STOP_LOSS and o.instrument_key == second_instrument.key
            and o.state.is_open
        ], "the healthy group was armed despite its sibling's error"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -k TestAnUnbookableFillIsContained -q`
Expected: FAIL at collection with `ImportError: cannot import name 'UnbookableFillError'`.

- [ ] **Step 3: Add `UnbookableFillError`** and raise it from `Ledger._apply_sell` (Interfaces block). Message text unchanged, so every existing `match="exceeds holding"` still holds.

- [ ] **Step 4: Rerun Step 2's command.** Expected: the first, third, fourth and fifth tests FAIL (the unscoped poll raises; nothing is recorded); the second may pass. This is the wedge.

- [ ] **Step 5: Implement poison and isolation in `ExecutionMonitor`**

`poll(self, instrument_keys: frozenset[str] | None = None) -> None`, under `self._polling`:

```python
in_scope = [g for g in list(self._tracked.values())
            if instrument_keys is None or g.order.instrument_key in instrument_keys]
failures: list[Exception] = []
synced: list[_Tracked] = []
for group in in_scope:
    try:
        await self._sync_group(group)          # entry, then each leg; assigns as it goes
    except UnbookableFillError as error:
        await self._poison(group, error)       # records once, keeps it tracked
    except Exception as error:                 # isolated, then re-raised below
        failures.append(error)
    else:
        group.poisoned = None                  # booked: the holding allowed it
        synced.append(group)
targets = self._targets()                      # after every sync, as today (design §2.4)
for group in synced:
    try:
        await self._maintain(group, targets.get(group.order.group_id))
    except Exception as error:
        failures.append(error)
if instrument_keys is not None:
    poison = next((g.poisoned for g in in_scope if g.poisoned is not None), None)
    if poison is not None:
        raise poison                           # fail closed: this caller sizes on this ledger
if failures:
    raise failures[0]
```

`_poison(group, error)`: only when `group.poisoned is None`, append `events_for(group.order).risk_event(tier=RiskTier.EXECUTION, rule=UNBOOKABLE_FILL_RULE, scope=error.instrument_key, action="group_poisoned", detail=f"{error.client_order_id}: a fill of {error.qty} exceeds the holding of {error.held}; left unbooked and still tracked")` and `logger.error(...)` with the same three fields; then always set `group.poisoned = error`. A poisoned group is skipped by `_maintain` (it is not in `synced`). Comment both `except Exception` clauses: collected and re-raised, so the class still governs handling. Update the module docstring's failure semantics with the poison rule.

- [ ] **Step 6: Run the monitor file**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q`
Expected: PASS (including `test_a_failed_placement_after_the_cancel_is_recorded_before_it_propagates`, whose error still propagates — now after the other groups)

- [ ] **Step 7: Write the supervisor test** in `tests/unit/test_supervisor.py`, class `TestOrdersArePolledBetweenCycles` (imports: `from decimal import Decimal`; `from tests.unit.test_monitor import poisoned_pair`; `SimBroker`, `ExecutionMonitor`, `ExecutionService`, `Ledger`, `Instrument`)

```python
    async def test_a_poisoned_group_does_not_stop_the_sweep_booking_a_sibling(
        self, factory: ScriptedFactory, configs: ConfigStore, clock: ManualClock,
        watchdog: Watchdog, states: RiskStateStore, store: EventStore, ledger: Ledger,
        instrument: Instrument, second_instrument: Instrument,
    ) -> None:
        """Before ADR 0030 one unbookable fill stopped the sweep at that group, every 30s, for
        every basket. The sweep is unscoped, so it neither raises nor stops."""
        broker = SimBroker(clock, balances={"USDT": Decimal(100_000)})
        monitor = ExecutionMonitor(broker, ExecutionService(broker, store, ledger, clock), store, clock)
        await poisoned_pair(monitor, broker, store, ledger, clock, instrument, second_instrument)
        supervisor = self._supervisor(factory, configs, clock, watchdog, states, monitor=monitor)  # type: ignore[arg-type]

        serving = asyncio.create_task(supervisor.serve(resync_seconds=60.0))
        await asyncio.sleep(0)
        await supervisor.stop()
        await asyncio.wait_for(serving, timeout=5)

        assert ledger.position(second_instrument.key).qty == Decimal("1")
```

- [ ] **Step 8: Write the chaos scenario** in `tests/scenario/test_chaos.py`, new class `TestUnbookableFill` (imports: `Mode`, `OrderRole`, `Side` from `tradebot.core.enums`; `BasketRunner`; `ContextBuilder`; `DecisionEngine`; `SeatRunner`; `StubLLMProvider`; `Tier1RiskEngine`; `Tier2RiskEngine`; `Fill` from `tradebot.core.orders`; `Tick` from `tradebot.execution.brokers.sim`; `Instrument`; `IGNORE_STALENESS` from `tests.scenario.harness`; `SERIES_START`, `TIMEFRAMES` from `tests.conftest`; `synthetic_candles`)

```python
@pytest.fixture
def two_markets(
    instrument: Instrument, second_instrument: Instrument, clock: ManualClock
) -> ReplayMarketData:
    return ReplayMarketData(
        {
            (held.key, timeframe): synthetic_candles(
                start=SERIES_START, timeframe=timeframe, count=200,
                open_price=Decimal("50000"), step=Decimal("25"),
            )
            for held in (instrument, second_instrument)
            for timeframe in TIMEFRAMES
        },
        clock,
    )


def sibling_runner(harness: Harness, sibling: Basket) -> BasketRunner:
    """A second basket on the harness's one venue portfolio: its ledger, execution and monitor."""
    return BasketRunner(
        sibling,
        mode=Mode.SIM,
        context_builder=ContextBuilder(
            harness.market_data, harness.ledger, harness.clock,
            staleness_tolerance=IGNORE_STALENESS, protective_orders_supported=True,
            trading_history=harness.history,
        ),
        decision_engine=DecisionEngine(
            SeatRunner({"stub": StubLLMProvider([DEFAULT_RESPONSE])}, harness.clock)
        ),
        risk_engine=Tier1RiskEngine(harness.clock),
        tier2=Tier2RiskEngine(harness.policy),
        watchdog=harness.watchdog, history=harness.history, execution=harness.execution,
        monitor=harness.monitor, ledger=harness.ledger, store=harness.store,
        clock=harness.clock, venue=harness.broker.venue_id, marks=harness.marks,
        universe=lambda: (*harness.basket.instruments, *sibling.instruments),
        global_policy=harness.policy,
    )


class TestUnbookableFill:
    async def test_an_unbookable_fill_halts_one_basket_not_all(
        self, basket: Basket, clock: ManualClock, two_markets: ReplayMarketData,
        second_instrument: Instrument,
    ) -> None:
        harness = await make_harness(basket, clock, two_markets, [DEFAULT_RESPONSE])
        sibling = basket.model_copy(
            update={"basket_id": "b2", "instruments": (second_instrument,)}
        )
        other = sibling_runner(harness, sibling)
        btc = basket.instruments[0].key
        try:
            assert (await harness.runner.run_once()).outcome is CycleOutcome.ORDERS_PLACED
            held = harness.ledger.position(btc).qty
            # Fault injection: the ledger already lost half the holding, then the venue's stop
            # sells the whole of it — a fill the ledger must refuse.
            harness.ledger.apply_fill(
                Fill(fill_id="external", client_order_id="elsewhere", instrument_key=btc,
                     side=Side.SELL, qty=held / 2, price=Decimal("50000"), filled_at=clock.now()),
                base_currency="BTC", quote_currency="USDT",
            )
            one = Decimal(1)
            harness.broker.observe(Tick(instrument_key=btc, bid=one, ask=one, last=one,
                                        high=one, low=one, covers_since=clock.now(),
                                        observed_at=clock.now()))

            failed = await harness.runner.run_once()
            assert failed.outcome is CycleOutcome.FAILED
            assert "exceeds holding" in failed.detail
            assert not harness.states.status_of(basket.basket_id).may_trade

            traded = await other.run_once()
            assert traded.outcome is CycleOutcome.ORDERS_PLACED
            assert harness.ledger.position(second_instrument.key).qty > 0
            assert harness.states.status_of("b2").may_trade

            await harness.monitor.poll()  # the supervisor's sweep: unscoped, must not raise
            assert len([r for r in harness.risk_events() if r[0] == "unbookable_fill"]) == 1
            assert [o for o in harness.monitor.tracked
                    if o.role is OrderRole.STOP_LOSS and o.instrument_key == btc
                    and o.state.is_open], "the fill is kept, not dropped"
        finally:
            harness.close()
```

- [ ] **Step 9: Write the manual-close refusal test** in `tests/unit/test_dashboard_control.py` (import `Fill` from `tradebot.core.orders`, `Side` from `tradebot.core.enums`, `UnbookableFillError` from `tradebot.core.errors`)

```python
async def test_a_close_over_an_unbookable_fill_is_refused_and_sends_nothing(
    cycled: Application, clock: ManualClock
) -> None:
    """Fail closed for the operator too: the close would size against a ledger known to be wrong."""
    held = cycled.ledger.position(BTC).qty
    cycled.ledger.apply_fill(
        Fill(fill_id="external", client_order_id="elsewhere", instrument_key=BTC, side=Side.SELL,
             qty=held / 2, price=Decimal("50000"), filled_at=clock.now()),
        base_currency="BTC", quote_currency="USDT",
    )
    cross_the_stop(cycled, BTC, clock)
    sent = len(events_of(cycled, EventType.ORDER_SUBMITTED))

    with pytest.raises(UnbookableFillError, match="exceeds holding"):
        await cycled.manual_close.close("demo", BTC, actor="test")

    assert len(events_of(cycled, EventType.ORDER_SUBMITTED)) == sent
    assert risk_events(cycled, "unbookable_fill")
```

- [ ] **Step 10: Run the four files**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py tests/unit/test_supervisor.py tests/scenario/test_chaos.py tests/unit/test_dashboard_control.py -q`
Expected: PASS. (Checked against Task 6's code: before Step 5, the supervisor test and the chaos test fail — the unscoped poll raised at BTC and never reached ETH.)

- [ ] **Step 11: Comments.** `Supervisor._poll_orders` docstring: an unscoped poll never raises for a poisoned group, so this guard now catches only transient errors. `backtest.py:223`: this unscoped poll never raises for a poisoned group; a cycle that hit one has already halted its basket, so `worker.stopped` below ends the pass short — which is why the memo's Option D part 3 is not needed (ADR 0030).

- [ ] **Step 12: Run the root gate**

Run: `.\check.ps1`
Expected: `all checks passed` (coverage: `execution/` and `ledger/` ≥ 95%)

- [ ] **Step 13: Commit**

```powershell
git add tradebot/core/errors.py tradebot/ledger/portfolio.py tradebot/execution/monitor.py tradebot/control/supervisor.py tradebot/validation/backtest.py tests/unit/test_monitor.py tests/unit/test_supervisor.py tests/scenario/test_chaos.py tests/unit/test_dashboard_control.py
git commit -m "fix(execution): an unbookable fill poisons one group, not the monitor (ADR 0030)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Remove the `STUB_SEED` pin — only if every seed is clean

**Files:**
- Modify (conditional): `decision_lab/tests/test_slice_b_end_to_end.py:27-53`, `decision_lab/tests/conftest.py` (no edit expected: it calls `built_corpus`, which loses its `seeded_stub` call), `decision_lab/tests/test_slice_e_end_to_end.py:37,225`

**Interfaces:**
- Consumes: Tasks 4 and 7 committed.
- Produces: the outcome Task 9's wording depends on — **DELETED** or **KEPT**, plus a per-seed table.

The comment at `test_slice_b_end_to_end.py:35` names "seven seeds" but not which; use **0, 1, 2, 3, 4, 5, 6 and 2024**.

- [ ] **Step 1: Make the seed overridable for measurement only (never committed)** — in `test_slice_b_end_to_end.py`, temporarily replace `STUB_SEED = 2024` with `STUB_SEED = int(os.environ.get("SLICE_B_SEED", "2024"))` and add `import os`.

- [ ] **Step 2: Run the whole decision_lab suite once per seed**

```powershell
$root = Join-Path $env:TEMP "ks5-seeds"
New-Item -ItemType Directory -Force $root | Out-Null
foreach ($s in 0,1,2,3,4,5,6,2024) {
  $env:SLICE_B_SEED = "$s"
  .venv\Scripts\python.exe -m pytest decision_lab/tests -q -p no:cacheprovider --basetemp "$root\seed-$s" *> "$root\seed-$s.log"
  "seed $s exit $LASTEXITCODE"
}
Remove-Item Env:SLICE_B_SEED
```

Expected: `exit 0` for every seed.

- [ ] **Step 3: Read what every pass actually did** — write `$env:TEMP\ks5-seeds\scan.py`:

```python
import json
import sqlite3
import sys
from pathlib import Path

for seed_dir in sorted(p for p in Path(sys.argv[1]).glob("seed-*") if p.is_dir()):
    for meta_path in sorted(seed_dir.rglob("corpus.json")):
        database = meta_path.with_name("corpus.db")
        if not database.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        con = sqlite3.connect(database)
        failed = con.execute("select count(*) from cycles where outcome = 'failed'").fetchone()[0]
        rules = dict(con.execute("select rule, count(*) from risk_events group by rule").fetchall())
        con.close()
        print(seed_dir.name, meta_path.parent.name[:8],
              f"{meta['ran_cycles']}/{meta['planned_cycles']}", f"failed={failed}",
              f"unbookable={rules.get('unbookable_fill', 0)}",
              f"autopause={rules.get('max_consecutive_losses', 0)}")
```

Run: `.venv\Scripts\python.exe -I "$env:TEMP\ks5-seeds\scan.py" "$env:TEMP\ks5-seeds"`
Expected: every row `failed=0 unbookable=0`.

- [ ] **Step 4: Decide, by these rules and no others**
  - Any seed's log contains `exceeds holding`, or any row shows `failed>0` or `unbookable>0`: **STOP.** Revert Step 1 (`git checkout -- decision_lab/tests/test_slice_b_end_to_end.py`), keep the pin, and report the seed, the log excerpt and the scan row. Outcome **KEPT**. Do not proceed to Step 5.
  - Any seed exits non-zero for another reason: **STOP** the same way and report it. A test depending on what the seed draws is not §5, and deleting the pin would make it flaky. Outcome **KEPT**.
  - A row with `ran < planned`, `failed=0`, `autopause>0` is an auto-pause, not a crash. The slice-B test already accounts for it — it asserts `corpus build` exits `EXIT_OK` and that every regime row renders, never a cycle count, and `corpus build` exits 0 for a short auto-paused pass (CLAUDE.md, decision_lab rules). Record the seed and its `ran/planned`; it does not block deletion. Do not add a cycle-count assertion.
  - Otherwise: outcome **DELETED**, go to Step 5.

- [ ] **Step 5: Delete the pin** — in `test_slice_b_end_to_end.py` remove `STUB_SEED`, its comment block (`:27-44`), `seeded_stub`, the `seeded_stub(monkeypatch)` call in `built_corpus`, and the now-unused imports (`functools`, `random`, `os`, `registry`, `StubLLMProvider`). In `test_slice_e_end_to_end.py` remove the import at `:37` and the call at `:225`. Leave one line in `built_corpus`'s docstring: the stub is unseeded on purpose since ADR 0030 closed the race that made a seed necessary.

- [ ] **Step 6: Run both gates, twice** (the suite is now unseeded; twice is a cheap check for a seed dependency the eight did not reach)

Run: `.\decision_lab\check.ps1` then `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: both PASS.
Run: `.\check.ps1`
Expected: `all checks passed`

- [ ] **Step 7: Commit** (DELETED only; paste the scan table into the body)

```powershell
git add decision_lab/tests/test_slice_b_end_to_end.py decision_lab/tests/test_slice_e_end_to_end.py
git commit -m "test(decision_lab): drop the STUB_SEED pin now KNOWN_GAPS §5 is closed" -m "<scan table: seed, ran/planned, failed, unbookable, autopause>" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/KNOWN_GAPS.md` (pass list `:12-31`, table `:34-42`, §6 `:215-243`, new §10 before `## Closed — the maintenance audit's four`, the *Closed* §5 section `:481-551`)
- Modify: `CLAUDE.md:315-325` (the decision_lab "A reference pass is not reproducible…" bullet)
- Modify: `decision_lab/PROGRESS.md:612-613` and `:730-743` ("What comes next" item 1)

**Interfaces:**
- Consumes: Task 8's outcome (DELETED or KEPT) and its scan table.

- [ ] **Step 1: KNOWN_GAPS — the *Closed* §5 section.** Rename the heading to `## Closed — the monitor polls between cycles (2026-09-21), and inside them (<today>)`. Replace the paragraph beginning **"What the fix does not reach"** (`:540-551`) with:

```markdown
**The intra-cycle half closed on <today>** ([ADR 0030](adr/0030-a-reconciling-poll-precedes-the-position-read.md)).
The paragraph that stood here said a running process was "largely covered by the 30-second sweep
having already been round". **That was wrong on a `SimBroker` stack** — sim, a replay, and paper,
whose primary venue is `SimBroker`. That venue matches resting orders only when `stack.prices` is
read, which only a snapshot and a manual close do; the sweep reads `read_only_prices`, so it can
only book what a cycle's own read has already matched, after that cycle has sized. A paper soak
was exactly as exposed as a replay.

`ContextBuilder.build` now reads every instrument's market data, polls the process's monitor
scoped to the basket's instruments, and only then reads positions; `ManualCloser` does the same
(quote → poll → position). One unbookable fill no longer wedges the monitor: it poisons its own
group, is recorded once (`RISK_EVENT`, `unbookable_fill`), stays tracked, and raises only to a
caller scoped to it. Tests: the third scenario in `test_protective_resize.py`, `TestSettleHook`,
`TestScopedPoll`, `TestAnUnbookableFillIsContained`, the replay in `test_backtest.py`, and
`TestUnbookableFill` in `test_chaos.py`. In live a stop can still fill during deliberation; the
resting leg reserves the coins there, so the SELL is refused rather than overselling (ADR 0030).
```

Then append one sentence matching Task 8: DELETED → "`STUB_SEED = 2024` was removed after the slice-B suite ran clean on seeds 0–6 and 2024." KEPT → "`STUB_SEED = 2024` stays: seed <n> still <what the scan showed>."

- [ ] **Step 2: KNOWN_GAPS — the pass list and §6/§10.** In the pass list, change "Five passes so far" to "Six passes so far", change the §5–8 bullet's last sentence to "**§5 has since been fixed, both halves** — see *Closed* below.", and add:

```markdown
- **§10, found while designing §5's intra-cycle fix** (<today>). Recorded, not fixed — see
  [ADR 0030](adr/0030-a-reconciling-poll-precedes-the-position-read.md)'s follow-ups.
```

Add a table row `| 10 | A cancel the venue refused is recorded as terminal | live · paper on a venue | fails open: a fill is lost to the monitor |`. Add before `## Closed — the maintenance audit's four`:

```markdown
## 10. A cancel the venue refused is recorded as terminal

**Where** — [service.py `cancel`](../tradebot/execution/service.py) ignores `ack.cancelled=False`
and advances the order to `CANCELLED`, a terminal state; `ExecutionMonitor._sync` never re-reads a
terminal order. A stop that fills between a poll's sync and `_replace_legs`'s cancel therefore loses
its fill — `BinanceSpotBroker.cancel`'s promise that "the monitor's next sweep books the fill"
cannot hold — and only the reconciler finds it later, as a `MISMATCH`.

**To close it.** On `cancelled=False`, re-sync the order instead of advancing it. Any fix to §6
(cancel legs before an exit) widens this window and must land with it.

---
```

At the end of §6's **Why no test and no soak has seen it** paragraph, add: "Consequently every discretionary partial exit in soak evidence and in `decision_lab` corpora so far was made against reserved coins, which live Binance would have refused; whether that evidence still counts is the operator's call. Making `SimBroker` refuse it (the memo's Option E) must ship with this section's fix (ADR 0030)."

- [ ] **Step 3: CLAUDE.md** — replace the bullet at `:315-325` with, for DELETED:

```markdown
- **A reference pass is not reproducible.** `SIM_PANEL`'s `varied-*` seats draw from an unseeded
  `random.Random` and `corpus build` has no `--seed`. It no longer dies on KNOWN_GAPS §5: the
  snapshot polls between its market and position reads (ADR 0030), and the `STUB_SEED` pin went
  once the slice-B suite ran clean on seeds 0–6 and 2024. An unbookable fill from any other cause
  now halts the basket and ends the pass short — `ran_cycles` below `planned_cycles` with a
  `FAILED` cycle, which is a defect to read, unlike an auto-pause.
```

For KEPT, the same first two sentences, then: "`STUB_SEED = 2024` is still pinned in `test_slice_b_end_to_end.py`: after ADR 0030, seed <n> still <what the scan showed>." Leave the Phase 12 bullet "The order poll precedes that valuation sweep" unchanged — the sweep's order is untouched and the cycle's own poll runs after the gate, inside the snapshot.

- [ ] **Step 4: decision_lab/PROGRESS.md** — at `:612-613` replace the pin bullet with DELETED: "- **The `STUB_SEED` pin is gone** (ADR 0030 closed KNOWN_GAPS §5's intra-cycle race; slice B ran clean on seeds 0–6 and 2024)." / KEPT: "- **`STUB_SEED = 2024` is still pinned**: <seed and reason>." Replace "What comes next" item 1 (`:730-743`) with:

```markdown
1. **KNOWN_GAPS §5 is closed, both halves** ([ADR 0030](../docs/adr/0030-a-reconciling-poll-precedes-the-position-read.md)).
   A snapshot now books what its own reads made the simulated venue do before it reads a holding,
   so `corpus build` and `calibrate long` no longer die on `exceeds holding`. Any other unbookable
   fill halts the basket and ends the pass short rather than crashing it. `calibrate long` still
   has neither a budget nor a resume.
```

and in item 3's first sub-bullet replace "Weigh closing §5's intra-cycle half first." with "§5 no longer threatens it."

- [ ] **Step 5: Check every link resolves and nothing else still claims the race is open**

Run: `git grep -n "intra-cycle\|STUB_SEED\|largely covered by the 30-second" -- docs CLAUDE.md decision_lab`
Expected: only the new text (and, if KEPT, the pin's own lines). The memo and older plans under `docs/superpowers/` are historical and stay as they are.

- [ ] **Step 6: Commit**

```powershell
git add docs/KNOWN_GAPS.md CLAUDE.md decision_lab/PROGRESS.md
git commit -m "docs: KNOWN_GAPS §5 closed inside the cycle; §10 recorded (ADR 0030)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Final gates**

Run: `.\check.ps1` and `.\decision_lab\check.ps1`
Expected: both `all checks passed`; `git status` clean.
