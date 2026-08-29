# Protective legs track the position — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ExecutionMonitor` keep each protective group's venue-held legs matched to the *position* rather than to its own entry order, so a partial exit taken by any other path can no longer leave an oversized stop resting at the venue.

**Architecture:** `plan_legs` stops deciding the quantity and is told it. `ExecutionMonitor` learns the holding through a new `ExecutionService.held`, deletes the local `protected_qty` counter in favour of a quantity derived from the legs it has just re-synced from the venue, and allocates each instrument's holding across its groups tightest-stop-first before maintaining any of them. The failure paths around that become honest: a zero target releases rather than reports, a target below venue minimums cancels before it reports, and a placement that fails after the cancel is recorded before it propagates.

**Tech Stack:** Python 3.11, pydantic v2, pytest + pytest-asyncio (auto mode), hypothesis, ruff, mypy. No new dependency.

**Spec:** [docs/superpowers/specs/2026-08-28-protective-legs-track-the-position-design.md](../specs/2026-08-28-protective-legs-track-the-position-design.md) — implement all of it. §1 D1–D4 are the decisions; §2 is the design; §3 is what a reviewer checks; §4 is this plan's tests.

## Global Constraints

- **Money is `Decimal`, always**, via `tradebot.core.money`. Never `float`, never `Decimal(some_float)`. Enforced by `tests/unit/test_money_discipline.py`.
- **Time is UTC-aware `datetime` from the injected `Clock`.** Never `datetime.now()` in library code.
- **Every state change emits an event.** The event log alone must reconstruct the module's state.
- **Prefer dispatch over branching.** Side-dependent behaviour is a `dict[Side, ...]`, as `_EXIT_SIDE` and `_OFFSET_SIGN` already are in `protective.py` — never an `if`.
- **Comments explain *why*, and cite the section they implement** (`design §2.2`, `KNOWN_GAPS §4`, `ADR 0011`). Don't restate what the code says.
- **Errors are classified**: `RetryableError` / `FailClosedError` / `FatalError`. A bare `except: pass` is a defect.
- **Line length 100**, `ruff format`, `from __future__ import annotations`, full type annotations.
- **The pre-existing protective-group tests in `tests/unit/test_monitor.py` must pass unmodified** through Tasks 1–4. That is the executable form of "the new rule subsumes the old one" (spec §3). The only licensed edits to that file are *additions*, plus the `coid` parameter added to its local `entry_intent` helper in Task 3.
- **Nothing outside these files changes:** `tradebot/execution/{monitor,service,protective}.py`, one stale comment in `tradebot/execution/brokers/binance.py`, `tests/unit/test_monitor.py`, `tests/unit/test_protective.py`, one new scenario test, and the two docs.
- Verification at every commit: `.\check.ps1`. `execution/` must stay ≥ 95% coverage.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `tradebot/execution/protective.py` | Turn an entry plus a quantity into leg intents | `plan_legs` gains a required `qty` keyword; one refusal message |
| `tradebot/execution/service.py` | Order lifecycle against the venue, booking into the ledger | one read accessor, `held` |
| `tradebot/execution/monitor.py` | Owns orders from ack to terminal state | `_Tracked` state, target allocation, the failure table |
| `tradebot/execution/brokers/binance.py` | Binance adapter | one stale comment |
| `tests/unit/test_monitor.py` | Rung 1 | additions, plus a `coid` parameter on a local helper |
| `tests/unit/test_protective.py` | Rung 1 | three call sites follow the new signature |
| `tests/scenario/test_protective_resize.py` | Rung 3 | new |

---

### Task 1: `plan_legs` is told the quantity, and the service can answer for the holding

Two signature changes, no behaviour change. Doing them first means every later task edits one file.

**Files:**
- Modify: `tradebot/execution/protective.py:58-88` (`plan_legs`)
- Modify: `tradebot/execution/service.py` (add `held`)
- Modify: `tradebot/execution/monitor.py:157-163` (the one call site)
- Modify: `tradebot/execution/brokers/binance.py:449` (one comment)
- Test: `tests/unit/test_protective.py`

**Interfaces:**
- Produces: `plan_legs(entry, instrument, capabilities, *, at, qty: Decimal, revision: int = 0) -> LegPlan`
- Produces: `ExecutionService.held(instrument_key: str) -> Decimal`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_protective.py`, in `TestSizing`:

```python
    def test_legs_guard_the_quantity_they_are_given_not_the_entry_fill(
        self, instrument: Instrument
    ) -> None:
        """Design §2: the caller is the only thing that can see the position.

        Sizing from `entry.filled_qty` here is KNOWN_GAPS §4 one level down — the decision made in
        the one place with no view of what is actually held.
        """
        plan = plan_legs(
            entry(instrument, qty="0.5", filled="0.5"),
            instrument,
            capabilities(),
            at=NOW,
            qty=Decimal("0.2"),
        )

        assert plan.protected
        assert {leg.qty for leg in plan.intents} == {Decimal("0.2")}
```

Add to `tests/unit/test_monitor.py`, in a new class:

```python
class TestHeld:
    async def test_held_answers_from_the_ledger(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Deliberately the ledger and not the venue — design D2.

        A monitor that asked the venue and quietly resized to its figure would absorb the one
        alarm `Reconciler` exists to raise (ADR 0006, KNOWN_GAPS §1).
        """
        service = ExecutionService(broker, store, ledger, clock)
        assert service.held(instrument.key) == Decimal(0)

        broker.observe(tick(instrument, clock, last="49000"))
        await service.submit(entry_intent(instrument, clock), instrument)

        assert service.held(instrument.key) == ledger.position(instrument.key).qty
        assert service.held(instrument.key) > Decimal(0)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_protective.py tests/unit/test_monitor.py -q`
Expected: FAIL — `plan_legs() got an unexpected keyword argument 'qty'` and `'ExecutionService' object has no attribute 'held'`.

- [ ] **Step 3: Give `plan_legs` its `qty`**

In `tradebot/execution/protective.py`, change the signature and the two lines that derived the quantity:

```python
def plan_legs(
    entry: Order,
    instrument: Instrument,
    capabilities: BrokerCapabilities,
    *,
    at: UtcDatetime,
    qty: Decimal,
    revision: int = 0,
) -> LegPlan:
    """Build the protective legs guarding `qty` of the position `entry` opened.

    `qty` is **required and never defaulted to `entry.filled_qty`**. Only the caller can see the
    position, and a leg sized from the entry alone is KNOWN_GAPS §4: a partial exit taken by any
    other path leaves it oversized and resting at the venue. An optional parameter falling back to
    the entry would let a future caller re-introduce that by omission (design §2).
    """
    plan = entry.protective
    if plan is None:
        return LegPlan(unprotected_reason="no protective plan on the entry")
    if not capabilities.protective_orders:
        return LegPlan(unprotected_reason=f"{capabilities.venue_id} holds no protective orders")

    if qty <= ZERO:
        return LegPlan(unprotected_reason="no quantity to protect")
```

Delete the `qty = entry.filled_qty` line that preceded the old check. Everything below is unchanged.

Update the docstring bullet at the top of the module, which currently promises the wrong thing:

```python
* **Legs are sized to the quantity the caller asks for**, which is the caller's view of the
  *position* — never to what was ordered, and since KNOWN_GAPS §4 never to what this entry
  filled either. A leg for more than is held tries to sell what is not there.
```

- [ ] **Step 4: Update the two existing tests that relied on the old sizing**

In `tests/unit/test_protective.py`:

```python
    def test_legs_guard_what_filled_not_what_was_ordered(self, instrument: Instrument) -> None:
        """A leg for the full order after a half fill tries to sell what is not held."""
        order = entry(instrument, qty="0.5", filled="0.2")
        plan = plan_legs(order, instrument, capabilities(), at=NOW, qty=order.filled_qty)

        assert plan.protected
        assert {leg.qty for leg in plan.intents} == {Decimal("0.2")}

    def test_a_zero_quantity_has_nothing_to_protect(self, instrument: Instrument) -> None:
        plan = plan_legs(
            entry(instrument, filled=None), instrument, capabilities(), at=NOW, qty=ZERO
        )

        assert not plan.protected
        assert "no quantity to protect" in plan.unprotected_reason
```

and in `test_legs_below_a_venue_minimum_are_reported_not_silently_skipped`, pass `qty=Decimal("0.5")`.

Add `from tradebot.core.money import ZERO` to that file's imports if it is not already there.

- [ ] **Step 5: Add `ExecutionService.held`**

In `tradebot/execution/service.py`, beside `events_for`:

```python
    def held(self, instrument_key: str) -> Decimal:
        """What the ledger says is held. Deliberately *not* the venue's answer (design D2).

        `ExecutionMonitor` sizes protective legs from this. Asking the venue here would make the
        monitor a second reconciler: a venue-versus-us difference is `Reconciler`'s to classify and
        escalate (ADR 0006), and silently resizing the legs to the venue's figure would absorb that
        alarm — tidy screen, nothing told, discrepancy invisible.
        """
        return self._ledger.position(instrument_key).qty
```

Add `from decimal import Decimal` to the imports if absent.

- [ ] **Step 6: Update the monitor's call site, preserving behaviour exactly**

In `tradebot/execution/monitor.py`, inside `_replace_legs`:

```python
        plan = plan_legs(
            entry,
            group.instrument,
            capabilities,
            at=self._clock.now(),
            qty=entry.filled_qty,
            revision=group.revision + 1,
        )
```

`qty=entry.filled_qty` is the old behaviour spelled out. Task 3 replaces it with the target.

- [ ] **Step 7: Fix the stale comment in the Binance adapter**

`tradebot/execution/brokers/binance.py:449` — "sizes both to the same filled quantity" becomes:

```python
        expressed. They never should: `plan_legs` sizes both legs to the same quantity. If they
```

- [ ] **Step 8: Run the full gate**

Run: `.\check.ps1`
Expected: PASS. Every pre-existing monitor test still green — this task changes no behaviour.

- [ ] **Step 9: Commit**

```bash
git add tradebot/execution/protective.py tradebot/execution/service.py tradebot/execution/monitor.py tradebot/execution/brokers/binance.py tests/unit/test_protective.py tests/unit/test_monitor.py
git commit -m "refactor(execution): plan_legs is told its quantity; the service can answer for the holding

Behaviour-preserving. plan_legs read entry.filled_qty itself, which is
KNOWN_GAPS §4 one level down: the sizing decision made in the one place with
no view of the position. It now takes a required qty, required rather than
defaulted so omission cannot re-introduce the bug.

ExecutionService.held is the monitor's route to the position — the ledger's
answer, deliberately not the venue's (design D2)."
```

---

### Task 2: derive the guarded quantity from the venue's own answer

Delete `protected_qty`. Still entry-driven; the position arrives in Task 3.

**Files:**
- Modify: `tradebot/execution/monitor.py:50-59` (`_Tracked`), `:146-181` (`_maintain`, `_replace_legs`, `_record_unprotected`)

**Interfaces:**
- Consumes: `Order.remaining_qty` (`qty - filled_qty`), `OrderState.is_open`
- Produces: `_Tracked.resting_qty -> Decimal`, `_Tracked.unprotected_at: Decimal | None`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_monitor.py`, in the protective-group test class:

```python
    async def test_the_guarded_quantity_is_read_off_the_legs_not_remembered(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Design D2: `poll` already re-reads every leg from the venue, so a counter beside that
        answer is a second opinion that can drift — and its drift *is* KNOWN_GAPS §4."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        group = monitor._tracked["sim-ENTRY"]
        assert group.resting_qty == Decimal("0.5")

        await broker.cancel(
            OrderRef(
                client_order_id=next(
                    leg.client_order_id
                    for leg in group.legs.values()
                    if leg.role is OrderRole.STOP_LOSS
                ),
                instrument_key=instrument.key,
            )
        )
        clock.advance(30)
        await monitor.poll()

        assert group.resting_qty < Decimal("0.5"), "a leg cancelled at the venue is not guarding"
```

Import `OrderRef` from `tradebot.interfaces.broker` in the test file.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q -k guarded_quantity`
Expected: FAIL — `'_Tracked' object has no attribute 'resting_qty'`.

- [ ] **Step 3: Replace the field with a derived property and a report marker**

In `tradebot/execution/monitor.py`:

```python
@dataclass(slots=True)
class _Tracked:
    order: Order
    instrument: Instrument
    #: How many times this group's protective legs have been replaced, so each replacement gets
    #: its own deterministic `client_order_id`. The one thing here that cannot be derived: two
    #: replacements at the same size must not collide.
    revision: int = 0
    legs: dict[str, Order] = field(default_factory=dict)
    #: The target last reported as unguardable, so that report fires once per target rather than
    #: once per poll. A de-duplication marker; nothing reasons from it.
    unprotected_at: Decimal | None = None

    @property
    def resting_qty(self) -> Decimal:
        """How much of the holding the venue is currently guarding for this group.

        A `max`, never a sum: with OCO the stop and the take-profit rest at the same size and the
        venue's order list reserves the coins once, not twice — summing would halve every group on
        the first poll after arming. Read off the legs `poll` has just re-synced, so this is the
        venue's own answer rather than a counter that can drift (design D2).
        """
        return max(
            (leg.remaining_qty for leg in self.legs.values() if leg.state.is_open),
            default=ZERO,
        )
```

`protected_qty` is deleted.

- [ ] **Step 4: Point the three readers at the new pair**

`_maintain` — note that `unprotected_at` now carries the de-duplication `protected_qty` used to provide, which is why the two replacements are not separable (spec §3):

```python
    async def _maintain(self, group: _Tracked) -> None:
        """Keep the protective legs matched to what the entry has actually filled."""
        entry = group.order
        target = entry.filled_qty
        if target > ZERO and target != group.resting_qty and target != group.unprotected_at:
            await self._replace_legs(group)
        if any(leg.state is OrderState.FILLED for leg in group.legs.values()):
            await self._close_group(group)
```

In `_replace_legs`, delete `group.protected_qty = entry.filled_qty` and, after the legs are placed, clear the marker — or a position that becomes unguardable, recovers, and becomes unguardable again at the same size is reported once and never again:

```python
        group.unprotected_at = None
```

In `_record_unprotected`, replace `group.protected_qty = entry.filled_qty` with:

```python
        group.unprotected_at = entry.filled_qty
```

- [ ] **Step 5: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q`
Expected: PASS, **including** `test_an_unprotected_position_is_flagged_once_not_every_poll` and `test_legs_are_replaced_when_more_of_the_entry_fills` unmodified. If either fails, the marker wiring is wrong — fix the code, never the test.

- [ ] **Step 6: Commit**

```bash
git add tradebot/execution/monitor.py tests/unit/test_monitor.py
git commit -m "refactor(execution): derive the guarded quantity from the legs, drop protected_qty

poll already fetch_order's every leg every sweep, so the local counter was a
second opinion sitting beside a fresher fact — and its drift is KNOWN_GAPS §4.
resting_qty is a max over the open legs' remaining quantity, never a sum: an
OCO pair reserves the coins once.

protected_qty did two jobs and both replacements land together. It also
suppressed the unprotected report from firing every poll, which resting_qty
cannot do — it is zero exactly when the report is warranted — so unprotected_at
takes that job."
```

---

### Task 3: allocate the holding across an instrument's groups, tightest stop first

The fix itself.

**Files:**
- Modify: `tradebot/execution/monitor.py` (`poll`, `_maintain`, `_replace_legs`, new `_targets` / `_protectable` / `_committed`, new `_TIGHTEST_FIRST`)
- Test: `tests/unit/test_monitor.py`

**Interfaces:**
- Consumes: `ExecutionService.held`, `_Tracked.resting_qty`, `plan_legs(..., qty=...)` from Tasks 1–2
- Produces: `ExecutionMonitor._targets() -> dict[str, Decimal]` keyed by `group_id`; `_maintain(group: _Tracked, target: Decimal)`

- [ ] **Step 1: Extend the test helper so two entries can coexist**

`entry_intent` hardcodes `client_order_id="sim-ENTRY"`, and this task needs two groups on one instrument. In `tests/unit/test_monitor.py`:

```python
def entry_intent(
    instrument: Instrument,
    clock: ManualClock,
    *,
    price: str = "50000",
    qty: str = "0.5",
    ttl: int | None = 60,
    plan: ProtectivePlan | None = PLAN,
    coid: str = "sim-ENTRY",
    side: Side = Side.BUY,
) -> OrderIntent:
    return OrderIntent(
        client_order_id=coid,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=side,
        qty=Decimal(qty),
        order_type=OrderType.LIMIT,
        limit_price=Decimal(price),
        protective=plan,
        ttl_seconds=ttl,
        created_at=clock.now(),
    )


def external_sell(instrument: Instrument, clock: ManualClock, *, qty: str) -> Fill:
    """A reduction booked by some other path: another cycle's exit, or an operator close.

    KNOWN_GAPS §4's own case was a plain cycle SELL (`sim-R7GB2OIBDAQWPVTG`), not a manual close.
    """
    return Fill(
        fill_id=f"external-{qty}",
        client_order_id="sim-ELSEWHERE",
        instrument_key=instrument.key,
        side=Side.SELL,
        qty=Decimal(qty),
        price=Decimal("49000"),
        filled_at=clock.now(),
    )


def book(ledger: Ledger, fill: Fill, instrument: Instrument) -> None:
    ledger.apply_fill(
        fill,
        base_currency=instrument.base_currency,
        quote_currency=instrument.quote_currency,
    )
```

Import `Fill` from `tradebot.core.orders`.

- [ ] **Step 2: Write the failing tests**

```python
class TestLegsTrackThePosition:
    """KNOWN_GAPS §4. The monitor had no view of the position, so a SELL from any other path
    reduced the holding while the legs kept their original size."""

    async def test_legs_shrink_when_the_position_is_reduced_elsewhere(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.5")

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        live = [leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open]
        assert live, "the position still exists, so it is still guarded"
        assert {leg.qty for leg in live} == {Decimal("0.3")}

    async def test_a_position_closed_elsewhere_releases_the_legs_without_flagging_it(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Zero is not "unprotected": that event means money is at risk with no stop behind it,
        and this group now guards nothing and risks nothing (design §2.3)."""
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        book(ledger, external_sell(instrument, clock, qty="0.5"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        assert not [
            leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open
        ]
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert not risk, "nothing is at risk, so nothing is flagged"

    @pytest.mark.parametrize("tight_first", [True, False])
    async def test_the_tighter_stop_keeps_its_cover_whichever_was_opened_first(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
        tight_first: bool,
    ) -> None:
        """Design D3. For a long the tightest stop is the *highest* — it fires first on the way
        down, so it is the cover worth keeping. Parametrized because age is a proxy that inverts:
        one order of these two fails under oldest-first, the other under newest-first."""
        tight = ("sim-TIGHT", ProtectivePlan(stop_price=Decimal("48000")))
        wide = ("sim-WIDE", ProtectivePlan(stop_price=Decimal("45000")))
        broker.observe(tick(instrument, clock, last="49000"))
        for coid, plan in (tight, wide) if tight_first else (wide, tight):
            await submit_entry(
                monitor, broker, store, ledger, clock, instrument,
                coid=coid, qty="0.3", plan=plan, ttl=None,
            )
            clock.advance(1)
        await monitor.poll()
        assert ledger.position(instrument.key).qty == Decimal("0.6")

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        clock.advance(30)
        broker.observe(tick(instrument, clock, last="49000"))
        await monitor.poll()

        guarded = {
            leg.group_id: leg.qty
            for leg in monitor.tracked
            if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
        }
        assert guarded == {"sim-TIGHT": Decimal("0.3"), "sim-WIDE": Decimal("0.1")}

    async def test_a_working_discretionary_sell_reduces_the_budget(
        self,
        monitor: ExecutionMonitor,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """A resting exit reserves the base asset exactly as a stop does. Ignoring it commits
        0.5 + 0.2 against a holding of 0.5, and a real venue rejects one of them (design §2.2)."""
        service = ExecutionService(broker, store, ledger, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        await submit_entry(monitor, broker, store, ledger, clock, instrument, ttl=None)
        await monitor.poll()

        # Well above the market, so it rests rather than crossing.
        exit_order = await service.submit(
            entry_intent(
                instrument, clock, coid="sim-EXIT", qty="0.2",
                price="60000", side=Side.SELL, plan=None, ttl=None,
            ),
            instrument,
        )
        monitor.track(exit_order, instrument)
        clock.advance(30)
        await monitor.poll()

        stops = [
            leg.qty
            for leg in monitor.tracked
            if leg.role is OrderRole.STOP_LOSS and leg.state.is_open
        ]
        assert stops == [Decimal("0.3")]
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert not risk, "a reducing SELL is the exit; it needs no protection and reports none"
```

`ProtectivePlan` here omits `take_profit_price`; confirm it is optional on the model, and if it is not, pass `take_profit_price=Decimal("54000")` on both.

- [ ] **Step 3: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py::TestLegsTrackThePosition -q`
Expected: FAIL — legs stay at their original size; the parametrized test reports `{"sim-TIGHT": 0.3, "sim-WIDE": 0.3}`.

- [ ] **Step 4: Add the ranking table**

Beside `DEFAULT_POLL_INTERVAL` in `tradebot/execution/monitor.py`:

```python
#: Which end of the stop-price ordering is funded first when the holding cannot cover every group
#: on an instrument, keyed on the side that *opened* the position. A long is opened BUY and its
#: stops sit below the market, so the tightest — the one that fires first on the way down — is the
#: highest, and `reverse=True` funds it first (design D3).
#:
#: A table rather than an `if`, as `_EXIT_SIDE` and `_OFFSET_SIGN` are in `protective.py`. v1 is
#: long-only so only the BUY row is ever taken; the table keeps the module honest rather than
#: assuming. Age was the first proposal and is a proxy that inverts in a falling market — an entry
#: at 100 stopped at 95 and a later one at 90 stopped at 85, newest-first keeps the 85.
_TIGHTEST_FIRST: dict[Side, bool] = {Side.BUY: True, Side.SELL: False}
```

Import `Side` from `tradebot.core.enums`.

- [ ] **Step 5: Compute the targets**

Add to `ExecutionMonitor`:

```python
    def _targets(self) -> dict[str, Decimal]:
        """How much of each instrument's holding each group's legs may guard.

        The invariant this exists for is that the sum over one instrument's groups never exceeds
        the holding — KNOWN_GAPS §4 is what its absence cost. A per-*group* clamp makes it worse:
        two groups guarding 0.0351 and 0.0852 against a position of 0.1116 would each resize to
        0.1116 and 0.0852, resting 0.1968 against 0.1116.
        """
        targets: dict[str, Decimal] = {}
        for instrument_key in {group.order.instrument_key for group in self._tracked.values()}:
            groups = self._protectable(instrument_key)
            budget = max(
                ZERO, self._execution.held(instrument_key) - self._committed(instrument_key)
            )
            for group in groups:
                target = min(group.order.filled_qty, budget)
                targets[group.order.group_id] = target
                budget -= target
        return targets

    def _protectable(self, instrument_key: str) -> list[_Tracked]:
        """This instrument's groups that can hold legs at all, tightest stop first.

        A group whose entry carries no `ProtectivePlan` is not one of them: a reducing SELL *is*
        the exit and an unprotected venue was charged the sizing haircut instead (`protective_plan`
        returns `None` for both). Running one through `plan_legs` is what made every filled
        discretionary SELL file an `unprotected_position` for an order that needs none.
        """
        ranked = [
            ((plan.stop_price, group.order.created_at, group.order.client_order_id), group)
            for group in self._tracked.values()
            if group.order.instrument_key == instrument_key
            and (plan := group.order.protective) is not None
        ]
        if not ranked:
            return []
        # Ties break on creation then id because startup adopts orders from the database in
        # arbitrary order, and the allocation must survive a restart unchanged.
        ranked.sort(key=lambda pair: pair[0], reverse=_TIGHTEST_FIRST[ranked[0][1].order.side])
        return [group for _, group in ranked]

    def _committed(self, instrument_key: str) -> Decimal:
        """Quantity our own working sells already commit, outside the protective legs.

        A discretionary exit or an ADR 0015 operator close still resting reserves the base asset at
        the venue exactly as a stop does. Only entries are considered — `legs` holds protective
        orders by construction, and those are what the budget is being divided among.
        """
        return sum(
            (
                group.order.remaining_qty
                for group in self._tracked.values()
                if group.order.instrument_key == instrument_key
                and group.order.side is Side.SELL
                and group.order.state.is_open
            ),
            start=ZERO,
        )
```

- [ ] **Step 6: Drive them from `poll`, and take the target in `_maintain`**

```python
    async def poll(self) -> None:
        """One sweep: sync every working order, expire what is past its TTL, mind the groups."""
        async with self._polling:
            for group in list(self._tracked.values()):
                group.order = await self._sync(group, group.order)
                for client_order_id, leg in list(group.legs.items()):
                    group.legs[client_order_id] = await self._sync(group, leg)
            # After the sync loop, never inside it: `_sync` books fills as it reads them, so a stop
            # that filled this sweep has already reduced the position. Computing per group inside
            # the loop would size some groups against a pre-fill holding and others against a
            # post-fill one (design §2.4).
            targets = self._targets()
            for group in list(self._tracked.values()):
                await self._maintain(group, targets.get(group.order.group_id, ZERO))

    async def _maintain(self, group: _Tracked, target: Decimal) -> None:
        """Keep the protective legs matched to the *position*, not to this entry's fills.

        KNOWN_GAPS §4: the legs tracked `entry.filled_qty`, so a SELL from any other path — another
        cycle's exit decision, an ADR 0015 operator close — reduced the holding while the legs kept
        their original size, and the oversized order rested at the venue until it triggered.
        """
        if target != group.resting_qty and target != group.unprotected_at:
            await self._replace_legs(group, target)
        if any(leg.state is OrderState.FILLED for leg in group.legs.values()):
            await self._close_group(group)

    async def _replace_legs(self, group: _Tracked, target: Decimal) -> None:
        if target <= ZERO:
            # Nothing is held behind this group any more. Not "unprotected" — that event means
            # money is at risk with no stop, and this guards nothing and risks nothing (§2.3).
            await self._cancel_legs(group, reason="released_to_position")
            group.unprotected_at = None
            return

        entry = group.order
        capabilities = self._broker.capabilities()
        plan = plan_legs(
            entry,
            group.instrument,
            capabilities,
            at=self._clock.now(),
            qty=target,
            revision=group.revision + 1,
        )
        if not plan.protected:
            await self._record_unprotected(group, plan.unprotected_reason, target)
            return

        await self._cancel_legs(group, reason="resized_to_position")
        group.revision += 1
        placed = await self._execution.submit_group(plan.intents, group.instrument)
        for leg in placed:
            group.legs[leg.client_order_id] = leg
        group.unprotected_at = None
        events = self._execution.events_for(entry)
        await self._store.append(events.protective_placed(entry, tuple(placed)))
```

`_record_unprotected` takes the target rather than reading the entry:

```python
    async def _record_unprotected(self, group: _Tracked, reason: str, target: Decimal) -> None:
        entry = group.order
        group.unprotected_at = target
```

The rest of that method is unchanged. Task 4 adds the cancel it is still missing.

- [ ] **Step 7: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q`
Expected: PASS, new and pre-existing alike.

- [ ] **Step 8: Run the full gate**

Run: `.\check.ps1`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add tradebot/execution/monitor.py tests/unit/test_monitor.py
git commit -m "fix(execution): protective legs track the position, not the entry order

KNOWN_GAPS §4. _maintain compared entry.filled_qty against the legs' own size,
so a SELL from any other path reduced the holding while the legs kept their
original size; when one later triggered it sold more than was held, hours or
days after the oversized order started resting at the venue.

The invariant is per instrument, not per group: two groups over one holding
would each clamp to the whole position and rest more than before. So poll now
allocates the holding across an instrument's groups before maintaining any of
them, tightest stop first (design D3) — for a long the highest stop fires first
on the way down, and age is a proxy for that which inverts in a falling market.

The budget also subtracts our own working sells: a resting exit reserves the
base asset exactly as a stop does.

A group whose entry carries no ProtectivePlan is now skipped, which stops every
filled discretionary SELL filing an unprotected_position for an order that is
itself the exit (observed at seq 2232 in data/sim.db)."
```

---

### Task 4: the two remaining failure paths tell the truth

**Files:**
- Modify: `tradebot/execution/monitor.py` (`_replace_legs`, `_record_unprotected`)
- Test: `tests/unit/test_monitor.py`

**Interfaces:**
- Consumes: everything from Task 3.

- [ ] **Step 1: Write the failing tests**

```python
    async def test_a_shrink_below_venue_minimums_cancels_the_legs_and_reports(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """Today the reason is recorded and the legs are left resting — an oversized order at the
        venue *and* a report saying the position is unguarded, both false (design §2.3)."""
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        # 0.0001 BTC at 49 000 is 4.90, below the instrument's min_notional of 10.
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        clock.advance(30)
        await monitor.poll()

        assert not [
            leg for leg in monitor.tracked if leg.role.is_protective and leg.state.is_open
        ], "an oversized leg must not outlive the holding it was sized for"
        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == ["unprotected_position"]

    async def test_a_failed_placement_after_the_cancel_is_recorded_before_it_propagates(
        self,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """The window is forced by the venue (design D4); what must not happen is a cancellation
        followed by silence, leaving the state to be inferred from an absence."""

        class _FailingGroupBroker(SimBroker):
            fail_group = False

            async def submit_group(self, intents):  # type: ignore[no-untyped-def]
                if self.fail_group:
                    self.fail_group = False
                    raise RetryableError("venue unavailable")
                return await super().submit_group(intents)

        broker = _FailingGroupBroker(clock, balances={"USDT": Decimal(100_000)})
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        book(ledger, external_sell(instrument, clock, qty="0.2"), instrument)
        broker.fail_group = True
        clock.advance(30)
        with pytest.raises(RetryableError):
            await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == ["unprotected_position"]
        assert "venue unavailable" in risk[0].payload["detail"]

    async def test_a_successful_placement_re_arms_the_report(
        self,
        broker: SimBroker,
        store: EventStore,
        ledger: Ledger,
        clock: ManualClock,
        instrument: Instrument,
    ) -> None:
        """`unprotected_at` is cleared when legs are placed (design §2.3). Without that, a position
        that becomes unguardable, recovers, and becomes unguardable again at the same size is
        reported once and never again."""
        service = ExecutionService(broker, store, ledger, clock)
        monitor = ExecutionMonitor(broker, service, store, clock)
        broker.observe(tick(instrument, clock, last="49000"))
        order = await service.submit(entry_intent(instrument, clock, ttl=None), instrument)
        monitor.track(order, instrument)
        await monitor.poll()

        # Down to dust: 0.0001 at 49 000 is 4.90, under the instrument's min_notional of 10.
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        clock.advance(30)
        await monitor.poll()

        # Back up, guardable again, then down to the same dust a second time.
        book(ledger, Fill(
            fill_id="refill", client_order_id="sim-ELSEWHERE", instrument_key=instrument.key,
            side=Side.BUY, qty=Decimal("0.4999"), price=Decimal("49000"), filled_at=clock.now(),
        ), instrument)
        clock.advance(30)
        await monitor.poll()
        book(ledger, external_sell(instrument, clock, qty="0.4999"), instrument)
        clock.advance(30)
        await monitor.poll()

        risk = [e for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert [e.payload["rule"] for e in risk] == [
            "unprotected_position",
            "unprotected_position",
        ], "the second time it becomes unguardable is a second fact, not a repeat"
```

Import `RetryableError` from `tradebot.core.errors`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q -k "venue_minimums or failed_placement"`
Expected: FAIL — the legs are still open in the first, and no risk event exists in the second.

- [ ] **Step 3: Cancel before reporting, and record before propagating**

In `_replace_legs`:

```python
        if not plan.protected:
            # Cancel *first*. What is resting was sized for a larger holding: if it triggered the
            # venue would reject it for insufficient balance, so leaving it is a false protection
            # on top of a true report (design §2.3).
            await self._cancel_legs(group, reason="below_venue_minimums")
            await self._record_unprotected(group, plan.unprotected_reason, target)
            return

        await self._cancel_legs(group, reason="resized_to_position")
        group.revision += 1
        try:
            placed = await self._execution.submit_group(plan.intents, group.instrument)
        except Exception as error:
            # The legs are already cancelled, so the position is bare until the next poll retries.
            # The venue error still reaches the caller's retry budget — what changes is that the
            # log says what state this left behind instead of showing a cancel and then nothing.
            await self._record_unprotected(group, f"placement failed: {error}", target)
            raise
```

`except Exception` is deliberate and is not a swallow: it records and re-raises, so the error class still decides the handling (`RetryableError` / `FailClosedError` / `FatalError`) exactly as before.

- [ ] **Step 4: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradebot/execution/monitor.py tests/unit/test_monitor.py
git commit -m "fix(execution): a leg that cannot be resized is cancelled, and a failed one is recorded

Two paths in _replace_legs that were honest about a growth and wrong about a
shrink. A target below venue minimums recorded the reason and returned with the
oversized legs still resting: an order at the venue that would be rejected when
it triggered, plus a report saying the position was unguarded — both false, in
opposite directions.

And a submit_group raising after the cancel wrote nothing at all, so the log
showed a cancellation followed by silence. It now records unprotected_position
with the venue's reason and re-raises, so the error still reaches the caller's
retry budget."
```

---

### Task 5: the invariant, as a property

**Files:**
- Test: `tests/unit/test_monitor.py`

**Interfaces:**
- Consumes: `ExecutionMonitor._targets`, `_Tracked`

- [ ] **Step 1: Write the test**

Hypothesis is not used with async anywhere in this suite, and `_targets` is synchronous — so this drives the allocation directly rather than through `poll`. That is the rule under test; the tests above already cover it reaching the venue.

```python
class TestAllocationInvariant:
    @given(
        fills=st.lists(
            st.decimals(min_value=Decimal("0.001"), max_value=Decimal("10"), places=3),
            min_size=1,
            max_size=5,
        ),
        stops=st.lists(
            st.decimals(min_value=Decimal("100"), max_value=Decimal("60000"), places=2),
            min_size=5,
            max_size=5,
            unique=True,
        ),
        holding=st.decimals(min_value=Decimal(0), max_value=Decimal("20"), places=3),
    )
    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_the_total_allocated_never_exceeds_the_holding(
        self,
        broker: SimBroker,
        store: EventStore,
        clock: ManualClock,
        instrument: Instrument,
        fills: list[Decimal],
        stops: list[Decimal],
        holding: Decimal,
    ) -> None:
        """Spec §3. Whatever the reduction was and wherever it came from, the sum of what the
        groups may guard is at most what is held. This is the property KNOWN_GAPS §4 violated."""
        monitor = _monitor_over(broker, store, clock, instrument, holding)
        for index, fill in enumerate(fills):
            monitor.track(
                _filled_entry(instrument, clock, coid=f"g{index}", filled=fill, stop=stops[index]),
                instrument,
            )

        targets = monitor._targets()

        assert sum(targets.values(), start=ZERO) <= holding
        for index, fill in enumerate(fills):
            assert targets[f"g{index}"] <= fill, "a group never guards more than its own fill"
        assert all(target >= ZERO for target in targets.values())
```

Two helpers beside it:

```python
def _monitor_over(
    broker: SimBroker,
    store: EventStore,
    clock: ManualClock,
    instrument: Instrument,
    holding: Decimal,
) -> ExecutionMonitor:
    ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(100_000)})
    ledger.adopt_position(Position(instrument_key=instrument.key, qty=holding))
    return ExecutionMonitor(broker, ExecutionService(broker, store, ledger, clock), store, clock)


def _filled_entry(
    instrument: Instrument, clock: ManualClock, *, coid: str, filled: Decimal, stop: Decimal
) -> Order:
    """An entry that has already filled `filled`, with its stop at `stop`."""
    intent = OrderIntent(
        client_order_id=coid,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=instrument.key,
        side=Side.BUY,
        qty=filled,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50000"),
        protective=ProtectivePlan(stop_price=stop, take_profit_price=stop + Decimal("10000")),
        created_at=clock.now(),
    )
    return Order.from_intent(intent).with_fill(
        Fill(
            fill_id=f"{coid}-1",
            client_order_id=coid,
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=filled,
            price=Decimal("50000"),
            filled_at=clock.now(),
        )
    )
```

Imports: `given`, `settings`, `HealthCheck`, `strategies as st` from `hypothesis`; `Position` from `tradebot.core.portfolio`; `Order` from `tradebot.core.orders`.

The fixtures are function-scoped and hypothesis reuses them across examples, hence the suppressed health check. That is sound here and only here: `_targets` reads and writes nothing, and a fresh `Ledger` and `ExecutionMonitor` are built per example inside the test. Do not copy the suppression into a test that mutates.

Reaching `_targets` directly is the point: it is the allocation rule, and driving it through `poll` would make the property a test of `SimBroker`'s matching engine instead.

- [ ] **Step 2: Run it**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_monitor.py::TestAllocationInvariant -q`
Expected: PASS. If hypothesis finds a counterexample, the allocation is wrong — fix `_targets`, never the strategy bounds.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_monitor.py
git commit -m "test(execution): the allocation invariant, as a property

Sum of what an instrument's groups may guard is at most what is held, over
arbitrary fills, stop prices and holdings. Drives _targets directly: through
poll it would be a test of SimBroker's matching engine instead."
```

---

### Task 6: the rung-3 scenario the gap names as its own absence

**Files:**
- Create: `tests/scenario/test_protective_resize.py`

**Interfaces:**
- Consumes: `tests.scenario.harness.Harness`, the `basket` / `clock` / `market_data` fixtures from `tests/conftest.py`

- [ ] **Step 1: Write the test**

```python
"""Rung 3: an entry, a partial discretionary exit, then a bar through the original stop.

KNOWN_GAPS §4 survived hundreds of backtests and the whole scenario suite because the `stub` panel
never takes a partial exit — every prior scenario either held or closed in full. This one scripts
the sequence that reaches it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tests.scenario.harness import Harness

from tradebot.core.enums import OrderRole, OrderState

pytestmark = pytest.mark.scenario

PARTIAL_SELL = """{
  "action": "SELL", "conviction": 5, "size_hint": "quarter",
  "thesis": "Take a quarter off.", "key_risks": [], "invalidation": "n/a"
}"""
HOLD_RESPONSE = """{
  "action": "HOLD", "conviction": 3, "size_hint": "none",
  "thesis": "Nothing to do.", "key_risks": [], "invalidation": "n/a"
}"""


@pytest.fixture
def crashing_market(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    """A rising series the panel buys into, then one bar that falls through the entry's stop.

    The shared `market_data` fixture only ever rises, which is why no scenario has fired a stop
    against a *reduced* position before.
    """
    series = {}
    for timeframe in TIMEFRAMES:
        rising = synthetic_candles(
            start=SERIES_START,
            timeframe=timeframe,
            count=200,
            open_price=Decimal("50000"),
            step=Decimal("25"),
        )
        last = rising[-1]
        series[(instrument.key, timeframe)] = (
            *rising,
            Candle(
                open_time=last.close_time,
                close_time=last.close_time + (last.close_time - last.open_time),
                open=last.close,
                high=last.close,
                low=last.close - Decimal("6000"),
                close=last.close - Decimal("5500"),
                volume=Decimal(1),
            ),
        )
    return ReplayMarketData(series, clock)


async def test_a_partial_exit_resizes_the_legs_before_the_stop_fires(
    basket: Basket, clock: ManualClock, crashing_market: ReplayMarketData
) -> None:
    """KNOWN_GAPS §4 end to end, through the real loop.

    Before the fix the third cycle raises out of `Ledger._apply_sell` — "sell of 0.5 exceeds
    holding 0.375" — because the stop still rests at the size the entry filled.
    """
    harness = Harness(
        basket, clock, crashing_market, [DEFAULT_RESPONSE, PARTIAL_SELL, HOLD_RESPONSE]
    )
    await harness.start()
    key = basket.instruments[0].key
    try:
        await harness.runner.run_once()
        opened = harness.ledger.position(key).qty
        assert opened > ZERO, "the panel bought and the entry filled"

        await harness.runner.run_once()
        reduced = harness.ledger.position(key).qty
        assert ZERO < reduced < opened, "the panel took part of the position off"

        resting = [
            order
            for order in harness.monitor.tracked
            if order.role is OrderRole.STOP_LOSS and order.state.is_open
        ]
        assert [order.qty for order in resting] == [reduced], (
            "the stop guards what is held, not what the entry filled — KNOWN_GAPS §4"
        )

        await harness.runner.run_once()

        filled = [
            order
            for order in harness.monitor.tracked
            if order.role is OrderRole.STOP_LOSS and order.state is OrderState.FILLED
        ]
        assert filled, "the crash bar crossed the trigger"
        assert filled[0].filled_qty <= reduced, "no exit sells more than was held"
        assert harness.ledger.position(key).is_flat
    finally:
        harness.close()
```

Imports beyond those already shown: `SERIES_START` and `TIMEFRAMES` from `tests.conftest`; `Candle` from `tradebot.core.market`; `ReplayMarketData` and `synthetic_candles` from `tradebot.marketdata.replay`; `Basket` from `tradebot.core.config`; `ManualClock` from `tradebot.core.clock`; `Instrument` from `tradebot.core.instrument`; `ZERO` from `tradebot.core.money`; `DEFAULT_RESPONSE` from `tradebot.decision.providers`, as `test_full_cycle.py` imports it.

**Tuning note.** The crash depth (`6000`) and where the three cycles land in the series are what decide whether the sequence actually reaches the defect, and both depend on how far `synthetic_candles` has walked and where the ATR-derived stop sits relative to it. Step 2 is the objective gate for getting them right: adjust them until the test **fails against the pre-fix code with `ReconciliationMismatchError`**. Never adjust them to make it pass — a version that passes both before and after the fix is not testing this defect, which is precisely how it survived the existing suite.

- [ ] **Step 2: Run it against the current code on a stash to confirm it catches the defect**

```bash
git stash push tradebot/execution/monitor.py
.venv\Scripts\python.exe -m pytest tests/scenario/test_protective_resize.py -q
git stash pop
```

Expected while stashed: FAIL with `ReconciliationMismatchError`. A scenario test that passes against the old code is not testing this defect — if it passes, the sequence is not reaching the bug and the sizing or the tick path needs adjusting until it does.

- [ ] **Step 3: Run it against the fix**

Run: `.venv\Scripts\python.exe -m pytest -m scenario -q`
Expected: PASS, the whole scenario suite included.

- [ ] **Step 4: Commit**

```bash
git add tests/scenario/test_protective_resize.py
git commit -m "test(scenario): an entry, a partial exit, then a bar through the original stop

The test KNOWN_GAPS §4 names as its own absence. It needs a panel that takes
partial exits, which is why the stub panel and every prior scenario missed it."
```

---

### Task 7: prove it on the run that found it, and close the gap

**Files:**
- Modify: `docs/KNOWN_GAPS.md`

- [ ] **Step 1: Re-run the reference pass that raised**

```powershell
.venv\Scripts\python.exe -m decision_lab corpus build --data data\history `
    --reference-panel sim --since 2024-01-01 --until 2024-07-01
```

`data\history` holds `binance__BTC_USDT__1h.csv` and `binance__ETH_USDT__1h.csv` — the dataset this pass was run over. `--every` defaults to `4h`; leave it, so this is the same pass that raised.

Expected: runs to completion. Before the fix it raised `ReconciliationMismatchError` on `binance:ETH/USDT` around 2024-01-08, with the entry at 2024-01-03 16:00 and the partial exit at 2024-01-04 12:00.

Note that this writes a **new** corpus under `decision_lab/workspace/` — `corpus_identity` keys on the reference panel and the config digest, so it will not overwrite the existing stub-panel corpus. The pinned day set and the dataset digests stay valid throughout, provided nothing runs `dataset verify --repair`.

- [ ] **Step 2: Check the pass for the noise this also removed**

Query the resulting corpus database for `RISK_EVENT` rows whose `rule` is `unprotected_position`, and confirm none of them is attributable to a discretionary SELL. Any that remain must name a real venue-minimum or placement failure.

- [ ] **Step 3: Run the whole gate one more time**

Run: `.\check.ps1`
Expected: PASS, `execution/` ≥ 95%.

- [ ] **Step 4: Move §4 to *Closed* in `KNOWN_GAPS.md`**

Delete the `| 4 | ... |` row from the table and renumber §5–§8 to §4–§7, updating the provenance bullets and every cross-reference to them — `§5` and `§6` are referenced by name in the new §4's own text and in the spec. Move the section under a new *Closed* heading dated today, keeping its evidence and adding what it now does, in the register the M1–M4 entries use: what was observed, what changed, and what was re-verified on real data.

- [ ] **Step 5: Commit**

```bash
git add docs/KNOWN_GAPS.md
git commit -m "docs: close gap 4, protective legs track the position

Re-verified on the six-month decision_lab reference pass that found it: it now
runs to completion where it previously raised ReconciliationMismatchError on
binance:ETH/USDT. Gaps 5-8 renumbered to 4-7 and left open."
```

---

## Definition of Done

- `.\check.ps1` green; `execution/` ≥ 95%.
- Every test in the spec's §4 present and passing.
- The pre-existing protective-group tests in `tests/unit/test_monitor.py` unmodified.
- The rung-3 scenario fails against the pre-fix `monitor.py` (Task 6 Step 2) and passes against the fix.
- The `decision_lab` reference pass on `--reference-panel sim` runs to completion.
- No `unprotected_position` in that pass attributable to a discretionary SELL.
- `KNOWN_GAPS.md` §4 closed; the remaining gaps renumbered and left open.
- `git diff --stat main` touches only the files in the File Structure table plus `docs/KNOWN_GAPS.md`.
