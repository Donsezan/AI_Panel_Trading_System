# CLAUDE.md

Guidance for working in this repository. [DESIGN.md](DESIGN.md) says *what* to build;
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) says *in what order and to what standard*.
Both are authoritative — this file only records conventions.

## Commands

```powershell
.\check.ps1            # format, lint, mypy, tests, coverage gates — run before every commit
.\check.ps1 -Fix       # same, but applies formatting and safe lint fixes first
.venv\Scripts\python.exe -m pytest tests/unit -q
.venv\Scripts\python.exe -m pytest -m scenario
.venv\Scripts\python.exe -m pytest -m contract      # one suite over every adapter
.venv\Scripts\python.exe -m tradebot run --mode sim --once
.venv\Scripts\python.exe -m tradebot run --mode sim            # supervised; each basket on its schedule
.venv\Scripts\python.exe -m tradebot risk status --mode sim
.venv\Scripts\python.exe -m tradebot config list --mode sim
.venv\Scripts\python.exe -m tradebot config history basket demo --mode sim
```

The dashboard serves the baskets alongside it. Auth is mandatory — including on localhost — and
the server **refuses to start** without a token ([ADR 0014](docs/adr/0014-the-dashboard-is-vendored-and-always-authenticated.md)):

```powershell
$env:TRADEBOT_DASHBOARD_TOKEN = "at-least-sixteen-characters"
.venv\Scripts\python.exe -m tradebot serve --mode sim                 # dashboard + supervisor
.venv\Scripts\python.exe -m tradebot serve --mode sim --observe       # comes up stopped; Start from Control
```

Rotating `TRADEBOT_DASHBOARD_TOKEN` and restarting invalidates every session — the cookie's
signing key is derived from it, and the session has no expiry. A non-loopback `--host` needs
`--allow-remote` on top of the token (PLAN §3.3).

`run --once` exits non-zero (4) if any basket's cycle failed; a supervised run absorbs the
failure, backs off, and halts the basket after three in a row.

Paper runs live market data against `SimBroker` by default; a real venue is opt-in and needs
mode-specific keys in the environment (`BINANCE_TESTNET_API_KEY`, `ALPACA_PAPER_KEY_ID`, …):

```powershell
.venv\Scripts\python.exe -m tradebot run --mode paper --once
.venv\Scripts\python.exe -m tradebot run --mode paper --once --broker binance
.venv\Scripts\python.exe -m pytest -m smoke          # read-only, hits the real test venues
```

The validation ladder is two commands, and both write a Markdown file under `reports/` rather
than printing — a promotion report is filed with the decision it justified:

```powershell
.venv\Scripts\python.exe -m tradebot catalogue fetch          # re-record the sim venue's rules
.venv\Scripts\python.exe -m tradebot backtest fetch --symbol BTC/USDT `
    --since 2026-01-01 --until 2026-06-01 --out data\history   # public, read-only, no key
.venv\Scripts\python.exe -m tradebot backtest run --mode sim --data data\history
.venv\Scripts\python.exe -m tradebot report promotion --mode paper   # exit 5 if a gate fails
.venv\Scripts\python.exe -m tradebot report shadow --mode paper      # champion vs challenger
```

Backups are taken daily by a running process, before any migration that will move the schema
revision — **a backup that cannot be taken stops the upgrade** — and on demand. Nothing is ever
auto-deleted; the restore drill is [docs/OPERATIONS.md §4](docs/OPERATIONS.md):

```powershell
.venv\Scripts\python.exe -m tradebot maintenance backup --mode sim   # exit 6 if refused
.venv\Scripts\python.exe -m tradebot maintenance status --mode sim
.venv\Scripts\python.exe -m tradebot maintenance compact --mode sim  # one pass now
.venv\Scripts\python.exe -m tradebot maintenance compact --mode sim --older-than 45 --keep-days 120
$env:TRADEBOT_BACKUP_DIR = "D:\tradebot-backups"   # a copy beside the database survives a bad
                                                   # migration, not a bad disk
```

`maintenance` is the only command that may be pointed at a database another process has open: it
wires no `Application` (a second writer is the thing `SingleWriter` exists to prevent) and opens
the database through `open_database`, which does **not** migrate — the point of copying `live.db`
before a release is to have a rollback point *for* that release.

Ops alerts are **on exactly when a destination is configured** — there is no flag, so a soak
cannot be started with alerting forgotten. Destinations are credentials and live in the
environment, never the database ([ADR 0019](docs/adr/0019-alerts-are-a-log-tail-with-a-persisted-cursor.md)):

```powershell
$env:TRADEBOT_ALERT_WEBHOOK_URL  = "https://hooks.example/..."
$env:TRADEBOT_TELEGRAM_BOT_TOKEN = "..."   # both of these, or neither: half-configured refuses
$env:TRADEBOT_TELEGRAM_CHAT_ID   = "..."
```

Clearing a safety state is a human act and needs the typed phrase:

```powershell
.venv\Scripts\python.exe -m tradebot risk rearm  --mode sim --confirm "RE-ARM TRADING"
.venv\Scripts\python.exe -m tradebot risk unhalt demo --mode sim --confirm "RE-ARM TRADING"
```

So is arming live, which is a different phrase authorising a different thing. Live is **wired and
disarmed**; the whole procedure is [docs/OPERATIONS.md](docs/OPERATIONS.md):

```powershell
.venv\Scripts\python.exe -m tradebot risk arm-live --mode live `
    --max-notional 50 --confirm "I ACCEPT REAL MONEY RISK"
.venv\Scripts\python.exe -m tradebot risk status --mode live      # state, arming, limits in force
.venv\Scripts\python.exe -m tradebot run --mode live --broker binance `
    --panel free --confirm "I ACCEPT REAL MONEY RISK" --once
.venv\Scripts\python.exe -m tradebot risk disarm-live --mode live --reason "..."
```

The same procedure is on the dashboard's Control page, with the phrase retyped for each act.
`serve --mode live` comes up unarmed rather than refusing, so there is something to arm from
([ADR 0021](docs/adr/0021-live-arming-and-supervision-move-to-a-runtime-gate.md)); `run --mode
live` still refuses on the spot.

Schema changes go through Alembic — never `create_all`. After editing
[persistence/schema.py](tradebot/persistence/schema.py):

```powershell
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "what changed"
```

Review the generated file: autogenerate does not see data migrations, and it renders custom
column types fully qualified (the template imports `tradebot.persistence.schema` for that).
`create_database` upgrades to head on every start, including for a fresh database — and takes a
backup first when that upgrade will actually move the revision **and** the database already holds
tables. A file the same call created has nothing to lose; a file with tables but no
`alembic_version` is copied, because it cannot be told apart from a real ledger whose version table
was lost.

`decision_lab` is a separate tool with its own entry point and its own gate — never a
`tradebot` subcommand ([docs/superpowers/specs/2026-08-23-decision-lab-design.md](docs/superpowers/specs/2026-08-23-decision-lab-design.md) §2.1):

```powershell
.venv\Scripts\python.exe -m decision_lab dataset verify --data data\history   # --repair re-asks the venue
.venv\Scripts\python.exe -m decision_lab dataset days   --data data\history   # pin the nine calibration days
.venv\Scripts\python.exe -m decision_lab corpus build --data data\history --every 8h --reference-panel sim
.venv\Scripts\python.exe -m decision_lab report --corpus <corpus_id>          # writes decision_lab\reports\*.md
.\decision_lab\check.ps1                                                      # its own format/lint/mypy/tests
```

Both gates must pass: `.\decision_lab\check.ps1` **and** the root `.\check.ps1`.

Dependencies are hash-pinned. After editing `pyproject.toml`:

```powershell
python -m uv pip compile pyproject.toml --generate-hashes -o requirements.lock
python -m uv pip compile pyproject.toml --extra dev --generate-hashes -o requirements-dev.lock
```

## Non-negotiables

These are the prime directives from PLAN §1. Code that violates one is wrong even if it works.

1. **Fail closed.** Every uncertainty resolves to *no trade*. Missing a trade costs nothing we
   can measure; an unintended order costs money and possibly an account.
2. **No LLM output reaches a venue unvalidated.** The panel emits a proposal; deterministic,
   unit-tested code decides whether anything happens and at what size.
3. **The venue is the source of truth.** Local state is a reconciled projection, never authority.
4. **Never submit without a durable, committed record first** — including the `client_order_id`.
5. **Live mode is opt-in, loud, and capped.** It cannot be reached by a default or a typo.

## Conventions

- **Money is `Decimal`, always.** Use `tradebot.core.money`; never `float`, never
  `Decimal(some_float)`. The one sanctioned crossing is `money.from_measurement`, for indicator
  output. Enforced by `tests/unit/test_money_discipline.py` (see [ADR 0001](docs/adr/0001-decimal-only-money-arithmetic.md)).
  A money field fails in two ways that need opposite handling, and `schema.parse_money` is where
  they part: a `float` raises `MoneyError`, because only our own code can put one there, while
  unreadable *text* — an operator typing `0,5` into a limit — is re-raised as a `ValueError`.
  pydantic converts only `ValueError`, so a validator raising `MoneyError` escapes the model
  entirely and reaches the operator as a 500 that names no field and loses their draft.
- **Time is UTC-aware `datetime`, from an injected `Clock`.** Naive datetimes are rejected at
  the model boundary. Never call `datetime.now()` directly in library code — `freezegun` cannot
  freeze `loop.time()`, so a component that reads the ambient clock cannot be tested.
- **Errors are classified**: `RetryableError` / `FailClosedError` / `FatalError`. The class is
  the handling instruction. A bare `except: pass` is a defect.
- **Every state change emits an event.** The event log alone must be able to reconstruct a
  module's state — that is the audit artifact, not a debugging aid.
- **A limit that a restart can clear is not a limit.** Cooldowns, daily caps, loss streaks and
  the kill switch are read from the database, never counted in memory ([ADR 0005](docs/adr/0005-risk-state-and-history-are-persisted.md)).
  Anything derivable from the log is derived, not cached in a field that drifts.
- **Prefer dispatch over branching.** Side-dependent rounding is a `dict[Side, str]`, not an
  `if`. Enum behaviour lives on the enum (`Action.is_tradable`, `OrderState.is_terminal`).
- **Comments explain *why*.** The spec sections they implement are cited (`DESIGN §6.6`,
  `PLAN §2.3`) so a reader can find the reasoning. Don't restate what the code says.
- **Docstrings state failure semantics** at module level: what happens when this module's
  dependency is down.

## Layering

```
core/         depends on nothing
interfaces/   depends on core           — protocols only, no implementations
venues/       one raw transport per venue, shared by marketdata/ and execution/brokers/
everything    depends on core + interfaces
app.py        the only module that imports concrete classes
```

Nothing outside `app.py` may import a concrete adapter. If a module needs a broker, it takes a
`BrokerAdapter`.

## Testing

| Rung | What | Where |
|---|---|---|
| 1 | Unit + property | `tests/unit/` |
| 2 | Adapter contract — one suite, every adapter | `tests/contract/` |
| 3 | Scenario / chaos — full loop, fault injection | `tests/scenario/` |

Coverage gates are CI-enforced by `scripts/coverage_gate.py`: `core/`, `risk/`, `execution/`,
`ledger/` ≥ 95%; everything else ≥ 80%.

Tests assert *behaviour under failure*, not just the happy path. For every failure row in
DESIGN §8.1 there should be a test asserting the documented response.

**Never write to the database while a background task reads it in a test.** `create_database(None)`
— the in-memory engine the suite runs on — uses `StaticPool`, so every connection *is* one shared
SQLite connection. A reader returning it to the pool issues a `ROLLBACK` that discards the writer
thread's open transaction: `await store.append(...)` returns having written nothing, silently. A
file database gives each checkout its own connection and is unaffected, so this is a harness trap,
not a production one — but a test that races it is testing the harness. Drive the reader
explicitly (`hub.drain()`, `hub.broadcast()`) instead of waiting for its loop to notice.

## decision_lab — grading the panel's judgement

A separate top-level package, not a bot phase. The bot can say what happened to the money; it
could never say whether a decision was *right*, which mixes good judgement with good luck. This
scores decisions against what the market did next, over recorded history, per regime and per seat.
Specced in [docs/superpowers/specs/2026-08-23-decision-lab-design.md](docs/superpowers/specs/2026-08-23-decision-lab-design.md);
five slices, of which **A (integrity, day set, corpus), B (regimes, scoring, per-seat, report) and
C (the sweep) have shipped**. D (calibration) and E (news) are not built, and only E touches
`tradebot` at all.

```
dataset.py             audit recorded history, repair holes, refuse an unverified dataset
  calibration_days.py  the nine pinned days, drawn per pool from the reference instrument
  corpus.py            one reference pass through the unmodified BacktestHarness -> corpus.db
    records.py         fold each cycle out of that log: snapshot, decisions, votes, outcome
    regimes.py         label every bar NORMAL | SHOCK_UP | SHOCK_DOWN, named windows overriding
    candidates.py      TOML matrix -> validated PanelConfig -> Basket, refused before any spend
      scoring.py       the long-only truth table, the ATR band, five verdicts, per-regime metrics
        seats.py       each seat against the same truth; round 0 vs final; swing; contribution
      sampling.py      the stratified sample a sweep draws its corpus entries from
        sweep.py       N candidates over one corpus: cache, budget ceiling, resume
        compare.py     the cross-candidate ranking and the pairwise agreement matrix
          registry.py  every run kept, so two setups are compared rather than remembered
          render.py    Markdown to decision_lab/reports/, never printed
```

Rules that are easy to get backwards:

- **Nothing under `tradebot/` may name `decision_lab`**, and the bot's CLI is untouched — a tuning
  tool reachable from a live process by accident is one an operator reaches by accident.
  `test_separation.py` enforces it, and `git diff --stat main -- tradebot/` staying empty is a
  slice exit criterion. Slice E is the one sanctioned exception, and its seam and its guard tests
  are **one change, never two**.
- **No `float`, anywhere**, enforced by `test_discipline.py` — the same rule
  `test_money_discipline.py` gives the bot, applied to a package that is entirely arithmetic.
- **The truth label is long-only aware, and it is the thing easiest to get backwards.** Tier-1
  refuses a short, so standing aside from a fall *while flat* is **correct**, not a missed
  opportunity. Scored the other way the tool systematically punishes the conservative behaviour
  the bot exists to have, and `SHOCK_DOWN` becomes a period it is doomed to fail rather than a
  test it can pass. While *holding*, the same fall demanded an exit.
- **`SHOCK_UP` and `SHOCK_DOWN` are never pooled, and every regime row is always rendered.** They
  ask opposite questions of a long-only system — did the seats catch the move, did they protect
  capital — and a blended figure hides both. An absent `SHOCK_DOWN` row reads as *not measured*,
  which is the opposite of *never happened*.
- **The band is `k × ATR` read off the frozen snapshot, never recomputed**, so a verdict is
  derived from exactly the evidence the panel had rather than from a better view of the same
  market. Unscorable is a **verdict with a reason** — gap, horizon, or no ATR — never a drop: a
  run that dropped them reports accuracy over a subset it chose after the fact.
- **Round 0 is reported beside the final vote.** Under `blind_then_debate` a seat's later votes
  are contaminated by its peers *by design*; "which seat reasons well" and "which seat is easily
  talked round" are different questions and one column answers neither.
  `reach_consensus` is **imported** for the swing rate, never reimplemented — a second consensus
  rule would make the measurement a measurement of the copy — and the counterfactual drops the
  seat from the `PanelConfig` too, because `required_votes` is `ceil(majority × seat_count)` and
  leaving it alone asks "what if this seat had abstained", a different question.
- **A corpus is reused at its identity, never rebuilt** (`corpus._existing`), so re-running
  `corpus build` with the same arguments returns the existing pass and exits 0 — including a
  short one that auto-paused. Delete the directory to retry. `decision_lab/workspace/` and
  `decision_lab/reports/` are gitignored, so none of this is visible from the repo.
- **A reference pass is not reproducible and can die on an open bot defect.** `SIM_PANEL`'s
  `varied-*` seats draw from an unseeded `random.Random` and `corpus build` has no `--seed`; worse,
  a pass can reach [KNOWN_GAPS](docs/KNOWN_GAPS.md) §5 — the monitor polls only *after* a cycle's
  order, so a stop that matched during the gap leaves the ledger holding a position the panel then
  sizes a SELL against, and the build dies on `sell of … exceeds holding …`. Measured over seven
  seeds, three did. `test_slice_b_end_to_end.py` therefore pins `STUB_SEED = 2024` through the
  `rng` seam `StubLLMProvider` documents; **delete that pin when §5 closes.**
- **A pass also ends when the basket auto-pauses.** `max_consecutive_losses` is a legitimate
  Tier-1 rule and a replay has no human to clear it, so the harness stops and the report shows how
  much of the window went unused. `ran_cycles` well below `planned_cycles` is that, not a crash.
- **A stub binding makes the run a plumbing check, and the *binding* decides — never a flag.** A
  flag would leave a registry of rows that behaved differently under identical recorded
  configuration, which is the same argument that keeps `varied-*` in panel data. An evaluation
  also refuses before spend when any declared key is missing — not merely when a seat is fully
  silenced, because a partly-reachable seat is one that answers on its backup. Deliberately
  stricter than ADR 0023: degrade-and-continue is right for a trading system and wrong for a
  measuring one.
- **A substitute model is not the panel under test, and it contaminates the whole cycle** — under
  `blind_then_debate` the peers read its arguments, and under either protocol its vote reaches
  `reach_consensus`. `on_fallback` (`halt` by default, or `exclude`) decides only whether the run
  stops; a contaminated decision is never scored either way. An **abstention is not a fallback**:
  the configured seat answered nothing, `WAIT (PANEL_DEGRADED)` is a real outcome of the real
  panel, and §9.5 already reports the degradation rate.
- **The cache is shared across matrices; the result files are not.** The key is
  `blake2s(snapshot.digest + panel_digest)` — everything that determines the answer and nothing
  else — so adding one candidate re-pays for none of the others. The `sweep-<matrix_digest>/`
  directories are scoped, because two matrices both hold a `baseline` and a flat layout would
  resume one experiment into the other's file.

### Phase 11 — the instrument master

Planned in [docs/PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md](docs/PHASE_11_INSTRUMENT_MASTER_AND_SETTINGS.md);
four slices, plus a fifth found while designing the third. **A (the catalogue), B (verification),
C (the settings workspace), D (quarantine leaves Settings) and E (basket exclusivity) have all
shipped.**

**An instrument's trading rules are venue reference data, never operator input** ([ADR 0025](docs/adr/0025-instrument-trading-rules-are-venue-reference-data.md)).
`lot_size`, `tick_size`, `min_qty` and `min_notional` decide what `quantize_order` rounds to and,
through `min_notional`, whether an order exists at all — so they come from `InstrumentCatalogue`
and nowhere else. `Application.catalogue` is **not** optional:

- **The simulated venue publishes a catalogue exactly as a real venue does.** `SimCatalogue` serves
  `marketdata/sim_markets.json`, a real `exchangeInfo` capture of thirty pairs carrying its `as_of`,
  refreshed by `tradebot catalogue fetch`. `tests/contract/test_catalogue_contract.py` runs one
  suite over every implementation, because a sim that quietly accepted what Binance refuses would
  mean the thing a soak validated is not the thing that trades (ADR 0020).
- **The catalogue answers for the venue whose *prices* are read**, not the one taking the orders. A
  paper soak is `SimBroker` fed by live Binance data: its orders reach no venue, but its lot sizes
  must be Binance's or the fills it simulates are not the ones live would get.
- **`control/reference.store_basket` is the only path that writes a basket**, and it re-resolves
  the instruments the edit *changed*. Changed-only is what keeps fail-closed from meaning
  fail-useless: a pause, a quarantine toggle or a tightened stop touches no instrument, so it costs
  no venue call and survives an outage. An unreachable venue while an instrument *did* change is a
  refusal naming the venue — a basket whose rules cannot be checked is not one that gets stored.
- **An unresolved row is refused by naming the act, not the fields** (`configure.ask_for_lookup`).
  The venue-owned inputs are `readonly` and blank until **Look up** fills them, and `nest()` omits
  empty values, so a new basket reaches the models as four `Field required` messages on `lot_size`,
  `tick_size` and both currencies — a refusal that appears to ask for the one thing ADR 0025 exists
  to prevent, on inputs the page will not let anyone type into. It is relocated onto the row's
  identifier, the only field on it a human fills in. Presentation only, and only when *none* of
  `VERIFIED_FIELDS` is present: a row carrying any venue-owned value keeps the models' own message,
  so "the venue publishes 0.00100000" still reads as itself.
- **Drift after publish scales with whether the cycles are evidence.** Startup and the supervisor's
  resync sweep re-verify everything; live and paper halt the affected *basket* (which alerts, via
  `BASKET_STATUS_CHANGED`), sim records one `RISK_EVENT` and runs on. Not because one is called
  "sim": the soak's primary venue **is** `SimBroker`, and those cycles stamp `venue: sim` and are
  what `report promotion` reads. An unreachable venue is **not** drift and halts nothing.
- **ISIN is declared and deliberately unserved.** `resolve` takes an `IdType`, validates an ISIN's
  check digit locally so a typo is caught first, then refuses with the venue's actual limitation.
  Faking a mapping would invent the identity of a tradable thing.

Two things collapsed into this seam rather than sitting beside it: `VenueMarketData.instruments`
delegates to a catalogue instead of holding a second opinion about what a venue lists, and
`--broker binance` no longer builds a second Binance transport with its own rate budget (ADR 0010).

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

**A tab may hide inputs; it may never omit them.** The basket form round-trips the whole document
and `nest()` drops absent fields, so a tab that conditionally renders its contents *deletes that
part of the basket on save* — the `_panel.html` hazard, one level up. Tabs are
`<input type="radio">` plus `:checked +` CSS: three generic rules over `radio, label, pane` triples,
which is what lets the same mechanism serve the six-section rail and the unbounded seat list, and
what makes the page degrade to one long form when the stylesheet is absent.
`test_dashboard_configure.py` asserts it two-sidedly — every `doc.` path in the stored document is
submitted, and the licensed omissions are *exactly* the two quarantine fields.

**Quarantine is not a field of the basket form.** It is an operational act and lives on the
workspace, which asks for a second deliberate click when the scope holds a position — a guard a
settings form cannot offer (ADR 0022). Because the form is the whole document, *deleting* the two
controls would have released every quarantine in force on the next publish, so `publish_basket`
drops them from the draft before validation and `carry_quarantine` re-attaches them from the
**stored** record: overwritten, never merged, so Settings cannot change a quarantine in either
direction, and a key naming an instrument the edit removed is dropped and reported rather than
refused. A multi-select is gone for the same class of reason — `f.checkboxes` replaced `f.multi`,
whose stray-click failure mode was silent deselection of everything.

### Phase 10 — the blotter workspace

Planned in [docs/PHASE_10_BLOTTER_WORKSPACE.md](docs/PHASE_10_BLOTTER_WORKSPACE.md); three passes,
**all shipped** — transport, the read side, then the control dock.

`/` is the workspace: a CSS grid of six panes over one selection, and the only screen the bot is
run from. Configure survives as Parameters/Settings, and the cycle drill-down, risk history, costs
and the realized equity curve live under Analytics (`/analytics/portfolio`, which `/portfolio`
redirects to). Four rules:

- **A pane is a template fragment with its own GET route**, refreshed in place by htmx on a custom
  `refresh` event that `static/workspace.js` dispatches when the socket names it. First paint and
  every later paint go through the *same* partial, so there is one rendering path and one set of
  filters however the request arrived. The chart pane is the one exception and carries no
  `hx-get`: an htmx swap would destroy and rebuild its canvas once a second, so it listens for the
  same event and re-fetches its own JSON.
- **Selection is a navigation, not client state.** A blotter row is `<a href="/?scope=…">`. htmx
  refreshes panes; it never selects. Reload, bookmark and socket-triggered refresh then land on
  the same view by construction rather than by keeping two copies of the selection in step.
- **Only the chart data route awaits the venue**, through the shared cache, under an explicit
  timeout, and it answers a failure with the reason and a `503` — a failed pane is information, a
  spinner that never resolves is not. Everything else is SQLite reads and configuration in memory.
- **The dashboard reads `VenueStack.read_only_prices`, never `prices`.** In the sim stack — which
  is also the *primary paper venue* — `prices` is a bridge: reading it feeds the tick to
  `SimBroker`, matching resting orders and setting the reference price of the next market order.
  An observer must never do that, so `Application.market_data` is the source *under* the bridge.
  Venue stacks set both to the same provider, because reading a real venue changes nothing.

**Control is the view.** `/control` is a 303 to `/`; the dock (⑤) and the risk-control pane (⑥)
carry every act the page did, and the `/control/*` **POSTs kept their URLs** — they are control
actions, not view fragments. Three rules follow:

- **A refusal re-renders the workspace**, through `workspace.page`, with the reason on it and the
  selection intact; a success 303s back to `/?scope=…`. Every form posts its scope in a hidden
  field, so an operator mid-incident never loses the screen they were acting from.
- **A typed phrase is a `<details>` drawer, not a modal**, and the phrase field lives inside it —
  so the only way to submit is to have opened it and typed, and the dock works with scripting off.
  The phrases are unchanged and there are four of them, because four different acts must not share
  one word. `dashboard/dock.py` owns the two the dashboard defines; `views.py` puts all four in the
  template globals, since a phrase passed per route is one some route forgets.
- **`dock.py` is pure assembly**, like `blotter.py`: a button's label is the current state
  reversed, an instrument excluded *through its basket* offers no release of its own (it would
  publish a version changing nothing), and a pause toggle never reverses a halt — that is the
  system's own doing and has its own typed act.

Floats exist in exactly one module, `dashboard/chart.py`, and only as coordinates: candle OHLC and
one marker price. Every quantity a human reads is the server's exact `Decimal` as a string.
`dashboard/` is outside the packages `test_money_discipline.py` walks, so `test_dashboard_chart.py`
asserts the boundary directly — the set of modules calling `float(` must be exactly `{chart.py}`.

**A chart bar carries one decision mark, and its colour is what came of the decision.** Both were
found on a running sim: a basket on a ten-minute grid put six decisions inside one hourly bar, and
six markers sharing a timestamp stacked into a column of green buy arrows over a portfolio that
had bought once. Two rules, both in `chart.py`:

- **Shape is what was decided; colour is what came of it.** An arrow for BUY or SELL, a tick for a
  cycle that asked for nothing; green or red when an entry order reached the venue, **amber** when
  one was decided on and none did, grey when nothing was decided. "Came of it" is the presence of
  an *entry order for that cycle and that instrument* — never the cycle's `outcome`, which is
  basket-wide: a real cycle in `sim.db` is `orders_placed` having placed ETH's order and had BTC's
  vetoed, and reading it would paint BTC as a purchase that never happened.
- **The fold is per bar, and takes the strongest outcome, not the last word.** Placed outranks
  refused outranks idle; the label carries the bar's *total* fill and its cycle count, so nothing
  is hidden, only summarised. A bar in which one cycle bought and four waited **bought**. Fills
  and cancellations are never folded — a repeated opinion is one fact, but two fills are two
  things that happened, at two prices.

Live updates are **read-only pane invalidation over a WebSocket** ([ADR 0024](docs/adr/0024-live-updates-are-read-only-pane-invalidation.md)).
`WS /ws/updates` carries `{"panes": [...]}` outward and accepts nothing:

- **The auth middleware is pure ASGI and guards `http` *and* `websocket`.** `BaseHTTPMiddleware`
  only ever sees HTTP, so a socket route behind it would have been unauthenticated by construction
  — ADR 0014's rule arriving through the door it did not cover. `lifespan` is the only exempt
  scope. `test_dashboard_auth.py` walks WebSocket routes too, and asserts every guarded scope has
  a refusal.
- **No data on the wire, so there is no second rendering path.** The refresh is an ordinary
  authenticated GET through the same templates and filters. Inbound frames are read only to notice
  a disconnect promptly, and discarded unparsed — nothing to validate because nothing is accepted.
- **No cursor to resume**, deliberately unlike `AlertDispatcher` (ADR 0019): a reconnecting page
  re-renders everything, so the tail anchors at the log's end and a missed notice costs nothing.
  A missed *alert* is not recoverable by looking at the screen, which is why that one persists.
- **`UpdateHub.register` completes the handshake**, and its order is load-bearing: anchor the
  cursor *before* accepting, so nothing appended once the client is told it is live is missed;
  join the fan-out *after*, because a notice sent mid-handshake raises and a socket that raises is
  dropped. Either half alone is a page that quietly stops updating.
- **The tail paces on `asyncio.sleep`, not the injected `Clock`** — the one sanctioned departure
  from that rule. It is a transport interval, not domain time; nothing here timestamps or ages
  anything. On a simulated clock it would be wrong twice over: a backtest stepping a month forward
  would spin it a million times, and `ManualClock.sleep` returns immediately, making it a busy
  loop. The poll interval **is** the debounce window — one tick, one notice, the union of what
  changed.
- **The tail is lazy.** No socket connected means no task and no polling, so a headless `run`, a
  closed tab and the whole suite pay nothing for it.

Selection lives in the URL (`/?scope=basket:demo`, `/?scope=instrument:demo:binance:BTC/USDT`),
never in JavaScript, so a reload, a bookmark and a socket-triggered refresh land on the same view.
Every scope carries its kind; an unparseable one is *no selection*, never a guess.

**Pane sizes are the one exception, and the only client-side state on the screen.** The workspace is
two columns of stacked panes with a draggable splitter between every neighbour — not one grid,
whose shared row tracks would make "pull the blotter taller" also move the chart. Three rules:

- **A size is a display preference, so it is not in the URL.** A bookmark reproduces a *view*; it
  must not reproduce someone's monitor. Sizes persist in `localStorage`, and an absent or junk
  entry leaves the stylesheet's defaults in place.
- **Sizes are `--size-*` custom properties on the container, never inline styles on a pane.** htmx
  replaces a pane's whole `<section>` on every refresh and would swap an inline style away within
  the second. The value is the pane's pixel extent at drag time, used as a flex-grow ratio.
- **The stylesheet carries the defaults and consumes the variables**, so a browser that never ran
  `workspace.js` still gets the designed layout. `test_dashboard_workspace.py` asserts that
  three-way contract: every name a splitter publishes is a region on the page, is defaulted in
  `app.css`, and is read back by it.

### Phase 9 — operator control

Two slices, planned together in [docs/PHASE_9_OPERATOR_CONTROL.md](docs/PHASE_9_OPERATOR_CONTROL.md).
**Both have shipped.**

**Start/stop and live arming are runtime actions** ([ADR 0021](docs/adr/0021-live-arming-and-supervision-move-to-a-runtime-gate.md)).
`SupervisionController` owns the supervisor's task, so "is anything cycling" has one answer:

- **Live's four facts are checked when supervision starts**, not when the process is wired, so
  `serve --mode live` comes up unarmed and *says so* instead of refusing before there is a
  dashboard to arm it from. `run --mode live` still refuses immediately — there is no GUI for a
  headless process to be armed from. Credentials are the one precondition that stayed at build
  time, because a transport cannot be constructed without a key and no dashboard could supply one.
- **The phrase is retyped at every arm and every start**, never cached in the session — an armed
  database alone must not be enough to start (ADR 0012).
- **`app.enforced_policy` is the single answer to "which Tier-2 limits apply"**, read fresh by the
  wiring, every runner rebuild, `risk status` and the dashboard. A cap armed mid-process reaches
  the runners at the next stop→start and can never be enforced by one caller while another reports
  the boot-time number. The `live_ceiling` clamp is written at each start, for the same reason.
- **Stop is not the kill switch.** It pauses cycling, cancels nothing, needs no phrase, and is
  never refused — an operator reaches for it during an incident. It does end the only thing polling
  open orders, so the page lists what is still working and *no order may be placed while stopped*,
  manual close included. `--observe` is the state a process starts in, not a lock.
- **The GUI's Disarm also stops supervision**, deliberately diverging from the CLI's `disarm-live`,
  which has no running process to reach into. A basket cycling against a revoked cap is the one
  silent state this must never produce.

Quarantine is an operator excluding one instrument, or a whole basket, from *automated* trading —
"I have doubts about this one, keep watching it but don't trade it". Four rules that are easy to
get backwards ([ADR 0022](docs/adr/0022-quarantine-is-a-tier-1-veto-rule.md)):

- **It is a Tier-1 veto rule, not a scheduling state.** The cycle keeps running: market data,
  indicators and the panel's deliberation are untouched, and only the resulting order is refused.
  That is the whole difference from a **pause**, which stops the cycle and so blinds the operator
  to the instrument they are making up their mind about — and from a **halt**, which is the system
  protecting itself and needs a typed phrase to clear. Quarantine is versioned configuration and
  releasing it is one click.
- **A held position stays closable by hand.** `QuarantineRule` stands aside for the existing
  ADR 0015 operator exit, and records that it did — but only when the quarantine would otherwise
  have bitten, so an ordinary manual close does not silently gain a fourth stood-aside rule. The
  dashboard asks for a second, deliberate click before quarantining a scope that holds something:
  from that moment the bot is hands-off, and inaction compounds a loss as readily as action causes
  one.
- **A whole-basket quarantine also skips the panel** (`CycleOutcome.QUARANTINED`, short-circuiting
  right after the snapshot is frozen) — there is nothing to spend a model call on when every order
  is vetoed downstream. The cycle is still *recorded*: a basket that stops appearing in the log is
  a basket nobody can audit. A *per-instrument* quarantine still pays for its panel call, which is
  deliberate — what the panel would have done while an instrument was under review is the research
  record worth having.
- **A quarantine may only name an instrument its basket holds**, enforced by `Basket`. A key
  matching nothing excludes nothing, and the operator would believe a limit is in force while the
  panel trades straight through it.

Edited in Configure (both fields) and toggled per basket *and* per instrument from Control. There
is no CLI mutation command, consistent with every other Tier-1 limit; `config list` and
`config history basket` show the state.

## Phase status

Phases 0–8 are **code-complete**, and Phase 12 has since found that one of them is not correct —
see below. What otherwise remains is not code: the paper soak (weeks of
`tradebot run --mode paper`, measured with `report promotion`) and then a human's decision to arm
live, following [docs/OPERATIONS.md](docs/OPERATIONS.md). **Live ships disarmed and I never arm it.**

### Phase 13 — retention and backup

Planned in [docs/superpowers/plans/](docs/superpowers/plans/); three pieces, **all shipped** —
A (backups) and B (retention), [ADR 0028](docs/adr/0028-retention-is-archive-then-compact.md), and
C (operator notifications), [ADR 0029](docs/adr/0029-notifications-are-a-projection-of-the-alert-rules.md).

`tradebot/maintenance/` is the one package that *writes* outside the money path, which is why it
is not part of `ops/`. One daily pass: back up, archive, compact only what was archived, delete
what has aged out. The order is the design, and the windows are a versioned `maintenance` config
document — the third `ConfigKind`, edited on the Parameters page.

Rules that are easy to get backwards:

- **No event row is ever deleted, and exactly one module updates one.** `COMPACTORS` has two
  entries — `SEAT_RESPONDED`'s `raw_text` and `SNAPSHOT_FROZEN`'s `snapshot` body — and a type
  absent from it is never touched. That registry *is* the containment story.
- **The invariant is asserted, not argued.** A projection rebuild after compaction is identical to
  one before it, tested on handmade events *and* on a real cycle driven through the actual loop. If
  it fails, the compactor is dropping a field a projector reads: fix the compactor, never the test.
- **Nothing is compacted without a *verified* archive** — re-read, row-counted and hashed, not
  merely written. Gzip's CRC catches corruption but not truncation at a record boundary.
- **Work is found by what is still heavy, never by event type.** `pending_days` selects on each
  compactor's own `heavy_key`. Selecting by type revisits every past day forever, and once that
  day's archive is deleted the next pass **recreates** it from already-compacted rows — a hollow
  archive reappearing daily, contradicting the promise that deletion is final. The cost of
  that choice is two unindexable `LIKE` scans over `payload_json`, so the call hops to a thread
  like every other filesystem step in the pass — the loop it shares with the supervisor, the
  execution monitor and the dashboard's socket may not stall behind housekeeping.
- **Compaction batches advance by `seq`, not by rows rewritten.** A seat that abstained has no
  `raw_text`, so it never gains a marker; a loop stopping on a zero rewrite count leaves a chunk of
  abstentions at the head of every batch and permanently stops compacting the day behind them.
- **A compacted cycle is shown as archived, never as empty.** The drill-down names the archive file
  and the unchanged digest. The seat transcript needs the same line and is easier to forget —
  compaction keeps the vote, thesis and cost, so it renders as *complete* unless something says so.
- **Deletion is the one irreversible act** and is the narrowest thing in the package: one mode's
  archive directory, matched by parsing each file's name. Never the database, never a backup.
- **An absent policy means the defaults, not a refusal**, and the windows are read fresh at every
  pass. A failed pass is recorded rather than raised, and still counts as the day's run.
- **Containment is per day, not per pass.** A day whose archive will not verify costs that day and
  nothing else, and `delete_aged` runs whatever the archive step did — it is scoped by file name and
  depends on none of it. Both were one `try` around the whole day loop, and a corrupt file is
  *permanent* because an existing day file is verified rather than rewritten: one of them stopped
  every day behind it and the 90-day deletion with it, so retention silently stopped while the
  database kept growing. The summary on the report is bounded, because it reaches the event payload
  *and* the notification body and one permissions fault fails every pending day at once; the full
  list is a `WARNING`.
- **A file that will not delete is a line in the daily report, not an alarm.** Spec §6.4 gives it
  its own row — reported, skipped, retried next pass — so it rides on `MaintenanceReport.undeletable`
  rather than on `failure`, which is what `ok` means. Folding it in made a locked file a HIGH
  `MAINTENANCE_FAILED`, and HIGH notices deliberately never supersede: a virus scanner holding one
  file stacked another red row every night *and* suppressed the LOW line carrying the night's real
  work.
- **`maintenance status` answers all six of the spec's questions**, and the two easiest to leave out
  are the ones an operator needs: the windows' *provenance* — "30 and 90 because nobody published
  anything" and "30 and 90 because somebody did" are different facts about how long financial
  records are kept — and the last pass, without which a dead daily tick and a healthy one look
  identical. It opens the database to answer them, which nothing else in the command family avoids:
  `open_database` never migrates, the reads write nothing, and the schema is WAL, so it stays
  pointable at a file the bot has open.

**Piece C makes the alerts `ops/rules.py` already produced visible, and nothing new decides what
an operator should be told** ([ADR 0029](docs/adr/0029-notifications-are-a-projection-of-the-alert-rules.md)).
`MAINTENANCE_RAN` joins `ALERT_TYPES` and becomes `MAINTENANCE_FAILED` (HIGH) or `MAINTENANCE_OK`
(LOW) through one new row in `RULES`. The bell is a `notifications` projection folded from
`NOTIFICATION_RAISED` and `ALERT_DISMISSED`. Rules that are easy to get backwards:

- **`enabled` gates delivery, not the tail.** It used to gate both, so on a machine with no
  webhook — the sim and paper case — the rules never evaluated at all. `poll` records
  unconditionally and `run` loops whatever is configured; only the sinks are configured-only.
- **Two cursors, because recording and delivering fail differently.** `recorded_seq` advances on
  the append, `last_seq` only after a sink took it. One cursor would also re-count the streaks:
  with delivery stalled, a second evaluation would fire `PROVIDER_FAILURE` at a different number
  on screen than in the webhook. The rules run **once**, in `_record`, which owns the streaks;
  `_drain` rebuilds the `Alert` from the payload and evaluates nothing.
- **The dispatcher must never read its own writes.** `NOTIFICATION_RAISED` and `ALERT_DISMISSED`
  are deliberately absent from `ALERT_TYPES` — either one in it is a notification about a
  notification, forever.
- **Insert and ignore on conflict, never upsert.** Recording is at-least-once and `alert_id` is
  deterministic, so a repeat folds onto the existing row; rewriting the payload columns would
  clear a `dismissed_at` and resurrect a notice the operator cleared eight minutes earlier.
- **Only quiet kinds supersede.** A new `MAINTENANCE_OK` retires yesterday's with
  `dismissed_by = "system"`; `MAINTENANCE_FAILED` never does, because hiding an unread failure
  behind today's is the one thing the list must not do.
- **The `<details>` is never swapped**, or the dropdown shuts under whoever is reading it. Its two
  regions carry their own `hx-get` and listen via `from:closest details`, because `workspace.js`
  dispatches `refresh` with `bubbles: false`. `PANES_BY_EVENT` keys it on exactly the two events
  the dispatcher appends — keying on the kill-switch trip would repaint it before the row exists.
- **Opening the bell refetches both regions**, on the `<details>`'s own `toggle` event. The counter
  and its dropdown must never contradict each other, and every path that refreshes one without the
  other produces exactly that: the counts refresh on every nudge while the list is filtered on
  `.open`, so a notice raised with the bell shut moved the counter, skipped the list, and opening
  fetched nothing — `0 | 0 | 1` above "Nothing to report" until the next notification arrived with
  the dropdown already open. The counts take it too, or a list refetched alone would be newer than
  the counter above it while the socket is down and the fallback poll is 30s apart.
- **The counts *and* the list come from `views.render`.** The bell is in the base template, so a
  page supplying only the counts renders "Nothing to report" under a counter reading two, and
  never corrects itself with scripting off or on a page without `workspace.js`.
- **Dismissal acknowledges a message and changes nothing the bot does**, and dismissing something
  already gone writes no event — one that projected onto no row would read as a dismissal that
  never happened.

### Phase 12 — one portfolio, valued in one notional currency

Planned in [docs/PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md](docs/PHASE_12_PORTFOLIO_VALUATION_AND_MIXED_ASSETS.md);
two pieces. **Piece 1 has shipped** ([ADR 0027](docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md));
it was a live defect fix, not a feature, and it blocked Piece 2. **Piece 2 — mixed crypto+equity
baskets — is not built.**

**Portfolio equity is mark-to-market, and one function answers it.** `risk.aggregate.aggregate` is
that function; `Ledger.equity` no longer exists. The ledger knows what is held, never what it is
worth. Equity is cash valued in the notional currency plus each position at its current mark, read
from `ledger.marks.Marks` — a shared cache written by every cycle's snapshot, the supervisor's
sweep, startup and a manual close. Six call sites each building their own price map out of
`avg_entry` is how the drawdown kill switch came to measure the cost basis and report 0% on a
portfolio that had halved.

Six rules that are easy to get backwards:

- **A stale mark is not a mark.** `price_of` returns `None` for absent *or* older than
  `mark_staleness_seconds`, and there is no third outcome. The fallback is a **freeze**, never cost.
  Valuing a position at what it cost is not conservative — it is differently wrong, in whichever
  direction the market moved. `test_valuation_boundary.py` asserts structurally that no module in
  `ledger/`, `risk/` or `control/` reintroduces it, the way `test_dashboard_chart.py` guards floats.
- **Freezing blocks new orders; it does not trip the kill switch.** The switch is for breaches, and
  a freeze is ignorance. Nothing is tripped, no baseline moves, no state is written, and it clears
  itself when marks return. A freeze spanning midnight leaves `day_start_equity` at yesterday's —
  the conservative direction, chosen rather than incidental. **It never blocks a reduce-only
  operator exit**, by construction: every rule reading `equity` or `basket_budget` already stands
  aside on `SELL` (ADR 0015).
- **A base asset is a position, not cash.** `value_cash`'s rungs are ordered, and rung 3 (a
  configured instrument's base asset → zero) must precede rung 4 (mark it against its own market),
  or every spot holding is counted twice. A *zero* balance in an unvaluable currency does not
  freeze: dust already converted away is not a reason to stop a live account trading.
- **A portfolio-wide limit reads the configured universe, never one basket's instruments.** Gross
  exposure, per-instrument exposure and cluster membership all take the universe; only
  `basket_exposure` stays basket-scoped. With one basket in service the two are equal, which is why
  `max_gross_exposure` silently omitted every sibling basket's positions for so long.
- **The sweep runs in two places and both are needed.** `BasketWorker.cycle` refreshes marks before
  the gate — the gate runs *before* the snapshot, so it cannot get them from the cycle, and every
  path that cycles goes through the worker. The supervisor's resync tick marks the positions of
  baskets that are **not** cycling; without it, pausing a basket that holds a position would freeze
  the whole portfolio and block every other basket. It reads `read_only_prices`, never `prices`:
  the sim stack's bridge would match resting orders, and a valuation sweep must never move the
  venue.
- **The valuation basis change is announced, never absorbed.** A high-water mark stored by an
  earlier version was recorded on cost basis. Startup records one `RISK_EVENT` naming both figures
  and changes nothing — an automatic re-baseline would silently forgive whatever unrealized loss
  was open at the moment of the upgrade. If it trips, that is a real loss, cleared by the typed
  `risk rearm`.

**Soak evidence gathered before Piece 1 landed ran under a drawdown gate that could not see
unrealized loss.** Whether those cycles still count is the operator's call, but it is a call.

**Piece 2 is still refused in four places**, all fail-closed: `app._quote_currency` (one quote
currency per process), one `VenueStack`/`Ledger`/`ExecutionService` per process,
`reference._findings_for_one`, and `_alpaca_stack` (no equity market-data provider exists). The
domain model is ready — `asset_class`, `venue:symbol` keys, an `equities` cluster, and `aggregate`
already taking `Mapping[str, Ledger]` and emitting a `VenueSlice` per venue. It was built for N
venues and is only ever called with one.

Phases 0–6: guardrails and money primitives, the sim-only walking skeleton, the
deterministic shell to full depth (order lifecycle with venue-held protective groups, the
`ExecutionMonitor`, the fills-driven ledger with round trips, the reconciler, both risk tiers,
the kill switch, and the DESIGN §8.2 startup sequence), the data layers (Binance spot market
data over ccxt with a weight-aware rate limiter and cache, the full indicator registry, and the
RSS news pipeline with dedup and point-in-time selection), the decision engine (three LLM
provider adapters, the blind-then-debate protocol, both decision modes, and the per-cycle cost
budget), and the broker adapters (`BinanceSpotBroker`, `AlpacaBroker`, `SimBroker` under one
contract suite, plus paper wiring and the live arming gate), and the control plane with its
dashboard.

### Phase 7 layering

Everything in `validation/` and `ops/` **reads**. Neither decides or trades; only the alert
dispatcher writes, and only its two cursors and the notifications it records (ADR 0029):

```
Evidence.gather(store)     folds the log's report-relevant types into counters
  promotion.evaluate       three automatic gates; the fourth is a human's signature
  Comparison.gather        pairs champion and challenger verdicts per cycle per instrument
  BacktestHarness.run      drives the real loop over recorded history, stepping the clock itself
    ReplayDataset          CSVs plus the venue trading rules they were recorded under

AlertDispatcher.poll       record, then deliver — two cursors, because they fail differently
  _record                  evaluates the rules once, appends NOTIFICATION_RAISED, owns the streaks
    ops/rules.evaluate     the six triggers, as a dispatch table over event types
  _drain                   tails its own NOTIFICATION_RAISED and delivers; evaluates nothing
    ops/sinks              webhook + Telegram over httpx, off unless configured
```

The one thing in Phase 7 that *runs a panel* is `decision/shadow.py`, which is why it lives in
`decision/` rather than `validation/` — the report about it is what lives in `validation/`.

Five rules that are easy to get backwards:

- **Reports read the log, not the projections** ([ADR 0016](docs/adr/0016-validation-reports-are-folded-from-the-event-log.md)).
  A kill switch trip and a basket halt have no projector at all, and the log is the compliance
  artifact. `EventStore.read_types` narrows to the types a report needs so a soak's snapshots and
  transcripts are never loaded.
- **A veto is not an incident.** An incident is one of five things that needed a *human*: a
  tripped switch, a halted basket, a failed cycle, an unexplained reconciliation, or an order
  still stranded in `SUBMIT_UNKNOWN` when the window closed. Counting vetoes would make the gate
  unreachable and select for a soak in which risk never engaged.
- **The evidence base is the `sim` venue.** Cycles carry their venue in `CYCLE_STARTED`; testnet
  runs are counted, shown, and excluded, as are `VENUE_RESET` reconciliations (R15). An unstamped
  cycle is `unknown` and never counted.
- **A backtest's window starts after the indicators' warm-up**, and the report prints what was
  requested beside what ran ([ADR 0017](docs/adr/0017-a-backtest-declares-its-warm-up-and-its-contamination.md)).
  Without it a replay opens with a wall of `DATA_STALE` that reads as a broken system.
- **A bar closing after the cycle's `now` is a hard error** — staleness has two directions, and the
  future one is a look-ahead leak wearing very fresh data. Enforced in `CandleSeries.require_fresh`,
  so it protects live runs too, not only replays.

Model knowledge cutoffs live in `validation/cutoffs.py` with a `source` per entry. An unknown
model reads as *contaminated*, never as clean, and the banner is on every backtest report
regardless of the verdict.

### Phase 7 pass 2 — the challenger and the alert tail

Four more rules, from [ADR 0018](docs/adr/0018-a-challenger-panel-is-evaluated-on-the-champions-snapshot.md)
and [ADR 0019](docs/adr/0019-alerts-are-a-log-tail-with-a-persisted-cursor.md):

- **The challenger runs last and never trades.** `Basket.shadow_panel` is deliberated on the
  champion's *already-frozen* snapshot after the champion has acted, and produces exactly one
  `SHADOW_EVALUATED` event — no decision, no risk check, no intent. Its cost is recorded on that
  event rather than on the cycle, so `$/decision` for the panel that traded stays true. Every
  exception it raises is caught and written into the event: the cycle's outcome is the champion's.
- **Both panels are edited by one macro** (`dashboard/templates/_panel.html`). A form rendering
  only the champion would delete a configured challenger on the first edit, because the form
  round-trips the whole document.
- **Alerting never touches the money path.** It tails the log by `seq` and advances its delivery
  cursor *after* delivery, so the guarantee is at-least-once and a fresh database starts at the
  log's end. Alert destinations are credentials: environment only, redactor-registered, never
  named in a log line. Since ADR 0029 it does *write* one thing besides its cursors — one
  `NOTIFICATION_RAISED` per alert, through `SingleWriter`, minutes apart and never in the path of
  an order intent.
- **A veto is not an alert**, for the same reason it is not an incident. "Repeated provider
  failure" is three consecutive `PANEL_DEGRADED` cycles, with the streak persisted beside the
  cursor — a streak counted in memory is a streak a restart forgives.

**Phase 6 is complete.** Pass 1 landed the control plane — versioned `ConfigStore`, `Scheduler`,
`Supervisor`, and a composition root that runs N baskets over one venue portfolio. Pass 2 landed
the dashboard: Configure, Monitor and Control over FastAPI + Jinja2 + vendored HTMX, with auth
mandatory. A basket can be created, configured, run, paused and killed entirely from the GUI, and
every action appears in the event log with `dashboard` as its actor — the PLAN §6 exit criterion,
asserted end to end in `tests/scenario/test_dashboard_lifecycle.py`.

### Phase 6 dashboard layering

```
create_dashboard(application, *, token, controller)   takes a wired Application; never builds one
  SessionMiddleware   auth by construction — a new route is protected without being told to
    routes/monitor    reads projections; the drill-down reads one cycle's events
    routes/configure  forms.py → pydantic → configs.put(); the models are the only validation
    routes/control    start/stop, live arm/disarm, pause/resume, un-halt, kill switch, manual close
```

Five rules that are easy to get backwards:

- **Auth is always on, and the server refuses to start without a token** — stricter than DESIGN
  §6.10, which only demands it off-loopback ([ADR 0014](docs/adr/0014-the-dashboard-is-vendored-and-always-authenticated.md)).
  Enforcement is middleware, not a per-route dependency: a dependency someone forgets to add is
  an unauthenticated route, and `test_dashboard_auth.py` walks every route to prove it.
- **A pause is configuration; a halt is database state.** Publishing `status: active` must never
  clear a halt the system imposed for cause — the two are different mechanisms and the UI keeps
  them apart.
- **Manual close has no side door.** It builds an `OrderIntent` and goes through the same Tier-1
  and Tier-2 engines a cycle uses ([control/manual_close.py](tradebot/control/manual_close.py)).
  The *metering* rules — cooldown, daily cap, loss streak, hourly rate — stand aside for it via
  `RiskProposal.is_operator_exit`, and each records that it did, so the exemption is visible in
  the log rather than implied by a missing veto ([ADR 0015](docs/adr/0015-an-operator-exit-is-exempt-from-metering-rules.md)).
  Correctness and venue legality are never exempt: long-only, quantization, minimums and the
  price collar still refuse. A tripped kill switch does not block a close — the switch stops the
  bot trading, not a human getting out.
- **Validation is the engine's own pydantic models.** `forms.py` turns a flat form into the
  document shape and hands it to `model_validate`; nothing restates a rule. A cross-field rule on
  a *nested* model is located on the parent field and matches no input, which is why the error
  summary shows every error rather than only the unlocated ones.
- **Never `SUM` a money column in SQL.** Money is TEXT precisely because SQLite's numeric affinity
  rounds through an IEEE-754 double; `Queries.cost_by_basket` totals in Python.

### Phase 6 layering

Configuration is data in the database, and a cycle records which version of it ran:

```
ConfigStore     versioned documents; updates add versions, nothing is overwritten
  Supervisor    one BasketWorker per basket — the only thing that cycles it
    Scheduler   next fire time: the earliest of the grid tick and the session's first cycle
      RunnerFactory   app.py builds a runner per basket *version*
```

Four rules that are easy to get backwards:

- **A basket is one versioned document**, panel and Tier-1 policy included, and a cycle pins
  `{"basket:demo": 4, "global_risk:global": 2}` ([ADR 0013](docs/adr/0013-configuration-is-versioned-and-pinned-per-cycle.md)).
  Retirement is a version too, so a deleted basket's cycles still resolve.
- **`config_versions` is not a projection.** A rebuild replays the log into the read model, and
  the log's pins resolve *against* that table — truncating it would erase the log's meaning.
- **`market_open+15m` is not a schedule kind.** It is a daily interval whose session candidate
  wins; one rule covers crypto and equities, and there is no branch on asset class.
- **An edit takes effect at the next cycle boundary.** The worker re-reads its basket each cycle
  and rebuilds the runner when the version moved. The *watchdog* outlives every cycle, so it is
  handed the new Tier-2 policy through `use_policy` rather than being rebuilt.

Baskets share one ledger, one execution service, one monitor and one watchdog, because positions
belong to the venue portfolio and not to a basket (DESIGN §4) — which is why the monitor
serializes its poll and each basket prunes only its own groups.

### Phase 8 layering — live, wired and disarmed

Live is `_assemble` with `Mode.LIVE`: the same runner, risk engines, ledger and log a soak
validated. A separate live path would mean the thing that was tested is not the thing that trades
([ADR 0020](docs/adr/0020-live-is-the-paper-wiring-minus-headroom.md)). What live adds is
*subtraction*:

```
build_live                refuses --broker sim; otherwise the paper wiring, with Mode.LIVE
  effective_policy            min(published, LIVE_CEILING) per limit, then the arming row's cap
    StartupSequence           …preflight, reconcile, resolve orders, and then:
      LiveReadiness           alerting · panel real+reachable · data complete · configs build
  SupervisionController.start the four facts of ADR 0012 — phrase, armed row, cap, credentials —
                              re-evaluated here, every start (ADR 0021)
```

Five rules that are easy to get backwards:

- **The ceiling only tightens.** `min(published, ceiling)`, so a policy an operator already
  narrowed keeps its own number, and widening past the ceiling is a source change rather than a
  dashboard edit at 03:00. Every clamp is logged *and* written as a `RISK_EVENT` — "what were the
  limits at 04:12" is answerable from the log alone, not by joining two documents against a
  constant.
- **Permission is not readiness.** All four arming preconditions can hold on a machine whose
  alerting was never configured and whose feed has been returning a holed series since the last
  restart. `control/readiness.py` is the second question, live only, and a failure leaves the
  process **up and halted** like every other startup step.
- **The panel probe is a real completion.** Sixteen tokens per seat. A socket test would pass for a
  model id that no longer resolves — R11 happening now rather than in theory. A seat answering on
  its *fallback* is a warning, not a refusal; the chain exists so an outage is survivable.
- **No seat may bind the stub in live, not even as a fallback.** That is one outage away from a
  real order sized by canned JSON. Checked before probing, because probing a stub succeeds.
- **A gap in the tape refuses.** ATR sizes every position, so an ATR computed across a hole is a
  stop distance derived from a bar the venue never published. Mid-run, the same fault becomes a
  `DATA_STALE` alert on a streak — persisted, sharing one rule with `PANEL_DEGRADED`, because both
  are "cycle after cycle deciding nothing".

Sim and paper have none of these gates, deliberately: an unreachable panel there is a `WAIT` and a
holed series is one `DATA_STALE` cycle. Running degraded is what those modes are *for*.

### Phase 5 layering

Two layers per venue, and only the lower one knows which venue this is:

```
BrokerAdapter        venue-agnostic: lifecycle, fills, groups, capabilities
  venues/*_transport signed I/O, rate budget shared with market data, error taxonomy
```

`execution/brokers/` holds `sim.py`, `binance.py`, `alpaca.py` and the trading calendars; all three
brokers run through the *same* `tests/contract/test_broker_contract.py`, each driven by a wire-level
fake speaking its own venue's JSON. An adapter whose semantics diverge fails CI — that identity is
the only thing making a paper result predictive of live behaviour.

Four Phase-5 rules that are easy to get backwards:

- **Linked exit legs go through `submit_group`, one venue call** ([ADR 0011](docs/adr/0011-protective-legs-are-submitted-as-a-group.md)).
  Where `oco_groups` is false, only a stop is placed: a take-profit is an optimisation, a double
  sell is an accidental short (R13).
- **An ambiguous *placement* is `SUBMIT_UNKNOWN`; nothing else is.** A rejection, a 429 and a ban
  are each a definite answer that nothing was placed ([ADR 0010](docs/adr/0010-one-signed-transport-per-venue.md)).
- **The self-trade check reads the venue, applies to entries only, and ignores untriggered stops.**
  A stop's limit sits below the market until it triggers; treating it as resting would veto every
  entry made while a stop is in place and leave the next position unguarded (R12).
- **Live is four preconditions in four places, one of them a database row** ([ADR 0012](docs/adr/0012-live-is-four-independent-preconditions.md)),
  and credentials come from *mode-specific* env var names, so a live key is unreachable from a
  paper run.

Adapters are stateless views of venue truth: an adapter that remembered which fills it had already
reported would advance that marker before the caller committed them, and a failure while booking
would lose them permanently.

### Phase 4 layering

The panel is data; nothing about a seat, a model, or a protocol is a constant in code.

```
DecisionEngine       decision mode + per-cycle cost budget, then the consensus rule
  DebateProtocol     blind_then_debate | single_round — rounds, anonymization, early stop
    SeatRunner       fallback chain, output contract, one repair attempt, else abstain
      LLMProvider    openai_compat | anthropic | gemini, over plain httpx
```

A `PanelConfig` is **self-describing**: it carries the `providers` it may reach *and* the seats
that reach them, so one GUI form (Phase 6) edits both and validation proves every binding resolves
before anything runs. Nothing outside `panel.providers` is ever constructed or contacted.

Real models are **off unless asked for**, like news: `--panel free` (or `local`) selects a seeded
panel; the default `stub` panel is offline, so the demo and the whole suite stay free and
repeatable.

**An unscripted stub answers in the schema the prompt asked for**, reading the symbols back out
of `prompts.symbols_requested`, because that is what a real model does with the same text. A stub
answering only the per-asset schema made `decision_mode: basket` unusable on the default panel:
every seat failed `BasketAssessment`, the repair replayed the same canned text, and the basket
resolved `WAIT (PANEL_DEGRADED)` on every cycle it ever ran. A *scripted* response is returned
verbatim — scripting a per-asset vote into a basket run is the rung-3 fault injection, and
adapting it would delete the only way to assert that a malformed answer fails closed.

**The stub serves two model families, and the seat's model name picks between them.** `stub-*`
recites `DEFAULT_RESPONSE`; `varied-*` draws a vote per instrument from
[providers/stub_responses.json](tradebot/decision/providers/stub_responses.json) and renders it
the way a real completion arrives — bare, fenced, or behind a sentence of prose. A model id only
means something to the provider serving it, and the stub is not a vendor, so the names are ours.
Four rules:

- **The switch is panel data, never a flag.** It is versioned, pinned per cycle (ADR 0013) and
  edited in Settings, so "was this cycle random?" is answerable from the log. A process-wide flag
  would leave a database of cycles that behaved differently under identical recorded
  configuration. `seat_responded` already persists each seat's `raw_text`, so *what was drawn* is
  in the log either way; what the flag would have lost is *which catalogue it was drawn from*.
- **One canned answer leaves the consensus rule unexercised**, and not because it is a stub —
  because `STUB_PANEL` has one seat, so `required_votes` is 1, no majority can be missed, no
  dissent recorded, no abstention fraction crossed, and `has_converged` ends `blind_then_debate`
  after the blind round. `SIM_PANEL` (`--panel sim`) is three `varied-*` seats over the fifteen
  entries; on a 60-cycle run it reaches BUY, SELL, HOLD, and `no qualified majority` → WAIT, with
  twelve distinct convictions and the early-stop path firing on a few cycles.
- **A script outranks the model name.** Scripting is the fault-injection path and stays verbatim
  whatever the seat is called, so the catalogue holds only *valid* votes — malformed JSON and
  `FAIL` remain scripted.
- **The draw is per instrument, not per call.** One draw shared across a basket's symbols would
  make every basket-mode answer internally unanimous — the same flatness, one level down.

A hand-edited catalogue that no longer parses raises `ConfigError` naming the file, rather than
becoming a seat that abstains on every cycle for reasons only the event log could explain. It is
read once per process, so an edit takes effect at the next start.

**Each seat has its own fallback chain** — an ordered list of `(provider, model)` bindings, not
provider names, because a model id only means something to the provider serving it. Two rules are
enforced at construction, not at runtime: a chain may not repeat a binding, and every binding must
name a declared provider. Seeded chains leave the vendor entirely and end at a local runtime, and
each seat gets a *different* backup so one outage cannot collapse heterogeneity — see
[ADR 0009](docs/adr/0009-llm-providers-over-plain-http.md).

**A provider whose `secret_ref` names an env var that is not set is *unreachable*, not *invalid***
([ADR 0023](docs/adr/0023-a-missing-provider-key-degrades-the-panel.md)). It is left unwired and
reported by `reach_of`; the seats bound to it fall back, and a seat whose whole chain is unwired
abstains, so the cycle resolves `WAIT (PANEL_DEGRADED)` — the DESIGN §8.1 response to a provider
being down. Sim and paper run on and *say so* (a warning, one `RISK_EVENT` at wiring, and a banner
on every dashboard page); **live refuses**, at startup in `control/readiness.py` and again at every
Start in `SupervisionController.blockers`. Refusing to boot would cost the operator the dashboard,
the log and the ledger view — the three things needed to fix it. The banner names the variable to
set: keys are environment-only, so no GUI field may ever accept one.

**A seat's standing instruction is operator text, and it sits *above* the rules it cannot relax.**
`SeatConfig.instruction` is free text edited per seat in Settings and rendered into that seat's
system prompt beneath the role line and before "Rules you must follow" — so the sizing prohibition
and the JSON schema frame it rather than follow it. It is *not* delimited the way news and peer
arguments are: those arrive from outside and are attacker-visible, while this was typed by the same
operator who sets the risk limits (same trust class as `role`). Three consequences:

- **The wording is versioned configuration, not code.** A cycle pins its basket version (ADR 0013),
  so "which instruction was this decision made under" is answerable from the log, and `ConfigStore`
  refuses a secret pasted into it like any other field.
- **An empty instruction must contribute nothing**, not even a blank line — every panel stored
  before the field existed has one, and their prompts must not move. Asserted directly.
- **The shared `_panel.html` macro gives the challenger the same field**, which is how two wordings
  are compared: run one as `panel` and the other as `shadow_panel` and read `report shadow`. The
  textarea's value is its element *body*; a macro copied from `f.field` would render an empty box
  that still posts its name and would clear every instruction on the next publish.

Model ids in `decision/presets.py` need verifying against openrouter.ai/models before a real run;
free slots churn (R11), which is what the fallback chains are for.

Cost is attributed to a **provider call**, not to a response: in `basket` mode one call answers
for every instrument, so anything totalling money goes through `total_cost`, which de-duplicates
by `call_id`.

### Phase 3 layering

Market data is four independently testable layers; only the bottom two know which venue this is:

```
CachingMarketData      one venue call per bar interval, single-flight
  VenueMarketData      venue-agnostic: point-in-time cutoff, observed_at, gap reporting
    BinanceSpotGateway Binance wire format → exact decimals (no I/O, no ccxt import)
      CcxtTransport    HTTP, rate budget, circuit breaker, error taxonomy
```

Venue prices are read from the venue's **string** fields via the raw response, never from ccxt's
float-parsed unified fields — see [marketdata/binance.py](tradebot/marketdata/binance.py).
News is off unless a source is named (`--news cointelegraph`): a default that reaches the
internet on the first simulated cycle is a surprise.

**The simulated venue is a provider, not a fixture** ([marketdata/synthetic.py](tradebot/marketdata/synthetic.py)).
`SyntheticMarketData` answers for *any* instrument and *any* known timeframe, on the venue's own
epoch-aligned bar grid, generated on demand and extended as the clock moves. It replaced a map
`app.py` built once from the baskets configured at wiring, which was wrong twice over: a basket
published from the dashboard afterwards — which the resync sweep is built to pick up — had no
prices, so its chart pane and its every cycle answered `DataStaleError: no replay series for …`;
and because the series ended at *start-up*, a `serve --mode sim` process left up for longer than a
bar interval plus the staleness tolerance went `DATA_STALE` on every cycle and simply stopped
deciding. Three rules:

- **It is not `ReplayMarketData`, and the split is the point.** Replay serves recorded bars and
  refuses what it was not given, because a backtest that fabricated a series the dataset never
  covered would be quietly meaningless. Fabrication is the product in one and a defect in the
  other, so they cannot be one class. `SyntheticMarketData` runs through the same
  `tests/contract/test_market_data_contract.py` as every other provider; `inception` is what gives
  it the "nothing before the series starts" refusal the others get from running out of bars.
- **A bar once published is final.** Extension resumes the walk from the last close rather than
  redrawing it, so the history a pane refreshes into is the history the panel deliberated on.
- **Timeframes are independent walks of one instrument**, unchanged from the map it replaced: a 4h
  series is not the aggregate of its 1h bars. `QUOTE_TIMEFRAME` is therefore fixed at `1h` rather
  than "the shortest series loaded", so the price a sim fill happens at stays the series the
  workspace charts by default.

`CandleSeries.point_in_time` is the one construction all three providers go through, so "only
closed bars, most recent `limit`, empty fails closed" is a property of the type rather than a rule
each of them remembers.

**"What do we trade" is read fresh, never captured at wiring.** `configured_instruments(configs)`
is the answer and `app._assemble` threads one callable into everything that outlives a publish:
`StackRequest.universe` (and through it `BinanceSpotBroker` and `AlpacaBroker`), `Reconciler`, and
`PortfolioWatch`. It is the same defect ADR 0021 fixed for the Tier-2 cap, and it reaches the money
path in three ways, because **on a spot venue an instrument's base asset *is* its position**:

- **A holding nobody can name looks like one that vanished.** `parse_account` turns venue balances
  into positions through this map. An instrument added after boot is not in it, so 1 200 XRP at the
  venue stays a nameless balance while the ledger holds `binance:XRP/USDT` — `ours=1200, theirs=0`,
  which classifies as `MISMATCH` and, above `mismatch_kill_pct`, trips the kill switch.
- **The same discrepancy is then counted twice.** `_diff` excludes the base assets of *known*
  instruments from the currency comparison so BTC is not diffed as both a position and a balance.
  An unknown instrument is diffed both ways, and the currency copy can classify as an external
  deposit — which moves the drawdown baselines with it.
- **`_is_venue_reset` and `fetch_open_orders` both gate on the same map**, so an R15 testnet wipe
  involving a late instrument is read as a mismatch rather than a reset, and its resting protective
  legs are dropped from the open-order sweep — orders at the venue with nobody polling them.

Two things deliberately keep a boot snapshot, because the question they ask is itself a boot-time
one: `_quote_currency`, the currency every basket in one process must agree on, and
`StartupSequence`, which completes DESIGN §8.2 before anything can be published. **Neither is
re-checked afterwards** — a basket published later naming a different quote currency is not
refused today, which is a known gap, not a decision:
[docs/KNOWN_GAPS.md](docs/KNOWN_GAPS.md) records it
alongside the other two found in the same seam.

The pieces most likely to surprise a reader are recorded as decisions:
[ADR 0004](docs/adr/0004-protective-orders-are-venue-held.md) (protective legs),
[ADR 0005](docs/adr/0005-risk-state-and-history-are-persisted.md) (risk state and history),
[ADR 0006](docs/adr/0006-reconciliation-classifies-before-it-reacts.md) (reconciliation),
[ADR 0007](docs/adr/0007-local-deterministic-embeddings.md) (news embeddings — a deliberate
departure from PLAN §4's ChromaDB),
[ADR 0008](docs/adr/0008-venue-calls-pass-a-sliding-window-budget.md) (rate limiting).
