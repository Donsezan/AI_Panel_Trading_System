# Phase 11 Slices E, C and D Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce that an instrument belongs to exactly one basket, then rebuild the basket editor as a tabbed master–detail workspace whose instrument rules come from the venue, and remove quarantine from that form without ever silently releasing one.

**Architecture:** Slice E adds one constraint to `control/reference.py`, the single basket write path, reusing the `changed()` exemption already there so a pause of a broken basket is never blocked; `DriftWatch` re-checks it at runtime and halts in every mode. Slice C restructures `configure/basket.html` into CSS-only tabs (`radio, label, pane` triples, three generic rules) driven by a new pure-assembly module `dashboard/editor.py`, and folds a **Look up** button into the existing draft route. Slice D moves quarantine out of the form and re-attaches it at publish from the stored record.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, pydantic v2, SQLAlchemy Core, pytest (asyncio), vendored htmx 2.0.7, hand-written CSS.

## Global Constraints

- **Money is `Decimal`, always.** Use `tradebot.core.money`; never `float`, never `Decimal(some_float)`. Enforced by `tests/unit/test_money_discipline.py`. `dashboard/chart.py` is the only module permitted to call `float(`, asserted by `tests/unit/test_dashboard_chart.py` — **do not add a second**.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never call `datetime.now()` in library code.
- **Errors are classified:** `RetryableError` / `FailClosedError` / `FatalError`. A bare `except: pass` is a defect. `ConfigError` is what a publish refusal raises; the dashboard already renders it.
- **Every state change emits an event.** Halts go through `Watchdog.halt_basket`, which writes `BASKET_STATUS_CHANGED`; findings go through `EventFactory(...).risk_event(...)`.
- **Nothing outside `app.py` may import a concrete adapter.**
- **A tab may hide inputs; it may never omit them.** The form round-trips the whole document and `nest()` drops absent fields, so a conditionally-rendered tab deletes that part of the basket on save.
- **Templates may not contain `https://`** — `tests/unit/test_dashboard_static.py::test_no_page_reaches_a_cdn` walks every template.
- **Run `.\check.ps1` before every commit.** Format, lint, mypy, tests, coverage gates. Coverage: `core/`, `risk/`, `execution/`, `ledger/` ≥ 95%; everything else (including `control/` and `dashboard/`) ≥ 80%.
- Test commands in this plan use `.venv\Scripts\python.exe -m pytest`. The shell is PowerShell.
- **Never run `git commit`, and never run `git config`.** The operator has decided to commit under their own identity, and this repository has none set. Every task ends at `git add` of exactly the files it names. The commit messages in each task are for the operator to use — write them into the task's final report, do not attempt to apply them.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `tradebot/control/reference.py` | **Modify.** Adds `holders_of`, `exclusive_findings`, `overlaps`; `store_basket` refuses an overlap; `DriftWatch` reports it | 1, 2 |
| `tests/unit/test_reference_data.py` | **Modify.** Exclusivity at publish and at runtime | 1, 2 |
| `docs/adr/0026-an-instrument-belongs-to-exactly-one-basket.md` | **Create.** The rule, the four failures, the exemption | 3 |
| `DESIGN.md`, `CLAUDE.md` | **Modify.** §8.1 row, §4 clause, two conventions | 3, 12 |
| `tradebot/dashboard/editor.py` | **Create.** Pure assembly over a draft: `focus_for`, `seat_rows`, `provider_rows`, `instrument_rows` | 4 |
| `tests/unit/test_dashboard_editor.py` | **Create.** The above, with no HTTP | 4 |
| `tradebot/interfaces/exchange.py` | **Modify.** `InstrumentCatalogue` gains `source` / `as_of` | 5 |
| `tradebot/marketdata/catalogue.py` | **Modify.** `Catalogue` defaults; `VenueCatalogue` stamps `as_of` | 5 |
| `tradebot/dashboard/static/app.css` | **Modify.** The three tab rules, rail/strip layout, resolved-field and pill styles, sticky bar | 6, 7, 9 |
| `tradebot/dashboard/templates/configure/basket.html` | **Modify.** Six sections as tabs; instrument row rewritten; sticky bar | 6, 7, 9, 10, 11 |
| `tradebot/dashboard/templates/_fields.html` | **Modify.** `resolved()`, `checkboxes()`, htmx on `row_buttons`; `multi()` deleted | 7, 9, 11 |
| `tradebot/dashboard/templates/_panel.html` | **Modify.** Tab shell, seat master–detail, indicators | 8 |
| `tradebot/dashboard/routes/configure.py` | **Modify.** `ui.*` round-trip, `lookup`, `carry_quarantine`, `released` banner | 6, 7, 10 |
| `tradebot/dashboard/static/configure.js` | **Create.** `beforeunload` guard only | 9 |
| `tests/unit/test_dashboard_configure.py` | **Modify.** Never-omit, lookup, `ui.*`, quarantine carry-over, no `<select multiple>` | 6, 7, 10, 11 |
| `tests/scenario/test_dashboard_lifecycle.py` | **Modify.** `alpha` gets its own instruments | 1 |
| `tests/contract/test_catalogue_contract.py` | **Modify.** Every catalogue answers `source` / `as_of` | 5 |
| `docs/PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md` | **Modify.** Slice status | 12 |

---

# Slice E — an instrument belongs to exactly one basket

### Task 1: Refuse an overlap at publish

**Files:**
- Modify: `tradebot/control/reference.py` (add after `changed`, ~line 103; edit `verify_publish` ~144-163 and `store_basket` ~166-194)
- Test: `tests/unit/test_reference_data.py`
- Modify: `tests/unit/test_dashboard_configure.py` (`new_basket_form`, `test_creating_a_basket_from_the_new_form`)
- Modify: `tests/scenario/test_dashboard_lifecycle.py:48-55`

**Interfaces:**
- Consumes: `ConfigStore.baskets() -> tuple[ConfigRecord[Basket], ...]` (already filters retired), `ConfigRecord.ref.config_id`, `ConfigRecord.document`, `Instrument.key`, existing `changed(instruments, previous) -> tuple[Instrument, ...]`.
- Produces:
  - `holders_of(records: Sequence[ConfigRecord[Basket]]) -> dict[str, tuple[str, ...]]`
  - `exclusive_findings(records: Sequence[ConfigRecord[Basket]], basket_id: str, edited: Sequence[Instrument]) -> tuple[str, ...]`
  - `EXCLUSIVITY_RULE: str = "instrument_exclusivity"`
  - `verify_publish(catalogue, basket, previous, *, edited: Sequence[Instrument]) -> tuple[str, ...]` — signature changes: `edited` is now computed by the caller.

---

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_reference_data.py`:

```python
class TestExclusivity:
    """An instrument belongs to exactly one basket in service (ADR 0026).

    Positions are keyed by `instrument_key` alone and baskets cycle as concurrent tasks, so two
    baskets holding one instrument oversell a holding through reduce-only, leave a protective leg
    resting against a position that is gone, attribute a round trip to whichever closed it, and
    double every metering limit.
    """

    def test_holders_names_every_basket_holding_each_key(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        beta = stored_basket().model_copy(update={"basket_id": "beta"})

        held = holders_of((_record(alpha, 1), _record(beta, 1)))

        assert held == {"sim:BTC/USDT": ("alpha", "beta")}

    def test_an_untaken_instrument_produces_no_finding(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        free = pinned(symbol="SOL/USDT", base_currency="SOL")

        assert exclusive_findings((_record(alpha, 1),), "beta", (free,)) == ()

    def test_a_basket_does_not_conflict_with_its_own_previous_version(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})

        assert exclusive_findings((_record(alpha, 1),), "alpha", (pinned(),)) == ()

    def test_taking_another_baskets_instrument_is_refused_by_name(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})

        findings = exclusive_findings((_record(alpha, 1),), "beta", (pinned(),))

        assert len(findings) == 1
        assert "sim:BTC/USDT" in findings[0]
        assert "'alpha'" in findings[0]

    async def test_publishing_a_basket_that_takes_a_held_instrument_is_refused(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        await harness.publish(alpha)
        beta = stored_basket().model_copy(update={"basket_id": "beta"})

        with pytest.raises(ConfigError) as refusal:
            await store_basket(harness.configs, catalogue, beta, actor="test", note="")

        assert "already held by basket 'alpha'" in str(refusal.value)
        assert {r.ref.config_id for r in harness.configs.baskets()} == {"alpha"}

    async def test_pausing_a_basket_that_already_overlaps_is_allowed(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """The exemption that keeps fail-closed from meaning fail-useless.

        A database written before this rule existed can hold an overlap, and the operator's way
        out of it is to pause or edit a basket. A check that blocked the fix would be a safety
        hazard rather than a safety mechanism.
        """
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        beta = stored_basket().model_copy(update={"basket_id": "beta"})
        await harness.publish(alpha)
        await harness.publish(beta)

        paused = beta.model_copy(update={"status": BasketStatus.PAUSED})
        record = await store_basket(harness.configs, catalogue, paused, actor="test", note="")

        assert record.document.status is BasketStatus.PAUSED

    async def test_a_new_version_keeping_its_own_instruments_publishes(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        await harness.publish(alpha)

        edited = alpha.model_copy(update={"name": "renamed"})
        record = await store_basket(harness.configs, catalogue, edited, actor="test", note="")

        assert record.ref.version == 2
```

Add this helper beside `stored_basket` near the top of the file:

```python
def _record(basket: Basket, version: int) -> ConfigRecord[Basket]:
    """A stored basket as `ConfigStore.baskets()` hands it back."""
    return ConfigRecord(
        ref=ConfigRef(kind=ConfigKind.BASKET, config_id=basket.basket_id, version=version),
        document=basket,
    )
```

Extend the existing imports at the top of the file:

```python
from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.reference import (
    DriftWatch,
    changed,
    exclusive_findings,
    findings_for,
    holders_of,
    store_basket,
    verify_publish,
)
from tradebot.core.config import Basket, ConfigRef, GlobalRiskPolicy, PanelConfig, SeatConfig
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_reference_data.py::TestExclusivity -v`
Expected: FAIL — `ImportError: cannot import name 'exclusive_findings' from 'tradebot.control.reference'`

- [ ] **Step 3: Add the two functions**

In `tradebot/control/reference.py`, add after the `DRIFT_RULE` constant:

```python
#: The rule name on the `RISK_EVENT` an overlap writes. Separate from `DRIFT_RULE` because they are
#: different faults with different severities: drift is the venue changing something under us, this
#: is a configuration that is internally inconsistent.
EXCLUSIVITY_RULE = "instrument_exclusivity"
```

Add after `changed`:

```python
def holders_of(records: Sequence[ConfigRecord[Basket]]) -> dict[str, tuple[str, ...]]:
    """Every basket in service holding each instrument key.

    A tuple rather than one id, because the whole point is to notice when there is more than one.
    `ConfigStore.baskets()` already excludes retired documents: a retired basket cycles nothing, so
    it cannot be a second writer, and counting it would make an id unusable forever.
    """
    held: dict[str, tuple[str, ...]] = {}
    for record in records:
        for instrument in record.document.instruments:
            held[instrument.key] = (*held.get(instrument.key, ()), record.ref.config_id)
    return held


def exclusive_findings(
    records: Sequence[ConfigRecord[Basket]], basket_id: str, edited: Sequence[Instrument]
) -> tuple[str, ...]:
    """Refusals for instruments this edit takes from another basket (ADR 0026).

    Over `edited` only, exactly as the venue verification is, and for the same reason: a database
    written before this rule can already hold an overlap, and the operator's way out of it is to
    pause or edit a basket. A check that blocked the fix would be a safety hazard.
    """
    held = holders_of(records)
    return tuple(
        f"{instrument.key} is already held by basket {other!r}. An instrument belongs to exactly "
        "one basket: positions are the portfolio's and are keyed by instrument alone, so a second "
        "basket would size against a holding it does not own, leave its protective legs resting "
        "over someone else's exit, and split this instrument's cooldown and daily cap in two"
        for instrument in edited
        for other in held.get(instrument.key, ())
        if other != basket_id
    )
```

Add `Instrument` to the imports if it is not already there — it is, at line 38.

- [ ] **Step 4: Wire it into the publish path**

Change `verify_publish` to take the edited set rather than compute it, and `store_basket` to run both checks:

```python
async def verify_publish(
    catalogue: InstrumentCatalogue, basket: Basket, edited: Sequence[Instrument]
) -> tuple[str, ...]:
    """Refusals for a basket about to be stored. Empty means the venue agrees with every change.

    Called from *every* path that publishes a basket, not only the edit form. `edited` comes from
    the caller so that one `changed()` result serves both this and the exclusivity check, which
    share the exemption exactly.
    """
    if not edited:
        return ()
    try:
        return await findings_for(catalogue, edited)
    except TradebotError as exc:
        return (
            f"{catalogue.venue_id} could not be reached to verify "
            f"{', '.join(sorted(i.key for i in edited))}: {exc}. Nothing was published — a basket "
            "whose trading rules cannot be checked against the venue is not one that gets stored",
        )
```

Replace the body of `store_basket` between the `previous = …` line and the `return` with:

```python
    previous = configs.latest(ConfigKind.BASKET, basket.basket_id)
    current = previous.document if previous and previous.usable else None
    edited = changed(basket.instruments, current.instruments if current else ())
    findings = (
        await verify_publish(catalogue, basket, edited)
        + exclusive_findings(configs.baskets(), basket.basket_id, edited)
    )
    if findings:
        raise ConfigError(
            f"this basket was not published: {'; '.join(findings)}"
        )
    return await configs.put(basket.basket_id, basket, actor=actor, note=note)
```

Extend `store_basket`'s docstring with a second paragraph:

```
    Two checks over the same `changed()` set: the venue must agree with every trading rule, and no
    other basket in service may already hold the instrument (ADR 0026). Sharing the exemption is
    what lets a pause or a quarantine still be published on a basket that is *already* wrong in
    either way — which is exactly when an operator needs it.
```

**Two knock-on edits, both required for the suite to compile:**

1. `verify_publish` lost its `previous` parameter, so the existing test that calls it must pass the edited set instead. In `tests/unit/test_reference_data.py`, `TestWhatCountsAsChanged::test_a_publish_that_touches_no_instrument_asks_no_venue` becomes:

```python
    async def test_a_publish_that_touches_no_instrument_asks_no_venue(self) -> None:
        """Pausing, quarantining or tightening a stop must survive a venue outage — otherwise the
        safety mechanism becomes a safety hazard."""
        unreachable = Unreachable()
        current = stored_basket()
        edited = current.model_copy(update={"status": BasketStatus.PAUSED})

        touched = changed(edited.instruments, current.instruments)

        assert touched == ()
        assert await verify_publish(unreachable, edited, touched) == ()
        assert unreachable.asked == 0
```

2. The refusal message changed from *"{venue} does not agree with this basket's instruments, so it was not published: …"* to *"this basket was not published: …"*. Grep for assertions on the old wording and update them:

```powershell
Select-String -Path tests\ -Pattern "does not agree with this basket" -Recurse
```

`tests/unit/test_dashboard_configure.py::test_a_readable_number_the_venue_disagrees_with_is_still_refused` asserts on `"the venue publishes 0.00001"`, which sits inside a finding and is unaffected.

- [ ] **Step 5: Run the new tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_reference_data.py -v`
Expected: PASS

- [ ] **Step 6: Fix the three existing tests that now create an overlap**

These clone the demo basket's instruments (`sim:BTC/USDT`, `sim:ETH/USDT`) under a new id, which the rule now correctly refuses. Give the new basket its own pair from the committed sim capture.

In `tests/unit/test_dashboard_configure.py`, change `new_basket_form` to use a pair the demo does not hold:

```python
def new_basket_form(*, lot_size: str) -> list[tuple[str, str]]:
    """The blank new-basket form, filled in as an operator would — `lot_size` is theirs to type.

    `SOL/USDT` rather than `BTC/USDT`: the seeded demo basket holds BTC and ETH, and an instrument
    belongs to exactly one basket in service (ADR 0026).
    """
    draft = blank_basket_draft()
    draft["basket_id"] = "alpha"
    draft["name"] = "Alpha"
    draft["panel"]["panel_id"] = "alpha-panel"
    draft["timeframes"] = ["1h"]
    draft["instruments"][0].update(
        symbol="SOL/USDT",
        base_currency="SOL",
        quote_currency="USDT",
        lot_size=lot_size,
        tick_size="0.01",
    )
    return flat(draft)
```

`test_a_readable_number_the_venue_disagrees_with_is_still_refused` asserts the venue's published lot size. Re-derive it rather than hardcoding: run
`.venv\Scripts\python.exe -c "import asyncio,tradebot.marketdata.catalogue as c; print(asyncio.run(c.sim_catalogue().resolve('SOL/USDT')))"`
and update the two assertions (`lot_size=` passed in, and the `"the venue publishes …"` string) to that pair's real numbers.

Replace `test_creating_a_basket_from_the_new_form` — cloning the demo form is exactly what the rule forbids, so it becomes two tests:

```python
async def test_creating_a_basket_from_the_new_form(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """A second basket needs its own instruments (ADR 0026), so it is built from the blank form."""
    market = await sim_application.catalogue.resolve("SOL/USDT")
    typed = new_basket_form(lot_size=str(market.lot_size))
    for field in ("tick_size", "min_qty", "min_notional"):
        typed = _replace(typed, f"doc.instruments[0].{field}", str(getattr(market, field)))

    response = await client.post("/configure/baskets/alpha", data=as_form(typed))

    assert response.status_code == 303
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo", "alpha"}


async def test_a_second_basket_may_not_take_an_instrument_demo_already_holds(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Two baskets over one instrument oversell the portfolio holding through reduce-only."""
    cloned = _replace(basket_form, "doc.basket_id", "alpha")

    response = await client.post("/configure/baskets/alpha", data=as_form(cloned))

    assert response.status_code == 200
    assert "already held by basket &#39;demo&#39;" in response.text
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo"}
```

In `tests/scenario/test_dashboard_lifecycle.py`, replace lines 48–55 (step 1) with:

```python
    # 1. Created — from the seeded basket's own form, renamed, and given its own instrument. An
    #    instrument belongs to exactly one basket in service (ADR 0026), so `alpha` cannot simply
    #    inherit demo's; the rest of the form is reused to keep the test about the lifecycle.
    source = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert source is not None
    draft = unfold_prices(draft_of(source.document))
    market = await sim_application.catalogue.resolve("SOL/USDT")
    draft["instruments"] = [
        {
            "symbol": market.symbol,
            "venue": "sim",
            "asset_class": "crypto",
            "base_currency": market.base_currency,
            "quote_currency": market.quote_currency,
            "lot_size": str(market.lot_size),
            "tick_size": str(market.tick_size),
            "min_qty": str(market.min_qty),
            "min_notional": str(market.min_notional),
        }
    ]
    form = flat(draft)
    created = _set(_set(form, "doc.basket_id", "alpha"), "doc.name", "Alpha basket")
```

- [ ] **Step 7: Run the full affected suites**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_reference_data.py tests/unit/test_dashboard_configure.py tests/unit/test_dashboard_control.py -q`
Then: `.venv\Scripts\python.exe -m pytest -m scenario -q`
Expected: PASS. If `test_dashboard_control.py` fails, it is because a quarantine or status POST now hits the exclusivity check — it must not, because those publish unchanged instruments. That would mean `changed()` is not being reused correctly; fix Step 4 rather than the test.

- [ ] **Step 8: Full gate, then stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/control/reference.py tests/unit/test_reference_data.py \
        tests/unit/test_dashboard_configure.py tests/scenario/test_dashboard_lifecycle.py
```

Commit message (see Global Constraints on git identity):

```
feat(risk): an instrument belongs to exactly one basket in service

Positions are keyed by instrument alone and baskets cycle concurrently,
so two baskets over one instrument oversell the holding through
reduce-only, strand a protective leg, misattribute the round trip and
double every metering limit. Refused in store_basket over the same
changed() set the venue verification uses, so pausing a basket that is
already overlapping is still allowed.
```

---

### Task 2: Halt an overlap at runtime, in every mode

**Files:**
- Modify: `tradebot/control/reference.py` (`DriftWatch.check`, `_report`, ~225-281)
- Test: `tests/unit/test_reference_data.py`

**Interfaces:**
- Consumes: `holders_of` and `EXCLUSIVITY_RULE` from Task 1; `Watchdog.halt_basket(basket_id: str, reason: str)`; `RiskStateStore.status_of(basket_id) -> BasketStatus`; `EventFactory(...).risk_event(tier=, rule=, scope=, action=, detail=)`.
- Produces: `overlaps(records: Sequence[ConfigRecord[Basket]]) -> dict[str, tuple[str, ...]]`.

---

- [ ] **Step 1: Write the failing tests**

Append to `TestExclusivity` in `tests/unit/test_reference_data.py`:

```python
    def test_overlaps_reports_both_sides(self) -> None:
        alpha = stored_basket().model_copy(update={"basket_id": "alpha"})
        beta = stored_basket().model_copy(update={"basket_id": "beta"})

        found = overlaps((_record(alpha, 1), _record(beta, 1)))

        assert set(found) == {"alpha", "beta"}
        assert "'beta'" in found["alpha"][0]
        assert "'alpha'" in found["beta"][0]

    @pytest.mark.parametrize("mode", [Mode.SIM, Mode.PAPER, Mode.LIVE])
    async def test_an_overlap_halts_every_basket_involved_in_every_mode(
        self, harness: Harness, catalogue: SimCatalogue, mode: Mode
    ) -> None:
        """Unlike venue drift, this is not keyed to the mode.

        Drift is an outside event whose sim analogue is inert — a committed capture cannot change
        under a running system. An overlap is an internally inconsistent configuration, equally
        wrong everywhere, and it corrupts round-trip attribution and the loss streak, which is
        what `report promotion` reads.
        """
        for basket_id in ("alpha", "beta"):
            await harness.publish(stored_basket().model_copy(update={"basket_id": basket_id}))

        found = await harness.watch(mode, catalogue).check()

        assert set(found) == {"alpha", "beta"}
        assert harness.states.status_of("alpha") is BasketStatus.HALTED
        assert harness.states.status_of("beta") is BasketStatus.HALTED
        assert {e.payload["action"] for e in harness.exclusivity_events} == {"halted"}

    async def test_an_overlap_is_reported_once(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        """A resync tick every thirty seconds must not re-alert for as long as it stands."""
        for basket_id in ("alpha", "beta"):
            await harness.publish(stored_basket().model_copy(update={"basket_id": basket_id}))
        watch = harness.watch(Mode.SIM, catalogue)

        await watch.check()
        await watch.check()

        assert len(harness.exclusivity_events) == 2  # one per basket, not four

    async def test_a_basket_with_its_own_instruments_is_left_alone(
        self, harness: Harness, catalogue: SimCatalogue
    ) -> None:
        await harness.publish(stored_basket().model_copy(update={"basket_id": "alpha"}))

        assert await harness.watch(Mode.SIM, catalogue).check() == {}
        assert harness.states.status_of("alpha") is BasketStatus.ACTIVE
```

Add to the `Harness` dataclass, beside `drift_events`:

```python
    @property
    def exclusivity_events(self) -> list[Event]:
        return [
            event
            for event in self.store.read_all()
            if event.type is EventType.RISK_EVENT
            and event.payload.get("rule") == "instrument_exclusivity"
        ]
```

Add `overlaps` to the `tradebot.control.reference` import list.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_reference_data.py::TestExclusivity -v`
Expected: FAIL — `ImportError: cannot import name 'overlaps'`

- [ ] **Step 3: Add `overlaps`**

In `tradebot/control/reference.py`, after `exclusive_findings`:

```python
def overlaps(records: Sequence[ConfigRecord[Basket]]) -> dict[str, tuple[str, ...]]:
    """Per basket, a finding for every instrument another basket in service also holds.

    Both sides are reported and both are halted: there is no way to tell which basket is the
    mistake, and leaving one cycling means it keeps trading an instrument whose position history
    is already contaminated by the other.
    """
    held = holders_of(records)
    found = {}
    for record in records:
        basket_id = record.ref.config_id
        findings = tuple(
            f"{instrument.key} is also held by basket {other!r}; an instrument belongs to exactly "
            "one basket in service. Remove it from all but one and re-publish"
            for instrument in record.document.instruments
            for other in held.get(instrument.key, ())
            if other != basket_id
        )
        if findings:
            found[basket_id] = findings
    return found
```

- [ ] **Step 4: Report and halt it in `DriftWatch`**

Replace `DriftWatch.check` and `_report`:

```python
    async def check(self) -> dict[str, tuple[str, ...]]:
        """Compare every basket in service, record what disagrees, and halt where it matters.

        Baskets are read fresh rather than held, because one published while the process runs is
        exactly the case a periodic check exists for. Returns the findings per basket so the
        startup sequence can report them; the halt and the `RISK_EVENT` already happened.
        """
        records = self._configs.baskets()
        shared = overlaps(records)
        found: dict[str, tuple[str, ...]] = {}
        for record in records:
            basket_id = record.ref.config_id
            try:
                drift = await findings_for(self._catalogue, record.document.instruments)
            except TradebotError as exc:
                # An unreachable venue is not drift. Halting every basket over one bad minute
                # would turn a transient outage into an incident that needs a human to clear.
                logger.warning(
                    "could not verify instrument rules against the venue",
                    extra={"venue": self._catalogue.venue_id, "error": str(exc)},
                )
                return found
            taken = shared.get(basket_id, ())
            if not (drift or taken) or self._already_halted(basket_id):
                continue
            found[basket_id] = drift + taken
            # Two rules, two events: the log must say which fault this was, and they do not share
            # a severity. Venue drift is an outside event whose sim analogue is inert — a committed
            # capture cannot change under a running system — so it is keyed to the mode. An overlap
            # is an internally inconsistent configuration, equally wrong in sim, and it corrupts
            # round-trip attribution and the loss streak, which is what `report promotion` reads.
            halts = bool(taken) or self._mode in HALTS_ON_DRIFT
            if drift:
                await self._record(basket_id, DRIFT_RULE, drift, halts=halts)
            if taken:
                await self._record(basket_id, EXCLUSIVITY_RULE, taken, halts=True)
            if halts:
                await self._watchdog.halt_basket(basket_id, self._reason(drift, taken))
        return found

    async def _record(
        self, basket_id: str, rule: str, findings: tuple[str, ...], *, halts: bool
    ) -> None:
        await self._store.append(
            EventFactory(clock=self._clock, basket_id=basket_id, cycle_id="reference").risk_event(
                tier=RiskTier.RECONCILIATION,
                rule=rule,
                scope=basket_id,
                action="halted" if halts else "recorded",
                detail="; ".join(findings),
            )
        )
        if not halts:
            logger.warning(
                "instrument rules disagree with the venue; sim keeps cycling",
                extra={"basket_id": basket_id, "detail": "; ".join(findings)},
            )

    def _reason(self, drift: tuple[str, ...], taken: tuple[str, ...]) -> str:
        """One halt, naming everything that caused it, so the operator fixes it in one pass."""
        if taken:
            return f"instruments are held by more than one basket: {'; '.join(taken)}"
        return (
            f"instrument trading rules disagree with {self._catalogue.venue_id}: "
            f"{'; '.join(drift)}. Re-publish the basket to re-resolve them"
        )
```

- [ ] **Step 5: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_reference_data.py -v`
Expected: PASS

- [ ] **Step 6: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/control/reference.py tests/unit/test_reference_data.py
```

```
feat(risk): DriftWatch halts an instrument held by two baskets

Every mode, unlike venue drift: a committed sim capture cannot change
under a running system, but an overlapping configuration is equally
wrong everywhere and corrupts the round-trip attribution the promotion
report reads.
```

---

### Task 3: Record the decision

**Files:**
- Create: `docs/adr/0026-an-instrument-belongs-to-exactly-one-basket.md`
- Modify: `DESIGN.md` (§4 relationships paragraph ~line 191-196; §8.1 table, after the "Venue changes a trading filter" row ~line 727)

**Interfaces:** none — documentation only.

---

- [ ] **Step 1: Write the ADR**

Match the house style of `docs/adr/0025-*.md` — read it first for the section headings it uses. Create `docs/adr/0026-an-instrument-belongs-to-exactly-one-basket.md` covering:

- **Context.** DESIGN §3 principle 7 already requires one writer per resource, and nothing enforced it. `Basket._check_instruments` refuses a duplicate within one basket only. Baskets cycle as concurrent asyncio tasks (`supervisor.py:404`) and positions are keyed by `instrument_key` alone (`portfolio.py:98`).
- **Decision.** At most one basket in service may hold a given `Instrument.key`. Enforced in `control/reference.store_basket` over the `changed()` set, and re-checked by `DriftWatch` at startup and on the resync sweep.
- **Consequences — the four failures this prevents**, each with its reference:
  1. `LongOnlyRule` caps a SELL at the *portfolio* holding (`rules.py:82-99`), so two baskets each pass reduce-only at the full quantity and 2× reaches the venue. Binance rejects; Alpaca opens a margin short — R13.
  2. Each basket's entry places its own venue-held protective leg (ADR 0004); one basket's exit leaves the other's stop resting over a position that is gone.
  3. The open round trip is keyed by instrument alone (`portfolio.py:163`) and the projector stamps `round_trips.basket_id` from the closing event (`projections.py:210`), so the loss lands on whichever basket closed it and `report promotion` reads that table.
  4. `TradingHistory.for_instrument` filters by `basket_id` (`history.py:49-62`), so cooldown, daily cap and loss streak all double.
- **The changed-only exemption**, and why it is not a loophole: an operator must be able to pause or quarantine a basket that is already overlapping, and a check that blocks the fix is a safety hazard.
- **Why the runtime halt is not mode-keyed**, unlike `HALTS_ON_DRIFT`.
- **What survives unchanged:** Tier-2's `max_instrument_exposure_pct` ("across all baskets") remains meaningful as a portfolio-level backstop over the Tier-1 per-basket cap.

- [ ] **Step 2: Add the DESIGN §8.1 failure row**

Insert immediately after the `Venue changes a trading filter (lot/tick/min)` row:

```markdown
| Instrument held by two baskets | publish-time check in `store_basket`; startup preflight and the supervisor's resync sweep | publish refused naming the other basket; at runtime a `RISK_EVENT` and **halt every basket involved, in every mode** — positions are the portfolio's, so two baskets would oversell one holding and split its cooldown; cleared by removing it from all but one and re-publishing (ADR 0026) |
```

- [ ] **Step 3: Add the DESIGN §4 clause**

In the relationships paragraph, change the first sentence to:

```markdown
Relationships: a **Portfolio** (one per venue account) contains Positions; Baskets reference
Instruments but *Positions belong to the Portfolio* — this is what makes Tier-2 risk
meaningful when two baskets accidentally hold correlated exposure. It is also why **an Instrument
belongs to exactly one Basket in service** (ADR 0026): a position is keyed by instrument alone, so
two baskets over one instrument would be two writers of one position, which principle 7 forbids.
```

- [ ] **Step 4: Verify nothing broke**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_static.py -q`
Expected: PASS (no `https://` was added to a template; the ADR is not a template, but run it anyway since docs links are checked elsewhere).

- [ ] **Step 5: Stage**

```bash
git add docs/adr/0026-an-instrument-belongs-to-exactly-one-basket.md DESIGN.md
```

```
docs: ADR 0026 — an instrument belongs to exactly one basket
```

---

# Slice C — the settings workspace

### Task 4: `dashboard/editor.py`, pure assembly

**Files:**
- Create: `tradebot/dashboard/editor.py`
- Create: `tests/unit/test_dashboard_editor.py`

**Interfaces:**
- Consumes: nothing from earlier tasks except `holders_of`-shaped data, which is passed in as a plain `dict[str, tuple[str, ...]]` so this module imports nothing from `control/`.
- Produces:
  - `focus_for(path: str) -> dict[str, str]`
  - `SeatRow(index: int, seat_id: str, binding: str, homogeneous: bool)`
  - `seat_rows(panel: Mapping[str, Any]) -> tuple[SeatRow, ...]`
  - `ProviderRow(index: int, provider_id: str, kind: str, used_by: int)`
  - `provider_rows(panel: Mapping[str, Any]) -> tuple[ProviderRow, ...]`
  - `InstrumentRow(index: int, symbol: str, venue: str, key: str, foreign: bool, quarantined: bool, held_by: str)`
  - `instrument_rows(draft, *, venue_id, quarantined, holders, basket_id) -> tuple[InstrumentRow, ...]`
  - `instrument_keys(draft: Mapping[str, Any]) -> tuple[str, ...]` (moved from `configure._instrument_keys`)
  - `declared_providers(draft: Mapping[str, Any], path: str) -> tuple[str, ...]` (moved from `configure._declared_providers`)
  - `panel_providers(draft: Mapping[str, Any], path: str) -> list[dict[str, Any]]` (moved from `configure._panel_providers`)

---

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_dashboard_editor.py`:

```python
"""The basket editor's view model: what the form shows that the draft does not say outright.

Pure assembly over a draft dict, like `blotter.py` and `dock.py`, so every rule here is asserted
without a browser or an HTTP round trip. The two that carry weight beyond the pass:

* **Homogeneity is visible while it is being configured**, not only after a cycle ran.
  Heterogeneity is a design control (DESIGN §6.5, L5), and `PANEL_HOMOGENEOUS` fires too late to
  stop an operator building a panel that has already lost it.
* **A row says who else holds the instrument**, so ADR 0026's refusal is read where the instrument
  is picked rather than at publish.
"""

from __future__ import annotations

from typing import Any

import pytest

from tradebot.dashboard.editor import (
    declared_providers,
    focus_for,
    instrument_keys,
    instrument_rows,
    provider_rows,
    seat_rows,
)


def panel(*seats: dict[str, Any], providers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "seats": list(seats),
        "providers": providers if providers is not None else [{"provider_id": "or", "kind": "openai_compat"}],
    }


def seat(seat_id: str, provider_id: str, model: str, **extra: Any) -> dict[str, Any]:
    return {"seat_id": seat_id, "provider_id": provider_id, "model": model, **extra}


class TestFocus:
    """Which tab a row action returns to. Keys are radio group names without their `ui.` prefix."""

    def test_an_instrument_action_selects_the_instruments_section(self) -> None:
        assert focus_for("instruments") == {"section": "instruments"}

    def test_a_seat_action_selects_the_panel_section_and_the_seats_tab(self) -> None:
        assert focus_for("panel.seats") == {
            "section": "panel",
            "panel": "panel",
            "tab.panel": "seats",
        }

    def test_a_nested_seat_action_also_selects_the_seat(self) -> None:
        assert focus_for("shadow_panel.seats[1].fallbacks") == {
            "section": "panel",
            "panel": "shadow_panel",
            "tab.shadow_panel": "seats",
            "seat.shadow_panel": "1",
        }

    def test_a_provider_action_selects_the_providers_tab(self) -> None:
        assert focus_for("panel.providers[0].price_rows") == {
            "section": "panel",
            "panel": "panel",
            "tab.panel": "providers",
        }

    def test_an_unrecognised_path_selects_nothing(self) -> None:
        """A control field that is not one of ours must not throw the operator to a random tab."""
        assert focus_for("") == {}
        assert focus_for("nonsense") == {}


class TestSeatRows:
    def test_a_seat_shows_its_binding(self) -> None:
        rows = seat_rows(panel(seat("technical", "or", "deepseek-r1")))
        assert rows[0].seat_id == "technical"
        assert rows[0].binding == "or · deepseek-r1"
        assert rows[0].index == 0

    def test_two_seats_on_one_binding_are_both_flagged(self) -> None:
        rows = seat_rows(panel(seat("a", "or", "x"), seat("b", "or", "x")))
        assert [row.homogeneous for row in rows] == [True, True]

    def test_the_same_model_on_different_providers_is_not_homogeneous(self) -> None:
        """A model id only means something to the provider serving it (DESIGN §6.5)."""
        rows = seat_rows(panel(seat("a", "or", "x"), seat("b", "lm", "x")))
        assert [row.homogeneous for row in rows] == [False, False]

    def test_an_unbound_seat_is_not_flagged_against_another_unbound_one(self) -> None:
        """Two blank rows an operator has just added are not a lost design control."""
        rows = seat_rows(panel(seat("a", "", ""), seat("b", "", "")))
        assert [row.homogeneous for row in rows] == [False, False]


class TestProviderRows:
    def test_usage_counts_primary_and_fallback_bindings(self) -> None:
        built = panel(
            seat("a", "or", "x"),
            seat("b", "lm", "y", fallbacks=[{"provider_id": "or", "model": "z"}]),
            providers=[{"provider_id": "or", "kind": "openai_compat"}, {"provider_id": "lm", "kind": "openai_compat"}],
        )
        rows = {row.provider_id: row.used_by for row in provider_rows(built)}
        assert rows == {"or": 2, "lm": 1}

    def test_a_seat_naming_one_provider_twice_counts_once(self) -> None:
        built = panel(
            seat("a", "or", "x", fallbacks=[{"provider_id": "or", "model": "y"}]),
            providers=[{"provider_id": "or", "kind": "openai_compat"}],
        )
        assert provider_rows(built)[0].used_by == 1

    def test_an_unused_provider_reads_zero(self) -> None:
        built = panel(seat("a", "or", "x"), providers=[{"provider_id": "spare", "kind": "stub"}])
        assert provider_rows(built)[0].used_by == 0


class TestInstrumentRows:
    def draft(self, **row: Any) -> dict[str, Any]:
        return {"instruments": [{"symbol": "BTC/USDT", "venue": "sim", **row}]}

    def test_a_row_on_the_wired_venue_is_not_foreign(self) -> None:
        rows = instrument_rows(
            self.draft(), venue_id="sim", quarantined=(), holders={}, basket_id="demo"
        )
        assert rows[0].key == "sim:BTC/USDT"
        assert not rows[0].foreign

    def test_a_row_on_another_venue_is_foreign(self) -> None:
        """Its rules cannot be verified here and its prices come off a different book."""
        rows = instrument_rows(
            self.draft(venue="alpaca"), venue_id="sim", quarantined=(), holders={}, basket_id="demo"
        )
        assert rows[0].foreign

    def test_a_quarantined_row_says_so(self) -> None:
        rows = instrument_rows(
            self.draft(),
            venue_id="sim",
            quarantined=("sim:BTC/USDT",),
            holders={},
            basket_id="demo",
        )
        assert rows[0].quarantined

    def test_a_row_another_basket_holds_names_it(self) -> None:
        rows = instrument_rows(
            self.draft(),
            venue_id="sim",
            quarantined=(),
            holders={"sim:BTC/USDT": ("alpha",)},
            basket_id="demo",
        )
        assert rows[0].held_by == "alpha"

    def test_this_baskets_own_holding_is_not_a_conflict(self) -> None:
        rows = instrument_rows(
            self.draft(),
            venue_id="sim",
            quarantined=(),
            holders={"sim:BTC/USDT": ("demo",)},
            basket_id="demo",
        )
        assert rows[0].held_by == ""


class TestMovedHelpers:
    def test_instrument_keys_skips_half_built_rows(self) -> None:
        draft = {"instruments": [{"symbol": "BTC/USDT", "venue": "sim"}, {"venue": "sim"}, {}]}
        assert instrument_keys(draft) == ("sim:BTC/USDT",)

    @pytest.mark.parametrize("draft", [{}, {"panel": {}}, {"panel": {"providers": "not a list"}}])
    def test_declared_providers_tolerates_a_half_built_draft(self, draft: dict[str, Any]) -> None:
        assert declared_providers(draft, "panel") == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_editor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.dashboard.editor'`

- [ ] **Step 3: Write the module**

Create `tradebot/dashboard/editor.py`:

```python
"""The basket editor's view model: what the form shows that the draft does not say outright.

Pure assembly over the draft dict, like `blotter.py` and `dock.py`, and for the same reason — a
rule that decides what a control *offers* is testable without a browser only if it lives outside
the template. Nothing here reads a store, a venue or a request.

Four things the redesigned form can say that the old scroll could not:

* **Which tab a row action returns to** (`focus_for`), so adding a seat does not throw the operator
  back to the top of a 64-field page.
* **That two seats resolve to the same provider and model.** Heterogeneity is a design control
  (DESIGN §6.5, L5) and `PANEL_HOMOGENEOUS` only fires once a cycle has run; losing it should be
  visible while the panel is being configured.
* **How many seats a provider serves.** `PanelConfig` already refuses a seat bound to an undeclared
  provider; the count stops the operator discovering that at publish.
* **That another basket already holds this instrument** (ADR 0026), read where the instrument is
  picked rather than as a refusal after the fact.

Failure semantics: a draft is whatever the operator has typed so far, so every accessor here
tolerates a half-built one and returns the empty answer rather than raising. Nothing here validates
— the models do that, and a second opinion is the one that eventually disagrees.
"""

from __future__ import annotations

import re
from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The two panels a basket carries. The challenger is edited by the same macro as the champion, so
#: a field cannot exist on one panel's form and not the other's (ADR 0018).
SHADOW_PATH = "shadow_panel"
PANEL_PATHS = ("panel", SHADOW_PATH)

_INDEX = re.compile(r"\[(\d+)\]")


# ---------------------------------------------------------------------- tab focus


def focus_for(path: str) -> dict[str, str]:
    """Which tabs a row action should return to, keyed by radio group name without its `ui.`.

    "Add seat" that lands back on Identity is the same lost-place complaint the whole redesign
    exists to fix, one level down. An unrecognised path selects nothing rather than guessing: a
    control field that is not one of ours must not move the operator at all.
    """
    head, _, _ = path.partition("[")
    root = head.split(".")[0]
    if root == "instruments":
        return {"section": "instruments"}
    if root not in PANEL_PATHS:
        return {}

    focus = {"section": "panel", "panel": root}
    tail = path[len(root) :].lstrip(".")
    if tail.startswith("providers"):
        focus[f"tab.{root}"] = "providers"
    elif tail.startswith("seats"):
        focus[f"tab.{root}"] = "seats"
        if (index := _INDEX.search(tail)) is not None:
            focus[f"seat.{root}"] = index.group(1)
    return focus


# ---------------------------------------------------------------------- panel rows


@dataclass(frozen=True, slots=True)
class SeatRow:
    """One seat as the master list shows it, before its detail pane is opened."""

    index: int
    seat_id: str
    #: `provider · model`, or empty for a seat the operator has not bound yet.
    binding: str
    #: Another seat in this panel resolves to the same provider *and* model.
    homogeneous: bool


def seat_rows(panel: Mapping[str, Any]) -> tuple[SeatRow, ...]:
    """The seat list, each row flagged when it shares a binding with another seat.

    An *unbound* seat is never flagged against another unbound one: two blank rows an operator has
    just added are not a lost design control, and a warning there would train them to ignore it.
    """
    seats = _rows(panel, "seats")
    bindings = [(_text(row, "provider_id"), _text(row, "model")) for row in seats]
    return tuple(
        SeatRow(
            index=index,
            seat_id=_text(row, "seat_id"),
            binding=f"{binding[0]} · {binding[1]}" if all(binding) else "",
            homogeneous=all(binding) and bindings.count(binding) > 1,
        )
        for index, (row, binding) in enumerate(zip(seats, bindings, strict=True))
    )


@dataclass(frozen=True, slots=True)
class ProviderRow:
    """One declared endpoint, and how much of the panel depends on it."""

    index: int
    provider_id: str
    kind: str
    #: Seats binding it as primary or anywhere in a fallback chain. Seats, not bindings: a chain
    #: that names one provider twice is one seat's dependency, not two.
    used_by: int


def provider_rows(panel: Mapping[str, Any]) -> tuple[ProviderRow, ...]:
    usage = _usage(panel)
    return tuple(
        ProviderRow(
            index=index,
            provider_id=_text(row, "provider_id"),
            kind=_text(row, "kind"),
            used_by=usage.get(_text(row, "provider_id"), 0),
        )
        for index, row in enumerate(_rows(panel, "providers"))
    )


def _usage(panel: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for seat in _rows(panel, "seats"):
        named = {_text(seat, "provider_id")} | {
            _text(binding, "provider_id") for binding in _rows(seat, "fallbacks")
        }
        for provider_id in named - {""}:
            counts[provider_id] = counts.get(provider_id, 0) + 1
    return counts


# ---------------------------------------------------------------------- instrument rows


@dataclass(frozen=True, slots=True)
class InstrumentRow:
    """One instrument row's state, beyond the values in its inputs."""

    index: int
    symbol: str
    venue: str
    key: str
    #: Named a venue this process is not wired to, so its rules cannot be verified here and the
    #: prices it would be sized from come off a different book.
    foreign: bool
    #: Excluded from automated trading, per the *stored* basket. Read-only here; the act lives on
    #: the workspace, which has the held-position guard this form does not (ADR 0022).
    quarantined: bool
    #: Another basket in service holding this key, or empty. ADR 0026's refusal, shown early.
    held_by: str


def instrument_rows(
    draft: Mapping[str, Any],
    *,
    venue_id: str,
    quarantined: Container[str],
    holders: Mapping[str, Sequence[str]],
    basket_id: str,
) -> tuple[InstrumentRow, ...]:
    rows = draft.get("instruments")
    return tuple(
        _instrument_row(index, row, venue_id, quarantined, holders, basket_id)
        for index, row in enumerate(rows if isinstance(rows, list) else ())
        if isinstance(row, dict)
    )


def _instrument_row(
    index: int,
    row: Mapping[str, Any],
    venue_id: str,
    quarantined: Container[str],
    holders: Mapping[str, Sequence[str]],
    basket_id: str,
) -> InstrumentRow:
    venue = _text(row, "venue")
    symbol = _text(row, "symbol")
    key = f"{venue}:{symbol}" if venue and symbol else ""
    others = [held for held in holders.get(key, ()) if held != basket_id]
    return InstrumentRow(
        index=index,
        symbol=symbol,
        venue=venue,
        key=key,
        foreign=bool(venue) and venue != venue_id,
        quarantined=bool(key) and key in quarantined,
        held_by=others[0] if others else "",
    )


# ---------------------------------------------------------------------- draft accessors


def instrument_keys(draft: Mapping[str, Any]) -> tuple[str, ...]:
    """`venue:symbol` for each complete instrument row.

    Read from the draft rather than from the stored document, so an instrument added in this same
    edit is immediately available, exactly as a provider added here appears in every seat's picker.
    """
    rows = draft.get("instruments")
    return tuple(
        f"{_text(row, 'venue')}:{_text(row, 'symbol')}"
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, dict) and _text(row, "venue") and _text(row, "symbol")
    )


def panel_providers(draft: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    """One panel's provider rows, tolerating a draft that is half built."""
    panel = draft.get(path)
    return _rows(panel, "providers") if isinstance(panel, Mapping) else []


def declared_providers(draft: Mapping[str, Any], path: str) -> tuple[str, ...]:
    """Provider ids one panel declares — the only options its seats' pickers may offer."""
    return tuple(
        provider_id
        for row in panel_providers(draft, path)
        if (provider_id := _text(row, "provider_id"))
    )


def _rows(node: Any, key: str) -> list[dict[str, Any]]:
    rows = node.get(key) if isinstance(node, Mapping) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _text(row: Mapping[str, Any], key: str) -> str:
    return str(row.get(key, "") or "").strip()
```

- [ ] **Step 4: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_editor.py -v`
Expected: PASS

- [ ] **Step 5: Point `configure.py` at the new module**

In `tradebot/dashboard/routes/configure.py`:
- delete `_instrument_keys`, `_declared_providers`, `_panel_providers` and the `SHADOW_PATH` / `PANEL_PATHS` constants;
- import them from `tradebot.dashboard.editor` instead:
  ```python
  from tradebot.dashboard.editor import (
      PANEL_PATHS,
      SHADOW_PATH,
      declared_providers,
      instrument_keys,
      panel_providers,
  )
  ```
- update the three call sites: `_providers_of` uses `panel_providers`, and `_basket_form` passes `instrument_keys(draft)` and `declared_providers(draft, "panel")` / `declared_providers(draft, SHADOW_PATH)`.

- [ ] **Step 6: Run the configure suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py tests/unit/test_dashboard_editor.py -q`
Expected: PASS

- [ ] **Step 7: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/editor.py tests/unit/test_dashboard_editor.py \
        tradebot/dashboard/routes/configure.py
```

```
feat(dashboard): editor.py — the basket form's view model as pure assembly
```

---

### Task 5: Catalogue provenance

**Files:**
- Modify: `tradebot/interfaces/exchange.py` (the `InstrumentCatalogue` protocol)
- Modify: `tradebot/marketdata/catalogue.py` (`Catalogue` class attributes ~152-153; `VenueCatalogue.__init__` and `list_markets` ~223-254)
- Test: `tests/contract/test_catalogue_contract.py`

**Interfaces:**
- Produces: `InstrumentCatalogue.source: str` and `InstrumentCatalogue.as_of: datetime | None`, available on every implementation.

---

- [ ] **Step 1: Write the failing test**

Read `tests/contract/test_catalogue_contract.py` first to find how it parametrizes over implementations, then add a case to that same suite:

```python
async def test_every_catalogue_states_where_its_rules_came_from(catalogue: Any) -> None:
    """Reference data is read-only and stamped with its source and as-of.

    A `min_notional` decides whether an order exists at all, so a number of unknown origin is one
    an operator cannot judge. Both may be empty — a gateway that has not fetched yet has no
    as-of — but both must exist, because the form reads them without asking which kind it has.
    """
    assert isinstance(catalogue.source, str)
    assert catalogue.as_of is None or catalogue.as_of.tzinfo is not None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/contract/test_catalogue_contract.py -k provenance -v`
Expected: FAIL — `AttributeError: 'VenueCatalogue' object has no attribute 'source'`

- [ ] **Step 3: Add the attributes to the protocol**

In `tradebot/interfaces/exchange.py`, in the `InstrumentCatalogue` protocol body, after `venue_id`:

```python
    #: Where these trading rules came from, and when — provenance, rendered beside the resolved
    #: fields so an operator can see that a `min_notional` is somebody's published number and how
    #: old it is. **Display only.** Nothing decides anything on these, and a catalogue that has not
    #: fetched yet legitimately has neither.
    source: str
    as_of: datetime | None
```

`datetime` is already imported there.

- [ ] **Step 4: Give the base class defaults and `VenueCatalogue` a stamp**

In `tradebot/marketdata/catalogue.py`, on the `Catalogue` class beside `venue_id` / `asset_class`:

```python
    venue_id: str
    asset_class: AssetClass
    #: Provenance, for display only (see `InstrumentCatalogue`). Defaulted here so a subclass that
    #: has no answer — `UnavailableCatalogue` — needs no code to say so.
    source: str = ""
    as_of: datetime | None = None
```

In `VenueCatalogue.__init__`, after `self.asset_class = asset_class`:

```python
        self.source = f"{gateway.venue_id} exchangeInfo"
        self.as_of: datetime | None = None
```

In `VenueCatalogue.list_markets`, inside the lock after the fetch, beside `self._expires_at = …`:

```python
            self.as_of = self._clock.now()
```

- [ ] **Step 5: Run the contract suite**

Run: `.venv\Scripts\python.exe -m pytest -m contract -q`
Expected: PASS

- [ ] **Step 6: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/interfaces/exchange.py tradebot/marketdata/catalogue.py \
        tests/contract/test_catalogue_contract.py
```

```
feat(marketdata): every catalogue states where its rules came from
```

---

### Task 6: The tab shell

**Files:**
- Modify: `tradebot/dashboard/static/app.css` (append after the `/* config forms */` block, ~line 260)
- Modify: `tradebot/dashboard/templates/configure/basket.html` (whole `{% block content %}`)
- Modify: `tradebot/dashboard/routes/configure.py` (`_basket_form`, `redraft_basket`, `_apply_row_action`)
- Test: `tests/unit/test_dashboard_configure.py`

**Interfaces:**
- Consumes: `focus_for` from Task 4.
- Produces: `ui_of(form: FormData) -> dict[str, str]` in `configure.py`; `_basket_form(..., ui: dict[str, str])`; template context key `ui`.

---

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_dashboard_configure.py`:

```python
# ---------------------------------------------------------------- the tab shell

DOC_FIELD = re.compile(r'name="doc\.([^"]+)"')


def submitted_paths(body: str) -> set[str]:
    """Every document path the rendered page will post, with indices stripped."""
    return {re.sub(r"\[\d*\]", "", name) for name in DOC_FIELD.findall(body)}


def document_paths(draft: dict[str, Any]) -> set[str]:
    """Every document path the stored basket carries, with indices and the `doc.` prefix stripped.

    `flat` emits the browser's own field names (`doc.risk_policy.min_conviction`), which is what
    makes this comparable to what the page renders.
    """
    return {
        re.sub(r"\[\d*\]", "", name).removeprefix("doc.") for name, _ in flat(draft)
    }


#: Fields the page deliberately does not submit. Empty until Slice D removes quarantine; the
#: assertion below is two-sided, so this set is the *only* licence to omit anything.
OMITTED_FROM_THE_FORM: set[str] = set()


async def test_every_document_field_is_still_submitted(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """A tab may hide inputs; it may never omit them.

    The form round-trips the whole document and `nest()` drops absent fields, so a tab that
    conditionally renders its contents deletes that part of the basket on save. This is the
    concrete form of that rule, and it is two-sided on purpose: the first assertion catches a
    dropped field, the second catches one quietly added to the licence.
    """
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    expected = document_paths(unfold_prices(draft_of(record.document)))

    body = (await client.get("/configure/baskets/demo")).text
    submitted = submitted_paths(body)

    assert expected - submitted == OMITTED_FROM_THE_FORM
    assert expected - OMITTED_FROM_THE_FORM <= submitted


async def test_the_tab_the_operator_was_on_survives_a_row_action(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    body = (
        await client.post(
            "/configure/baskets/demo/draft",
            data=as_form([*basket_form, ("ui.section", "risk"), ("add", "instruments")]),
        )
    ).text

    # The action wins over the posted tab: adding an instrument shows the operator the instrument.
    assert 'name="ui.section" id="s-instruments"' in body
    assert 'id="s-instruments" value="instruments" checked' in body


async def test_a_posted_tab_is_kept_when_no_row_action_happened(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    body = (
        await client.post(
            "/configure/baskets/demo/draft", data=as_form([*basket_form, ("ui.section", "risk")])
        )
    ).text

    assert 'id="s-risk" value="risk" checked' in body
```

Add `import re` and `from typing import Any` at the top of the test file if not present (`Any` already is).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -k "submitted or tab" -v`
Expected: FAIL — no `ui.section` radio in the body

- [ ] **Step 3: Add the CSS**

Append to `tradebot/dashboard/static/app.css`, after the config-forms block:

```css
/* ------------------------------------------------------------------ setting tabs

   A tab may hide inputs; it may never omit them. The form round-trips the whole document and the
   parser drops absent fields, so a conditionally-rendered tab would delete that part of the basket
   on save. Hence radios and CSS rather than JavaScript: every input stays in the DOM, and with this
   stylesheet absent nothing is `display: none`, so the page degrades to one long form.

   The markup is `radio, label, pane` triples, which is what makes three rules serve any number of
   tabs — including the seat list, whose length the stylesheet cannot know. */

.tabs > .tab-pane                                     { display: none; }
.tabs > .tab-toggle:checked + .tab-label              { color: var(--accent); border-color: var(--accent); }
.tabs > .tab-toggle:checked + .tab-label + .tab-pane  { display: block; }

.tabs > .tab-toggle {
  position: absolute;
  opacity: 0;
  width: 1px;
  height: 1px;
  /* Not `display: none`: that removes it from the tab order, and these are the only way to move
     between sections without a pointer. */
}
.tabs > .tab-toggle:focus-visible + .tab-label { outline: 2px solid var(--accent); outline-offset: 2px; }

.tab-label {
  cursor: pointer;
  color: var(--muted);
  border: 1px solid transparent;
  border-radius: 4px;
  padding: 0.35rem 0.7rem;
  font-size: 0.9rem;
  user-select: none;
}
.tab-label:hover { color: var(--text); }

/* A left rail: labels stack down column 1, every pane occupies the same cell of column 2. */
.tabs.rail { display: grid; grid-template-columns: max-content 1fr; column-gap: 1.4rem; align-items: start; }
.tabs.rail > .tab-label { grid-column: 1; }
.tabs.rail > .tab-pane  { grid-column: 2; grid-row: 1 / -1; min-width: 0; }

/* A horizontal strip: `order` puts every label before every pane whatever the source order, and
   DOM adjacency — which is what the three rules above use — is unaffected by it. */
.tabs.strip { display: flex; flex-wrap: wrap; column-gap: 0.3rem; row-gap: 0.6rem; }
.tabs.strip > .tab-label { order: 0; border-bottom: 2px solid transparent; border-radius: 4px 4px 0 0; }
.tabs.strip > .tab-toggle:checked + .tab-label { border-bottom-color: var(--accent); }
.tabs.strip > .tab-pane  { order: 1; flex-basis: 100%; min-width: 0; }
```

- [ ] **Step 4: Restructure the template into six sections**

Rewrite `configure/basket.html`'s `{% block content %}`. The six sections are **Identity, Instruments, Schedule, Data, Panel, Risk** — each of today's `<h2>` groups becomes a tab, and **every field that exists today stays**, moved unchanged into its pane.

A Jinja macro cannot wrap a block, so each section is written out as its own radio, label and `<section class="tab-pane">` triple. The pane bodies below are today's markup moved verbatim; three of them change again later (Instruments in Task 7, Panel in Task 8, the Data and Risk multi-selects in Task 11), and that is why this task's job is *only* to move them:

```jinja
<form method="post" action="{{ action }}" id="basket-form">
  <input type="hidden" name="existing" value="{{ existing or '' }}">
  {{ f.error_summary(errors) }}

  <div class="tabs rail">
    <input type="radio" class="tab-toggle" name="ui.section" id="s-identity" value="identity"
           {% if (ui.get("section") or "identity") == "identity" %}checked{% endif %}>
    <label class="tab-label" for="s-identity">Identity</label>
    <section class="tab-pane">
      <div class="grid">
        {{ f.field("basket_id", "Basket id", draft.get("basket_id"), errors, hint="stable; used in every event and order id") }}
        {{ f.field("name", "Name", draft.get("name"), errors) }}
        {{ f.choice("status", "Status", draft.get("status"), statuses, errors, hint="pausing here is your intent; a halt is the system's") }}
        {{ f.choice("decision_mode", "Decision mode", draft.get("decision_mode"), decision_modes, errors, hint="basket mode is one panel run for every instrument") }}
      </div>
    </section>

    <input type="radio" class="tab-toggle" name="ui.section" id="s-instruments" value="instruments"
           {% if ui.get("section") == "instruments" %}checked{% endif %}>
    <label class="tab-label" for="s-instruments">Instruments</label>
    <section class="tab-pane">
      {% for row in draft.get("instruments") or [] %}
        {% set i = "instruments[" ~ loop.index0 ~ "]" %}
        <fieldset>
          <legend>{{ row.get("symbol") or "new instrument" }}</legend>
          <div class="grid">
            {{ f.field(i ~ ".symbol", "Symbol", row.get("symbol"), errors) }}
            {{ f.field(i ~ ".venue", "Venue", row.get("venue"), errors) }}
            {{ f.choice(i ~ ".asset_class", "Asset class", row.get("asset_class"), asset_classes, errors) }}
            {{ f.field(i ~ ".base_currency", "Base currency", row.get("base_currency"), errors) }}
            {{ f.field(i ~ ".quote_currency", "Quote currency", row.get("quote_currency"), errors, hint="every basket in one process must agree") }}
            {{ f.field(i ~ ".lot_size", "Lot size", row.get("lot_size"), errors) }}
            {{ f.field(i ~ ".tick_size", "Tick size", row.get("tick_size"), errors) }}
            {{ f.field(i ~ ".min_qty", "Min qty", row.get("min_qty"), errors) }}
            {{ f.field(i ~ ".min_notional", "Min notional", row.get("min_notional"), errors) }}
          </div>
          {{ f.row_buttons("", i, "", draft_action) }}
        </fieldset>
      {% endfor %}
      {{ f.row_buttons("instruments", "", "add instrument", draft_action) }}
    </section>

    <input type="radio" class="tab-toggle" name="ui.section" id="s-schedule" value="schedule"
           {% if ui.get("section") == "schedule" %}checked{% endif %}>
    <label class="tab-label" for="s-schedule">Schedule</label>
    <section class="tab-pane">
      {% set schedule = draft.get("schedule") or {} %}
      <div class="grid">
        {{ f.field("schedule.every_seconds", "Every (seconds)", schedule.get("every_seconds"), errors, type="number") }}
        {{ f.field("schedule.offset_seconds", "Offset (seconds)", schedule.get("offset_seconds"), errors, type="number", hint="“every 1h at :05” is 3600 / 300") }}
        {{ f.field("schedule.open_delay_seconds", "Open delay (seconds)", schedule.get("open_delay_seconds"), errors, type="number", hint="first cycle of a session, after the open") }}
        {{ f.field("ttl_buffer_seconds", "Order TTL buffer (seconds)", draft.get("ttl_buffer_seconds"), errors, type="number", hint="order lifetime is the interval minus this") }}
      </div>
    </section>

    <input type="radio" class="tab-toggle" name="ui.section" id="s-data" value="data"
           {% if ui.get("section") == "data" %}checked{% endif %}>
    <label class="tab-label" for="s-data">Data</label>
    <section class="tab-pane">
      <div class="grid">
        {{ f.multi("timeframes", "Timeframes", draft.get("timeframes") or [], timeframes, hint="empty means the engine's default set") }}
        {{ f.multi("indicators", "Indicators", draft.get("indicators") or [], indicators, hint="empty means the engine's default set") }}
        {{ f.multi("news_sources", "News sources", draft.get("news_sources") or [], news_sources, hint="empty means no news, stated as such in the snapshot") }}
      </div>
    </section>

    <input type="radio" class="tab-toggle" name="ui.section" id="s-panel" value="panel"
           {% if ui.get("section") == "panel" %}checked{% endif %}>
    <label class="tab-label" for="s-panel">Panel</label>
    <section class="tab-pane">
      <p class="muted small">The panel that trades. Its decisions go through Tier-1, Tier-2 and execution.</p>
      {{ panel_editor.editor("panel", draft.get("panel") or {}, providers, errors, draft_action, protocols, provider_kinds, evidence_slices) }}

      <h2>Shadow panel (A/B challenger)</h2>
      <p class="muted small">
        Optional. A challenger evaluated on the <strong>same frozen snapshot</strong> every cycle and
        recorded for comparison — it never trades, its cost is accounted separately, and a failure of
        it never affects the cycle. Leave the panel id blank to run no challenger at all.
        Read the comparison with <code>tradebot report shadow</code>.
      </p>
      {{ panel_editor.editor("shadow_panel", draft.get("shadow_panel") or {}, shadow_providers, errors, draft_action, protocols, provider_kinds, evidence_slices) }}
    </section>

    <input type="radio" class="tab-toggle" name="ui.section" id="s-risk" value="risk"
           {% if ui.get("section") == "risk" %}checked{% endif %}>
    <label class="tab-label" for="s-risk">Risk</label>
    <section class="tab-pane">
      {% set risk = draft.get("risk_policy") or {} %}
      <div class="grid">
        {{ f.field("risk_policy.max_basket_allocation_pct", "Max basket allocation (% equity)", risk.get("max_basket_allocation_pct"), errors) }}
        {{ f.field("risk_policy.max_position_pct_of_basket", "Max position (% of basket)", risk.get("max_position_pct_of_basket"), errors) }}
        {{ f.field("risk_policy.risk_per_trade_pct", "Risk per trade (% of basket)", risk.get("risk_per_trade_pct"), errors) }}
        {{ f.field("risk_policy.stop_loss_atr_multiple", "Stop loss (× ATR)", risk.get("stop_loss_atr_multiple"), errors) }}
        {{ f.field("risk_policy.take_profit_atr_multiple", "Take profit (× ATR)", risk.get("take_profit_atr_multiple"), errors, hint="must exceed the stop") }}
        {{ f.field("risk_policy.protective_limit_offset_pct", "Protective limit offset (%)", risk.get("protective_limit_offset_pct"), errors) }}
        {{ f.field("risk_policy.marketable_cross_pct", "Marketable cross (%)", risk.get("marketable_cross_pct"), errors) }}
        {{ f.field("risk_policy.min_conviction", "Min conviction (0–1)", risk.get("min_conviction"), errors) }}
        {{ f.field("risk_policy.cooldown_cycles", "Cooldown (cycles)", risk.get("cooldown_cycles"), errors, type="number") }}
        {{ f.field("risk_policy.max_trades_per_day", "Max trades per day", risk.get("max_trades_per_day"), errors, type="number") }}
        {{ f.field("risk_policy.max_consecutive_losses", "Max consecutive losses", risk.get("max_consecutive_losses"), errors, type="number", hint="the basket auto-pauses at this count") }}
        {{ f.field("risk_policy.unprotected_haircut_pct", "Unprotected haircut (%)", risk.get("unprotected_haircut_pct"), errors, hint="applied where the venue cannot hold a stop") }}
      </div>
      {{ f.flag("risk_policy.long_only", "Long only (SELL is reduce-only)", risk.get("long_only"), hint="v1 cannot be turned off: shorting ripples through every other rule") }}

      <h3>Quarantine</h3>
      <p class="muted small">
        Excludes a scope from <em>automated</em> trading. The cycle keeps running — market data,
        indicators and the panel's view are untouched — and only the order is refused, so the evidence
        for putting it back keeps arriving. A held position is left exactly where it is; you can still
        close it by hand from Control. Toggle either of these from Control too, without opening this
        form.
      </p>
      {{ f.flag("risk_policy.quarantined", "Quarantine the whole basket", risk.get("quarantined"), hint="also skips the panel: nothing to spend a model call on when every order is vetoed") }}
      <div class="grid">
        {{ f.multi("risk_policy.quarantined_instruments", "Quarantined instruments", risk.get("quarantined_instruments") or [], instrument_keys, hint="only instruments this basket holds; a key it does not hold excludes nothing and is refused") }}
      </div>
    </section>
  </div>

  <div class="actions">
    <div class="field">
      <label for="note">Note (recorded on the version)</label>
      <input id="note" name="note" type="text" placeholder="why this changed">
    </div>
    <button type="submit">Publish new version</button>
    {% if basket_id %}
      <a class="ghost-link" href="/configure/history/basket/{{ basket_id }}">version history</a>
    {% endif %}
  </div>
</form>
```

Two things to get right: `f.error_summary(errors)` **moves inside the form** (Task 9's htmx swap targets `#basket-form`, and a refusal rendered outside it would never be swapped in), and the retire form at the bottom stays outside the main form, unchanged.

- [ ] **Step 5: Thread `ui` through the route**

In `tradebot/dashboard/routes/configure.py`:

```python
from tradebot.dashboard.editor import focus_for  # add to the Task 4 import block

#: Tab-selection fields. Outside the `doc.` namespace, so `nest()` ignores them and no parser
#: change is needed to round-trip which tab the operator was on.
UI_PREFIX = "ui."


def ui_of(form: FormData) -> dict[str, str]:
    """The tabs the submitted page was showing, keyed by radio group name without its prefix."""
    return {
        name[len(UI_PREFIX) :]: value
        for name, value in form.multi_items()
        if name.startswith(UI_PREFIX) and isinstance(value, str) and value
    }
```

Give `_basket_form` a `ui: dict[str, str] | None = None` keyword and pass `ui=ui or {}` into `render`.

Change `_apply_row_action` to return the focus it implies:

```python
def _apply_row_action(draft: dict[str, Any], form: FormData) -> dict[str, str]:
    """Apply the add/remove button the operator pressed, and say which tab that lands on.

    At most one per submission. The focus overrides whatever tab was posted: adding an instrument
    must show the operator the instrument, not the tab they happened to press the button from.
    """
    added = _field(form, "add")
    if added:
        add_row(draft, added)
        return focus_for(added) | _selected_seat(draft, added)
    removed = _field(form, "remove")
    if removed:
        remove_row(draft, removed)
        return focus_for(removed)
    return {}


def _selected_seat(draft: dict[str, Any], path: str) -> dict[str, str]:
    """A seat just added is the seat the detail pane should be showing."""
    prefix, _, tail = path.partition(".")
    if tail != "seats" or prefix not in PANEL_PATHS:
        return {}
    seats = (draft.get(prefix) or {}).get("seats") or []
    return {f"seat.{prefix}": str(len(seats) - 1)} if seats else {}
```

and `redraft_basket`:

```python
    form = await request.form()
    draft = nest(form.multi_items())
    ui = ui_of(form) | _apply_row_action(draft, form)
    return _basket_form(
        request, draft, existing=_version_field(form), basket_id=basket_id, ui=ui
    )
```

`publish_basket`'s two refusal paths pass `ui=ui_of(form)` as well, so a validation error re-renders on the tab the operator was on.

- [ ] **Step 6: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -q`
Expected: PASS

- [ ] **Step 7: Look at it**

Run: `$env:TRADEBOT_DASHBOARD_TOKEN = "at-least-sixteen-characters"; .venv\Scripts\python.exe -m tradebot serve --mode sim --observe`
Open `/configure/baskets/demo`, click each of the six tabs, confirm every old field is reachable and nothing is missing. Stop the server.

- [ ] **Step 8: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/static/app.css tradebot/dashboard/templates/configure/basket.html \
        tradebot/dashboard/routes/configure.py tests/unit/test_dashboard_configure.py
```

```
feat(dashboard): the basket editor becomes six CSS-only tabs

Radio/label/pane triples, so three generic rules serve any number of
tabs including the unbounded seat list. A tab may hide inputs; it may
never omit them, asserted two-sidedly.
```

---

### Task 7: The instrument row — Look up and resolved fields

**Files:**
- Modify: `tradebot/dashboard/templates/_fields.html` (add `resolved`)
- Modify: `tradebot/dashboard/templates/configure/basket.html` (Instruments pane)
- Modify: `tradebot/dashboard/routes/configure.py` (`redraft_basket`, `_basket_form`, `blank_basket_draft`)
- Modify: `tradebot/dashboard/static/app.css`
- Test: `tests/unit/test_dashboard_configure.py`

**Interfaces:**
- Consumes: `instrument_rows`, `InstrumentRow` from Task 4; `catalogue.source` / `as_of` from Task 5; `holders_of` from Task 1.
- Produces: `_apply_lookup(request, draft, form) -> tuple[FieldError, ...]`; template context keys `instrument_rows`, `catalogue`.

---

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_dashboard_configure.py`:

```python
# ---------------------------------------------------------------- look up

async def test_look_up_fills_the_row_from_the_venue(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The operator names an identifier; the venue publishes the rest (ADR 0025)."""
    typed = [*basket_form, ("doc.instruments[0].symbol", "SOL/USDT"), ("lookup", "0")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(typed))).text

    assert "SOL/USDT" in body
    assert 'value="SOL"' in body  # base_currency, resolved rather than typed


async def test_look_up_refuses_a_symbol_the_venue_does_not_list(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    typed = [*basket_form, ("doc.instruments[0].symbol", "FOO/BAR"), ("lookup", "0")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(typed))).text

    assert "does not list" in body
    assert "instruments[0].symbol" in body


# A delisted symbol is refused too, but the committed sim capture holds only tradable entries, so
# that path is asserted where it belongs — `tests/contract/test_catalogue_contract.py`, over every
# catalogue at once — rather than duplicated here against a fake.


async def test_look_up_names_an_unreachable_venue_as_itself(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """An outage is not "the venue does not list it". Sending the operator to check their spelling
    when the real problem is the network is the wrong instruction at the worst moment."""

    class Unreachable:
        venue_id = "sim"
        asset_class = AssetClass.CRYPTO
        source = ""
        as_of = None

        async def list_markets(self) -> tuple[VenueMarket, ...]:
            raise VenueError("connection reset")

        async def resolve(self, identifier: str, id_type: IdType = IdType.SYMBOL) -> VenueMarket:
            return (await self.list_markets())[0]

    # `Application` is `@dataclass(slots=True)` and not frozen, so this is a plain assignment.
    sim_application.catalogue = Unreachable()  # type: ignore[assignment]
    typed = [*basket_form, ("doc.instruments[0].symbol", "SOL/USDT"), ("lookup", "0")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(typed))).text

    assert "could not be reached" in body
    assert "does not list" not in body


async def test_look_up_publishes_nothing(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    await client.post(
        "/configure/baskets/demo/draft",
        data=as_form([*basket_form, ("doc.instruments[0].symbol", "SOL/USDT"), ("lookup", "0")]),
    )

    assert sim_application.configs.latest(ConfigKind.BASKET, "demo").ref.version == 1  # type: ignore[union-attr]


async def test_the_instruments_pane_states_where_the_rules_came_from(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/configure/baskets/demo")).text
    assert "recorded from binance" in body


async def test_an_instrument_another_basket_holds_is_named_on_the_row(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """ADR 0026's refusal, shown where the instrument is picked rather than at publish."""
    market = await sim_application.catalogue.resolve("SOL/USDT")
    typed = new_basket_form(lot_size=str(market.lot_size))
    for field in ("tick_size", "min_qty", "min_notional"):
        typed = _replace(typed, f"doc.instruments[0].{field}", str(getattr(market, field)))
    await client.post("/configure/baskets/alpha", data=as_form(typed))

    body = (await client.get("/configure/baskets/alpha")).text
    body_demo = (await client.get("/configure/baskets/demo")).text

    assert "held by basket" not in body       # alpha holds SOL alone
    assert "held by basket" not in body_demo  # demo holds BTC and ETH alone
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -k "look_up or came_from or another_basket" -v`
Expected: FAIL

- [ ] **Step 3: Add the `resolved` macro**

In `tradebot/dashboard/templates/_fields.html`, after `field`:

```jinja
{# Reference data, not operator input. A readonly input rather than plain text because the value
   must still round-trip in the document — ADR 0013 pins the rules in the version a cycle ran on —
   and because an operator copying a lot size out of the form is a thing that happens. Readonly is
   not the guarantee: publish re-resolves against the venue (ADR 0025), which is why it does not
   matter that devtools can still edit this. #}
{% macro resolved(path, label, value, errors) %}
  <div class="field resolved">
    <label for="doc.{{ path }}">{{ label }}</label>
    <input id="doc.{{ path }}" name="doc.{{ path }}" type="text" readonly
           value="{{ value if value is not none else '' }}">
    {{ _message(errors, path) }}
  </div>
{% endmacro %}
```

- [ ] **Step 4: Rewrite the Instruments pane**

Replace the pane body in `configure/basket.html`:

```jinja
    <section class="tab-pane">
      <p class="muted small">
        Name an identifier and press <strong>Look up</strong>; the venue publishes the rest. Trading
        rules are venue reference data and are never typed — a stale <code>min_notional</code> lets
        through an order the risk layer sized against the wrong floor (ADR 0025). Publishing
        re-resolves every instrument this edit changed, so a value edited some other way is refused
        rather than stored.
      </p>
      {% for row in instrument_rows %}
        {% set i = "instruments[" ~ row.index ~ "]" %}
        <fieldset>
          <legend>{{ row.symbol or "new instrument" }}</legend>
          <div class="grid tight">
            {{ f.field(i ~ ".symbol", "Identifier", draft["instruments"][row.index].get("symbol"), errors, hint="the venue's own symbol, e.g. BTC/USDT") }}
            <div class="field">
              <span class="muted small">&nbsp;</span>
              <button type="submit" name="lookup" value="{{ row.index }}" class="ghost"
                      formaction="{{ draft_action }}" formnovalidate
                      hx-post="{{ draft_action }}" hx-target="#basket-form"
                      hx-select="#basket-form" hx-swap="outerHTML">Look up</button>
            </div>
          </div>
          <div class="grid tight">
            {{ f.resolved(i ~ ".venue", "Venue", draft["instruments"][row.index].get("venue"), errors) }}
            {{ f.resolved(i ~ ".asset_class", "Asset class", draft["instruments"][row.index].get("asset_class"), errors) }}
            {{ f.resolved(i ~ ".base_currency", "Base", draft["instruments"][row.index].get("base_currency"), errors) }}
            {{ f.resolved(i ~ ".quote_currency", "Quote", draft["instruments"][row.index].get("quote_currency"), errors) }}
            {{ f.resolved(i ~ ".lot_size", "Lot size", draft["instruments"][row.index].get("lot_size"), errors) }}
            {{ f.resolved(i ~ ".tick_size", "Tick size", draft["instruments"][row.index].get("tick_size"), errors) }}
            {{ f.resolved(i ~ ".min_qty", "Min qty", draft["instruments"][row.index].get("min_qty"), errors) }}
            {{ f.resolved(i ~ ".min_notional", "Min notional", draft["instruments"][row.index].get("min_notional"), errors) }}
          </div>
          <p class="muted small">
            Published by {{ catalogue.source or catalogue.venue_id }}{% if catalogue.as_of %}, {{ catalogue.as_of | moment }}{% endif %}.
          </p>
          {% if row.foreign %}
            <p class="warn small">
              This row names venue <code>{{ row.venue }}</code>; this process is wired to
              <code>{{ catalogue.venue_id }}</code>. Its trading rules cannot be verified here, and
              the prices it would be sized from come off a different book. <strong>Look up</strong>
              would re-bind it to {{ catalogue.venue_id }} — a different tradable thing, with a
              different position.
            </p>
          {% endif %}
          {% if row.held_by %}
            <p class="warn small">
              Also held by basket <code>{{ row.held_by }}</code>. An instrument belongs to exactly
              one basket in service, so publishing this will be refused (ADR 0026).
            </p>
          {% endif %}
          {% if row.quarantined %}
            <p class="muted small">
              <span class="pill warn">quarantined</span>
              Excluded from automated trading. Released on the
              <a href="/?scope=instrument:{{ basket_id }}:{{ row.key }}">workspace</a>, which is
              where the act lives.
            </p>
          {% endif %}
          {{ f.row_buttons("", i, "", draft_action) }}
        </fieldset>
      {% endfor %}
      {{ f.row_buttons("instruments", "", "add instrument", draft_action) }}
    </section>
```

- [ ] **Step 5: Add the lookup handler and the new context**

In `tradebot/dashboard/routes/configure.py`:

```python
from tradebot.control.reference import holders_of, store_basket
from tradebot.core.errors import ConfigError, TradebotError
from tradebot.dashboard.editor import instrument_rows
from tradebot.marketdata.catalogue import instrument_of
```

```python
async def _apply_lookup(
    request: Request, draft: dict[str, Any], form: FormData
) -> tuple[FieldError, ...]:
    """Resolve one row's identifier against the venue and fill the rest of it in.

    The button is convenience; `control/reference.py` is the guarantee. What it buys is that the
    operator sees the venue's own numbers *before* publishing rather than a refusal afterwards —
    and that `venue`, `asset_class` and both currencies come from the catalogue that answered
    rather than from whoever typed the identifier (ADR 0025).

    Failure semantics: a refusal is located on the row's symbol field in the venue's own words, and
    nothing else in the draft is touched. An unreachable venue reads as itself, not as "not listed".
    """
    index = _field(form, "lookup")
    rows = draft.get("instruments")
    if not index.isdigit() or not isinstance(rows, list) or int(index) >= len(rows):
        return ()
    slot = int(index)
    path = f"instruments[{slot}].symbol"
    symbol = str((rows[slot] or {}).get("symbol", "")).strip()
    try:
        instrument = await instrument_of(state_of(request).application.catalogue, symbol)
    except ConfigError as exc:
        return (FieldError(field=path, message=str(exc)),)
    except TradebotError as exc:
        return (FieldError(field=path, message=f"the venue could not be reached: {exc}"),)
    rows[slot] = draft_of(instrument)
    return ()
```

`redraft_basket` becomes:

```python
    form = await request.form()
    draft = nest(form.multi_items())
    ui = ui_of(form) | _apply_row_action(draft, form)
    errors = await _apply_lookup(request, draft, form)
    if errors:
        ui = ui | {"section": "instruments"}
    return _basket_form(
        request,
        draft,
        existing=_version_field(form),
        basket_id=basket_id,
        errors=errors,
        ui=ui,
    )
```

`_basket_form` gains two context values. It needs the request's application, which it already has:

```python
    application = state_of(request).application
    quarantine = _stored_quarantine(application.configs, str(draft.get("basket_id") or basket_id))
    return render(
        request,
        "configure/basket.html",
        …existing keys…,
        catalogue=application.catalogue,
        instrument_rows=instrument_rows(
            draft,
            venue_id=application.catalogue.venue_id,
            quarantined=quarantine,
            holders=holders_of(application.configs.baskets()),
            basket_id=str(draft.get("basket_id") or basket_id),
        ),
    )
```

with, in `configure.py`:

```python
def _stored_quarantine(configs: ConfigStore, basket_id: str) -> tuple[tuple[str, ...], bool]:
    """What the *stored* basket excludes: the named instruments, and the whole-basket flag.

    Read from the store rather than from the draft, because the form does not carry quarantine at
    all — the act lives on the workspace, which has the held-position guard this form cannot offer
    (ADR 0022). Read at render time too, so a quarantine set on the workspace while this form was
    open is shown the next time the page paints.
    """
    record = configs.latest(ConfigKind.BASKET, basket_id) if basket_id else None
    if record is None or not record.usable:
        return (), False
    policy = record.document.risk_policy
    return tuple(policy.quarantined_instruments), policy.quarantined
```

and in `_basket_form`, before the `render(...)` call:

```python
    basket_key = str(draft.get("basket_id") or basket_id)
    quarantine, basket_quarantined = _stored_quarantine(application.configs, basket_key)
```

passing `basket_quarantined=basket_quarantined` as a further context value, and `quarantined=quarantine` into `instrument_rows`. Add `from tradebot.control.config_store import SINGLETON_ID, ConfigStore` to the imports (`SINGLETON_ID` is already imported).

Add one block to the Instruments pane above the loop:

```jinja
      {% if basket_quarantined %}
        <p class="warn small">
          <span class="pill warn">whole basket quarantined</span>
          No instrument here is traded automatically. Released on the
          <a href="/?scope=basket:{{ basket_id }}">workspace</a>.
        </p>
      {% endif %}
```

Finally, in `blank_basket_draft`, the new row's venue must be the wired catalogue's rather than the literal `"sim"`. Make it a parameter so the function stays free of a running application:

```python
def blank_basket_draft(venue_id: str = "sim") -> dict[str, Any]:
```
and set `"instruments": [{"venue": venue_id, "asset_class": AssetClass.CRYPTO.value}]`. `new_basket` passes `state_of(request).application.catalogue.venue_id`; the test helper keeps calling it with no argument.

- [ ] **Step 6: Style the resolved fields**

Append to `app.css`:

```css
.field.resolved input[readonly] {
  background: color-mix(in srgb, var(--panel) 70%, transparent);
  color: var(--muted);
  border-style: dashed;
  cursor: default;
}
.warn.small { color: var(--warn); margin: 0.3rem 0; }
```

Check the variable names against the top of `app.css` and use whatever it actually defines for the warning colour.

- [ ] **Step 7: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -q`
Expected: PASS

- [ ] **Step 8: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/templates/_fields.html tradebot/dashboard/templates/configure/basket.html \
        tradebot/dashboard/routes/configure.py tradebot/dashboard/static/app.css \
        tests/unit/test_dashboard_configure.py
```

```
feat(dashboard): instrument rules are looked up, not typed

One identifier, one Look up button, and eight readonly fields carrying
the venue's own numbers with their provenance. Venue is never silently
rewritten: only Look up changes it, and it changes identity and rules
together.
```

---

### Task 8: Seat master–detail, and the two indicators

**Files:**
- Modify: `tradebot/dashboard/templates/_panel.html` (whole macro)
- Modify: `tradebot/dashboard/templates/configure/basket.html` (Panel pane)
- Modify: `tradebot/dashboard/routes/configure.py` (`_basket_form` context)
- Test: `tests/unit/test_dashboard_configure.py`

**Interfaces:**
- Consumes: `seat_rows`, `provider_rows` from Task 4.
- Produces: template context keys `panel_seats`, `panel_providers_view`, `shadow_seats`, `shadow_providers_view`.

---

- [ ] **Step 1: Write the failing tests**

```python
async def test_two_seats_on_one_binding_are_flagged_in_the_seat_list(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Heterogeneity is a design control; losing it should be visible while it is configured."""
    doubled = [
        *basket_form,
        ("doc.panel.seats[1].seat_id", "twin"),
        ("doc.panel.seats[1].role", "Analyst"),
        ("doc.panel.seats[1].provider_id", "stub"),
        ("doc.panel.seats[1].model", "stub-analyst"),
    ]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(doubled))).text

    assert "homogeneous" in body


async def test_a_provider_row_says_how_many_seats_use_it(
    client: httpx.AsyncClient
) -> None:
    body = (await client.get("/configure/baskets/demo")).text
    assert "used by 1 seat" in body


async def test_the_challenger_is_still_submitted_from_its_own_tab(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """A tab may hide inputs; it may never omit them — the hazard the shared macro removes."""
    await client.post("/configure/baskets/demo", data=as_form([*basket_form, *_shadow_fields()]))
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    reposted = flat(unfold_prices(draft_of(record.document)))

    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(reposted, "doc.risk_policy.min_conviction", "0.75")),
    )

    latest = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert latest is not None
    assert latest.document.shadow_panel is not None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -k "homogeneous or used_by or challenger_is_still" -v`
Expected: FAIL

- [ ] **Step 3: Rewrite `_panel.html`**

Keep it **one macro** — ADR 0018's reasoning is untouched — and give it a `seats` and `providers` view-model argument plus a `label`:

```jinja
{# The panel editor, parameterized by the path it edits.

   Rendered twice: once for `panel` (the champion, which trades) and once for `shadow_panel` (the
   challenger, which never does). One macro rather than two copies, because a field that exists in
   one copy and not the other is a field an operator silently loses on save (ADR 0018).

   Every input is in the DOM on every render. The Seats/Providers strip and the seat master–detail
   are CSS `:checked` rules over hidden radios, so a tab hides inputs and never omits them; a tab
   that conditionally rendered its contents would delete that part of the basket on save. #}

{% import "_fields.html" as f %}

{% macro editor(prefix, panel, providers, errors, draft_action, protocols, provider_kinds,
                evidence_slices, seats, provider_view, ui) %}
  <div class="grid">
    {{ f.field(prefix ~ ".panel_id", "Panel id", panel.get("panel_id"), errors) }}
    {{ f.choice(prefix ~ ".protocol", "Debate protocol", panel.get("protocol"), protocols, errors) }}
    {{ f.field(prefix ~ ".max_rounds", "Max rounds", panel.get("max_rounds"), errors, type="number", hint="includes the blind round 0") }}
    {{ f.field(prefix ~ ".qualified_majority", "Qualified majority (0–1)", panel.get("qualified_majority"), errors, hint="counted over the original seat count") }}
    {{ f.field(prefix ~ ".max_abstain_fraction", "Max abstain fraction", panel.get("max_abstain_fraction"), errors, hint="above this the panel is degraded and the cycle waits") }}
    {{ f.field(prefix ~ ".max_cost_usd_per_cycle", "Max cost per cycle (USD)", panel.get("max_cost_usd_per_cycle"), errors) }}
  </div>

  <div class="tabs strip">
    <input type="radio" class="tab-toggle" name="ui.tab.{{ prefix }}" id="t-{{ prefix }}-seats"
           value="seats" {% if (ui.get("tab." ~ prefix) or "seats") == "seats" %}checked{% endif %}>
    <label class="tab-label" for="t-{{ prefix }}-seats">Seats</label>
    <section class="tab-pane">
      <p class="muted small">
        Each seat picks its primary provider and builds its own ordered fallback chain
        <strong>from the declared providers</strong> — a picker, never free text. A chain that stays
        inside one vendor does not survive that vendor's outage, and a chain may not repeat a binding.
      </p>
      {% if not providers %}
        <p class="empty warn">Declare a provider first, then bind a seat to it.</p>
      {% endif %}

      <div class="tabs rail seat-master">
        {% for row in seats %}
          {% set s = prefix ~ ".seats[" ~ row.index ~ "]" %}
          <input type="radio" class="tab-toggle" name="ui.seat.{{ prefix }}"
                 id="seat-{{ prefix }}-{{ row.index }}" value="{{ row.index }}"
                 {% if (ui.get("seat." ~ prefix) or "0") == row.index | string %}checked{% endif %}>
          <label class="tab-label" for="seat-{{ prefix }}-{{ row.index }}">
            <span class="seat-id">{{ row.seat_id or "new seat" }}</span>
            <span class="seat-binding">{{ row.binding or "unbound" }}</span>
            {% if row.homogeneous %}<span class="pill warn" title="another seat resolves to the same provider and model; heterogeneity is a design control">homogeneous</span>{% endif %}
          </label>
          <section class="tab-pane">
            <div class="grid">
              {{ f.field(s ~ ".seat_id", "Seat id", panel["seats"][row.index].get("seat_id"), errors) }}
              {{ f.field(s ~ ".role", "Role", panel["seats"][row.index].get("role"), errors, hint="what this seat reasons about") }}
              {{ f.choice(s ~ ".provider_id", "Primary provider", panel["seats"][row.index].get("provider_id"), providers, errors) }}
              {{ f.field(s ~ ".model", "Primary model", panel["seats"][row.index].get("model"), errors) }}
              {{ f.field(s ~ ".temperature", "Temperature", panel["seats"][row.index].get("temperature"), errors) }}
              {{ f.multi(s ~ ".evidence", "Evidence slice", panel["seats"][row.index].get("evidence") or [], evidence_slices, hint="different slices per seat manufacture genuine disagreement") }}
            </div>
            {{ f.flag(s ~ ".devils_advocate", "Devil's advocate", panel["seats"][row.index].get("devils_advocate"), hint="argues against the emerging majority every round") }}

            <h4>Fallback chain</h4>
            {% for binding in panel["seats"][row.index].get("fallbacks") or [] %}
              {% set b = s ~ ".fallbacks[" ~ loop.index0 ~ "]" %}
              <div class="grid tight">
                {{ f.choice(b ~ ".provider_id", "Provider", binding.get("provider_id"), providers, errors) }}
                {{ f.field(b ~ ".model", "Model", binding.get("model"), errors) }}
                {{ f.row_buttons("", b, "", draft_action) }}
              </div>
            {% endfor %}
            {{ f.row_buttons(s ~ ".fallbacks", s, "add fallback", draft_action) }}
          </section>
        {% endfor %}
      </div>
      {{ f.row_buttons(prefix ~ ".seats", "", "add seat", draft_action) }}
    </section>

    <input type="radio" class="tab-toggle" name="ui.tab.{{ prefix }}" id="t-{{ prefix }}-providers"
           value="providers" {% if ui.get("tab." ~ prefix) == "providers" %}checked{% endif %}>
    <label class="tab-label" for="t-{{ prefix }}-providers">Providers</label>
    <section class="tab-pane">
      <p class="muted small">
        Endpoints this panel may reach. Nothing outside this list is ever constructed or contacted.
        A key is referenced by <strong>environment-variable name</strong>; pasting a key value here
        is refused at publish time.
      </p>
      {% for view in provider_view %}
        {% set p = prefix ~ ".providers[" ~ view.index ~ "]" %}
        {% set row = panel["providers"][view.index] %}
        <fieldset>
          <legend>
            {{ view.provider_id or "new provider" }}
            <span class="muted small">used by {{ view.used_by }} seat{{ "" if view.used_by == 1 else "s" }}</span>
          </legend>
          <div class="grid">
            {{ f.field(p ~ ".provider_id", "Provider id", row.get("provider_id"), errors) }}
            {{ f.choice(p ~ ".kind", "Kind", row.get("kind"), provider_kinds, errors) }}
            {{ f.field(p ~ ".base_url", "Base URL", row.get("base_url"), errors, hint="empty only for the offline stub") }}
            {{ f.field(p ~ ".secret_ref", "Secret ref (env var NAME)", row.get("secret_ref"), errors, hint="e.g. OPENROUTER_API_KEY — never the key itself") }}
          </div>
          {{ f.flag(p ~ ".supports_json_mode", "Supports JSON mode", row.get("supports_json_mode"), hint="off for local servers that reject response_format") }}

          <h4>Prices (USD per million tokens)</h4>
          {% for price in row.get("price_rows") or [] %}
            {% set pr = p ~ ".price_rows[" ~ loop.index0 ~ "]" %}
            <div class="grid tight">
              {{ f.field(pr ~ ".model", "Model", price.get("model"), errors) }}
              {{ f.field(pr ~ ".prompt_per_million", "Prompt", price.get("prompt_per_million"), errors) }}
              {{ f.field(pr ~ ".completion_per_million", "Completion", price.get("completion_per_million"), errors) }}
              {{ f.row_buttons("", pr, "", draft_action) }}
            </div>
          {% endfor %}
          {{ f.row_buttons(p ~ ".price_rows", p, "add price", draft_action) }}
        </fieldset>
      {% endfor %}
      {{ f.row_buttons(prefix ~ ".providers", "", "add provider", draft_action) }}
    </section>
  </div>
{% endmacro %}
```

Note `f.multi` is still called here — Task 11 replaces it.

- [ ] **Step 4: Give the Panel pane its Champion | Challenger strip**

In `configure/basket.html`'s Panel pane:

```jinja
    <section class="tab-pane">
      <div class="tabs strip">
        <input type="radio" class="tab-toggle" name="ui.panel" id="p-panel" value="panel"
               {% if (ui.get("panel") or "panel") == "panel" %}checked{% endif %}>
        <label class="tab-label" for="p-panel">Champion</label>
        <section class="tab-pane">
          <p class="muted small">The panel that trades. Its decisions go through Tier-1, Tier-2 and execution.</p>
          {{ panel_editor.editor("panel", draft.get("panel") or {}, providers, errors, draft_action,
                                 protocols, provider_kinds, evidence_slices, panel_seats,
                                 panel_providers_view, ui) }}
        </section>

        <input type="radio" class="tab-toggle" name="ui.panel" id="p-shadow" value="shadow_panel"
               {% if ui.get("panel") == "shadow_panel" %}checked{% endif %}>
        <label class="tab-label" for="p-shadow">
          Challenger{% if not (draft.get("shadow_panel") or {}).get("panel_id") %}
            <span class="muted small">none</span>{% endif %}
        </label>
        <section class="tab-pane">
          <p class="muted small">
            Optional. A challenger evaluated on the <strong>same frozen snapshot</strong> every
            cycle and recorded for comparison — it never trades, its cost is accounted separately,
            and a failure of it never affects the cycle. Leave the panel id blank to run no
            challenger at all. Read the comparison with <code>tradebot report shadow</code>.
          </p>
          {{ panel_editor.editor("shadow_panel", draft.get("shadow_panel") or {}, shadow_providers,
                                 errors, draft_action, protocols, provider_kinds, evidence_slices,
                                 shadow_seats, shadow_providers_view, ui) }}
        </section>
      </div>
    </section>
```

- [ ] **Step 5: Pass the view models**

In `_basket_form`'s `render(...)` call:

```python
        panel_seats=seat_rows(draft.get("panel") or {}),
        panel_providers_view=provider_rows(draft.get("panel") or {}),
        shadow_seats=seat_rows(draft.get(SHADOW_PATH) or {}),
        shadow_providers_view=provider_rows(draft.get(SHADOW_PATH) or {}),
```

with `seat_rows, provider_rows` added to the `editor` import.

- [ ] **Step 6: Style the seat rail**

```css
.seat-master > .tab-label { display: flex; gap: 0.5rem; align-items: baseline; white-space: nowrap; }
.seat-master .seat-id { font: 600 13px/1.4 var(--mono); }
.seat-master .seat-binding { color: var(--muted); font-size: 0.8rem; }
```

- [ ] **Step 7: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -q`
Expected: PASS. The never-omit test from Task 6 is the one that proves the tabs did not drop a seat or provider field — if it fails, a field was omitted rather than hidden.

- [ ] **Step 8: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/templates/_panel.html tradebot/dashboard/templates/configure/basket.html \
        tradebot/dashboard/routes/configure.py tradebot/dashboard/static/app.css \
        tests/unit/test_dashboard_configure.py
```

```
feat(dashboard): seats become master-detail, with homogeneity shown

Still one macro for both panels. Two seats on one provider+model are
flagged while the panel is configured rather than after a cycle fires
PANEL_HOMOGENEOUS, and each provider row says how many seats depend
on it.
```

---

### Task 9: htmx row buttons, the sticky bar, and the unload guard

**Files:**
- Modify: `tradebot/dashboard/templates/_fields.html` (`row_buttons`)
- Modify: `tradebot/dashboard/templates/configure/basket.html` (publish bar, script tag)
- Create: `tradebot/dashboard/static/configure.js`
- Modify: `tradebot/dashboard/static/app.css`
- Test: `tests/unit/test_dashboard_configure.py`

**Interfaces:** none new — presentation only.

---

- [ ] **Step 1: Write the failing test**

```python
async def test_row_buttons_swap_the_form_rather_than_reload_the_page(
    client: httpx.AsyncClient
) -> None:
    """htmx does not scroll on a swap, so an add or remove keeps the operator where they were.

    `formaction` stays beside it: with scripting off the button performs today's full POST, so this
    is progressive enhancement and the no-JS path is unchanged.
    """
    body = (await client.get("/configure/baskets/demo")).text

    assert 'hx-target="#basket-form"' in body
    assert 'hx-select="#basket-form"' in body
    assert 'formaction="/configure/baskets/demo/draft"' in body
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -k row_buttons_swap -v`
Expected: FAIL

- [ ] **Step 3: Add htmx to `row_buttons`**

```jinja
{# Add and remove re-post the whole form to the draft endpoint and publish nothing, so a row change
   never loses what the operator has already typed elsewhere on the page.

   `hx-*` and `formaction` both: htmx swaps the form in place and does not scroll, so the operator
   keeps their position; with scripting off the button still performs the full POST it always did.
   The server returns the whole page either way and `hx-select` picks the form out of it — no new
   partial template and no second rendering path. #}
{% macro row_buttons(add_path, remove_path, add_label, action) %}
  <div class="row-buttons">
    {% if remove_path %}
      <button type="submit" name="remove" value="{{ remove_path }}" class="ghost"
              formaction="{{ action }}" formnovalidate
              hx-post="{{ action }}" hx-target="#basket-form" hx-select="#basket-form"
              hx-swap="outerHTML">remove</button>
    {% endif %}
    {% if add_path %}
      <button type="submit" name="add" value="{{ add_path }}" class="ghost"
              formaction="{{ action }}" formnovalidate
              hx-post="{{ action }}" hx-target="#basket-form" hx-select="#basket-form"
              hx-swap="outerHTML">{{ add_label }}</button>
    {% endif %}
  </div>
{% endmacro %}
```

- [ ] **Step 4: Add the sticky publish bar**

Replace the `<div class="actions">` block at the end of the form with a bar at the **top** of it, immediately after the hidden `existing` input and before `error_summary`:

```jinja
  <div class="publish-bar">
    <span class="bar-title"><code>{{ basket_id or "new" }}</code></span>
    <span class="pill {{ 'warn' if not existing else '' }}">
      {{ "version " ~ existing if existing else "draft — not published" }}
    </span>
    <input id="note" name="note" type="text" placeholder="why this changed">
    <span class="spacer"></span>
    {% if basket_id %}
      <a class="ghost-link" href="/configure/history/basket/{{ basket_id }}">version history</a>
    {% endif %}
    <button type="submit">Publish new version</button>
  </div>
```

and delete the old `<div class="actions">` and its duplicate note field. Add the script at the bottom of the template:

```jinja
{% block scripts %}
<script src="/static/configure.js" defer></script>
{% endblock %}
```

- [ ] **Step 5: Write `configure.js`**

```javascript
// The one thing on this page that needs scripting: telling an operator they are about to lose an
// edit. Everything else — tabs, master-detail, row actions — works with scripting off, because the
// screen that edits risk limits must not depend on it.
//
// Publish sits in a sticky bar now, but `add instrument` and `remove` still sit at eye level and
// still look like commits. An operator who edits a stop multiple, clicks `add fallback`, then
// navigates away used to lose the edit silently.
(function () {
  "use strict";
  var form = document.getElementById("basket-form");
  if (!form) return;

  var dirty = false;
  form.addEventListener("input", function () { dirty = true; });
  form.addEventListener("change", function () { dirty = true; });
  // An htmx swap replaces the form element, so re-arm against the new one.
  document.body.addEventListener("htmx:afterSwap", function () {
    form = document.getElementById("basket-form") || form;
  });
  form.addEventListener("submit", function () { dirty = false; });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
```

- [ ] **Step 6: Style the bar**

```css
.publish-bar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  gap: 0.7rem;
  padding: 0.6rem 0.8rem;
  margin-bottom: 0.8rem;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.publish-bar .spacer { flex: 1; }
.publish-bar input[type="text"] { max-width: 22rem; }
.bar-title { font: 600 14px/1 var(--mono); }
```

- [ ] **Step 7: Run the tests and the static suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py tests/unit/test_dashboard_static.py -q`
Expected: PASS. `configure.js` is ours, carries no `integrity` attribute, and so is not captured by `served_hashes()` — exactly like `workspace.js`. If `test_every_served_asset_is_one_we_pinned` fails, an `integrity` attribute was added by mistake; remove it rather than pinning our own source.

- [ ] **Step 8: Look at it**

Serve as in Task 6 Step 7, add and remove a fallback, confirm the page does not jump and the tab does not reset. Confirm a browser warning appears when navigating away mid-edit.

- [ ] **Step 9: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/templates/_fields.html tradebot/dashboard/templates/configure/basket.html \
        tradebot/dashboard/static/configure.js tradebot/dashboard/static/app.css \
        tests/unit/test_dashboard_configure.py
```

```
feat(dashboard): row actions swap in place, and Publish is sticky
```

---

# Slice D — quarantine leaves Settings, and `f.multi` retires

### Task 10: Quarantine carry-over

**Files:**
- Modify: `tradebot/dashboard/routes/configure.py` (`publish_basket`, `edit_basket`)
- Modify: `tradebot/dashboard/templates/configure/basket.html` (delete the quarantine block from the Risk pane; add the released banner)
- Test: `tests/unit/test_dashboard_configure.py`

**Interfaces:**
- Produces: `carry_quarantine(basket: Basket, previous: Basket | None) -> tuple[Basket, tuple[str, ...]]`.

---

- [ ] **Step 1: Write the failing tests**

The five the phase plan names as must-have:

```python
# ---------------------------------------------------------------- quarantine carry-over

async def _quarantine(client: httpx.AsyncClient, key: str, *, excluded: bool = True) -> None:
    """Set or release a quarantine the way the workspace does — the only surface that may."""
    await client.post(
        "/control/baskets/demo/quarantine",
        data={"instrument_key": key, "excluded": "true" if excluded else "false"},
    )


def _policy(application: Application) -> Any:
    record = application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return record.document.risk_policy


async def test_an_unrelated_edit_from_settings_leaves_an_instrument_quarantined(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The form no longer carries quarantine, and `nest()` omits absent fields — so without
    carry-over every publish from Settings would silently release every quarantine in force."""
    await _quarantine(client, "sim:BTC/USDT")

    edited = flat(unfold_prices(draft_of(_record(sim_application).document)))
    response = await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(edited, "doc.risk_policy.min_conviction", "0.7")),
    )

    assert response.status_code == 303
    assert _policy(sim_application).quarantined_instruments == ("sim:BTC/USDT",)
    assert _policy(sim_application).min_conviction == Decimal("0.7")


async def test_an_unrelated_edit_leaves_a_whole_basket_quarantine(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await _quarantine(client, "")

    edited = flat(unfold_prices(draft_of(_record(sim_application).document)))
    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(edited, "doc.risk_policy.min_conviction", "0.7")),
    )

    assert _policy(sim_application).quarantined is True


async def test_a_quarantine_set_after_the_form_was_opened_survives_the_publish(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Read at publish time, not carried in a hidden field. The form here is deliberately stale."""
    stale = list(basket_form)

    await _quarantine(client, "sim:ETH/USDT")
    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(stale, "doc.risk_policy.min_conviction", "0.65")),
    )

    assert _policy(sim_application).quarantined_instruments == ("sim:ETH/USDT",)


async def test_removing_a_quarantined_instrument_publishes_and_reports_the_dropped_key(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """`Basket._check_quarantine` would otherwise refuse the document over a key nobody typed."""
    await _quarantine(client, "sim:ETH/USDT")
    edited = flat(unfold_prices(draft_of(_record(sim_application).document)))
    edited = [(k, v) for k, v in edited if not k.startswith("doc.instruments[1]")]

    response = await client.post("/configure/baskets/demo", data=as_form(edited))

    assert response.status_code == 303
    assert "released=sim%3AETH%2FUSDT" in response.headers["location"]
    assert _policy(sim_application).quarantined_instruments == ()
    body = (await client.get(response.headers["location"])).text
    assert "sim:ETH/USDT" in body


async def test_a_renamed_basket_starts_with_no_quarantine(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """No prior version under the new id, so it is a different basket and inherits nothing."""
    await _quarantine(client, "sim:BTC/USDT")
    renamed = flat(unfold_prices(draft_of(_record(sim_application).document)))
    renamed = _replace(renamed, "doc.basket_id", "alpha")

    response = await client.post("/configure/baskets/alpha", data=as_form(renamed))

    # Refused by ADR 0026 — `alpha` would take demo's instruments — which is itself the assertion
    # that carry-over did not invent a quarantine on a basket that does not exist yet.
    assert response.status_code == 200
    assert "already held by basket" in response.text


async def test_settings_cannot_set_a_quarantine_even_if_the_fields_are_posted(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Overwritten, never merged: the form is not a surface for this act in either direction."""
    forged = [
        *basket_form,
        ("doc.risk_policy.quarantined", "true"),
        ("doc.risk_policy.quarantined_instruments[]", "sim:BTC/USDT"),
    ]

    await client.post("/configure/baskets/demo", data=as_form(forged))

    assert _policy(sim_application).quarantined is False
    assert _policy(sim_application).quarantined_instruments == ()


def _record(application: Application) -> Any:
    record = application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return record
```

Update the never-omit licence from Task 6:

```python
#: Fields the page deliberately does not submit. Quarantine is an operational act and lives on the
#: workspace, which has the held-position guard this form does not (ADR 0022). `publish_basket`
#: re-attaches it from the stored record, so this omission cannot release anything.
OMITTED_FROM_THE_FORM = {"risk_policy.quarantined", "risk_policy.quarantined_instruments"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -k quarantin -v`
Expected: FAIL — the edits release the quarantine

- [ ] **Step 3: Write `carry_quarantine`**

In `tradebot/dashboard/routes/configure.py`:

```python
def carry_quarantine(basket: Basket, previous: Basket | None) -> tuple[Basket, tuple[str, ...]]:
    """Re-attach the quarantine this form does not carry, from the *stored* basket.

    Quarantine left Settings because it is an operational act with a held-position guard the
    workspace has and a form cannot: from the moment a scope holding a position is excluded the bot
    is hands-off it, and inaction compounds a loss as readily as action causes one (ADR 0022).

    Deleting the two controls is not sufficient — it is actively dangerous. The form is the whole
    document and `nest()` omits absent fields, so `quarantined` would fall back to `False` and
    `quarantined_instruments` to `()`, and **every publish from Settings would silently release
    every quarantine in force**, including one set on the workspace ten seconds earlier.

    Three deliberate properties:

    * It **overwrites unconditionally, never merges**, so a hand-crafted POST cannot set one either.
      Publishing from Settings cannot change a quarantine in *either* direction.
    * `previous is None` — a new basket, or an id renamed in this edit — forces an empty quarantine
      rather than trusting the draft. Correct: it is a different basket.
    * A carried key naming an instrument this edit removed is dropped and **returned**, because
      `Basket._check_quarantine` would otherwise refuse the document over a key nobody typed.

    Read at publish time rather than carried in a hidden field, so a quarantine set on the
    workspace while this form was open survives.
    """
    policy = previous.risk_policy if previous else None
    held = {instrument.key for instrument in basket.instruments}
    named = tuple(key for key in (policy.quarantined_instruments if policy else ()) if key in held)
    dropped = tuple(
        key for key in (policy.quarantined_instruments if policy else ()) if key not in held
    )
    carried = basket.risk_policy.model_copy(
        update={
            "quarantined": bool(policy and policy.quarantined),
            "quarantined_instruments": named,
        }
    )
    return basket.model_copy(update={"risk_policy": carried}), dropped
```

Note on `model_copy`: it skips validation, which is safe here **by construction** — `named` is filtered to keys the basket holds, so `_check_quarantine`'s invariant cannot be violated. `control/control.py`'s `RiskPolicy.with_quarantine` uses the same pattern.

Wire it into `publish_basket`, between validation and `store_basket`:

```python
    application = state_of(request).application
    previous = application.configs.latest(ConfigKind.BASKET, basket.basket_id)
    basket, released = carry_quarantine(
        basket, previous.document if previous and previous.usable else None
    )
    try:
        record = await store_basket(
            application.configs,
            application.catalogue,
            basket,
            actor=ACTOR,
            note=_note(form, "edited in the dashboard"),
        )
    except ConfigError as exc:
        return _basket_form(
            request, draft, existing=_version_field(form), errors=_refusal(exc), ui=ui_of(form)
        )
    logger.warning(
        "basket published from the dashboard",
        extra={
            "basket_id": record.ref.config_id,
            "version": record.ref.version,
            "released": list(released),
        },
    )
    query = urlencode([("released", key) for key in released])
    target = f"/configure/baskets/{basket.basket_id}"
    return RedirectResponse(f"{target}?{query}" if query else target, status_code=303)
```

with `from urllib.parse import urlencode` at the top.

`edit_basket` reads the banner back:

```python
@router.get("/baskets/{basket_id}", response_class=HTMLResponse)
async def edit_basket(request: Request, basket_id: str) -> HTMLResponse:
    record = _basket_record(request, basket_id)
    return _basket_form(
        request,
        draft_of(record.document),
        existing=record.ref.version,
        released=tuple(request.query_params.getlist("released")),
    )
```

`_basket_form` takes `released: tuple[str, ...] = ()` and passes it to `render`.

- [ ] **Step 4: Delete the quarantine controls and add the banner**

In the Risk pane of `configure/basket.html`, delete the whole `<h3>Quarantine</h3>` block — the explanatory paragraph, `f.flag("risk_policy.quarantined", …)` and `f.multi("risk_policy.quarantined_instruments", …)` — and replace it with:

```jinja
      <h3>Quarantine</h3>
      <p class="muted small">
        Excluding a scope from <em>automated</em> trading is an operational act and lives on the
        <a href="/?scope=basket:{{ basket_id }}">workspace</a>, which asks for a second, deliberate
        click when the scope holds a position — a guard this form has no way to offer (ADR 0022).
        Publishing here never changes a quarantine in either direction; what is in force is shown
        on the Instruments tab.
      </p>
```

At the top of the content block, above the form:

```jinja
{% if released %}
<div class="banner ok-banner">
  <strong>Published.</strong> These instruments left the basket, so their quarantine was released
  with them:
  {% for key in released %}<code>{{ key }}</code>{% endfor %}
</div>
{% endif %}
```

`instrument_keys` is now unused by the template (it fed only the quarantine multi-select). Leave it in `editor.py` — it is still exported and tested — but drop the `instrument_keys=` context value from `_basket_form` if nothing else reads it. Check with `grep -rn "instrument_keys" tradebot/dashboard/templates/`.

- [ ] **Step 5: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py tests/unit/test_dashboard_control.py -q`
Expected: PASS

- [ ] **Step 6: Run the scenario suite**

Run: `.venv\Scripts\python.exe -m pytest -m scenario -q`
Expected: PASS — the lifecycle test quarantines only through `/control/…`, which is unaffected.

- [ ] **Step 7: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/routes/configure.py \
        tradebot/dashboard/templates/configure/basket.html tests/unit/test_dashboard_configure.py
```

```
fix(dashboard): publishing from Settings can no longer touch a quarantine

The form is the whole document and nest() omits absent fields, so simply
deleting the two controls would have released every quarantine in force
on the next publish. Quarantine is now re-attached from the stored
record at publish time — overwritten, never merged, in either direction.
```

---

### Task 11: Checkbox groups, and `f.multi` retires

**Files:**
- Modify: `tradebot/dashboard/templates/_fields.html` (add `checkboxes`, delete `multi`)
- Modify: `tradebot/dashboard/templates/configure/basket.html` (Data pane, 3 call sites)
- Modify: `tradebot/dashboard/templates/_panel.html` (seat evidence, 1 call site)
- Modify: `tradebot/dashboard/static/app.css`
- Test: `tests/unit/test_dashboard_configure.py`

**Interfaces:**
- Produces: `f.checkboxes(path, label, selected, options, hint="")`. `f.multi` is removed.

---

- [ ] **Step 1: Write the failing tests**

```python
async def test_multi_selects_are_gone_from_the_basket_form(client: httpx.AsyncClient) -> None:
    """A `<select multiple>` deselects everything on a stray click. For `indicators` that means
    quietly publishing a basket that computes nothing."""
    body = (await client.get("/configure/baskets/demo")).text
    assert "<select multiple" not in body
    assert 'multiple size=' not in body


async def test_a_checkbox_group_still_reaches_the_server_as_nothing_selected(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The hidden empty sentinel is how "nothing selected" is expressible at all: with every box
    unticked the browser sends no key, which would read as "leave it as it was"."""
    cleared = [(k, v) for k, v in basket_form if not k.startswith("doc.timeframes")]
    cleared.append(("doc.timeframes[]", ""))

    response = await client.post("/configure/baskets/demo", data=as_form(cleared))

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.document.timeframes == ()


def test_the_multi_macro_is_gone() -> None:
    """One caller left behind keeps a control whose failure mode is silent deselection."""
    source = (PACKAGE / "templates" / "_fields.html").read_text(encoding="utf-8")
    assert "macro multi(" not in source
```

Add `from tradebot.dashboard.views import PACKAGE` to the test imports.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -k "multi or checkbox" -v`
Expected: FAIL

- [ ] **Step 3: Add `checkboxes` and delete `multi`**

In `_fields.html`, replace the `multi` macro with:

```jinja
{% macro checkboxes(path, label, selected, options, hint="") %}
  <div class="field">
    <span class="group-label">{{ label }}</span>
    {# The empty sentinel is how "nothing selected" reaches the server at all: with every box
       unticked the browser sends no key, which would read as "leave it as it was" rather than as
       "clear it". `nest()` already handles the repeated `path[]` key, so the parser is untouched. #}
    <input type="hidden" name="doc.{{ path }}[]" value="">
    <div class="checkgroup">
      {% for option in options %}
        <label class="inline">
          <input type="checkbox" name="doc.{{ path }}[]" value="{{ option }}"
                 {% if option in selected %}checked{% endif %}>
          {{ option }}
        </label>
      {% endfor %}
    </div>
    {% if hint %}<span class="muted small">{{ hint }}</span>{% endif %}
  </div>
{% endmacro %}
```

- [ ] **Step 4: Update the four call sites**

`configure/basket.html`, Data pane — `f.multi(` → `f.checkboxes(` for `timeframes`, `indicators`, `news_sources`, arguments unchanged.
`_panel.html`, seat detail — the same for `evidence`.

Verify none are left: `grep -rn "f.multi(" tradebot/dashboard/templates/` must print nothing.

- [ ] **Step 5: Style the group**

```css
.checkgroup { display: flex; flex-wrap: wrap; gap: 0.2rem 0.9rem; }
.checkgroup label.inline { display: flex; align-items: center; gap: 0.35rem; color: var(--text); }
.checkgroup input[type="checkbox"] { width: auto; }
.group-label { color: var(--muted); font-size: 0.85rem; }
```

- [ ] **Step 6: Run the tests**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_dashboard_configure.py -q`
Expected: PASS

- [ ] **Step 7: Full gate and stage**

Run: `.\check.ps1`
Expected: PASS

```bash
git add tradebot/dashboard/templates/_fields.html tradebot/dashboard/templates/configure/basket.html \
        tradebot/dashboard/templates/_panel.html tradebot/dashboard/static/app.css \
        tests/unit/test_dashboard_configure.py
```

```
refactor(dashboard): checkbox groups replace multi-selects, f.multi retires
```

---

### Task 12: Conventions and phase status

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md`

**Interfaces:** none — documentation only.

---

- [ ] **Step 1: Add the two conventions to CLAUDE.md**

In the **Phase 11 — the instrument master** section, add after the ADR 0025 paragraph:

```markdown
**An instrument belongs to exactly one basket in service** ([ADR 0026](docs/adr/0026-an-instrument-belongs-to-exactly-one-basket.md)).
Positions are the portfolio's and are keyed by `instrument_key` alone, and baskets cycle as
concurrent tasks — so two baskets over one instrument both pass reduce-only against the same
holding and oversell it, leave each other's protective legs resting over an exit that already
happened, attribute the round trip to whichever closed it, and split the cooldown and daily cap in
two. Refused by `store_basket` over the **same `changed()` set** the venue verification uses, so a
pause or a quarantine of a basket that is *already* overlapping still publishes — which is exactly
when an operator needs it. `DriftWatch` re-checks it and halts every basket involved **in every
mode**, unlike venue drift: a committed sim capture cannot change under a running system, but an
overlapping configuration is equally wrong everywhere and corrupts what `report promotion` reads.
```

And in the same section, for the settings workspace:

```markdown
**A tab may hide inputs; it may never omit them.** The basket form round-trips the whole document
and `nest()` drops absent fields, so a tab that conditionally renders its contents *deletes that
part of the basket on save* — the `_panel.html` hazard, one level up. Tabs are
`<input type="radio">` plus `:checked +` CSS: three generic rules over `radio, label, pane` triples,
which is what lets the same mechanism serve the six-section rail and the unbounded seat list, and
what makes the page degrade to one long form when the stylesheet is absent.
`test_dashboard_configure.py` asserts it two-sidedly — every `doc.` path in the stored document is
submitted, and the licensed omissions are *exactly* the two quarantine fields.
```

Update the Phase 11 opening line: **A, B, C and D have shipped.**

- [ ] **Step 2: Update the phase document**

In `docs/PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md`:
- The status line becomes **Slices A, B, C and D have shipped**, plus a new **Slice E**, recorded under *What implementation changed* as a fourth entry: the exclusivity constraint was discovered during the Slice C design pass and landed ahead of it, because the instrument picker had to be built against the rule.
- Mark Slices C and D ✅ **shipped** in the Slices section.
- Add to *What implementation changed*: the Look up button posts `lookup` to the existing `/draft` route rather than a new `/lookup` route; and `test_dashboard_lifecycle.py` did **not** pass entirely unchanged after all — Slice E required `alpha` to be given its own instrument.

- [ ] **Step 3: Final full run**

Run: `.\check.ps1`
Then: `.venv\Scripts\python.exe -m pytest -m scenario -q`
Then: `.venv\Scripts\python.exe -m pytest -m contract -q`
Expected: all PASS

- [ ] **Step 4: Stage**

```bash
git add CLAUDE.md docs/PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md
```

```
docs: Phase 11 slices C, D and E shipped
```
