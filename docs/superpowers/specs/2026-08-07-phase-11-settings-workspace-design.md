# Phase 11 Slices E, C and D — basket exclusivity, the settings workspace, and quarantine cleanup

> Design agreed with the operator on 2026-08-07, before any code. Authoritative specs remain
> [DESIGN.md](../../../DESIGN.md) and [IMPLEMENTATION_PLAN.md](../../../IMPLEMENTATION_PLAN.md);
> the phase plan is [PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md](../../PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md).
> Conventions that outlive this document move to [CLAUDE.md](../../../CLAUDE.md); decisions move to
> `docs/adr/`.

Slices A (the catalogue) and B (verification) have shipped. This document specifies the rest:

| Slice | What | Why in this order |
|---|---|---|
| **E** | An instrument belongs to exactly one basket | A money-path defect, discovered during this design pass. Lands first, so C's instrument picker is built against the rule rather than retrofitted. |
| **C** | The settings workspace | The presentation work the phase exists for. |
| **D** | Quarantine leaves Settings; `f.multi` retires | Depends on C's template having been restructured. |

Nothing here changes what the risk engine computes. Slice E adds a constraint the engine already
assumed; C and D move presentation and, for one field, its *source*.

---

## Slice E — an instrument belongs to exactly one basket

### The rule

**At most one basket in service may hold a given `Instrument.key`.** Retired baskets are excluded,
or an id could never be reused.

This is not a new policy. DESIGN §3 principle 7 already states it — *"One writer per resource. A
basket's runner is the only thing that trades that basket's assets… No concurrent mutation of the
same position from two code paths."* Nothing enforces it.

### What is enforced today

`Basket._check_instruments` refuses a duplicate *within* one basket
([config.py:566-567](../../../tradebot/core/config.py#L566-L567)). That is the only uniqueness check
in the codebase. There is none across baskets, at publish, at wiring, or at startup.

Meanwhile baskets cycle as concurrent asyncio tasks, one per basket
([supervisor.py:404](../../../tradebot/control/supervisor.py#L404)), and positions are keyed by
`instrument_key` alone — venue:symbol, no basket
([portfolio.py:98](../../../tradebot/ledger/portfolio.py#L98)).

### Four failures, in descending severity

1. **Orphan quantity → accidental short (R13).** `LongOnlyRule` caps a SELL at
   `proposal.position.qty` ([rules.py:82-99](../../../tradebot/risk/rules.py#L82-L99)) — the
   *portfolio* holding. Two baskets holding one instrument both read 1.0 held, both pass reduce-only
   at 1.0, and 2.0 reaches the venue against a 1.0 position. Entries are limit orders with a TTL, so
   this is not even a race: the first order rests unfilled for most of a cycle interval. Binance
   rejects it; Alpaca opens a margin short with unlimited-loss semantics none of these rules model —
   the exact failure that rule's own docstring exists to prevent.
2. **Protective legs outlive their position (ADR 0004).** Each basket's entry places its own
   venue-held stop. One basket exits; the other's stop is still resting against a holding that is
   gone, and triggers into flat.
3. **Round trips are attributed to whoever closed them.** The open trip is keyed by instrument alone
   ([portfolio.py:163](../../../tradebot/ledger/portfolio.py#L163)) and the projector stamps
   `round_trips.basket_id` from the **closing** event
   ([projections.py:210](../../../tradebot/persistence/projections.py#L210)). Basket A opens, basket
   B closes → B's `ConsecutiveLossRule` counts a loss A caused, A's streak never sees it, and
   `report promotion` reads that table.
4. **Every metering limit silently doubles.** `TradingHistory.for_instrument` filters orders by
   `basket_id` ([history.py:49-62](../../../tradebot/ledger/history.py#L49-L62)), so cooldown, daily
   trade cap and loss streak are per-basket. One instrument in two baskets has two of each — a limit
   the operator believes is in force and is not.

One DESIGN line reads the other way: Tier-2's *"max single-instrument exposure across all baskets"*
(§6.6). It survives unchanged as a portfolio-level backstop over the Tier-1 per-basket cap. Principle
7 and the four failures settle the question.

### Enforcement 1 — at publish

`control/reference.store_basket`, beside the venue verification Slice B put there, for the reason
that module's docstring already gives: it is the one write path, so this cannot be forgotten by a
caller.

It reuses `changed()` — **the same exemption, with the same reasoning**:

| Case | Behaviour |
|---|---|
| An instrument is added to a basket while another holds it | changed → **refused**, naming the other basket and the key |
| A new basket cloning another's instruments | `previous is None`, so all changed → **refused** |
| A basket that *already* overlaps is **paused** or quarantined | unchanged → not checked → **allowed** |
| A tightened stop, a schedule edit, a status change | unchanged → **allowed** |

The third row is the one that carries weight. An operator must be able to pause or quarantine a
basket that is already overlapping — that is exactly when they most need to, and it is the argument
`reference.py` already makes about drifted rules. A fail-closed check that blocks the fix is a
safety hazard, not a safety mechanism.

Consequence for the code: `store_basket` computes `changed(...)` once and feeds both checks, rather
than `verify_publish` computing it privately.

### Enforcement 2 — at runtime

A database that already carries an overlap cannot be caught at publish. `DriftWatch.check()` already
sweeps every in-service basket at startup and on the supervisor's resync tick, already writes a
`RISK_EVENT`, already halts, and already suppresses re-reporting for an already-halted basket.

Overlap becomes a **second finding kind**, with its own `rule` on its own event — they are different
rules and the log must say which — and **every basket sharing the key is halted, in every mode.**

The divergence from `HALTS_ON_DRIFT` needs a comment where the severity is chosen, because that set
is on the adjacent lines. Venue drift is an outside event whose sim analogue is inert: a committed
capture cannot change under a running system. An overlap is an internally inconsistent
configuration, equally wrong in every mode, and it corrupts round-trip attribution and the loss
streak — which is what `report promotion` reads.

**Recovery:** remove the instrument from all but one basket and publish — allowed, because the
removal changes that basket and the remaining holder is now unique — then un-halt with the typed
phrase, as with any halt.

### Documents

- **ADR 0026 — an instrument belongs to exactly one basket.** The rule, the four failures, the
  changed-only exemption, and the every-mode halt with its divergence from `HALTS_ON_DRIFT`.
- **DESIGN §8.1** gains a failure row: *instrument held by two baskets → publish refusal, and the
  startup/resync sweep → `RISK_EVENT` + halt every basket involved, in every mode; cleared by
  removing it from all but one and re-publishing.*
- **DESIGN §4** gains one clause in the relationships paragraph, since the constraint belongs to the
  domain model and not only to an ADR.

---

## Slice C — the settings workspace

### C.1 The tab mechanism: three CSS rules, any depth, no JavaScript

The phase plan's constraint is **a tab may hide inputs; it may never omit them.** The form
round-trips the whole document and `nest()` drops absent fields, so a tab that conditionally renders
its contents deletes that part of the basket on save.

The obvious `radio + :checked ~ #pane-x` needs one CSS rule per pane, which is impossible for the
seat list — seats are unbounded. The markup is therefore `radio, label, pane` **triples**, with grid
or flex `order` placing the labels in the rail:

```html
<div class="tabs rail">
  <input type="radio" class="tab-toggle" name="ui.section" id="s-identity" value="identity" checked>
  <label class="tab-label" for="s-identity">Identity</label>
  <section class="tab-pane"> … </section>

  <input type="radio" class="tab-toggle" name="ui.section" id="s-instruments" value="instruments">
  <label class="tab-label" for="s-instruments">Instruments</label>
  <section class="tab-pane"> … </section>
</div>
```

```css
.tabs > .tab-pane                                    { display: none; }
.tabs > .tab-toggle:checked + .tab-label             { /* active */ }
.tabs > .tab-toggle:checked + .tab-label + .tab-pane { display: block; }
```

Three generic rules serve **every** level: the six-entry section rail, Champion | Challenger,
Seats | Providers, and the N-seat master–detail. Radios are visually hidden but focusable, so tabs
are keyboard-reachable. With the stylesheet absent no pane is `display: none`, so the page degrades
to today's long scroll with every input present and submitted.

Radio group names, all outside the `doc.` namespace so `nest()` ignores them unchanged:
`ui.section`, `ui.panel`, `ui.tab.panel` / `ui.tab.shadow_panel`, `ui.seat.panel` /
`ui.seat.shadow_panel`.

The six sections are **Identity, Instruments, Schedule, Data, Panel, Risk**, replacing six `<h2>`s in
a 64-field scroll.

### C.2 Tab state round-trip, and focus after a row action

`_basket_form` takes `ui: dict[str, str]`; each radio renders `checked` when `ui` names it, and
falls back to the first in its group (`ui.section` defaults to `identity`). Posted `ui.*` fields
carry the operator's tab across a draft round-trip.

A row action overrides them, through a pure function whose keys are the radio group names without
their `ui.` prefix:

```
focus_for("instruments")                     → {"section": "instruments"}
focus_for("panel.seats")                     → {"section": "panel", "panel": "panel",
                                                "tab.panel": "seats"}
focus_for("shadow_panel.seats[1].fallbacks") → {"section": "panel", "panel": "shadow_panel",
                                                "tab.shadow_panel": "seats",
                                                "seat.shadow_panel": "1"}
```

So *add seat* returns to the Seats tab with the new seat selected; *remove* clamps the index to the
list that remains.

### C.3 Look up — one control field on the existing draft route

`lookup=<row index>` sits beside the existing `add` / `remove` control fields on
`POST /configure/baskets/{id}/draft`. One route, one re-render path. A second route would duplicate
the whole draft→page assembly for one control field, and the phase plan's own words are *"same
stateless-draft mechanism as add/remove — no new state machine."*

Resolution goes through the existing `catalogue.instrument_of()` — no new resolution code — and
writes every field of the returned `Instrument` into the row: `venue`, `asset_class`,
`base_currency`, `quote_currency` and the four trading rules, all from the catalogue that answered.

Failure semantics: a `ConfigError` (not listed, delisted, ISIN refusal) becomes a `FieldError`
located on `instruments[i].symbol`, in the venue's own words. An unreachable venue is a different
message naming the venue. Nothing is written either way, and the rest of the draft is intact.

**One upstream addition:** `InstrumentCatalogue` gains `source: str` and `as_of: datetime | None` —
provenance for display only, never a decision input. The `Catalogue` base class carries empty
defaults, so `UnavailableCatalogue` and `replay_catalogue` need no change; `SimCatalogue` already
holds both; `VenueCatalogue` sets `as_of` at each fetch. This is what lets the read-out say *"binance
exchangeInfo, 2026-08-06"* rather than showing numbers of unknown origin, which is the Charles River
pattern §2.1 names: reference data is read-only and stamped with source and as-of. One assertion is
added to the contract suite.

### C.4 The instrument row

One editable input (`symbol`), one **Look up** button, and readonly inputs for the eight resolved
fields in a `.grid.tight`, through a new `f.resolved()` macro — visible and copyable, and still
round-tripping so ADR 0013 keeps the rules pinned in the version.

Readonly is not the guarantee and is not presented as one: devtools can still edit it, and
publish-time re-resolution (Slice B) is what makes that not matter.

Below the fields:

- the **provenance line** — source and as-of from C.3;
- a **foreign-venue warning** when `row.venue != catalogue.venue_id`, carrying the sentence
  `control/reference.py` already writes;
- a read-only **quarantined** pill, read from the stored record, linking to
  `/?scope=instrument:…` on the workspace (Slice D);
- **"held by basket `alpha`"** when another in-service basket holds the key — Slice E's refusal
  surfaced where the instrument is picked, rather than at publish.

**Venue is never silently rewritten.** An existing row renders its own stored `venue`; a new row
prefills with the wired catalogue's venue; **Look up is the only thing that changes it**, and it
changes venue, asset class, currencies and rules together, because they are one answer.

The alternative — always rendering `application.catalogue.venue_id` — was rejected. `Instrument.key`
is `f"{venue}:{symbol}"` and is what positions, cooldown history, round trips, Tier-2 exposure and
`quarantined_instruments` are keyed by, so venue is half an instrument's identity. The seeded demo
basket is pinned to `sim` in every mode (phase plan, implementation change 2), so editing an
unrelated field on it under `--broker binance` would rebind every key from `sim:BTC/USDT` to
`binance:BTC/USDT`, orphan the ledger position and the trading history, and — if Binance happened to
agree with the recorded sim rules — publish cleanly.

### C.5 Seat master–detail, providers, and the two new indicators

Panel → **Champion | Challenger**, then Champion → **Seats | Providers**. `_panel.html` stays one
macro; ADR 0018's reasoning is untouched, and the macro simply renders into a tab shell and takes a
label. The Challenger tab reads *"no challenger configured"* with an **Add challenger** button when
`panel_id` is blank — text only, since the inputs must stay in the DOM regardless.

Seats become a master–detail list: each row is a rail label carrying `seat_id`, its
`provider · model` binding, and its warnings; the selected seat's fields fill the pane below. Every
seat's inputs stay in the DOM.

Two things the new shape can show that the old one cannot:

- **Homogeneity at configuration time** — a seat is flagged when its `(provider_id, model)` appears
  on more than one seat in the same panel. `PANEL_HOMOGENEOUS` is a runtime event today;
  heterogeneity is a design control (DESIGN §6.5, L5) and losing it should be visible while it is
  being configured.
- **Provider usage** — *"used by 2 seats"* on each provider row, counting seats that reference it as
  primary or anywhere in a fallback chain. `PanelConfig` already refuses a seat bound to an
  undeclared provider; the count stops the operator discovering it at publish.

### C.6 htmx row buttons, and the sticky publish bar

`row_buttons` gains `hx-post`, `hx-target="#basket-form"`, `hx-select="#basket-form"`,
`hx-swap="outerHTML"`, and keeps `formaction`, so the no-JS path is byte-for-byte today's and this
is progressive enhancement. htmx does not scroll on a swap unless told to, so position is preserved.
The server keeps returning the full page and `hx-select` picks the form out of it: no new partial
template, no route change.

**`error_summary` moves inside `#basket-form`.** Outside it, a Look up refusal would render into a
region htmx never swaps.

A sticky bar inside the form carries the basket id, `draft — not published` / `version N`, the Note
field (moved up from the bottom, where it is 60 fields below the fold while `add instrument` and
`remove` sit at eye level looking like commits), and Publish.

`static/configure.js` — ours, unpinned, precedent `workspace.js` — does exactly one thing: arm a
`beforeunload` guard on the first edit inside the form, disarm on submit. The tabs stay CSS-only.

### C.7 New module `dashboard/editor.py`

Pure assembly over the draft, the pattern `blotter.py` and `dock.py` establish and for the reason
`dock.py`'s docstring gives: it is testable without a browser. It absorbs `_instrument_keys` and
`_declared_providers` from `configure.py`, which are already exactly this, and adds:

- `focus_for(path)` — C.2;
- `seat_rows(panel)` — id, binding label, `homogeneous`;
- `provider_rows(panel)` — id, kind, `used_by`;
- `instrument_rows(draft, catalogue, quarantine, holders)` — C.4's per-row state, where `holders`
  is the `key → basket_id` map built from `configs.baskets()` for Slice E's indicator.

`configure.py` keeps routing, validation and publish semantics only.

---

## Slice D — quarantine leaves Settings, and `f.multi` retires

### D.1 The hazard, stated before the change

Quarantine is already fully built on the workspace, with a held-position guard Settings does not
have. Settings offers the same act through a multi-select where a stray click *releases* a
quarantine.

Deleting the two controls is not sufficient — it is actively dangerous. The form is the whole
document and `nest()` omits absent fields, so `risk_policy.quarantined` would fall back to `False`
and `quarantined_instruments` to `()`. **Every publish from Settings would silently release every
quarantine in force**, including one set on the workspace ten seconds earlier.

### D.2 Carry-over

`publish_basket` re-attaches quarantine from the **latest stored version, read at publish time** —
not carried in a hidden field, so a quarantine set on the workspace while the form was open
survives:

```python
def carry_quarantine(basket: Basket, previous: Basket | None) -> tuple[Basket, tuple[str, ...]]:
    """Whatever the form posted, quarantine comes from the store. Returns the keys dropped."""
```

Three things it does deliberately:

- It **overwrites unconditionally, never merges.** A hand-crafted POST carrying
  `doc.risk_policy.quarantined=true` changes nothing. That is the literal form of the exit
  criterion: publishing from Settings can no longer change a quarantine in either direction.
- `previous is None` — a new basket, or the id was renamed in this edit — forces an **empty**
  quarantine rather than trusting the draft. Correct: it is a different basket.
- A carried key naming an instrument this edit removed is **dropped and returned**, because
  `Basket._check_quarantine` would otherwise refuse the document over a key the operator never
  typed.

| Case | Behaviour |
|---|---|
| New basket, or the id was renamed in this edit | No prior version → empty quarantine |
| Whole-basket `quarantined` flag | Carried over unconditionally |
| A quarantined instrument was removed in this edit | Dropped from the carried set and reported |
| Nothing quarantined | No-op |

**It lives in `routes/configure.py`, not in `store_basket`.** Putting it in the one write path is
the obvious move and is wrong: the workspace's quarantine toggle goes through `store_basket` *in
order to* change quarantine, and carry-over there would silently revert it. `control.set_status`
needs nothing — it copies the stored document, so quarantine rides along already.

### D.3 Reporting a dropped key

Success 303s to `/configure/baskets/{id}?released=<key>`, repeated per key and URL-encoded — an
instrument key carries `:` and `/` — and the edit page renders an ok-banner naming what was released. This keeps redirect-after-POST — so a browser refresh cannot
republish — needs no session or flash storage, and the notice survives the reload.

### D.4 `f.multi` retires

`f.checkboxes()` replaces it at all four surviving call sites — `timeframes`, `indicators`,
`news_sources`, and seat `evidence` — keeping the hidden `[]` sentinel, so `nest()` is untouched.
`<select multiple>` is a control where a stray click silently deselects everything; for `indicators`
that means quietly publishing a basket that computes nothing.

The quarantine call site is deleted by D.2, which is what leaves the macro with no callers. `f.multi`
is then removed, rather than left behind as one caller's worth of a control with a silent failure
mode.

---

## Testing

| Test | What it pins |
|---|---|
| **Slice E** — publish refuses an overlap, naming the other basket | The rule |
| **Slice E** — a pause of an already-overlapping basket is allowed | The changed-only exemption; the fix is never blocked |
| **Slice E** — the startup sweep halts every basket involved, **in sim** as well as live | The divergence from `HALTS_ON_DRIFT` |
| **Slice E** — a new version of a basket keeping its own instruments publishes | No self-conflict |
| `test_dashboard_configure.py` — **never-omit** | Every `doc.` path in the stored document is submitted by the redesigned page, and the set of omissions is *exactly* the two quarantine fields. The concrete form of the tab rule, and what catches a future tab that conditionally renders. |
| Look up fills the row / refuses an unlisted symbol / refuses a delisted one / names an unreachable venue | C.3 |
| `ui.*` survives a row action, and *add seat* lands on the new seat | C.2 |
| No `<select multiple>` on the page | D.4 |
| The five quarantine tests the phase plan lists as must-have | D.2, including a quarantine set on the workspace *after* the form was opened |
| New `test_dashboard_editor.py` | Homogeneity, provider usage, `focus_for`, foreign-venue and held-by-basket row state — no HTTP |

**Existing tests that must change, and why that is correct.**
`test_creating_a_basket_from_the_new_form`, `new_basket_form()` in `test_dashboard_configure.py`, and
`tests/scenario/test_dashboard_lifecycle.py:52` each clone the demo basket's instruments under a new
id. Slice E refuses that, correctly. They move to another pair from the committed sim capture. Beyond
those three, `test_dashboard_lifecycle.py` passes unchanged: it quarantines only through `/control/…`,
and the demo basket holds no quarantine at the point it publishes.

Coverage: `dashboard/` sits in the ≥ 80% bucket, `control/` and `risk/` in the ≥ 95% bucket, so
`editor.py` and the `reference.py` additions both need direct unit tests rather than incidental
route coverage.

---

## Out of scope, deliberately

- **ISIN resolution.** Declared and unserved, per the phase plan's decision 6.
- **An Alpaca catalogue.** Needs the equity gateway Phase 3 did not build.
- **Splitting the form into multiple POSTs.** Tabs are presentation. Separate submits per section
  would reintroduce the partial-document failure at a larger scale than any bug this phase fixes.
- **Any change to what the risk engine computes.** Slice E adds a constraint the engine already
  assumed; C and D move presentation and one input's source.
- **The missing periodic reconciliation** (DESIGN §6.8, "every M minutes") remains a gap, as recorded
  in the phase plan. `DriftWatch` continues to hang off the supervisor's resync sweep.

## Conventions for CLAUDE.md

- **"A tab may hide inputs; it may never omit them."** Beside the existing "both panels are edited by
  one macro" note. This is the rule the next contributor will break.
- **"An instrument belongs to exactly one basket in service."** With the changed-only exemption, so
  the next reader does not remove it as over-permissive.
