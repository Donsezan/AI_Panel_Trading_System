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
.venv\Scripts\python.exe -m tradebot backtest fetch --symbol BTC/USDT `
    --since 2026-01-01 --until 2026-06-01 --out data\history   # public, read-only, no key
.venv\Scripts\python.exe -m tradebot backtest run --mode sim --data data\history
.venv\Scripts\python.exe -m tradebot report promotion --mode paper   # exit 5 if a gate fails
.venv\Scripts\python.exe -m tradebot report shadow --mode paper      # champion vs challenger
```

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
`create_database` upgrades to head on every start, including for a fresh database.

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

Phases 0–8 are **code-complete**. What remains is not code: the paper soak (weeks of
`tradebot run --mode paper`, measured with `report promotion`) and then a human's decision to arm
live, following [docs/OPERATIONS.md](docs/OPERATIONS.md). **Live ships disarmed and I never arm it.**

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
dispatcher writes, and only its own delivery cursor:

```
Evidence.gather(store)     folds the log's report-relevant types into counters
  promotion.evaluate       three automatic gates; the fourth is a human's signature
  Comparison.gather        pairs champion and challenger verdicts per cycle per instrument
  BacktestHarness.run      drives the real loop over recorded history, stepping the clock itself
    ReplayDataset          CSVs plus the venue trading rules they were recorded under

AlertDispatcher.poll       tails the log by seq, delivers, then advances a persisted cursor
  ops/rules.evaluate       the five PLAN triggers, as a dispatch table over event types
  ops/sinks                webhook + Telegram over httpx, off unless configured
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
- **Alerting never touches the money path.** It tails the log by `seq` and advances its cursor
  *after* delivery, so the guarantee is at-least-once and a fresh database starts at the log's
  end. Alert destinations are credentials: environment only, redactor-registered, never named in
  a log line.
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

The pieces most likely to surprise a reader are recorded as decisions:
[ADR 0004](docs/adr/0004-protective-orders-are-venue-held.md) (protective legs),
[ADR 0005](docs/adr/0005-risk-state-and-history-are-persisted.md) (risk state and history),
[ADR 0006](docs/adr/0006-reconciliation-classifies-before-it-reacts.md) (reconciliation),
[ADR 0007](docs/adr/0007-local-deterministic-embeddings.md) (news embeddings — a deliberate
departure from PLAN §4's ChromaDB),
[ADR 0008](docs/adr/0008-venue-calls-pass-a-sliding-window-budget.md) (rate limiting).
