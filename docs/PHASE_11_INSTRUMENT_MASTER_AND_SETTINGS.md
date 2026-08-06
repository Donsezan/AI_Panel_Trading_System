# Phase 11 — the instrument master, and Settings as a master–detail workspace

> Authoritative specs remain [DESIGN.md](../DESIGN.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
> This records what was decided, why, and what it will take to build. Conventions that outlive it
> move to [CLAUDE.md](../CLAUDE.md); decisions move to `docs/adr/`. Written and reviewed before any
> code changes, per the standing rule that a change touching operator control gets a design pass
> first. The reference the operator pointed at is again the Charles River IMS — this time its
> *setup* workspaces rather than its trading blotter ([PHASE_10](PHASE_10_BLOTTER_WORKSPACE.md)).

**Status: Slices A and B have shipped ([ADR 0025](adr/0025-instrument-trading-rules-are-venue-reference-data.md));
C and D have not.** Three things were decided during implementation and are recorded at the end,
under [What implementation changed](#what-implementation-changed).

## Why now

Phase 10 rebuilt the operational screen and left `/configure` untouched. Reviewed against the real
rendered page, the basket editor is **55 controls on a new basket and 64 on `demo`**, in one
unbroken scroll, and one of those groups is wrong in a way that reaches the money path.

Three problems, in descending order of consequence:

1. **The operator hand-types venue trading rules.** `lot_size`, `tick_size`, `min_qty` and
   `min_notional` are free-text fields ([basket.html:44-52](../tradebot/dashboard/templates/configure/basket.html#L44-L52)).
   They flow, unverified, into `quantize_order` ([risk/tier1.py:83](../tradebot/risk/tier1.py#L83)),
   into the Tier-2 minimum check ([risk/tier2.py:308](../tradebot/risk/tier2.py#L308)) and into
   `BinanceSpotBroker(instruments=…)` ([app.py:761-766](../tradebot/app.py#L761-L766)). The system
   already says this must not happen, in [interfaces/exchange.py:98-104](../tradebot/interfaces/exchange.py#L98-L104):
   *"Fetched, never hand-configured. … a stale `min_notional` lets through an order the risk layer
   sized against the wrong floor."* The CLI honours that rule
   ([\_\_main\_\_.py:492](../tradebot/__main__.py#L492)); the GUI does not.
2. **The panel editor is rendered twice with identical headings and no visible hierarchy.**
   `_panel.html` emits bare `<h3>Providers</h3>` / `<h3>Seats</h3>` for the champion and again for
   the challenger. On `new`, the challenger contributes six empty fields, two empty sections and a
   warning for a feature most baskets never use.
3. **Every add/remove button reloads the whole page**, so the operator is thrown to the top mid-edit
   ([_fields.html:65-76](../tradebot/dashboard/templates/_fields.html#L65-L76)).

Nothing here changes what the risk engine computes. §1 changes *where one of its inputs comes from*;
everything else is presentation. The plan's job is to keep that line sharp.

## Decisions

Confirmed with the operator before this document was written:

1. **Instrument trading rules are venue reference data, never operator input.** The operator names
   an identifier; the venue publishes the rest. The GUI gets an explicit **Look up** button, and
   publishing re-verifies against the venue.
2. **The sim venue publishes a catalogue exactly as a real venue does.** Sim *simulates a venue*; it
   is not a mode with a different data path. A GUI flow that fetches from Binance but falls back to
   hand-typing under sim would mean the thing tested is not the thing that trades — the failure
   [ADR 0020](adr/0020-live-is-the-paper-wiring-minus-headroom.md) exists to prevent.
3. **The basket editor becomes master–detail with sub-tabs, and remains exactly one `<form>` and one
   POST.** Tabs hide inputs; they never unmount them.
4. **Quarantine leaves Settings.** It is an operational act, it already lives on the workspace with a
   better guard, and a second surface for it is a second place for it to disagree.

Two further decisions this plan makes, to be challenged in review rather than discovered in code:

5. **Verification at publish is strict in every mode; the response to drift *at runtime* scales with
   whether the cycles are evidence.** These are two different checks and an earlier draft of this
   document conflated them.

   Publishing an instrument whose rules disagree with the venue is refused everywhere, sim included
   — it is a form refusal, not an interruption, the sim catalogue is local and instant, and it is
   the check that actually catches the typo before the document can reach a soak. *There is no mode
   in which a bogus lot size is accepted.*

   Runtime drift is a different event: it means the venue changed a filter underneath a running
   system. Its severity follows the standing convention (ADR 0023, `readiness.py`) of keying
   response to mode:

   | Mode | Mechanism | On drift |
   |---|---|---|
   | Live | identical | refuse / halt |
   | Paper | identical | halt affected basket + alert |
   | Sim | identical | one `RISK_EVENT` + banner, keep cycling |

   The reason paper is strict and sim is not has nothing to do with the word "sim": per DESIGN §9.5,
   the soak's **primary venue is `SimBroker` fed by live Binance data**, and those cycles stamp
   `venue: sim` and *are* the promotion evidence base. A wrong `lot_size` there makes the numbers
   in `report promotion` describe a system that is not the one which will trade. In `Mode.SIM` the
   same class is doing rehearsal, nothing is at stake, and the catalogue is a committed file that
   cannot change without a human editing it — so the check has no work to do and halting on it is
   cost without benefit.
6. **ISIN resolution is designed for and deliberately not implemented.** Neither Binance nor
   Alpaca's free API publishes an ISIN→symbol mapping. The resolver takes `(identifier, id_type)`,
   validates an ISIN's check digit locally so a typo is caught, and refuses `id_type=isin` with the
   venue's actual limitation rather than guessing. Faking it is worse than not having it.

---

## Section 1 — Instrument details come from the venue

### 1.1 One catalogue protocol, one contract suite

New protocol in `interfaces/exchange.py`, beside `VenueGateway`:

```python
@runtime_checkable
class InstrumentCatalogue(Protocol):
    """What a venue lists, and the precision it lists it at."""

    venue_id: str

    async def list_markets(self) -> tuple[VenueMarket, ...]: ...
    async def resolve(self, identifier: str, id_type: IdType = IdType.SYMBOL) -> VenueMarket: ...
```

Two implementations, one behaviour:

| Implementation | Source | Notes |
|---|---|---|
| `VenueCatalogue` | delegates to `VenueGateway.fetch_markets()` | Binance today; Alpaca when it gets a gateway |
| `SimCatalogue` | an in-memory `Mapping[str, VenueMarket]` | the simulated venue's published rule set |

`SimCatalogue` is fed from one of two places, and it is the *same class* either way:

- **interactive sim** — a checked-in snapshot, `tradebot/marketdata/sim_markets.json`, recorded once
  from real `exchangeInfo` and carrying its `as_of`. Offline, deterministic, hash-reviewable.
- **backtest** — `ReplayDataset.manifest.instruments`, which already stores the rules the prices
  were recorded under ([marketdata/recorder.py](../tradebot/marketdata/recorder.py)).

This deletes the hardcoded rules in `demo_basket()` ([app.py:281-292](../tradebot/app.py#L281-L292)),
which today are a second source of truth for `min_notional = 10`.

**Rung-2 test**: `tests/contract/test_catalogue_contract.py` — one suite, every catalogue, driven by
a wire-level fake per venue. It asserts identical semantics for: unknown identifier, delisted
symbol (`tradable=False`), case handling, and the exact error type raised. That identity *is*
decision 2; without the suite it is an intention.

`Application` gains `catalogue: InstrumentCatalogue` — **not** `| None`. Every mode has one; that is
the parity requirement expressed in the type.

### 1.2 The GUI: an identifier, a Look up button, and resolved fields

The instrument row collapses from nine inputs to one input, one button, and a resolved read-out:

```
┌ Instrument ────────────────────────────────────────────────────────────┐
│ Identifier  [ BTC/USDT              ]  ( Look up )   binance · verified │
│ BTC/USDT · crypto · BTC/USDT · lot 0.00001 · tick 0.01                  │
│ min qty 0.00001 · min notional 10  — published by binance, 2026-08-06   │
└────────────────────────────────────────────────────────────────────────┘
```

- **Look up** posts the whole draft to `/configure/baskets/{id}/lookup` with the row index. The
  server resolves against `application.catalogue`, fills the row, re-renders. Same stateless-draft
  mechanism as add/remove — no new state machine, nothing stored.
- Resolved fields render as `readonly` inputs, so they still round-trip in the document (ADR 0013
  needs the rules pinned in the version) and are visible and copyable, but not editable.
- Refusals are the venue's own words: *"binance does not list FOO/BAR"*, *"binance lists LUNA/USDT
  as delisted; it is not tradable"*.
- `venue`, `asset_class`, `base_currency`, `quote_currency` come from the resolution, not from the
  operator. This also closes the free-text `venue` typo, which today is caught only at startup by
  `_check_venue` ([readiness.py:121-135](../tradebot/control/readiness.py#L121-L135)).

### 1.3 Verification at publish is what makes it safe

The button is convenience. **The guarantee is at publish.** `publish_basket` re-resolves each
instrument against the catalogue and refuses any document whose rules differ from the venue's. Once
that exists it no longer matters whether a field was typed, pasted, or edited in devtools — the only
documents that can be stored are ones the venue agrees with.

One deliberate exemption, because fail-closed must not mean fail-useless:

> **Only instruments that changed are re-resolved.** An instrument identical to the one in the
> current version keeps its pinned rules without a venue call. Otherwise a venue outage would block
> an operator from *tightening a stop loss* — turning a safety mechanism into a safety hazard.

Failure semantics: catalogue unreachable while an instrument *did* change → the publish is refused,
nothing is written, and the message says which venue could not be reached. A basket whose rules
cannot be verified is not a basket that gets published.

### 1.4 Drift after publish

Rules change under a running system, which is the case a creation-time fetch does not cover.

- **Startup preflight** (all modes) re-resolves every configured instrument and compares against the
  pinned document.
- A difference emits a `RISK_EVENT` naming the field, the pinned value and the venue's value. Per
  decision 5, live and paper then **halt the affected basket**, exactly as a failed startup step
  does today ([DESIGN §8.2 step 5](../DESIGN.md)); sim logs it and keeps cycling. The operator
  clears a halt by re-publishing the basket, which re-resolves.
- The same check runs on the periodic reconciliation tick, so a mid-soak filter change is caught
  within minutes rather than at the next restart.
- **The sim snapshot must be a real `exchangeInfo` capture, not invented numbers.** `min_notional`
  is the one rule that decides whether an order exists at all — a Tier-2 shrink below the minimum
  becomes a veto (DESIGN §6.6). Set it too high in sim and everything vetoes, which is obvious; set
  it too low and the veto path is never exercised, which is not. This is why the snapshot is
  recorded rather than written by hand, and it is the practical reason sim can afford to be lenient
  about drift.

Add the row to the DESIGN §8.1 failure table: *venue changed a trading filter → preflight/recon
comparison → `RISK_EVENT` + halt affected basket; cleared by re-publishing.*

---

## Section 2 — Providers and Seats, reorganised

### 2.1 What is worth borrowing from Charles River

Four patterns, and each answers a specific complaint about the current page:

| CRD pattern | Applied here |
|---|---|
| Master–detail: a list drives a detail pane | Seats become a list; the selected seat's fields fill the pane |
| Tabbed detail sections | Champion/Challenger, then Seats/Providers |
| Reference data is read-only, stamped with source and as-of | §1's resolved instrument fields |
| Setup workspaces are separate from operational blotters | Quarantine leaves Settings (Other 2) |

### 2.2 The shape

```
Basket: demo                            draft — not published    [ Publish ]
┌─────────────┬────────────────────────────────────────────────────────────┐
│ Identity    │  ┌ Champion ─┬ Challenger ┐                                │
│ Instruments │  │ ┌ Seats ──┬ Providers ┐                                 │
│ Schedule    │  │ │                                                       │
│ Data        │  │ │  technical    openrouter · deepseek-r1  ⚠ homogeneous │
│ ▸ Panel     │  │ │  news         openrouter · llama-3.3                  │
│ Risk        │  │ │  skeptic      lmstudio  · qwen3         [+ add seat]  │
│             │  │ │ ──────────────────────────────────────────────────    │
│             │  │ │  seat "technical"                                     │
│             │  │ │  role · evidence · temperature · devil's advocate      │
│             │  │ │  fallback chain:  lmstudio · qwen3      [+ add]       │
└─────────────┴──┴─┴───────────────────────────────────────────────────────┘
```

- **Left rail** = section selector. Six entries, replacing six `<h2>`s in a 64-field scroll.
- **Panel → Champion | Challenger** replaces the two identically-headed copies. The Challenger tab
  reads *"no challenger configured"* with an **Add challenger** button when `panel_id` is blank.
- **Champion → Seats | Providers** separates the two lists that currently interleave.
- `_panel.html` stays **one macro** — [ADR 0018](adr/0018-a-challenger-panel-is-evaluated-on-the-champions-snapshot.md)'s
  reasoning is untouched; the macro simply renders into a tab shell and takes a `label`.

### 2.3 The constraint that makes this safe

> **A tab may hide inputs. It may never omit them.**

The form round-trips the whole document, and `nest()` drops absent fields
([forms.py:68-79](../tradebot/dashboard/forms.py#L68-L79)), so a tab that conditionally renders its
contents *deletes that part of the basket on save*. This is the same hazard `_panel.html`'s header
comment already warns about, one level up.

Implementation: `<input type="radio" class="tab-toggle">` plus `:checked ~` CSS. No JavaScript, every
input stays in the DOM, and it degrades to a plain long page if CSS fails to load. This constraint
goes into CLAUDE.md's conventions, because the next person adding a tab will not read this document.

Tab state is echoed back through non-`doc.` control fields — `ui.section`, `ui.panel`, `ui.seat` —
so a draft round-trip re-renders on the tab the operator was on. `nest()` already ignores anything
outside the `doc.` namespace, so no parser change is needed.

### 2.4 Two things the new shape can show that the old one cannot

- **Homogeneity, at configuration time.** When two seats resolve to the same provider+model, flag it
  in the seat list. Today `PANEL_HOMOGENEOUS` is only a runtime event; heterogeneity is a design
  control ([DESIGN §6.5, L5](../DESIGN.md)) and losing it should be visible while it is being
  configured, not after a cycle ran.
- **Provider usage.** *"used by 2 seats"* on each provider row. `PanelConfig` already refuses a seat
  bound to an undeclared provider; showing the count stops the operator discovering it at publish.

---

## Section 3 — Scroll position

Falls out of §2 and is not a separate mechanism.

- `row_buttons` gains `hx-post="{{ action }}" hx-target="#basket-form" hx-select="#basket-form"
  hx-swap="outerHTML"`. htmx does not scroll on a swap unless told to, so position is preserved.
- **`formaction` stays alongside it.** With JS off the button still performs today's full POST, so
  this is progressive enhancement and the no-JS path is unchanged. The server keeps returning the
  full page; `hx-select` picks the form out of it. No new partial template, no route change.
- `ui.*` restores the tab, so "add seat" returns you to the Seats tab with the new row in view and
  its first input focused.
- **Sticky header bar** carrying `draft — not published` and the Publish button. Today Publish is 60
  fields below the fold while `add instrument` and `remove` sit at eye level looking like commits;
  an operator who edits a stop multiple, clicks `add fallback`, then navigates away loses the edit
  silently. Add a `beforeunload` guard for the same reason.

---

## Other 1 — the smaller correctness items

- **`venue`, `base_currency`, `quote_currency`, `asset_class` become resolved and read-only.** Falls
  out of §1.2. Removes the free-text `venue` typo and the mismatched-`quote_currency` failure that
  today raises out of `_quote_currency` ([app.py:721-728](../tradebot/app.py#L721-L728)) at wiring.
- **Multi-selects become checkbox groups.** `timeframes` (4), `indicators` (8) and `news_sources` (3)
  are `<select multiple>` ([_fields.html:48-61](../tradebot/dashboard/templates/_fields.html#L48-L61)),
  a control where a stray click silently deselects everything — for `indicators` that means quietly
  publishing a basket that computes nothing. A new `f.checkboxes()` macro replaces `f.multi()`, which
  is then retired: after this phase and Other 2, it has no callers. `nest()` already handles repeated
  `path[]` keys, so the parser is untouched; keep the hidden empty sentinel.

## Other 2 — quarantine leaves Settings

Quarantine is already fully built on the workspace — `_controls.html`, `_rc.html`, `_blotter.html`,
and the second-click confirmation in `_notices.html` when the scope holds a position
([dock.py:58-62](../tradebot/dashboard/dock.py#L58-L62)). Settings offers the same act through a
multi-select with **no** held-position guard, and where a stray click *releases* a quarantine. There
is no reason for the second surface.

### The hazard, stated before the change

Deleting the two controls is not sufficient — it is actively dangerous. The form is the whole
document and `nest()` omits absent fields, so `risk_policy.quarantined` would fall back to `False`
and `quarantined_instruments` to `()`. **Every publish from Settings would silently release every
quarantine in force**, including one set on the workspace ten seconds earlier.

### The change

Quarantine stops being one of the form's fields, and `publish_basket` re-attaches it from the
**latest stored version of the basket being published** — read at publish time, not carried in a
hidden field, so a quarantine set on the workspace while the form was open survives:

```python
policy = basket.risk_policy.model_copy(update=quarantine_of(current_record))
```

Four rules:

| Case | Behaviour |
|---|---|
| New basket, or the id was renamed in this edit | No prior version → empty quarantine. Correct: it is a different basket. |
| Whole-basket `quarantined` flag | Carried over unconditionally. |
| A quarantined instrument was removed in this edit | Its key is dropped from the carried set and **reported in the publish result** — otherwise `Basket._check_quarantine` refuses the document with a message about a key the operator never typed. |
| Nothing quarantined | No-op. |

Settings still *tells the truth* about what it is editing: the Instruments list shows a
`quarantined` pill, read-only, linking to the workspace.

### Tests (must-have, not nice-to-have)

- publishing an unrelated edit from Settings while an instrument is quarantined leaves it quarantined
- ditto for a whole-basket quarantine
- a quarantine set on the workspace *after* the form was opened survives the publish
- removing a quarantined instrument publishes cleanly and reports the dropped key
- a renamed basket starts with no quarantine

---

## Recommendations beyond the ask

1. **ADR 0025 — instrument trading rules are venue reference data.** §1 moves where a risk input
   comes from; that is exactly what an ADR is for. It should also record decision 5 (drift refuses in
   every mode) and its divergence from the sim/paper degraded-running stance.
2. **CLAUDE.md convention: "a tab may hide inputs, never omit them."** One line, beside the existing
   "both panels are edited by one macro" note. This is the rule the next contributor will break.
3. **Do not split the form into multiple POSTs.** Tabs are presentation. Separate submits per section
   would reintroduce the partial-document failure at a larger scale than any of the bugs this phase
   fixes.
4. **`f.multi` retires with this phase.** Leaving one caller behind keeps a control whose failure
   mode is silent deselection.
5. **Deferred, deliberately**: ISIN resolution (decision 6), an Alpaca catalogue (needs the equity
   gateway Phase 3 did not build), and a full security-master abstraction. Each is a real want; none
   is a blocker for the two problems this phase exists to fix.

## Slices

Ordered so that safety lands before presentation, and so each slice leaves a working system.

**Slice A — the catalogue** ✅ **shipped** (no UI change). `InstrumentCatalogue`, `VenueCatalogue`, `SimCatalogue`,
the committed sim snapshot, the contract suite, `Application.catalogue`, and `demo_basket()` reading
its rules from the catalogue instead of literals. *Exit: sim and Binance pass one contract suite; no
trading rule is a literal outside the snapshot.*

**Slice B — verification** ✅ **shipped** (no UI change). Publish-time re-resolution with the changed-instruments-only
exemption; preflight and recon drift check with its mode-keyed severity; the `RISK_EVENT`; the
DESIGN §8.1 row. *Exit: a basket whose `min_notional` disagrees with the venue cannot be published in
any mode, and a mid-run filter change halts the basket in live and paper while sim says so and runs
on.*

**Slice C — the settings workspace.** Tab shell and `ui.*` round-trip, section rail, Champion/
Challenger and Seats/Providers tabs, seat master–detail, the Look up button and resolved read-only
fields, htmx row buttons, sticky publish bar, homogeneity and provider-usage indicators. *Exit: a
basket is created end to end by typing one identifier per instrument; no add/remove button moves the
scroll position; `tests/scenario/test_dashboard_lifecycle.py` still passes unchanged.*

**Slice D — cleanup.** Quarantine removal with carry-over and its five tests; checkbox groups; `f.multi`
retired. *Exit: publishing from Settings can no longer change a quarantine in either direction.*

Slice D's quarantine work is independent of A–C and can be pulled forward if the silent-release path
is judged urgent — it is a latent bug today only because Settings does not yet omit the fields, but
it becomes live the moment anyone removes them.

## Risks

- **Publish-time resolution adds a venue call to a write path.** Mitigated by the
  changed-instruments-only rule and by caching `list_markets` behind the existing single-flight
  budget (ADR 0008). Worth measuring on the first Binance publish.
- **The committed sim snapshot is a checked-in copy of someone else's data** and will age. It carries
  an `as_of`, sim is not evidence for promotion, and the drift check does not run against it — but a
  refresh should be part of the periodic maintenance the soak already needs.
- **Paper is strict and sim is not, and both are called "the sim venue".** The distinction rests on
  `CYCLE_STARTED`'s venue stamp being the promotion report's filter, which is easy to lose track of
  when reading the code. Slice B should name it in a comment where the severity is chosen, not only
  here.
- **The tab shell is new CSS on the one page that edits risk limits.** `test_dashboard_auth.py` and
  the lifecycle scenario both walk this page; neither asserts layout. Slice C should add a test that
  every `doc.`-prefixed field present before the redesign is still submitted after it — the concrete
  form of the "never omit" rule.

---

## What implementation changed

Three decisions taken while building Slices A and B, recorded here because the plan above assumed
otherwise.

1. **The periodic drift check runs on the supervisor's resync sweep, because there is no periodic
   reconciliation tick.** §1.4 said "the same check runs on the periodic reconciliation tick".
   There is none: `Reconciler.reconcile()` is called only from `StartupSequence`, and DESIGN §6.8's
   "every M minutes" was never built. Rather than build a venue-diffing loop inside a phase about
   reference data, `DriftWatch` hangs off `Supervisor.serve`'s existing sweep — the only loop in
   the process that outlives every cycle. **The missing periodic reconciliation is still missing**
   and remains a gap against DESIGN §6.8.

2. **The seeded demo basket stays on the simulated venue in every mode**, resolved from
   `sim_catalogue()`. Seeding needs a catalogue *before* the venue stack exists, and building one
   early for `--broker binance` would mean a second Binance transport with its own rate budget —
   which ADR 0010 forbids. It is also the honest reading: `demo_basket` is a demonstration. A fresh
   database wired to a real exchange gets a basket the drift check names as foreign-venue, exactly
   as `control/readiness.py` already did in live, instead of a basket that auto-appears for a
   venue holding real money.

3. **The sim capture is thirty curated pairs, not the venue's whole listing.** Binance publishes
   3,680 symbols and 17.5 MB of `exchangeInfo`; even reduced to the eight `VenueMarket` fields the
   tradable set is ~1,400 rows. Thirty liquid pairs is 8 KB — a file whose `min_notional` values
   someone will actually read, which is the only reason a committed capture beats a hand-written
   one. A symbol outside the set is refused as a venue refuses one it does not list.

Two adjacent defects were fixed because the same code was being restructured, and both are noted
in ADR 0025's consequences: `--broker binance` was building a second Binance transport with an
independent rate budget (ADR 0010), whose HTTP session was never registered for shutdown; and
`VenueMarketData.instruments` held a second implementation of "resolve a symbol against
`fetch_markets`", which now delegates to the catalogue.

One behaviour changed outside the plan's scope, deliberately: **`run --once` now exits 3 when a
basket in service is halted.** The check moved a failure that used to surface as a failed cycle
(exit 4) into a basket halt at startup, and a `--once` run that cycled nothing while exiting zero
would tell a supervisor script the soak is fine while it produces no evidence at all.
