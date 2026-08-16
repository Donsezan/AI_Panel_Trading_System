# Implementation Plan — AI Panel Trading System

> Companion to [DESIGN.md](DESIGN.md). DESIGN.md says *what* to build; this says *in what order,
> to what standard, and with what proof*. Written for a system that will eventually touch real
> money on a real exchange under a real account that can be banned.

**Confirmed scope decisions** (agreed 2026-07-25):

| Decision | Choice |
|---|---|
| Codebase | Fresh repo in `AI_Panel_Trading_System/`. `../Python_trade_bot` stays untouched as a working fallback and a source of reference code. |
| Sequencing | Vertical walking skeleton first (sim-only, end-to-end), then thicken module by module. |
| Venue scope | Crypto (Binance/CCXT) **and** equities (Alpaca) in v1 — asset-agnosticism proven, not assumed. |
| Live money | Rungs 1–5 of DESIGN §9 delivered. Live adapter code is written and contract-tested but **hard-locked**; enabling it is a human act, never mine. |
| Paid APIs | **None in v1** (decided 2026-07-26). RSS-only news (NewsAPI dropped), free-tier LLM models as the default panel, free venue APIs (Binance, Alpaca paper). Paid providers stay possible later as config-swappable plugins. |
| Risk configuration | **Every Tier-1/Tier-2 limit and risk control is GUI-editable**, stored as versioned rows in the DB ConfigStore, and persists across stops/restarts. Risk *state* (tripped kill switch, halted baskets, HWM, day-start equity) also survives restart (DESIGN §6.6, §8.2). |

---

## Table of Contents

1. [Prime Directives](#1-prime-directives)
2. [Money-Safety Engineering Rules](#2-money-safety-engineering-rules)
3. [Account-Ban & Legal Risk Controls](#3-account-ban--legal-risk-controls)
4. [Default Technical Choices](#4-default-technical-choices)
5. [Phase Plan](#5-phase-plan)
6. [Definition of Done](#6-definition-of-done-applies-to-every-module)
7. [Test Strategy](#7-test-strategy)
8. [Risk Register](#8-risk-register)
9. [What I Need From You](#9-what-i-need-from-you)
10. [Reference Code in the Prototype](#10-reference-code-in-the-prototype)

---

## 1. Prime Directives

These override convenience, elegance, and schedule. Every PR is judged against them.

1. **Fail closed.** Every uncertainty resolves to *no trade*. A missed opportunity costs nothing
   we can measure; an unintended order costs money and possibly an account.
2. **No LLM output reaches a venue unvalidated.** The panel emits a *proposal*. Deterministic,
   unit-tested code decides whether anything happens and at what size. (DESIGN [L3])
3. **The venue is the source of truth.** Local state is a reconciled projection, never authority.
   (DESIGN [L4][L10])
4. **Never submit without a durable, committed record first.** The intent — including its
   `client_order_id` — is written and committed to the DB *before* the network call. A crash
   mid-submit must leave a recoverable trace, never an orphan order on the exchange.
5. **Live mode is opt-in, loud, and capped.** It cannot be reached by a default, a typo, or a
   missing env var.

---

## 2. Money-Safety Engineering Rules

Non-negotiable, enforced by tests and CI, not by discipline.

### 2.1 Decimal-only arithmetic

Every price, quantity, notional, fee, and balance is `decimal.Decimal`. **No `float` ever touches
a money path.** Floats are permitted only inside indicator math (RSI, MACD…), and indicator
outputs never size an order without conversion through the money layer.

- `core/money.py` owns all arithmetic and rounding. Context precision is set explicitly;
  `ROUND_HALF_EVEN` is banned for sizing.
- Quantization rules, and they are asymmetric on purpose:
  - **Quantity** → always `ROUND_DOWN` to `lot_size`. Rounding up can exceed a risk limit or
    available balance.
  - **Buy limit price** → `ROUND_DOWN` to `tick_size`; **sell limit price** → `ROUND_UP`.
    Always the more passive side: quantization must never make a price *more aggressive than
    intended* (a marketable limit crosses the spread deliberately; rounding must not deepen it).
  - After quantization, re-check `min_notional` and `min_qty`. Below minimum ⇒ **veto**, never
    "bump it up to the minimum" (that silently oversizes past a risk limit).
- Enforcement: a unit test walks every pydantic model in `core/` and fails if a money-semantic
  field is typed `float`. A `ruff` custom check flags `float(` in `risk/`, `execution/`, `ledger/`.
- Property tests (hypothesis): quantization is idempotent, never increases quantity, never
  produces a notional above the pre-quantization notional.

### 2.2 Idempotency and the `client_order_id`

Binance caps `newClientOrderId` at 36 chars with charset `[.A-Za-z0-9_:/-]` (regex
`^[\.A-Z\:/a-z0-9_-]{1,36}$`), so the human-readable tuple `{basket}-{cycle}-{instrument}-{seq}`
is used only as *hash input*. Concrete scheme (adopted in DESIGN §6.7):

```
{prefix}-{base32(blake2s(basket_id|cycle_id|instrument|seq)[:10])}   # ≤ 36 chars, venue-safe
```

- Deterministic and **recomputable** from durable data, so recovery can query the venue by id
  without having stored the string (though we store it anyway, belt and braces).
- `prefix` is per-environment (`sim`, `pap`, `liv`) so a testnet id can never collide with or be
  mistaken for a live one, and so reconciliation can adopt "our" orders by prefix.
- A uniqueness constraint on `client_order_id` in the DB; a duplicate insert is a bug that
  fails loudly rather than a second order.

### 2.3 `SUBMIT_UNKNOWN` is the most important state in the system

Any timeout, 5xx, connection reset, or ambiguous response during submit ⇒ `SUBMIT_UNKNOWN`.
The **only** legal transitions out are (a) query the venue by `client_order_id` and adopt what
is found, or (b) after a bounded window, mark failed and **halt the basket for human review**.
Blind resubmission is not implemented — there is no code path that can do it. This is the single
defense against the duplicate-order-after-retry failure that dominates practitioner incident
reports. (DESIGN [L9][L11])

### 2.4 Mode confusion is treated as a catastrophic failure class

The classic way to lose real money is running live while believing you're on testnet.

- Mode is a **required** CLI argument. There is no default. No env var can silently select live.
- Each mode uses a **separate database file** (`data/{mode}.db`). A paper ledger can never be
  interpreted as a live one.
- At startup the adapter's *actual* resolved endpoint is asserted against the mode
  (e.g. CCXT `sandbox` flag and base URL must match; Alpaca paper vs live host must match).
  Mismatch ⇒ refuse to start.
- Live requires: `--mode live` **plus** a typed confirmation phrase **plus** a `live_armed`
  config row **plus** a non-null `max_live_notional` cap. Any one missing ⇒ refuse to start.
- Every log line carries `mode`; the dashboard header is colour-coded and unmissable.

### 2.5 Money is never reconstructed from order state

Positions and balances update from **fills only**, never from an order reaching a terminal
state. Partial fills are the normal case, not an edge case. (DESIGN §6.7)

### 2.6 Single writer

One asyncio task owns each basket's orders; one serialized writer owns the DB. No position is
mutated from two code paths. Enforced by an assertion on the writer task identity, not by
convention.

---

## 3. Account-Ban & Legal Risk Controls

You named account blocking and legal consequences explicitly, so these are first-class
deliverables, not afterthoughts.

### 3.1 Not getting the API key banned

- **Token-bucket rate limiter** in front of every venue call, budgeted below the venue's
  published limit (Binance: request *weight* per minute and order count per 10s/24h — weight,
  not request count, is what bans you). CCXT's `enableRateLimit` is a floor, not the ceiling;
  we track weight ourselves from response headers (`X-MBX-USED-WEIGHT-1M`).
- **Backoff ladder** on `429`: honour `Retry-After`, jittered exponential backoff, and a
  **circuit breaker** that stops all calls to that venue after N consecutive failures.
  A `418` (IP auto-ban) trips the kill switch immediately — continuing to hammer a banned IP
  extends the ban.
- **No polling storms.** ExecutionMonitor polls only while orders are actually open, and its
  cadence is derived from the rate budget, not hardcoded.
- Startup asserts clock skew vs venue time (signature rejection and repeated auth failure is
  itself a ban vector). Warn > 2 s, refuse to start > 30 s.

### 3.2 Exchange-side key hygiene, verified not assumed

- Keys are **trade-only, withdrawals disabled, IP-allowlisted**.
- Where the venue exposes it, this is *verified at startup*: Binance
  `GET /sapi/v1/account/apiRestrictions` reports `enableWithdrawals`. If withdrawals are enabled
  on a live key, the process **refuses to start**. Trusting a checkbox you set months ago is not
  a control; asserting it every boot is.
- Keys live in env / OS keyring only. Never in the DB, never in a log, never in a prompt.
  A logging filter redacts anything matching known key shapes, and a unit test asserts the
  redaction works by pushing a fake key through the real logger.

### 3.3 Legal / compliance posture

- **News ingestion via RSS and official APIs, not scraping.** The prototype scrapes
  Cointelegraph's HTML; Cointelegraph publishes RSS. Scraping raises ToS and copyright exposure
  for zero benefit. Policy: RSS/API first, respect `robots.txt`, identify with a real
  `User-Agent`, cache aggressively, store only title + short excerpt + link (not full article
  bodies) beyond a short retention window.
- **Audit trail.** The append-only event log is the compliance artifact: for any order we can
  show the exact data seen, the deliberation, the risk decision, and the venue response. Retained
  with the event log's retention policy; this is also what makes tax reconstruction possible.
- **Self-trade / wash-trade avoidance.** A pre-submit check rejects an order that would cross
  our own resting order on the same instrument. Cheap to implement, and both venues treat
  self-matching as an abuse pattern.
- **Jurisdiction and instrument eligibility** are a *human* precondition documented in
  `docs/OPERATIONS.md`, not something the bot infers. (See [§9](#9-what-i-need-from-you).)
- **No investment advice surface.** The dashboard is single-user and localhost-bound by default;
  binding to a non-loopback interface requires auth and an explicit config flag.

---

## 4. Default Technical Choices

Stated so you can veto now rather than after they're load-bearing. None are hard to change
in Phase 0; all are expensive to change in Phase 4.

| Area | Choice | Why |
|---|---|---|
| Python | 3.11 (matches installed 3.11.9), `pyproject.toml` (PEP 621) | Already on the machine; 3.11 is security-fixes-only since 2024 but its EOL (Oct 2027) covers the project horizon — revisit at the first dependency that demands 3.12+ |
| Env/deps | `venv` + `pip` at runtime; lock compiled with `uv pip compile --generate-hashes` (single-binary install; pip-tools if uv is vetoed) | pip alone cannot compile hash-pinned locks. Hash pinning is supply-chain defence — a compromised transitive dep in a trading bot is a worst case |
| Models | pydantic v2 (`frozen=True` on all snapshots/events) | Immutability is what makes "the snapshot the panel saw" a truthful claim |
| DB | SQLite + WAL via SQLAlchemy 2.0, Alembic migrations | DESIGN §6.9; interface thin enough to swap to Postgres |
| Async | stdlib `asyncio`, one task per basket, no broker/queue | Minutes-scale cadence; a message broker is unearned complexity |
| Dashboard | FastAPI + Jinja2 + HTMX, no JS build step | Auditable, no npm supply chain, fast to iterate |
| Vector store | ChromaDB behind a `VectorStore` interface (prototype already uses it) | Reuses working code; interface allows sqlite-vec swap if Chroma proves heavy. Embedding runs in a thread executor — never on the trading event loop |
| Lint/type | `ruff` (format + lint), `mypy --strict` on `core/`, `risk/`, `execution/`, `ledger/` | Strict typing exactly where mistakes cost money |
| Tests | `pytest`, `hypothesis`, `pytest-asyncio`, `freezegun` (datetime only — it does not freeze `loop.time()`; the scheduler is tested through the injectable `core/clock.py`), coverage gate | See [§7](#7-test-strategy) |
| Time | UTC-aware `datetime` everywhere; naive datetimes rejected at model boundary | Candle alignment and auth signatures both break on this |
| CI | GitHub Actions if you push to GitHub, else a `make check` script | Rungs 1–3 on every commit, rung 4 nightly |

---

## 5. Phase Plan

Estimates assume focused work. Each phase ends with a demoable, tested artifact — no phase
leaves the tree in a non-working state.

### Phase 0 — Repo, guardrails, money primitives · ~3 days

Scaffolding that makes every later phase safe by construction.

- `git init`; `.gitignore` (`.env*`, `*.db`, `data/`, `.venv/`); `pyproject.toml`; hash-pinned lock.
- `ruff` + `mypy` + `pytest` + coverage config; `make check` (format, lint, type, test) and CI.
- `core/money.py` — Decimal context, quantization (lot/tick/min-notional) with the asymmetric
  rounding of §2.1, fully property-tested. **This is the first real code written.**
- `core/clock.py` (injectable, UTC-only), `core/ids.py` (venue-safe `client_order_id`, §2.2),
  `core/errors.py` (error taxonomy: `Retryable` / `Fatal` / `FailClosed`).
- `core/logging.py` — structured JSON logs, correlation ids (`cycle_id`, `basket_id`,
  `client_order_id`), **secret-redaction filter + its test**.
- `docs/adr/` for decision records; `CLAUDE.md` for repo conventions.

**Exit:** `make check` green. Money quantization and id generation at 100% branch coverage.
No float in money paths (enforced by test).

### Phase 1 — Walking skeleton, simulation only · ~2 weeks

Thinnest possible end-to-end loop. No real venue, no real LLM. Proves the wiring and gives
every later phase a harness to land in.

- `core/` domain models: `Instrument`, `Basket`, `ContextSnapshot`, `Decision`, `OrderIntent`,
  `Order`, `Fill`, `Position`, plus the event envelope.
- `interfaces/` — **the full plugin surface defined now** (`MarketDataProvider`, `NewsSource`,
  `LLMProvider`, `BrokerAdapter`, `DebateProtocol`, `RiskRule`, plus the supporting
  `RelevanceFilter` and `VectorStore`). Freezing the plugin surface early is what keeps venue
  assumptions out of core.
- `persistence/` — append-only event log + projections in one transaction, single-writer queue,
  Alembic baseline, event-log→projection replay.
- `marketdata/replay.py` — serves recorded CSV/JSON series, honours `end=` for point-in-time.
- `indicators/` — RSI only, with deterministic verbalization.
- `decision/` — one seat, `StubLLMProvider` returning canned schema-valid JSON.
- `risk/` — one tier-1 rule (max position size) + deterministic sizing.
- `execution/sim_broker.py` — instant fills, configurable slippage/fee.
- `control/basket_runner.py` — the cycle loop; `app.py` composition root.
- CLI: `python -m tradebot run --mode sim --once`.

**Exit:** one command runs a full cycle end to end; a scenario test asserts the complete event
chain (`CYCLE_STARTED → SNAPSHOT_FROZEN → SEAT_RESPONDED → DECISION_MADE → RISK_CHECKED →
ORDER_SUBMITTED → FILL_RECEIVED → CYCLE_COMPLETED`) and that replaying the log reproduces
identical projections.

### Phase 2 — The deterministic shell, to full depth · ~4 weeks

The money-touching core. **The highest-value phase; where the bugs that cost money live.**
Built before any real LLM or real venue on purpose.

- **2a. Order lifecycle** — explicit state machine (DESIGN §6.7) with illegal transitions
  raising, not logging. Write-ahead intent persistence (§Prime Directive 4). `SUBMIT_UNKNOWN`
  recovery. TTL/cancel-remainder (TTL is bot-enforced — no venue-side GTT on Binance spot).
  **Linked protective-order groups** (entry + venue-native SL/TP legs, DESIGN §6.7): one leg
  fills ⇒ cancel the sibling; legs resize on partial entry fills. `ExecutionMonitor` owning
  each order end-to-end.
- **2b. Ledger** — fills-driven positions, avg entry, realized/unrealized PnL, per-currency
  free/locked, high-water mark. Read-only projection exposed to everything else.
- **2c. Reconciler** — startup / post-gap / periodic. Classify match / explainable drift
  (fees, funding, dust) / **corporate action** (splits, dividends — Phase 5 wires the venue's
  announcement data) / **venue reset** (`VENUE_RESET`: halt + notify, not kill) / unexplained
  mismatch. Adopt orders by `client_order_id` prefix. Absorb external changes (manual trades,
  deposits) as logged `EXTERNAL_CHANGE` events that also flow-adjust the high-water mark and
  day-start equity (DESIGN §6.6).
- **2d. Tier-1 risk** — all rules from DESIGN §6.6, including **long-only/reduce-only
  enforcement** (SELL while flat ⇒ veto) and the **unprotected-position haircut** for venues
  without native protective orders; volatility-normalized sizing with explicit units
  (`qty = risk_amount / (stop_multiple × ATR)`, ATR absolute in quote currency);
  exchange-minimum clamping; `RiskCheckResult` provenance on every intent. All rule
  parameters are data (config objects — wired to the versioned ConfigStore in Phase 6),
  never constants in code.
- **2e. Tier-2 risk** — per-venue hard limits plus cross-venue `PortfolioAggregate` rules
  (DESIGN §6.6), veto/shrink semantics, **flow-adjusted HWM/daily-loss baselines**
  (`EXTERNAL_CHANGE`-aware — a withdrawal must never read as a drawdown), continuous
  watchdog task, **kill switch** (auto + manual, typed re-arm, `flatten_on_kill=false`
  default). All limits are config data (ConfigStore-wired in Phase 6); risk state (kill
  switch, halts, HWM, day-start equity) is persisted in the DB and restored on startup — a
  restart never silently un-halts.
- **2f. Startup/recovery sequence** — DESIGN §8.2 exactly, including "any step fails ⇒ process
  stays up, halted, nothing trades".

**Exit:** near-100% unit coverage on `risk/`, `execution/`, `ledger/`. Chaos suite asserts
**every row** of the DESIGN §8.1 failure table. Kill switch demonstrated tripping from each of
its three triggers.

### Phase 3 — Data layers · ~2 weeks

- `marketdata/ccxt_.py`, normalization to a common `CandleSeries` (UTC, gaps explicit,
  `observed_at`), staleness policy → `DATA_STALE` abort, rate limiter (§3.1), cache.
- `indicators/` — full registry (MACD, EMA/SMA, ATR, Bollinger, volume profile), per-timeframe,
  session-aware for equities (no naive computation across overnight gaps).
  Verbalization under golden tests so wording can't drift between runs.
- `news/` — `rss_generic` + Cointelegraph **RSS** only (no paid APIs in v1; NewsAPI dropped —
  its free tier delays articles 24 h and forbids commercial use; a plugin can be added later
  behind the same interface). Normalize → dedupe (URL hash +
  embedding similarity) → relevance score → store with `published_at` *and* `observed_at`;
  point-in-time filter (`observed_at <= cycle_start`) with a test that proves future news
  cannot leak into a replayed decision.

**Exit:** replay and live-fetch paths produce byte-identical `ContextSnapshot` structures for
the same inputs. Rate limiter proven to stay under budget under a burst test.

### Phase 4 — Decision engine · ~2 weeks

- `LLMProvider` adapters: `openai_compat` (OpenRouter/OpenAI/vLLM/**LM Studio**/**llama.cpp**),
  `anthropic`, `gemini` — written against the HTTP APIs, not vendor SDKs ([ADR 0009](docs/adr/0009-llm-providers-over-plain-http.md)).
- Seats bind (role, provider, model, params) — **panel is data, not code**. A `PanelConfig`
  carries its own `providers[]`, so it is self-describing and GUI-editable as one tree.
- `blind_then_debate` protocol: blind round 0, anonymized transcripts, devil's-advocate seat,
  differentiated evidence slices per seat.
- Consensus in **deterministic code**: qualified majority, conviction scaling, non-consensus ⇒
  `WAIT` with recorded dissent. (DESIGN [L6])
- Hard output contract: pydantic validation, one repair attempt, then `ABSTAIN`;
  >⅓ abstain ⇒ `WAIT (PANEL_DEGRADED)`. (DESIGN [L8])
- **Per-seat fallback chains, each configured independently** — an ordered list of
  `(provider, model)` bindings per seat, crossing vendor families and ending at a local runtime.
  Validated at configuration time: no repeated binding within a chain, and every binding must
  name a provider the panel declares. Seeded panels give each seat a *different* backup so one
  vendor outage cannot collapse heterogeneity (R11).
- Per-cycle token/cost budget with early truncation; cost persisted per cycle and de-duplicated
  by provider call, so `basket` mode is not double-counted.
- Both decision modes: `per_asset` and `basket` (one panel run, an assessment per instrument).
- Prompt-injection hardening: news **and peer transcripts** wrapped in delimited data blocks
  (a seat's thesis is model text derived from news, so an injection can otherwise launder through
  one seat into every other seat's prompt), panel has no tools, output schema-bound and
  risk-gated.

**Exit:** cassette-based tests (recorded provider responses) make the suite deterministic and
free. A malformed-response fuzz test proves no junk escapes into a `Decision`.

### Phase 5 — Broker adapters · ~3 weeks

DESIGN [L11]: *budget most engineering effort here.* Deliberately the largest phase.

- `CcxtBroker` — Binance live + testnet through one adapter; protective-order support
  (OCO / `STOP_LOSS_LIMIT`) declared via `BrokerCapabilities`.
- `AlpacaBroker` — equities, paper + live; trading calendar, fractional shares (limit orders
  + extended hours are supported), market-hours scheduling, extended-hours policy,
  bracket/stop protective orders, corporate-action announcements wired into the reconciler.
  **Do not port any prototype or reference logic touching PDT fields**
  (`pattern_day_trader`, `daytrade_count`, `daytrading_buying_power`, …) — FINRA retired the
  PDT rule and Alpaca replaced it with the Intraday Margin Framework (June 2026); the fields
  are removed from the API. Assert Alpaca's `client_order_id` length limit in the contract
  tests (our ≤ 36-char scheme fits).
- **Contract test suite** run identically against `SimBroker`, `CcxtBroker` (recorded fixtures),
  and `AlpacaBroker` (recorded fixtures): partial fills, cancel races, `SUBMIT_UNKNOWN`
  recovery, rejects, rate-limit responses, precision/minimum handling. Any adapter that
  diverges fails CI.

**Exit:** all three adapters pass one identical contract suite. A live-mode arming test proves
the process refuses to start on every one of the §2.4 missing-precondition cases.

### Phase 6 — Control plane, ConfigStore, dashboard · ~3 weeks

- Versioned `ConfigStore` (updates create versions; cycles pin versions; `secret_ref` indirection).
- `Scheduler` with per-basket schedules and trading-hours calendars; never overlaps a cycle.
- `Supervisor` — start/stop on config change, exponential-backoff restart, auto-halt after N
  consecutive failures (human un-halt only).
- Dashboard: **Configure** (basket/panel/risk CRUD — every Tier-1/Tier-2 limit and risk
  control from DESIGN §6.6 is editable and DB-persisted, nothing hardcoded; server-side
  validation against the same pydantic schemas), **Monitor** (equity curve, cycle history, decision drill-down = the
  research artifact, cost tracking), **Control** (pause/resume, un-halt, manual close *through
  the normal risk/execution path — no side doors*, kill switch).
- **Panel editor** specifically (the Phase 4 config shapes made editable, DESIGN §6.10):
  a provider list (endpoint, kind, `secret_ref` by name, per-model prices) and a seat list where
  each seat picks its primary provider+model and builds its **own ordered fallback chain** from
  the declared providers — a picker rather than free text, so an undeclared provider cannot be
  entered. `PanelConfig`'s existing validators are the server-side check, unchanged; the form
  surfaces their messages rather than reimplementing them.
- Auth mandatory on non-loopback bind.

**Exit:** a basket can be created, configured, run, paused, and killed entirely from the GUI,
with every action appearing in the event log.

### Phase 7 — Validation ladder · ~2 weeks build + 4+ weeks wall-clock soak

- Backtest harness over historical data — **plumbing and risk validation only**. Report banner
  states which periods pre-date model knowledge cutoffs; results are never labelled alpha.
  (DESIGN [L12])
- Paper soak, forward-only, weeks of continuous running. **Primary mode: live market data +
  `SimBroker`** (real data, no venue test artifacts); Binance testnet + Alpaca paper run
  alongside as adapter integration checks — Binance's spot testnet resets to a blank state
  roughly monthly without notice, which the reconciler classifies `VENUE_RESET` and the
  gates exclude.
- **Shadow A/B harness**: two PanelConfigs evaluated on the same snapshots each cycle — the
  statistically honest way to compare panels over weeks of data (promoted from DESIGN §12
  future work).
- Promotion gates enforced in code/report: ≥200 cycles, zero unhandled risk events, every
  reconciliation clean (venue resets excluded), then human review.
- Ops alerts (Telegram/webhook): kill switch, basket halt, recon mismatch, repeated provider
  failure, daily summary.

**Exit:** a promotion report generated from the event log that a human signs off on.

### Phase 8 — Live, locked · ~2 days

Live wiring completed, tested, and **left disabled**. Delivered with `docs/OPERATIONS.md`:
the pre-live checklist (key permissions, IP allowlist, withdrawal disable, caps, alerting,
jurisdiction confirmation), the arming procedure, and the incident runbook. I do not arm it.

### Phase 9 — Operator control · ~1 week

Scoped after Phase 8 shipped, from two operator requests. Full plan and decisions:
[docs/PHASE_9_OPERATOR_CONTROL.md](docs/PHASE_9_OPERATOR_CONTROL.md). Two independent slices:

- **Quarantine** (done) — an operator's exclusion of one instrument, or a whole basket, from
  *automated* trading, while the cycle keeps running so the data needed to release it keeps
  arriving. A Tier-1 veto rule reading a versioned `RiskPolicy` field, not a scheduling state:
  neither a pause (which stops the cycle) nor a halt (which is the system's own doing). A held
  position stays closable by hand through the existing operator-exit exemption
  ([ADR 0022](docs/adr/0022-quarantine-is-a-tier-1-veto-rule.md)).
- **GUI arm/start/stop for live** (planned) — live's four preconditions move from a build-time
  gate to a runtime one, so an unarmed `serve --mode live` comes up saying "NOT ARMED" instead of
  refusing, and can be armed and started from the dashboard with the phrase retyped each time
  ([ADR 0021](docs/adr/0021-live-arming-and-supervision-move-to-a-runtime-gate.md)).

**Exit:** an instrument can be quarantined and released from the GUI, with no automated order
reaching it in between and its market data uninterrupted; and live can be armed, started and
stopped from the GUI without weakening any §2.4 precondition.

**Total: ~16–18 weeks of focused work**, plus soak time that runs in parallel from Phase 7.
Phases 3 and 4 can be parallelized against Phase 2 if more than one person works on this.

---

## 6. Definition of Done (applies to every module)

A module is not done until all of these hold:

1. Public surface is an interface in `interfaces/`; nothing outside `app.py` imports a concrete class.
2. `mypy --strict` clean (mandatory for `core/`, `risk/`, `execution/`, `ledger/`).
3. Unit tests cover every branch of every money-affecting decision.
4. Every failure path is *tested*, not just written — including the one that fails closed.
5. Every state change emits an event; the event log alone can reconstruct the module's state.
6. No secret can reach a log, a DB row, or a prompt (test-enforced).
7. Errors are classified (`Retryable` / `Fatal` / `FailClosed`) — never a bare `except: pass`.
8. Docstring states the failure semantics: what happens when this module's dependency is down.

---

## 7. Test Strategy

Mapped to DESIGN §9, with the specific mechanics:

| Rung | What | Mechanics | When |
|---|---|---|---|
| 1 | Unit | pytest + hypothesis (property tests on sizing, quantization, state machine) | every commit |
| 2 | Contract | one suite, run against every `BrokerAdapter` and every `MarketDataProvider`; recorded golden fixtures per real venue | every commit |
| 3 | Scenario / chaos | full loop on `ReplayMarketData` + `SimBroker` with fault injection: feed drops, seat returns junk, submit times out, recon mismatch injected, 429 storm, clock skew. Asserts the §8.1 *response*, never PnL | every commit |
| 4 | Backtest | long-horizon replay; validates point-in-time discipline, costs, risk behaviour. **Not alpha evidence** | nightly |
| 5 | Paper soak | live data + `SimBroker` (primary); Binance testnet + Alpaca paper (adapter integration), forward-only | continuous, Phase 7+ |

Coverage gates: `core/` `risk/` `execution/` `ledger/` ≥ 95% branch, CI-enforced. Elsewhere ≥ 80%.

Two tests I consider load-bearing and will write early:

- **The orphan-order test** — kill the process between `submit()` and the response; assert
  restart finds the order by `client_order_id` and adopts it rather than resubmitting.
- **The look-ahead test** — a replayed cycle at time T given news published at T+1h must produce
  a snapshot containing none of it.

---

## 8. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Duplicate order after retry | Med | **Severe (money)** | §2.2 idempotency, §2.3 `SUBMIT_UNKNOWN`, no resubmit path exists |
| R2 | Live/testnet confusion | Med | **Severe (money)** | §2.4 — separate DBs, endpoint assertion, required flag, typed confirm |
| R3 | Float rounding oversizes an order | Med | High | §2.1 Decimal-only, test-enforced, asymmetric rounding |
| R4 | API key banned by rate-limit abuse | Med | High (ops) | §3.1 weight-aware limiter, circuit breaker, 418 ⇒ kill switch |
| R5 | Ledger drifts from venue truth | High | High | Phase 2c reconciler; mismatch ⇒ halt/kill |
| R6 | LLM cost overrun | High | Med (budget) | Per-cycle token budget, cost persisted, dashboard $/decision, free-tier models by default |
| R7 | Prompt injection via news headline | Low | Med | Delimited data blocks, no tools, schema-bound output, risk gate — injection can flip a marginal decision but can never size, route, or exceed limits on an order |
| R8 | Backtest mistaken for alpha evidence | High | Med (research validity) | Banner + docs; paper trading is the only evaluation |
| R9 | Scraping ToS exposure | Med | Med (legal) | §3.3 RSS/API only, robots.txt respected, excerpt-only storage |
| R10 | Scope overrun — this is a big system | High | Med | Walking skeleton first; every phase independently demoable |
| R11 | Free OpenRouter models degrade/disappear | High | Low | Per-seat fallback chains of `(provider, model)` bindings that leave the vendor entirely and end at a local runtime (LM Studio / llama.cpp); each seat given a *different* backup so one outage cannot collapse the panel; substitution recorded in transcript; `PANEL_HOMOGENEOUS` flag when fallbacks collapse model heterogeneity |
| R12 | Venue lacks native protective orders → position unprotected between cycles | Med | High | `BrokerCapabilities` gate; sizing haircut; `unprotected_position` flag in RiskCheckResult and panel context (DESIGN §6.7) |
| R13 | Accidental short on equities (SELL while flat) | Med | **Severe (money)** | Tier-1 long-only/reduce-only veto (DESIGN §6.6) |
| R14 | Corporate action (split/dividend) mistaken for ledger drift → false halt/kill | Med | Med (ops) | Reconciler corporate-action classification against venue announcements (Phase 2c/5) |
| R15 | Binance testnet monthly reset breaks soak/reconciliation | High | Med (ops) | `VENUE_RESET` classification; SimBroker-primary soak; promotion gates exclude resets |
| R16 | External deposit/withdrawal trips drawdown kill switch or masks losses | Med | High | Flow-adjusted HWM and day-start equity from `EXTERNAL_CHANGE` events (DESIGN §6.6). **Closed** by [ADR 0027](docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md): `record_flow` takes the whole `ExternalFlow`, converts through the same `value_cash` ladder that values the balance it lands in, and raises `FailClosedError` on a currency it cannot value rather than adjusting a baseline by a number in the wrong unit |
| R17 | Drawdown gate measures cost basis, so unrealized loss never trips the kill switch | **Was realized** | **Severe (money)** | **Closed** by [ADR 0027](docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md). `Ledger.equity` is deleted and `risk.aggregate` is the one valuation: equity is cash-in-notional plus each position at its mark. A missing or stale mark freezes rather than falling back to cost, enforced structurally by `tests/unit/test_valuation_boundary.py`. Note that soak evidence gathered *before* this landed ran under a gate that could not see unrealized loss |
| R18 | Non-quote cash is valued at zero, fabricating a drawdown on a stablecoin conversion | Med | High | **Closed** by [ADR 0027](docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md). `value_cash` values every balance or freezes: notional at face, USD stablecoins at par under a peg check that now receives real prices, a configured base asset as the position it already is, anything else against its `{CUR}/{notional}` market |
| R19 | The depeg guard had never run: `PortfolioAggregate.frozen` had no consumer and `stablecoin_prices` was never supplied | **Was realized** | Med | **Closed** by [ADR 0027](docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md). The freeze is now consumed by `Watchdog.check` → the cycle gate → `BLOCKED`, and `_peg_check` reads `Marks`. A freeze blocks new orders, alerts, and counts as an incident — but never trips the switch |
| R20 | `max_gross_exposure` and `max_instrument_exposure` were enforced against one basket, not the portfolio | **Was realized** | High | **Closed** by [ADR 0027](docs/adr/0027-portfolio-equity-is-mark-to-market-in-one-notional-currency.md). Every portfolio-wide input — gross exposure, per-instrument exposure, cluster membership — is computed over the configured instrument universe. Only `basket_exposure` stays basket-scoped. Invisible with one basket in service, unbounded with two |

---

## 9. What I Need From You

Nothing here blocks Phase 0 or Phase 1 — I can start immediately. These are needed by the phase
noted.

**By Phase 3 (market data):**
- Confirm Binance is the crypto venue (prototype's `.env.example` suggests yes) and whether the
  account is spot-only. Derivatives are explicitly out of scope in DESIGN §12.

**By Phase 4 (decision engine):**
- LLM providers: decided 2026-07-26 — **free-tier models only in v1** (OpenRouter free slots
  form the default panel; the `anthropic`/`gemini` adapters are still built and
  contract-tested, but unused unless you later opt into paid keys). Only remaining input:
  which free OpenRouter models to seed the default panel with.

**By Phase 5 (brokers):**
- An **Alpaca paper account** (free) for equities. Without it I can build `AlpacaBroker` against
  recorded fixtures but cannot verify it against the real API.
- Binance **testnet** keys.

**By Phase 7/8 (paper → live), and these are yours alone:**
- Confirmation that automated trading of the chosen instruments is permitted for you in your
  jurisdiction and under each venue's terms.
- Live keys, if ever: trade-only, **withdrawals disabled**, IP-allowlisted. I will assert these
  at startup but I will never create them or arm live mode.
- Your tax/record-keeping requirements, so event-log retention is set correctly from the start
  rather than discovering a gap later.

**Open items I've defaulted (say the word if you disagree):**
- Everything in [§4](#4-default-technical-choices).
- Cointelegraph via **RSS, not HTML scraping** — a deliberate change from the prototype (§3.3).
- `flatten_on_kill = false` (DESIGN's default): the kill switch halts and cancels but does not
  liquidate. Flattening into a broken market is often the worse outcome.

---

## 10. Reference Code in the Prototype

`../Python_trade_bot` is not migrated, but these carry real value and will be read and adapted
(not copied wholesale):

| Prototype file | Value | Treatment |
|---|---|---|
| `Logic/execution/ccxt_adapter.py` | Working Binance/CCXT call shapes | Reference for `CcxtBroker`; rewritten around the state machine and Decimal |
| `Logic/indicators/indicators_engine.py` | Indicator formulas | Port formulas, add per-timeframe + verbalization + golden tests |
| `Logic/decision_engine/llm_decision_engine.py` | Prompt text, debate structure, JSON contract | Prompts are the genuinely reusable asset; structure is rebuilt per DESIGN §6.5 |
| `Logic/rag_store/chromadb_service.py` | Working ChromaDB integration | Adapt behind the `VectorStore` interface |
| `Logic/news/news_ingestor.py` | Dedup approach | Reuse logic; **switch source from scraping to RSS** |
| `tests/test_llm_decision_engine.py` | Largest existing test (379 LOC) | Mine for cases; rewrite against new contracts |

Everything else — orchestrator, config, persistence, risk manager, dashboard — is superseded by
a materially different design and will be written fresh.
