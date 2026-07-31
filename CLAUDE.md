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
.venv\Scripts\python.exe -m tradebot serve --mode sim --observe       # dashboard only; nothing cycles
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

Clearing a safety state is a human act and needs the typed phrase:

```powershell
.venv\Scripts\python.exe -m tradebot risk rearm  --mode sim --confirm "RE-ARM TRADING"
.venv\Scripts\python.exe -m tradebot risk unhalt demo --mode sim --confirm "RE-ARM TRADING"
```

So is arming live, which is a different phrase authorising a different thing:

```powershell
.venv\Scripts\python.exe -m tradebot risk arm-live --mode live `
    --max-notional 50 --confirm "I ACCEPT REAL MONEY RISK"
```

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

## Phase status

Phases 0–6 are complete: guardrails and money primitives, the sim-only walking skeleton, the
deterministic shell to full depth (order lifecycle with venue-held protective groups, the
`ExecutionMonitor`, the fills-driven ledger with round trips, the reconciler, both risk tiers,
the kill switch, and the DESIGN §8.2 startup sequence), the data layers (Binance spot market
data over ccxt with a weight-aware rate limiter and cache, the full indicator registry, and the
RSS news pipeline with dedup and point-in-time selection), the decision engine (three LLM
provider adapters, the blind-then-debate protocol, both decision modes, and the per-cycle cost
budget), and the broker adapters (`BinanceSpotBroker`, `AlpacaBroker`, `SimBroker` under one
contract suite, plus paper wiring and the live arming gate), and the control plane with its
dashboard. **Phase 7 (the validation ladder) is next.**

**Phase 6 is complete.** Pass 1 landed the control plane — versioned `ConfigStore`, `Scheduler`,
`Supervisor`, and a composition root that runs N baskets over one venue portfolio. Pass 2 landed
the dashboard: Configure, Monitor and Control over FastAPI + Jinja2 + vendored HTMX, with auth
mandatory. A basket can be created, configured, run, paused and killed entirely from the GUI, and
every action appears in the event log with `dashboard` as its actor — the PLAN §6 exit criterion,
asserted end to end in `tests/scenario/test_dashboard_lifecycle.py`.

### Phase 6 dashboard layering

```
create_dashboard(application, *, token, observe_only)   takes a wired Application; never builds one
  SessionMiddleware   auth by construction — a new route is protected without being told to
    routes/monitor    reads projections; the drill-down reads one cycle's events
    routes/configure  forms.py → pydantic → configs.put(); the models are the only validation
    routes/control    pause/resume, un-halt, kill switch, manual close
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

**Live mode still refuses to start.** Every PLAN §2.4 precondition is built and tested, and
`build` evaluates all of them before raising: completing the wiring is Phase 8's deliverable,
shipped with `docs/OPERATIONS.md` and armed by a human.

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
