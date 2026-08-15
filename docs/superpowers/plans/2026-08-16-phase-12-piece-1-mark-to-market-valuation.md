# Phase 12 Piece 1 — mark-to-market valuation: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make portfolio equity mark-to-market in the notional currency, so the drawdown kill switch can see unrealized loss, and every consumer reads one function.

**Architecture:** A shared `Marks` price cache is written by cycles, a supervisor sweep, startup and manual close, and read by one valuation function — `risk.aggregate.aggregate` — which values all cash, marks every position, and **freezes** rather than falling back to cost. `Ledger` stops having any opinion about prices. A frozen valuation blocks new orders through the watchdog verdict the cycle gate already consults; it never trips the kill switch.

**Tech Stack:** Python 3.12+, pydantic v2, SQLAlchemy Core, pytest + pytest-asyncio, Decimal-only money arithmetic.

**Spec:** [docs/superpowers/specs/2026-08-16-phase-12-piece-1-mark-to-market-valuation-design.md](../specs/2026-08-16-phase-12-piece-1-mark-to-market-valuation-design.md)

## Global Constraints

- **Money is `Decimal`, always.** Use `tradebot.core.money`; never `float`, never `Decimal(some_float)`. Enforced by `tests/unit/test_money_discipline.py`.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never call `datetime.now()` in library code.
- **Errors are classified**: `RetryableError` / `FailClosedError` / `FatalError`. A bare `except: pass` is a defect.
- **Every state change emits an event.** The event log alone must reconstruct a module's state.
- **Prefer dispatch over branching.** Side-dependent behaviour is a `dict`, not an `if`.
- **Comments explain *why*** and cite the spec section they implement (`DESIGN §6.6`, `PLAN §2.3`).
- **Docstrings state failure semantics** at module level.
- **Layering:** `core/` depends on nothing. `interfaces/` depends on `core` only. `risk/` may import `ledger/`; `ledger/` may **not** import `risk/`. Only `app.py` imports concrete adapters.
- **Coverage gates:** `core/`, `risk/`, `execution/`, `ledger/` ≥ 95%; everything else ≥ 80%.
- **Every task ends green.** `.\check.ps1` must pass before each commit. No task may leave the tree failing to typecheck "until the next task fixes it".
- Run the suite with `.venv\Scripts\python.exe -m pytest`, and the full gate with `.\check.ps1`.

**Branch:** all work lands on `feat/phase-12-piece-1-valuation`, cut from `main`.

---

### Task 0: Branch

- [ ] **Step 1: Cut the branch**

```bash
git checkout -b feat/phase-12-piece-1-valuation
```

- [ ] **Step 2: Commit the approved spec**

```bash
git add docs/superpowers/specs/2026-08-16-phase-12-piece-1-mark-to-market-valuation-design.md docs/superpowers/plans/2026-08-16-phase-12-piece-1-mark-to-market-valuation.md
git commit -m "docs: design and plan for Phase 12 Piece 1 mark-to-market valuation"
```

---

### Task 1: `Marks` — the shared price cache

**Files:**
- Create: `tradebot/ledger/marks.py`
- Test: `tests/unit/test_marks.py`

**Interfaces:**
- Consumes: `tradebot.core.market.Quote`, `tradebot.core.schema.UtcDatetime`
- Produces: `Mark(price, observed_at)`; `Marks.observe(key, price, at)`, `Marks.observe_quote(quote)`, `Marks.price_of(key, *, now, tolerance) -> Decimal | None`, `Marks.age_of(key, *, now) -> timedelta | None`, `Marks.keys() -> frozenset[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_marks.py`:

```python
"""The shared price cache the whole portfolio is valued against.

One property is load-bearing and is the entire defect this phase fixes: a mark that is absent
or stale is `None`, never a fallback. Valuing a position at a four-hour-old price is not more
conservative than valuing it at cost — it is differently wrong (PHASE_12 §1.4).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tradebot.core.clock import ManualClock
from tradebot.core.instrument import Instrument
from tradebot.core.market import Quote
from tradebot.ledger.marks import Marks

TOLERANCE = timedelta(minutes=5)


class TestObservation:
    def test_a_fresh_mark_is_returned(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        assert marks.price_of("sim:BTC/USDT", now=clock.now(), tolerance=TOLERANCE) == Decimal(
            "50000"
        )

    def test_a_quote_is_observed_under_its_own_instrument_key(
        self, clock: ManualClock, quote: Quote
    ) -> None:
        marks = Marks()
        marks.observe_quote(quote)

        assert marks.price_of(
            quote.instrument_key, now=clock.now(), tolerance=TOLERANCE
        ) == quote.last

    def test_a_later_observation_replaces_an_earlier_one(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())
        marks.observe("sim:BTC/USDT", Decimal("51000"), clock.now())

        assert marks.price_of("sim:BTC/USDT", now=clock.now(), tolerance=TOLERANCE) == Decimal(
            "51000"
        )


class TestStaleness:
    def test_an_absent_mark_is_none_never_a_fallback(self, clock: ManualClock) -> None:
        assert Marks().price_of("sim:BTC/USDT", now=clock.now(), tolerance=TOLERANCE) is None

    def test_a_mark_older_than_tolerance_is_none(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        later = clock.now() + TOLERANCE + timedelta(seconds=1)

        assert marks.price_of("sim:BTC/USDT", now=later, tolerance=TOLERANCE) is None

    def test_a_mark_exactly_at_tolerance_is_still_a_mark(self, clock: ManualClock) -> None:
        """The boundary is inclusive, so a sweep landing exactly on it does not freeze."""
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        assert marks.price_of(
            "sim:BTC/USDT", now=clock.now() + TOLERANCE, tolerance=TOLERANCE
        ) == Decimal("50000")

    def test_age_is_reported_for_an_operator_to_read(self, clock: ManualClock) -> None:
        marks = Marks()
        marks.observe("sim:BTC/USDT", Decimal("50000"), clock.now())

        assert marks.age_of("sim:BTC/USDT", now=clock.now() + timedelta(minutes=2)) == timedelta(
            minutes=2
        )

    def test_age_of_an_absent_mark_is_none(self, clock: ManualClock) -> None:
        assert Marks().age_of("sim:BTC/USDT", now=clock.now()) is None


class TestKeyspace:
    def test_instrument_and_currency_marks_share_one_namespace_without_colliding(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Instrument keys are `venue:symbol` and always carry a colon; currencies never do."""
        marks = Marks()
        marks.observe(instrument.key, Decimal("50000"), clock.now())
        marks.observe("BTC", Decimal("50000"), clock.now())

        assert ":" in instrument.key
        assert marks.keys() == frozenset({instrument.key, "BTC"})

    def test_a_float_price_is_refused(self, clock: ManualClock) -> None:
        """`Marks` is on the money path, so the money discipline applies to it."""
        import pytest

        from tradebot.core.errors import MoneyError

        with pytest.raises(MoneyError):
            Marks().observe("sim:BTC/USDT", 50000.0, clock.now())  # type: ignore[arg-type]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_marks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.ledger.marks'`

- [ ] **Step 3: Write the implementation**

Create `tradebot/ledger/marks.py`:

```python
"""Current prices for everything the portfolio holds, in the notional currency.

This is a **cache, not a ledger**. Nothing here may adjust a position, a balance or a baseline,
and it has no write path to the database. It is shared mutable state read on the money path, and
the staleness rule below is the only thing keeping it honest.

**A stale mark is not a mark.** `price_of` returns `None` for a key that is absent or older than
the caller's tolerance, and there is no third outcome. Valuing a position at a four-hour-old
price is not more conservative than valuing it at cost — it is differently wrong, in whichever
direction the market moved, and a fallback to cost is the entire mechanism of the drawdown
defect this phase exists to fix (PHASE_12 Finding 1, §1.4).

Instrument marks and currency marks share one map. Instrument keys are `venue:symbol` and always
carry a colon; currency codes never do, so the two cannot collide. A future key format without a
colon would break that, which is why it is stated here rather than left to be noticed.

Failure semantics: this module has no dependencies and cannot fail from the outside. An
unobserved key reads as unknown, which its callers resolve to a frozen aggregate — never to a
guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from tradebot.core.market import Quote
from tradebot.core.money import refuse_float
from tradebot.core.schema import UtcDatetime


@dataclass(frozen=True, slots=True)
class Mark:
    """One observed price, and when it was observed."""

    price: Decimal
    observed_at: UtcDatetime


class Marks:
    """Instrument and currency prices, in the notional currency, with their ages."""

    def __init__(self) -> None:
        self._marks: dict[str, Mark] = {}

    def observe(self, key: str, price: Decimal, at: UtcDatetime) -> None:
        """Record a price. A later observation replaces an earlier one."""
        refuse_float(price)
        self._marks[key] = Mark(price=price, observed_at=at)

    def observe_quote(self, quote: Quote) -> None:
        """Record a quote's last trade under its own instrument key."""
        self.observe(quote.instrument_key, quote.last, quote.observed_at)

    def price_of(self, key: str, *, now: UtcDatetime, tolerance: timedelta) -> Decimal | None:
        """The current mark, or `None` if it is absent or stale. Never a fallback."""
        mark = self._marks.get(key)
        if mark is None or now - mark.observed_at > tolerance:
            return None
        return mark.price

    def age_of(self, key: str, *, now: UtcDatetime) -> timedelta | None:
        """How old this mark is, for an operator to read. `None` when there is none."""
        mark = self._marks.get(key)
        return None if mark is None else now - mark.observed_at

    def keys(self) -> frozenset[str]:
        return frozenset(self._marks)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_marks.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/ledger/marks.py tests/unit/test_marks.py
git commit -m "feat(ledger): add Marks, the shared price cache with no cost-basis fallback"
```

---

### Task 2: `mark_staleness_seconds` on `GlobalRiskPolicy`

**Files:**
- Modify: `tradebot/core/config.py` (`GlobalRiskPolicy`, and its `_check_ranges` validator)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: nothing new
- Produces: `GlobalRiskPolicy.mark_staleness_seconds: int` (default `300`), and `GlobalRiskPolicy.mark_tolerance -> timedelta`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_config.py`:

```python
class TestMarkStaleness:
    def test_it_defaults_to_five_minutes(self) -> None:
        assert GlobalRiskPolicy().mark_staleness_seconds == 300
        assert GlobalRiskPolicy().mark_tolerance == timedelta(minutes=5)

    def test_a_non_positive_tolerance_is_refused(self) -> None:
        """A zero tolerance freezes the portfolio permanently, which is not a limit."""
        with pytest.raises(ValidationError):
            GlobalRiskPolicy(mark_staleness_seconds=0)

    def test_an_existing_policy_document_gains_the_default(self) -> None:
        """Stored policies predate the field; they read back with the default, not a failure."""
        stored = {"max_drawdown_pct": "10"}

        assert GlobalRiskPolicy.model_validate(stored).mark_staleness_seconds == 300
```

Add `from datetime import timedelta` and `from pydantic import ValidationError` to that file's imports if absent.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -k MarkStaleness -v`
Expected: FAIL — `AttributeError`/`ValidationError` on the unknown field

- [ ] **Step 3: Write the implementation**

In `tradebot/core/config.py`, add `from datetime import timedelta` to the imports, then add to `GlobalRiskPolicy` immediately after `stablecoin_peg_tolerance_pct`:

```python
    #: How old a price may be and still value a position. Beyond it the mark is *absent*, the
    #: aggregate freezes, and new orders stop — because a stale mark is not a more conservative
    #: mark, it is a wrong one (PHASE_12 §1.4). Policy rather than a constant for the reason every
    #: other limit is: a limit a restart can clear is not a limit (ADR 0005).
    mark_staleness_seconds: int = Field(default=300, gt=0)

    @property
    def mark_tolerance(self) -> timedelta:
        return timedelta(seconds=self.mark_staleness_seconds)
```

The **other** validation — that this comfortably exceeds the supervisor's resync cadence — deliberately does not live here: `core/` depends on nothing, and `DEFAULT_RESYNC_SECONDS` belongs to `control/supervisor.py`. It is asserted at `PortfolioWatch` construction in Task 11.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_config.py -k MarkStaleness -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/core/config.py tests/unit/test_config.py
git commit -m "feat(config): add mark_staleness_seconds, the tolerance a stale mark freezes past"
```

---

### Task 3: `base_currencies_of` — one definition of "already a position"

**Files:**
- Modify: `tradebot/core/instrument.py` (append the helper)
- Modify: `tradebot/ledger/reconciler.py:173` (use it)
- Test: `tests/unit/test_instrument.py`

**Interfaces:**
- Consumes: `tradebot.core.instrument.Instrument`
- Produces: `base_currencies_of(instruments: Iterable[Instrument]) -> frozenset[str]`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_instrument.py`:

```python
class TestBaseCurrencies:
    def test_it_names_every_base_asset_once(
        self, instrument: Instrument, second_instrument: Instrument
    ) -> None:
        assert base_currencies_of((instrument, second_instrument)) == frozenset({"BTC", "ETH"})

    def test_it_is_empty_for_no_instruments(self) -> None:
        assert base_currencies_of(()) == frozenset()

    def test_two_instruments_sharing_a_base_contribute_one_entry(
        self, instrument: Instrument
    ) -> None:
        other = instrument.model_copy(update={"symbol": "BTC/USDC", "quote_currency": "USDC"})

        assert base_currencies_of((instrument, other)) == frozenset({"BTC"})
```

Import it: `from tradebot.core.instrument import Instrument, base_currencies_of`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_instrument.py -k BaseCurrencies -v`
Expected: FAIL — `ImportError: cannot import name 'base_currencies_of'`

- [ ] **Step 3: Write the implementation**

Append to `tradebot/core/instrument.py`:

```python
def base_currencies_of(instruments: Iterable[Instrument]) -> frozenset[str]:
    """The base assets already counted as positions rather than as cash.

    On a spot venue an instrument's base asset *is* a balance, so two readers need this exact
    set and must not compute it twice: the reconciler excludes it from the balance diff, or it
    reports every position discrepancy a second time, and the valuation excludes it from cash,
    or it counts every holding twice (PHASE_12 §3.3 rung 3).
    """
    return frozenset(instrument.base_currency for instrument in instruments)
```

Add `from collections.abc import Iterable` to that file's imports.

Then in `tradebot/ledger/reconciler.py`, replace line 173:

```python
        held_as_positions = {i.base_currency for i in self._instruments.values()}
```

with:

```python
        held_as_positions = base_currencies_of(self._instruments.values())
```

and import it: `from tradebot.core.instrument import Instrument, base_currencies_of` (merge with the existing `Instrument` import).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_instrument.py tests/unit/test_reconciler.py -v`
Expected: PASS — the new tests, and every existing reconciler test unchanged

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/core/instrument.py tradebot/ledger/reconciler.py tests/unit/test_instrument.py
git commit -m "refactor(core): extract base_currencies_of so the reconciler and valuation share one set"
```

---

### Task 4: `value_cash` — the four-rung ladder

**Files:**
- Modify: `tradebot/risk/aggregate.py` (add `value_cash`; leave `aggregate` alone for now)
- Test: `tests/unit/test_aggregate.py`

**Interfaces:**
- Consumes: `Marks` (Task 1), `base_currencies_of` (Task 3), `USD_STABLECOINS`
- Produces: `value_cash(currency, amount, marks, *, notional_currency, position_currencies, now, tolerance) -> Decimal | None`

**Rung order is load-bearing.** Rung 3 (base asset → already a position → zero) *must* precede rung 4 (mark it against its own market), or every spot holding is counted twice: `BTC` is both a configured instrument's base asset and a currency with a `BTC/USDT` market.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_aggregate.py`:

```python
class TestValueCash:
    """The four rungs, in order. Rung 3 before rung 4 is what stops a double count."""

    def _value(
        self,
        currency: str,
        amount: Decimal,
        *,
        clock: ManualClock,
        marks: Marks | None = None,
        position_currencies: frozenset[str] = frozenset(),
    ) -> Decimal | None:
        return value_cash(
            currency,
            amount,
            marks or Marks(),
            notional_currency="USDT",
            position_currencies=position_currencies,
            now=clock.now(),
            tolerance=timedelta(minutes=5),
        )

    def test_rung_1_the_notional_currency_is_face_value(self, clock: ManualClock) -> None:
        assert self._value("USDT", Decimal(1000), clock=clock) == Decimal(1000)

    def test_rung_2_a_usd_stablecoin_is_par(self, clock: ManualClock) -> None:
        """Finding 3: 9,000 USDC used to contribute nothing at all."""
        assert self._value("USDC", Decimal(9000), clock=clock) == Decimal(9000)

    def test_rung_3_a_configured_base_asset_is_already_a_position(
        self, clock: ManualClock
    ) -> None:
        assert (
            self._value("BTC", Decimal("0.1"), clock=clock, position_currencies=frozenset({"BTC"}))
            == Decimal(0)
        )

    def test_rung_3_precedes_rung_4_so_a_holding_is_never_counted_twice(
        self, clock: ManualClock
    ) -> None:
        marks = Marks()
        marks.observe("BTC", Decimal("50000"), clock.now())

        assert (
            self._value(
                "BTC",
                Decimal("0.1"),
                clock=clock,
                marks=marks,
                position_currencies=frozenset({"BTC"}),
            )
            == Decimal(0)
        )

    def test_rung_4_an_unconfigured_currency_is_valued_at_its_mark(
        self, clock: ManualClock
    ) -> None:
        marks = Marks()
        marks.observe("DOGE", Decimal("0.5"), clock.now())

        assert self._value("DOGE", Decimal(100), clock=clock, marks=marks) == Decimal(50)

    def test_rung_5_an_unmarked_currency_has_no_admissible_valuation(
        self, clock: ManualClock
    ) -> None:
        assert self._value("DOGE", Decimal(100), clock=clock) is None

    def test_a_stale_currency_mark_is_no_valuation_rather_than_an_old_one(
        self, clock: ManualClock
    ) -> None:
        marks = Marks()
        marks.observe("DOGE", Decimal("0.5"), clock.now())
        clock.set(clock.now() + timedelta(minutes=6))

        assert self._value("DOGE", Decimal(100), clock=clock, marks=marks) is None
```

Extend that file's imports:

```python
from datetime import timedelta

from tradebot.ledger.marks import Marks
from tradebot.risk.aggregate import USD_STABLECOINS, aggregate, peg_deviation_pct, value_cash
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_aggregate.py -k ValueCash -v`
Expected: FAIL — `ImportError: cannot import name 'value_cash'`

- [ ] **Step 3: Write the implementation**

Add to `tradebot/risk/aggregate.py`, after `USD_STABLECOINS`:

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
) -> Decimal | None:
    """What a balance is worth in the notional currency, or `None` if nothing can say.

    Four rungs, first match wins, and **their order is load-bearing**. Rung 3 precedes rung 4
    because a spot venue's base asset is both a balance and a position: `BTC` is a configured
    instrument's base asset *and* a currency with a `BTC/USDT` market, so reaching rung 4 first
    would value every holding twice (PHASE_12 §3.3).

    `None` is not zero. A balance with no admissible valuation means "we do not know what this
    portfolio is worth", and the caller's answer to that is a frozen aggregate — never a
    silently-zero balance, which is Finding 3.
    """
    if currency == notional_currency:
        return amount
    if currency in USD_STABLECOINS:
        # Par is the *assumption*; `_peg_check` is what falsifies it, and it freezes the whole
        # aggregate rather than adjusting a number here — a depeg is not a valuation nuance.
        return amount
    if currency in position_currencies:
        return ZERO
    mark = marks.price_of(currency, now=now, tolerance=tolerance)
    return None if mark is None else multiply(amount, mark)
```

Extend that module's imports:

```python
from datetime import timedelta

from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.ledger.marks import Marks
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_aggregate.py -v`
Expected: PASS — 7 new tests, and every existing aggregate test still green (`aggregate` is untouched)

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/risk/aggregate.py tests/unit/test_aggregate.py
git commit -m "feat(risk): add value_cash, the four-rung ladder that values every balance or refuses"
```

---

### Task 5: `aggregate` reads `Marks` and the configured universe

**Files:**
- Modify: `tradebot/risk/aggregate.py` (`aggregate`, `PortfolioAggregate`, `_peg_check`)
- Modify: `tradebot/control/basket_runner.py:310-348` (`_build_proposal`, its only production caller)
- Modify: `tradebot/control/reference.py` (add `configured_instruments`)
- Test: `tests/unit/test_aggregate.py`, `tests/unit/test_basket_runner.py`

**Interfaces:**
- Consumes: `value_cash` (Task 4), `Marks` (Task 1), `GlobalRiskPolicy.mark_tolerance` (Task 2), `base_currencies_of` (Task 3)
- Produces:
  - `aggregate(ledgers, universe, marks, policy, *, as_of, notional_currency) -> PortfolioAggregate`
  - `PortfolioAggregate.cash: Money` (new field)
  - `configured_instruments(configs: ConfigStore) -> tuple[Instrument, ...]`

This task changes `aggregate`'s signature. It has exactly **one** production caller, so it stays green in one commit. `Ledger.equity` is *not* touched here — Task 6 does that.

**Finding 6 lives here:** `gross_exposure`, `per_instrument` and the caller's `cluster_members` all move from the cycling basket's instruments to the configured universe. `basket_exposure` stays basket-scoped, because that one is genuinely a question about the basket.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_aggregate.py`:

```python
def marked(clock: ManualClock, **prices: str) -> Marks:
    marks = Marks()
    for key, price in prices.items():
        marks.observe(key.replace("__", ":").replace("_", "/"), Decimal(price), clock.now())
    return marks


class TestMarkToMarket:
    def test_equity_is_cash_plus_marks_not_cost(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """Finding 1: 10,000 USDT, 0.1 BTC bought at 50,000, BTC halves to 25,000."""
        marks = Marks()
        marks.observe(instrument.key, Decimal("25000"), clock.now())

        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marks,
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.equity == Decimal("7500")  # 5000 cash + 0.1 × 25000
        assert not summary.frozen

    def test_a_stale_mark_freezes_rather_than_falling_back_to_cost(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        marks = Marks()
        marks.observe(instrument.key, Decimal("25000"), clock.now())
        clock.set(clock.now() + timedelta(minutes=6))

        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marks,
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.frozen
        assert instrument.key in summary.frozen_reason

    def test_an_unmarked_position_freezes(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            Marks(),
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.frozen

    def test_a_flat_portfolio_never_freezes_and_needs_no_marks(self, clock: ManualClock) -> None:
        """A fresh database and the seeded demo must run with no venue call at all."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})

        summary = aggregate(
            {"sim": ledger},
            (),
            Marks(),
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert not summary.frozen
        assert summary.equity == Decimal(10_000)

    def test_non_quote_cash_is_valued_rather_than_worth_nothing(
        self, clock: ManualClock
    ) -> None:
        """Finding 3: 1,000 USDT + 9,000 USDC used to value at 1,000."""
        ledger = Ledger(
            clock, venue="sim", balances={"USDT": Decimal(1000), "USDC": Decimal(9000)}
        )

        summary = aggregate(
            {"sim": ledger},
            (),
            Marks(),
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.equity == Decimal(10_000)
        assert summary.cash == Decimal(10_000)

    def test_an_unvaluable_balance_freezes_and_names_the_currency(
        self, clock: ManualClock
    ) -> None:
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(1000), "DOGE": Decimal(50)})

        summary = aggregate(
            {"sim": ledger},
            (),
            Marks(),
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.frozen
        assert "DOGE" in summary.frozen_reason

    def test_a_zero_balance_in_an_unvaluable_currency_does_not_freeze(
        self, clock: ManualClock
    ) -> None:
        """Dust that has been fully converted is not a reason to stop trading."""
        ledger = Ledger(clock, venue="sim", balances={"USDT": Decimal(1000), "DOGE": ZERO})

        summary = aggregate(
            {"sim": ledger},
            (),
            Marks(),
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert not summary.frozen

    def test_a_base_asset_balance_is_not_counted_twice(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """`funded` leaves 0.1 BTC as both a position and a balance."""
        marks = Marks()
        marks.observe(instrument.key, Decimal("50000"), clock.now())

        summary = aggregate(
            {"sim": funded(clock, instrument)},
            (instrument,),
            marks,
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.equity == Decimal(10_000)  # 5000 cash + 0.1 × 50000, counted once


class TestGrossExposureSpansTheUniverse:
    def test_exposure_covers_every_configured_instrument_not_one_baskets(
        self, clock: ManualClock, instrument: Instrument, second_instrument: Instrument
    ) -> None:
        """Finding 6: `gross_exposure` used to omit every sibling basket's positions."""
        ledger = funded(clock, instrument)
        ledger.apply_fill(
            Fill(
                fill_id="f2",
                client_order_id="sim-ETH",
                instrument_key=second_instrument.key,
                side=Side.BUY,
                qty=Decimal("1"),
                price=Decimal("3000"),
                filled_at=clock.now(),
            ),
            base_currency="ETH",
            quote_currency="USDT",
        )
        marks = Marks()
        marks.observe(instrument.key, Decimal("50000"), clock.now())
        marks.observe(second_instrument.key, Decimal("3000"), clock.now())

        summary = aggregate(
            {"sim": ledger},
            (instrument, second_instrument),
            marks,
            GlobalRiskPolicy(),
            as_of=clock.now(),
            notional_currency="USDT",
        )

        assert summary.gross_exposure == Decimal("8000")  # 5000 + 3000
```

Add `from tradebot.core.money import ZERO` and `from tradebot.core.enums import Side` / `from tradebot.core.orders import Fill` to that file if not already imported. Delete the now-obsolete `prices`-based `TestAggregation` and `TestPegCheck` bodies and rewrite them to pass a `Marks` and `notional_currency`, keeping every assertion — the peg tests now supply `stablecoin_prices` through `Marks` (`marks.observe("USDT", Decimal("0.90"), clock.now())`).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_aggregate.py -v`
Expected: FAIL — `TypeError: aggregate() got an unexpected keyword argument 'notional_currency'`

- [ ] **Step 3: Rewrite `aggregate`**

Replace `PortfolioAggregate`'s field block, `aggregate` and `_peg_check` in `tradebot/risk/aggregate.py`:

```python
class PortfolioAggregate(DomainModel):
    """Read-only summary across every venue portfolio, valued in the notional currency."""

    equity: Money
    #: Cash alone, valued in the notional currency. Held separately so the dashboard can show the
    #: split without a second summation — and so a reader can tell 10,000 of cash from 10,000 of
    #: marked holdings, which drawdown behaves very differently about.
    cash: Money = ZERO
    gross_exposure: Money
    per_instrument: tuple[tuple[str, Money], ...] = ()
    venues: tuple[VenueSlice, ...] = ()
    frozen_reason: str = ""
    as_of: UtcDatetime

    @property
    def frozen(self) -> bool:
        """A frozen aggregate cannot back a risk decision, so nothing new may be sent."""
        return bool(self.frozen_reason)

    def exposure_of(self, *instrument_keys: str) -> Money:
        wanted = frozenset(instrument_keys)
        return sum((value for key, value in self.per_instrument if key in wanted), start=ZERO)


def aggregate(
    ledgers: Mapping[str, Ledger],
    universe: tuple[Instrument, ...],
    marks: Marks,
    policy: GlobalRiskPolicy,
    *,
    as_of: UtcDatetime,
    notional_currency: str,
) -> PortfolioAggregate:
    """The one answer to "what is this portfolio worth", in the notional currency.

    `universe` is **every configured instrument**, never one basket's. Every number here except a
    basket's own exposure is a portfolio-wide question, and answering it from one basket's slice
    is what let `max_gross_exposure` be enforced against a single basket while claiming to cover
    all of them (PHASE_12 Finding 6).

    A position or a balance that cannot be valued **freezes** the aggregate rather than falling
    back to cost. Freezing blocks new orders; it does not trip the kill switch, because the
    switch is for breaches and this is ignorance (PHASE_12 §1.4).
    """
    tolerance = policy.mark_tolerance
    positions = frozenset(
        position.instrument_key
        for ledger in ledgers.values()
        for position in ledger.positions()
        if not position.is_flat
    )
    prices = {
        key: price
        for key in positions
        if (price := marks.price_of(key, now=as_of, tolerance=tolerance)) is not None
    }
    cash, unvaluable = _value_all_cash(
        ledgers, marks, notional_currency=notional_currency, universe=universe,
        now=as_of, tolerance=tolerance,
    )
    frozen = _frozen_reason(
        unmarked=tuple(sorted(positions - prices.keys())),
        unvaluable=unvaluable,
        peg=_peg_check(ledgers, policy, marks, now=as_of, tolerance=tolerance),
    )
    if frozen:
        # Short-circuit, and it is not an optimisation. `Ledger.exposure` raises on a held key it
        # was given no price for — deliberately, so no caller can reintroduce the cost fallback —
        # so computing exposures here would turn a freeze into a crash. A frozen aggregate
        # reports no numbers at all: that is what "we do not know what this is worth" means.
        return PortfolioAggregate(
            equity=ZERO, cash=cash, gross_exposure=ZERO, frozen_reason=frozen, as_of=as_of
        )
    by_instrument = tuple(
        (
            instrument.key,
            sum(
                (ledger.exposure((instrument.key,), prices) for ledger in ledgers.values()),
                start=ZERO,
            ),
        )
        for instrument in universe
    )
    keys = tuple(instrument.key for instrument in universe)
    slices = tuple(
        VenueSlice(
            venue=venue,
            equity=_equity_of(
                ledger, prices, marks, notional_currency=notional_currency, universe=universe,
                now=as_of, tolerance=tolerance,
            ),
            exposure=ledger.exposure(keys, prices),
        )
        for venue, ledger in sorted(ledgers.items())
    )
    return PortfolioAggregate(
        equity=sum((s.equity for s in slices), start=ZERO),
        cash=cash,
        gross_exposure=sum((s.exposure for s in slices), start=ZERO),
        per_instrument=by_instrument,
        venues=slices,
        frozen_reason=frozen,
        as_of=as_of,
    )
```

Add the three helpers below it:

```python
def _cash_of(
    ledger: Ledger,
    marks: Marks,
    *,
    notional_currency: str,
    universe: tuple[Instrument, ...],
    now: UtcDatetime,
    tolerance: timedelta,
) -> tuple[Decimal, tuple[str, ...]]:
    """One ledger's cash in the notional currency, and the currencies nothing could value."""
    position_currencies = base_currencies_of(universe)
    total, refused = ZERO, []
    for balance in ledger.snapshot().balances:
        valued = value_cash(
            balance.currency,
            balance.total,
            marks,
            notional_currency=notional_currency,
            position_currencies=position_currencies,
            now=now,
            tolerance=tolerance,
        )
        if valued is None:
            # Only a *non-zero* balance freezes: dust already converted away is not a reason to
            # stop trading, and a zero balance has the same value in every currency (D2).
            if balance.total != ZERO:
                refused.append(balance.currency)
            continue
        total += valued
    return total, tuple(sorted(refused))


def _value_all_cash(
    ledgers: Mapping[str, Ledger],
    marks: Marks,
    *,
    notional_currency: str,
    universe: tuple[Instrument, ...],
    now: UtcDatetime,
    tolerance: timedelta,
) -> tuple[Decimal, tuple[str, ...]]:
    total, refused = ZERO, set()
    for ledger in ledgers.values():
        cash, unvaluable = _cash_of(
            ledger, marks, notional_currency=notional_currency, universe=universe,
            now=now, tolerance=tolerance,
        )
        total += cash
        refused.update(unvaluable)
    return total, tuple(sorted(refused))


def _equity_of(
    ledger: Ledger,
    prices: Mapping[str, Decimal],
    marks: Marks,
    *,
    notional_currency: str,
    universe: tuple[Instrument, ...],
    now: UtcDatetime,
    tolerance: timedelta,
) -> Decimal:
    """One venue portfolio: its cash in the notional currency, plus its marked holdings."""
    cash, _ = _cash_of(
        ledger, marks, notional_currency=notional_currency, universe=universe,
        now=now, tolerance=tolerance,
    )
    holdings = sum(
        (position.market_value(prices[key]) for position in ledger.positions()
         if not position.is_flat and (key := position.instrument_key) in prices),
        start=ZERO,
    )
    return cash + holdings


def _frozen_reason(
    *, unmarked: tuple[str, ...], unvaluable: tuple[str, ...], peg: str
) -> str:
    """Why this portfolio cannot be valued, in the order an operator can act on.

    The peg comes first: a depeg is a market event with an immediate response, while an unmarked
    position is usually a feed that will recover on its own.
    """
    if peg:
        return peg
    if unmarked:
        return (
            f"no fresh mark for {', '.join(unmarked)}; a stale mark is not a mark, so the "
            "portfolio cannot be valued and no new order may be sent"
        )
    if unvaluable:
        return (
            f"balances in {', '.join(unvaluable)} have no admissible valuation in the notional "
            "currency; equity is unknown"
        )
    return ""


def _peg_check(
    ledgers: Mapping[str, Ledger],
    policy: GlobalRiskPolicy,
    marks: Marks,
    *,
    now: UtcDatetime,
    tolerance: timedelta,
) -> str:
    """Freeze if a held USD stablecoin has drifted beyond tolerance.

    Now actually fed: it used to receive an empty `stablecoin_prices` at every call site, so it
    could not fire (PHASE_12 Finding 5). A stablecoin with no mark still reads as par — the
    assumption stands until a quote falsifies it, which is what `USD_STABLECOINS` documents.
    """
    held = {
        currency
        for ledger in ledgers.values()
        for balance in ledger.snapshot().balances
        if (currency := balance.currency) in USD_STABLECOINS and balance.total > ZERO
    }
    for currency in sorted(held):
        observed = marks.price_of(currency, now=now, tolerance=tolerance)
        deviation = peg_deviation_pct({currency: observed} if observed else {}, currency)
        if deviation > policy.stablecoin_peg_tolerance_pct:
            return (
                f"{currency} is {deviation}% off par, beyond the "
                f"{policy.stablecoin_peg_tolerance_pct}% tolerance; equity cannot be valued"
            )
    return ""
```

Import `base_currencies_of` from `tradebot.core.instrument`.

- [ ] **Step 4: Add `configured_instruments`**

Append to `tradebot/control/reference.py`:

```python
def configured_instruments(configs: ConfigStore) -> tuple[Instrument, ...]:
    """Every instrument any basket in service may trade, deduplicated.

    The universe every portfolio-wide question is answered over. Read fresh rather than held: a
    basket published while the process runs changes it, and a set captured at boot is the same
    defect ADR 0021 fixed for the Tier-2 cap (PHASE_12 §3.5).
    """
    seen: dict[str, Instrument] = {}
    for record in configs.baskets():
        seen.update({i.key: i for i in record.document.instruments})
    return tuple(seen.values())
```

Then in `tradebot/app.py`, replace `_instruments_of`'s body with a call to it, or delete `_instruments_of` and use `configured_instruments` — `_assemble` has `records` in hand, so pass `configs`.

- [ ] **Step 5: Migrate the one caller**

In `tradebot/control/basket_runner.py`, `_build_proposal`: replace the `prices`/`equity`/`summary` block (lines 315-324) with

```python
        summary = self._valuation(snapshot.as_of)
        equity = summary.equity
```

and add `_valuation`, plus a `_universe` callable and a `marks` handle to `__init__` (see Task 6 for the gate). Replace `cluster_members(instrument, self._basket.instruments)` with `cluster_members(instrument, self._universe())` — Finding 6.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_aggregate.py tests/unit/test_basket_runner.py -v`
Expected: PASS

- [ ] **Step 7: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/risk/aggregate.py tradebot/control/reference.py tradebot/control/basket_runner.py tradebot/app.py tests/unit/test_aggregate.py tests/unit/test_basket_runner.py
git commit -m "feat(risk): aggregate values marks and all cash, freezing rather than falling back to cost"
```

---

### Task 6: `Ledger` stops pricing, and the six equity callers move

**Files:**
- Modify: `tradebot/ledger/portfolio.py` (delete `equity`, delete `unrealized_pnl`, make `exposure` strict)
- Modify: `tradebot/risk/watchdog.py` (`check` takes the aggregate)
- Modify: `tradebot/control/basket_runner.py` (`_gate`, `_equity`, `_prices` removed)
- Modify: `tradebot/app.py` (`Application.equity` → `Application.valuation`)
- Modify: `tradebot/control/startup.py:206`, `:321`
- Modify: `tradebot/control/manual_close.py:264`
- Modify: `tradebot/__main__.py:857`, `tradebot/dashboard/routes/control.py:293`, `monitor.py:83`, `workspace.py:234,310`
- Modify: `tradebot/dashboard/templates/workspace/_rc.html`, `_portfolio.html`
- Test: `tests/unit/test_portfolio.py`, `tests/unit/test_watchdog.py`, and every test naming `ledger.equity(`

**Interfaces:**
- Consumes: `aggregate` (Task 5)
- Produces:
  - `Watchdog.check(valuation: PortfolioAggregate) -> WatchdogVerdict`
  - `WatchdogVerdict.frozen: bool`
  - `Application.valuation() -> PortfolioAggregate`
  - `Ledger.exposure(keys, prices)` raises `KeyError` on an unpriced held key rather than using cost

**This is the atomic type change.** It cannot be split further without leaving the tree red, because deleting `Ledger.equity` is exactly what forces the six callers to move.

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_portfolio.py`:

```python
class TestTheLedgerHasNoOpinionAboutPrices:
    def test_equity_is_not_the_ledgers_to_answer(self, ledger: Ledger) -> None:
        """One function answers "what is the portfolio worth", and it is not this one."""
        assert not hasattr(ledger, "equity")

    def test_unrealized_pnl_is_gone(self, ledger: Ledger) -> None:
        assert not hasattr(ledger, "unrealized_pnl")

    def test_exposure_refuses_a_held_key_it_was_given_no_price_for(
        self, clock: ManualClock, instrument: Instrument
    ) -> None:
        """The old fallback to `avg_entry` here is Finding 1 wearing a different hat."""
        ledger = funded(clock, instrument)

        with pytest.raises(KeyError):
            ledger.exposure((instrument.key,), {})
```

Add to `tests/unit/test_watchdog.py`:

```python
def valuation(equity: Decimal, *, at: datetime, frozen: str = "") -> PortfolioAggregate:
    return PortfolioAggregate(
        equity=equity, cash=equity, gross_exposure=ZERO, frozen_reason=frozen, as_of=at
    )


class TestFreeze:
    async def test_a_frozen_valuation_stops_new_orders(
        self, watchdog: Watchdog, clock: ManualClock
    ) -> None:
        await armed(watchdog)

        verdict = await watchdog.check(
            valuation(ZERO, at=clock.now(), frozen="no fresh mark for sim:BTC/USDT")
        )

        assert not verdict.may_trade
        assert verdict.frozen
        assert "no fresh mark" in verdict.reason

    async def test_a_freeze_does_not_trip_the_kill_switch(
        self, watchdog: Watchdog, states: RiskStateStore, clock: ManualClock
    ) -> None:
        """The switch is for breaches, not for ignorance (PHASE_12 §1.4)."""
        await armed(watchdog)

        await watchdog.check(valuation(ZERO, at=clock.now(), frozen="cannot value"))

        assert states.load().kill_switch is KillSwitchState.ARMED

    async def test_a_freeze_does_not_move_the_high_water_mark(
        self, watchdog: Watchdog, states: RiskStateStore, clock: ManualClock
    ) -> None:
        await armed(watchdog)

        await watchdog.check(valuation(Decimal(99_999), at=clock.now(), frozen="cannot value"))

        assert states.load().high_water_mark == START_EQUITY

    async def test_a_freeze_does_not_roll_the_day(
        self, watchdog: Watchdog, states: RiskStateStore, clock: ManualClock
    ) -> None:
        """A freeze spanning midnight leaves yesterday's baseline — the conservative direction."""
        await armed(watchdog)
        before = states.load().day_started_on
        clock.set(clock.now() + timedelta(days=1))

        await watchdog.check(valuation(Decimal(1), at=clock.now(), frozen="cannot value"))

        assert states.load().day_started_on == before


class TestUnrealizedLossIsVisible:
    async def test_an_unrealized_drawdown_trips_the_switch(
        self, watchdog: Watchdog, states: RiskStateStore, clock: ManualClock
    ) -> None:
        """Finding 1, at the watchdog's own boundary."""
        await armed(watchdog)

        verdict = await watchdog.check(valuation(Decimal(8500), at=clock.now()))

        assert verdict.tripped
        assert states.load().kill_switch is KillSwitchState.TRIPPED
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_portfolio.py tests/unit/test_watchdog.py -v`
Expected: FAIL — `hasattr` assertions fail, and `check()` rejects a `PortfolioAggregate`

- [ ] **Step 3: Slim the ledger**

In `tradebot/ledger/portfolio.py`, delete `equity` and `unrealized_pnl` entirely, and replace `exposure`:

```python
    def exposure(self, instrument_keys: tuple[str, ...], prices: Mapping[str, Decimal]) -> Decimal:
        """Value deployed across a set of instruments — a basket's exposure.

        `prices` is strict: a held key it does not carry raises rather than falling back to
        `avg_entry`. The caller has already decided what an unmarked position means, and the
        answer is a frozen aggregate, never a position quietly valued at what it cost
        (PHASE_12 Finding 1).
        """
        return sum(
            (
                position.market_value(prices[key])
                for key in instrument_keys
                if not (position := self.position(key)).is_flat
            ),
            start=ZERO,
        )
```

Update the module docstring: the ledger no longer values anything.

- [ ] **Step 4: Move `Watchdog.check` onto the aggregate**

In `tradebot/risk/watchdog.py`, add `frozen: bool = False` to `WatchdogVerdict` and make `may_trade` account for it, then:

```python
    async def check(self, valuation: PortfolioAggregate) -> WatchdogVerdict:
        """Evaluate the baselines against current equity, or decline to for want of one.

        A frozen valuation is *ignorance*, not a breach: nothing is tripped, no baseline moves,
        and no state is written — but no new order may be sent either. Rolling the day or raising
        the mark on a number the system has just said it cannot compute would persist a fiction
        that outlives the outage (PHASE_12 §3.4).
        """
        state = self._states.load()
        if valuation.frozen:
            return WatchdogVerdict(state=state, frozen=True, reason=valuation.frozen_reason)
        equity = valuation.equity
        state = await self._roll_day(state, equity)
        ...  # unchanged from here
```

- [ ] **Step 5: Move the six callers**

Each becomes an `aggregate(...)` call. `Application.equity()` is renamed:

```python
    def valuation(self) -> PortfolioAggregate:
        """What the portfolio is worth right now, or why that cannot be said.

        Returns the aggregate rather than a bare `Decimal` deliberately: a method still called
        `equity` returning a number is what let six call sites each build their own price map
        (PHASE_12 §3.5).
        """
        return aggregate(
            {self.ledger_venue: self.ledger},
            configured_instruments(self.configs),
            self.marks,
            self.policy.policy,
            as_of=self.clock.now(),
            notional_currency=self.quote_currency,
        )
```

`Application` gains `marks: Marks` and `ledger_venue: str` fields, both set in `_assemble`.

`risk rearm` (`__main__.py:857`, `control.py:293`) must **refuse while frozen**:

```python
    current = application.valuation()
    if current.frozen:
        raise ConfigError(
            f"the portfolio cannot be valued, so there is no equity to re-arm against: "
            f"{current.frozen_reason}"
        )
    state = await application.watchdog.rearm(current.equity, actor=CLI_ACTOR)
```

`startup.py:206` becomes `self._valuation().equity`; `startup.py:321` the same. `manual_close.py:264` pushes its fresh quote into `Marks` first (`self._marks.observe_quote(quote)`), then reads the aggregate.

Dashboard: `monitor.py`, `workspace.py` pass the aggregate; `_rc.html` and `_portfolio.html` render `valuation.frozen_reason` when set, in place of the figure.

- [ ] **Step 6: Run the whole suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: PASS. Expect a large diff across tests that used `ledger.equity(` — that breadth *is* the fix reaching the seam that had no test.

- [ ] **Step 7: Run the gate and commit**

```bash
.\check.ps1
git add -A
git commit -m "feat: one valuation function; Ledger stops pricing and the watchdog reads the aggregate"
```

---

### Task 7: The freeze reaches the cycle gate

**Files:**
- Modify: `tradebot/control/basket_runner.py` (`_gate`)
- Test: `tests/unit/test_basket_runner.py`, `tests/scenario/test_valuation_freeze.py` (create)

**Interfaces:**
- Consumes: `WatchdogVerdict.frozen` (Task 6), `BasketRunner._valuation` (Task 5)
- Produces: a `BLOCKED` cycle whose detail names the freeze

- [ ] **Step 1: Write the failing test**

Create `tests/scenario/test_valuation_freeze.py` asserting, through a real `BasketRunner`:

```python
async def test_an_unmarked_position_blocks_the_cycle_before_the_panel(...) -> None:
    """A blocked cycle must still cost nothing — no market data, no panel call."""
    result = await runner.run_once()

    assert result.outcome is CycleOutcome.BLOCKED
    assert "no fresh mark" in result.detail
    assert panel.calls == 0


async def test_a_manual_close_still_works_while_frozen(...) -> None:
    """ADR 0015: the freeze stops the bot trading, not a human getting out."""
    outcome = await closer.close("b1", instrument.key, actor="operator")

    assert outcome.order is not None
```

- [ ] **Step 2: Run to verify it fails** — `.venv\Scripts\python.exe -m pytest tests/scenario/test_valuation_freeze.py -v`

- [ ] **Step 3: Implement** — `_gate` already returns `verdict.reason` when `not verdict.may_trade`, so this should pass once Task 6 lands. If it does not, the defect is in `_gate`'s ordering; fix it there, not by special-casing the freeze.

- [ ] **Step 4: Verify the manual-close half needs no new exemption.** Every rule reading `equity`/`basket_budget` already stands aside on `Side.SELL`. If the test fails, do **not** add an exemption — find which rule reads equity on a SELL and report it, because that is a separate defect.

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/control/basket_runner.py tests/scenario/test_valuation_freeze.py tests/unit/test_basket_runner.py
git commit -m "feat(control): a frozen valuation blocks the cycle and never blocks an operator exit"
```

---

### Task 8: The boundary test — no cost-basis fallback survives

**Files:**
- Test: `tests/unit/test_valuation_boundary.py` (create)

**Interfaces:**
- Consumes: nothing; it reads source text, in the manner of `tests/unit/test_dashboard_chart.py`

- [ ] **Step 1: Write the test**

```python
"""No module may value a position at what it cost.

Asserted structurally rather than by review, in the manner `test_dashboard_chart.py` asserts the
float boundary. The fallback-to-cost in `Ledger.equity` was the entire mechanism of the drawdown
defect (PHASE_12 Finding 1), and a "helpful" fallback re-added later is how it comes back.
"""

from __future__ import annotations

import re
from pathlib import Path

import tradebot

#: `prices.get(key, position.avg_entry)` and every spelling of it.
FALLBACK = re.compile(r"\.get\(\s*[^)]*?,\s*[^)]*avg_entry", re.DOTALL)

WATCHED = ("ledger", "risk", "control")


def test_no_module_falls_back_to_cost_basis() -> None:
    root = Path(tradebot.__file__).parent
    offenders = [
        path.relative_to(root).as_posix()
        for package in WATCHED
        for path in (root / package).rglob("*.py")
        if FALLBACK.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"these modules value a position at its cost when a price is missing: {offenders}. "
        "The fallback is a freeze, never cost — see PHASE_12 §1.4."
    )
```

- [ ] **Step 2: Run it** — `.venv\Scripts\python.exe -m pytest tests/unit/test_valuation_boundary.py -v`
Expected: PASS (Tasks 5 and 6 removed every occurrence). If it fails, it has found a real one — remove it rather than loosening the regex.

- [ ] **Step 3: Prove the test can fail.** Temporarily reintroduce `prices.get(key, position.avg_entry)` in `ledger/portfolio.py`, run the test, confirm FAIL, then revert. A guard that cannot fail is not a guard.

- [ ] **Step 4: Run the gate and commit**

```bash
.\check.ps1
git add tests/unit/test_valuation_boundary.py
git commit -m "test: assert structurally that no module values a position at cost"
```

---

### Task 9: Serialize the watchdog's read-modify-write

**Files:**
- Modify: `tradebot/risk/watchdog.py`
- Test: `tests/unit/test_watchdog.py`

**Interfaces:**
- Produces: `check`, `trip`, `rearm` and `record_flow` are mutually exclusive

- [ ] **Step 1: Write the failing test**

```python
async def test_concurrent_checks_do_not_lose_a_high_water_raise(
    watchdog: Watchdog, states: RiskStateStore, clock: ManualClock
) -> None:
    """Load-compare-save from N basket tasks plus the sweep; SingleWriter does not make it atomic."""
    await armed(watchdog)

    await asyncio.gather(
        *(watchdog.check(valuation(Decimal(10_000 + n), at=clock.now())) for n in range(1, 21))
    )

    assert states.load().high_water_mark == Decimal(10_020)
```

- [ ] **Step 2: Run to verify it fails** (it may pass intermittently; run with `-p no:randomly --count 20` if `pytest-repeat` is available, otherwise reason from the code)

- [ ] **Step 3: Implement** — add `self._lock = asyncio.Lock()` in `__init__` and wrap the bodies of `check`, `trip`, `rearm` and `record_flow`. Extract the current bodies to `_check`, `_trip`, … so the lock is acquired exactly once and `trip` called from inside `check` does not deadlock.

- [ ] **Step 4: Run to verify it passes**

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/risk/watchdog.py tests/unit/test_watchdog.py
git commit -m "fix(risk): serialize the watchdog's read-modify-write on the kill-switch row"
```

---

### Task 10: `record_flow` carries its currency

**Files:**
- Modify: `tradebot/risk/watchdog.py` (`record_flow`, `use_universe`)
- Modify: `tradebot/control/startup.py:202`
- Test: `tests/unit/test_watchdog.py`, `tests/unit/test_startup.py`

**Interfaces:**
- Consumes: `ExternalFlow` (already carries `currency`), `value_cash` (Task 4)
- Produces: `Watchdog.record_flow(flow: ExternalFlow) -> RiskState`; `Watchdog.use_universe(instruments) -> None`

- [ ] **Step 1: Write the failing tests**

```python
class TestFlowsCarryTheirCurrency:
    async def test_a_stablecoin_deposit_moves_the_baselines_by_its_notional_value(
        self, watchdog: Watchdog, states: RiskStateStore
    ) -> None:
        """Findings 3+4 together: the compound case that guaranteed a spurious trip."""
        await armed(watchdog)

        await watchdog.record_flow(ExternalFlow(currency="USDC", amount=Decimal(9000)))

        assert states.load().high_water_mark == Decimal(19_000)

    async def test_a_flow_in_a_currency_nothing_can_value_is_refused(
        self, watchdog: Watchdog, states: RiskStateStore
    ) -> None:
        """A baseline adjusted by a number in the wrong unit is worse than no adjustment."""
        await armed(watchdog)

        with pytest.raises(FailClosedError):
            await watchdog.record_flow(ExternalFlow(currency="DOGE", amount=Decimal(9000)))

        assert states.load().high_water_mark == START_EQUITY
```

Plus a startup test asserting the refusal leaves the process **up and halted** — `recovery.halted is True` and `recovery.failures` names the currency.

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Implement**

```python
    def use_universe(self, instruments: Iterable[Instrument]) -> None:
        """Adopt the configured instrument set, so a flow is converted against current truth.

        Not captured at construction: it moves whenever a basket adds an instrument, and a set
        fixed at boot is the same defect ADR 0021 fixed for the Tier-2 cap (PHASE_12 §3.7).
        """
        self._position_currencies = base_currencies_of(instruments)

    async def record_flow(self, flow: ExternalFlow) -> RiskState:
        """Move both baselines by an external deposit or withdrawal, in the notional currency.

        The currency is not decoration. `startup.py` used to drop it and add the bare amount to
        baselines denominated in the notional currency, so a 9,000 USDC deposit raised the
        high-water mark by 9,000 while contributing nothing to equity — a guaranteed spurious
        kill-switch trip (PHASE_12 Finding 4, R16).
        """
        amount = value_cash(
            flow.currency, flow.amount, self._marks,
            notional_currency=self._notional_currency,
            position_currencies=self._position_currencies,
            now=self._clock.now(), tolerance=self._policy.mark_tolerance,
        )
        if amount is None:
            raise FailClosedError(
                f"an external flow of {flow.amount} {flow.currency} cannot be valued in "
                f"{self._notional_currency}, so the drawdown baselines cannot be adjusted for it"
            )
        ...  # the existing max(...) update, using `amount`
```

`startup.py:202` becomes `await self._watchdog.record_flow(flow)`.

- [ ] **Step 4: Run to verify they pass**

- [ ] **Step 5: Run the gate and commit**

```bash
.\check.ps1
git add tradebot/risk/watchdog.py tradebot/control/startup.py tests/unit/test_watchdog.py tests/unit/test_startup.py
git commit -m "fix(risk): record_flow converts through the valuation and refuses what it cannot value"
```

---

### Task 11: `PortfolioWatch` — the continuous sweep

**Files:**
- Create: `tradebot/control/valuation.py`
- Modify: `tradebot/control/supervisor.py` (`serve`), `tradebot/control/startup.py` (seed), `tradebot/app.py` (wire + expose), `tradebot/validation/backtest.py:220`
- Test: `tests/unit/test_portfolio_watch.py`, `tests/scenario/test_valuation_freeze.py`

**Interfaces:**
- Consumes: `Marks`, `aggregate`, `Watchdog.check`, `MarketDataProvider`, `InstrumentCatalogue`, `configured_instruments`
- Produces: `PortfolioWatch.sweep() -> PortfolioAggregate`; `Application.portfolio_watch`

- [ ] **Step 1: Write the failing tests** — the four that made the sweep mandatory:

```python
async def test_a_paused_basket_holding_a_position_does_not_freeze_the_portfolio(...)
async def test_a_halted_basket_holding_a_position_does_not_freeze_the_portfolio(...)
async def test_a_quarantined_basket_holding_a_position_does_not_freeze_the_portfolio(...)
async def test_the_sweep_measures_drawdown_with_every_basket_stopped(...)
async def test_a_position_in_an_unconfigured_instrument_cannot_be_marked_and_freezes(...)
async def test_an_unreachable_venue_at_startup_freezes_rather_than_failing_startup(...)
async def test_a_tolerance_below_the_resync_cadence_refuses_to_wire(...)
```

- [ ] **Step 2: Run to verify they fail**

- [ ] **Step 3: Implement `PortfolioWatch`** — the four-step sweep from spec §3.6: resolve the held set, refresh through the shared cache, observe into `Marks`, then `Watchdog.check`. Constructor asserts `policy.mark_tolerance > resync_interval` and raises `ConfigError` naming both numbers.

- [ ] **Step 4: Hang it off `Supervisor.serve`** beside `_check_drift`, guarded identically (`except Exception: logger.exception(...)`; supervision continues).

- [ ] **Step 5: Seed at startup** — best-effort, over held instruments, inside `StartupSequence.recover`. A failure logs and leaves marks absent; it must **not** append to `failures`.

- [ ] **Step 6: Drive it from the backtest** — in `backtest._replay`, after `monitor.poll()`:

```python
            await self._application.portfolio_watch.sweep()
```

- [ ] **Step 7: Run the whole suite** — `.venv\Scripts\python.exe -m pytest -q`

- [ ] **Step 8: Run the gate and commit**

```bash
.\check.ps1
git add -A
git commit -m "feat(control): PortfolioWatch refreshes marks and measures drawdown between cycles"
```

---

### Task 12: Operator surfaces — the basis change, the alert, the incident

**Files:**
- Modify: `tradebot/control/startup.py` (basis-change `RISK_EVENT`)
- Modify: `tradebot/ops/rules.py` (`ALERT_TYPES`, `RULES`, new rule), `tradebot/interfaces/alerts.py` (`AlertKind.VALUATION_FROZEN`)
- Modify: `tradebot/validation/evidence.py` (matching `IncidentKind`), `tradebot/validation/promotion.py` (boundary line)
- Test: `tests/unit/test_ops_rules.py`, `tests/unit/test_startup.py`, `tests/unit/test_evidence.py`

- [ ] **Step 1: Write the failing tests** — the basis-change event is emitted once and changes no state; a freeze alerts once per transition, not once per sweep; the promotion report names the boundary.

- [ ] **Step 2–4: Implement, verify, and run the gate**

- [ ] **Step 5: Exercise it against a copy of the soak database**

```bash
copy data\paper.db data\paper-precheck.db
.venv\Scripts\python.exe -m tradebot risk status --mode paper
```

Record the old mark, the new equity and the implied drawdown in the commit message.

```bash
.\check.ps1
git add -A
git commit -m "feat(ops): announce the valuation basis change and alert on a frozen portfolio"
```

---

### Task 13: Records

**Files:**
- Create: `docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md`
- Modify: `DESIGN.md` (§6.6 mark-to-market claim; §8.1 gains "portfolio cannot be valued")
- Modify: `IMPLEMENTATION_PLAN.md` (R16/R17/R18 mitigations; add R19 Finding 5, R20 Finding 6)
- Modify: `CLAUDE.md` (Phase 12 Piece 1 section, from spec §4)
- Modify: `docs/OPERATIONS.md` (the basis-change migration note)
- Modify: `docs/PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md` (mark Piece 1 shipped; record Findings 5 and 6)

- [ ] **Step 1: Write ADR 0027**, recording the freeze-on-unvaluable rule and its deliberate divergence from fallback-to-cost.
- [ ] **Step 2: Update `DESIGN.md`, `IMPLEMENTATION_PLAN.md`, `CLAUDE.md`, `OPERATIONS.md`, and the phase document.**
- [ ] **Step 3: Final gate**

```bash
.\check.ps1
git add -A
git commit -m "docs: ADR 0027 and the Phase 12 Piece 1 records"
```

---

## Verification against the Definition of Done

Run before opening the PR:

- [ ] `grep -rn "def equity" tradebot/` returns nothing (DoD 1)
- [ ] `.venv\Scripts\python.exe -m pytest tests/unit/test_valuation_boundary.py -v` passes (DoD 2)
- [ ] Tests 4, 6, 11, 12 and 19 from spec §5 exist by name (DoD 5)
- [ ] `.venv\Scripts\python.exe -m pytest -q` — all green
- [ ] `.\check.ps1` — clean, coverage gates hold (DoD 6)
- [ ] `.venv\Scripts\python.exe -m tradebot run --mode sim --once` exits 0
- [ ] A run against a copy of the soak database is exercised and its basis-change event recorded (DoD 7)
