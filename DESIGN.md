# Design Document — AI Panel Trading System (Target Architecture)

> Status: **proposed clean-slate design**. This document describes the target architecture the
> current prototype (`Logic/` packages) would be migrated toward. It is written against
> [FUNCTIONAL_SPECIFICATION_OVERVIEW.md](FUNCTIONAL_SPECIFICATION_OVERVIEW.md) and incorporates
> lessons from published research and mature open-source trading engines (see [Prior Art](#2-prior-art--lessons-learned)).

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Prior Art & Lessons Learned](#2-prior-art--lessons-learned)
3. [Design Principles](#3-design-principles)
4. [Domain Model](#4-domain-model)
5. [High-Level Architecture](#5-high-level-architecture)
6. [Module Specifications](#6-module-specifications)
7. [Data Contracts](#7-data-contracts)
8. [Reliability Engineering](#8-reliability-engineering)
9. [Testing & Validation Pipeline](#9-testing--validation-pipeline)
10. [Proposed Repository Layout](#10-proposed-repository-layout)
11. [Migration From the Current Prototype](#11-migration-from-the-current-prototype)
12. [Open Questions & Future Work](#12-open-questions--future-work)
13. [Sources](#13-sources)

---

## 1. Goals and Non-Goals

### Goals

- **Multi-LLM deliberation** — a configurable panel of AI models analyses market data,
  indicators, and news, debates over several rounds, and converges on a decision per asset.
- **Asset-agnostic** — the same engine trades one crypto, one stock, an index, or a *basket*
  of mixed instruments. Asset class differences (market hours, lot sizes, order types) live
  in adapters, never in core logic.
- **Basket- or per-asset operation, configured via GUI** — the user creates baskets in the
  dashboard; each basket has its own schedule, decision mode, agent panel, and risk budget.
- **Two-tier risk control** — Tier 1 limits per asset/basket; Tier 2 global portfolio limits
  that can veto or shrink any trade regardless of what a basket wants.
- **Pluggable everything** — exchanges/brokers, market-data feeds, news sources, indicator
  sets, LLM providers, and debate protocols are all plugins behind stable interfaces.
- **Auditable** — every decision must be reconstructable: what data the panel saw, what each
  agent said each round, why risk approved/rejected, what the exchange actually did.
- **Safe by default** — simulation → paper → live is a promotion ladder with explicit gates;
  live mode requires deliberate opt-in and starts with hard caps.

### Non-Goals

- **Not HFT.** LLM inference takes seconds-to-minutes; the system targets cycle times of
  minutes to hours. Latency-sensitive strategies (market making, latency arbitrage) are out
  of scope — research consistently shows LLM inference latency makes them non-viable.
- **Not a profit product.** Per the functional spec, this is a research testbed. The design
  still treats money-touching paths with production rigor, because the *live* mode is real.
- **Not a portfolio optimizer.** The panel proposes discrete actions (buy/sell/hold/wait
  with sizing hints); it does not solve mean-variance allocation. That could be a future
  strategy plugin.

---

## 2. Prior Art & Lessons Learned

Design decisions below trace back to these findings. Numbers `[Ln]` are referenced
throughout the doc.

### 2.1 TradingAgents (arXiv 2412.20138)

The closest published system: LLM agents in specialized roles (fundamental / sentiment /
news / technical analysts, researchers, trader, risk manager) organized in stages —
analysis → research debate → trading decision → risk gate. Reported improved Sharpe and
drawdown vs. single-model baselines.

- **[L1] Role specialization beats homogeneous panels.** Give each agent a distinct role
  and distinct evidence slice, not three copies of the same prompt.
- **[L2] Risk assessment is a separate stage after the trade decision**, not part of the
  debate. The risk gate must be *deterministic code*, with the LLM risk-analyst role being
  advisory only.

### 2.2 TradeTrap (arXiv 2512.02261) — reliability of LLM trading agents

Showed that small perturbations in any one component (market intelligence, strategy,
ledger, execution) *propagate through the agent loop* into concentrated positions,
uncontrolled exposure growth, and large losses.

- **[L3] Never let LLM output flow to the exchange unchecked.** Every decision passes
  through deterministic validation (schema, bounds, risk) before becoming an order.
- **[L4] The ledger (positions/balances) must never be LLM-maintained or LLM-read-modified.**
  The exchange is the source of truth; the local ledger is reconciled code, and the LLM
  only ever receives a read-only snapshot of it.

### 2.3 Multi-agent debate research (CONSENSAGENT, "Cost of Consensus", sycophancy studies)

Debate helps on some tasks but **sycophancy collapses debates into premature consensus**;
homogeneous unguided debate often underperforms isolated self-correction; majority pressure
suppresses correct dissent.

- **[L5] Fight sycophancy structurally:** heterogeneous models (different providers/families
  per seat), blind first round (agents produce independent positions before seeing each
  other), anonymized transcripts (no model names/prestige cues), and an explicit
  devil's-advocate role in at least one round.
- **[L6] Cap debate rounds and treat non-consensus as signal.** If the panel still
  disagrees after N rounds, that *is* the answer: WAIT. Don't force convergence.

### 2.4 LLM-specific failure modes (hallucination in financial pipelines)

Financial hallucinations execute before anyone can verify them, and confident presentation
makes them dangerous. Practitioner consensus: *never ask the LLM what the price/news is —
tell it*, injecting only data your own pipeline fetched.

- **[L7] LLMs get a closed data packet; no tool-use for facts.** All market data, indicator
  values, news, and portfolio state are computed by code and injected into the prompt. The
  LLM has no live retrieval and no calculator duties — any number it needs is pre-computed.
- **[L8] Validate structured output hard.** JSON schema validation, enum checks, range
  checks, and cross-field checks (e.g. `action=BUY` requires `conviction ≥ floor`); a
  malformed response is a failed vote, never a "best-effort parse".

### 2.5 Engineering lessons from Freqtrade / Hummingbot and practitioner reports

Mature engines converge on the same hard-won patterns: order lifecycle state machines with
executor objects owning their orders end-to-end; WebSocket connections drop silently
(close code 1006) so you must auto-reconnect, resubscribe, *and reconcile state after
reconnect*; bots break on partial fills, timeouts, 5xx responses, and duplicate orders
after retries; most production incidents originate in the exchange integration layer, not
strategy code.

- **[L9] Model the order lifecycle as an explicit state machine** with idempotency keys
  (client order IDs) so retries can never double-submit.
- **[L10] Reconciliation is a first-class subsystem**, run at startup, after every
  reconnect, and periodically — local state is a cache of exchange truth.
- **[L11] Budget most engineering effort for the broker adapter layer.** Strategy logic is
  the easy part.

### 2.6 Look-ahead bias in LLM backtesting

LLMs memorize historical financial data from pretraining; backtests inside the model's
knowledge window are contaminated (Sharpe decays >50% out-of-window in published studies).
Code-level point-in-time correctness cannot rule out *model-level* leakage.

- **[L12] Classical backtesting validates plumbing and risk logic, not LLM alpha.**
  The only honest evaluation of the decision engine is *forward*: paper trading and
  post-knowledge-cutoff periods. The doc treats backtest results over pre-cutoff data as
  "pipeline tests", never as performance evidence.

---

## 3. Design Principles

1. **Deterministic shell, probabilistic core.** The LLM panel is a black box that emits a
   *proposal*. Everything around it — data prep, validation, risk, execution, accounting —
   is deterministic, testable code. [L2][L3]
2. **Exchange is the source of truth.** Local DB state is a reconciled projection. [L4][L10]
3. **Fail closed.** Any uncertainty (unparseable LLM output, stale data, failed
   reconciliation, breached limit) resolves to *no trade* and, when severe, to a halted
   basket or a global kill switch. Missing a trade is always acceptable; an unintended
   trade never is.
4. **Point-in-time data discipline.** Every datum carries `observed_at`; a decision context
   is a frozen snapshot. This enables honest replay and keeps simulated/backtest/live paths
   identical. [L12]
5. **Everything is a plugin behind a small interface.** Core packages depend only on
   interfaces; concrete adapters are registered by entry-point name and selected by config.
6. **Config lives in the database, not env files.** Baskets, panels, risk limits are
   GUI-edited, versioned rows. Env vars hold only secrets and bootstrap settings (DB path,
   mode). Every config change is recorded so past decisions can be replayed against the
   config that produced them.
7. **One writer per resource.** A basket's runner is the only thing that trades that
   basket's assets; the global risk manager is the only thing that can halt everything.
   No concurrent mutation of the same position from two code paths.

---

## 4. Domain Model

Core entities (persisted; names are the ubiquitous language of the codebase):

| Entity | Meaning | Key fields |
|---|---|---|
| **Instrument** | A tradable thing, asset-class-aware | `symbol`, `asset_class` (crypto/equity/index_etf), `venue`, `quote_currency`, `lot_size`, `min_notional`, `tick_size`, `trading_hours` |
| **Basket** | GUI-created group of 1..N instruments with its own config | `name`, `instruments[]`, `decision_mode` (per_asset \| basket), `schedule`, `panel_config_id`, `risk_policy_id`, `status` (active/paused/halted) |
| **PanelConfig** | The agent panel definition | `seats[]` (role, provider, model, temperature), `debate_protocol`, `max_rounds`, `consensus_rule`, `token_budget` |
| **RiskPolicy** | Tier-1 limits attached to a basket (and defaults per asset) | see [§6.6](#66-risk-management-two-tier) |
| **GlobalRiskPolicy** | Tier-2 limits: one instance per venue portfolio (hard, synchronous), plus cross-venue aggregate rules over the PortfolioAggregate | see [§6.6](#66-risk-management-two-tier) |
| **DecisionCycle** | One run of the loop for one basket | `basket_id`, `started_at`, `context_snapshot_id`, `status`, `outcome` |
| **ContextSnapshot** | Frozen, point-in-time input packet the panel saw | candles, indicators, news items, portfolio slice, all with `observed_at` |
| **Deliberation** | Full debate transcript | per round, per seat: prompt hash, raw response, parsed vote |
| **Decision** | The panel's validated output for one instrument | `action` (BUY/SELL/HOLD/WAIT), `conviction` (0–1, normalized from seat ratings per §6.5), `size_hint`, `reasoning_summary`, `dissent` (minority views) |
| **OrderIntent** | Risk-approved, sized, ready-to-submit instruction | `instrument`, `side`, `qty`, `order_type`, `limit_price?`, `client_order_id`, `risk_checks[]` |
| **Order** | Exchange-acknowledged order and its lifecycle | `client_order_id`, `exchange_order_id`, `state`, `fills[]` |
| **Position / Ledger** | Reconciled holdings & balances | per instrument: `qty`, `avg_entry`, `realized_pnl`, `unrealized_pnl`; per currency: `free`, `locked` |
| **RiskEvent** | Any limit trigger, veto, halt, or kill-switch activation | `tier`, `rule`, `scope`, `action_taken` |

Relationships: a **Portfolio** (one per venue account) contains Positions; Baskets reference
Instruments but *Positions belong to the Portfolio* — this is what makes Tier-2 risk
meaningful when two baskets accidentally hold correlated exposure. A **PortfolioAggregate**
(read-only, computed) sums all venue Portfolios into one USD-valued summary — USD-stablecoins
valued at par with a depeg sanity check — and is what the cross-venue Tier-2 rules and the
dashboard's equity view read.

**Decision modes** (per basket, GUI-selected):

- `per_asset` — the panel runs once per instrument; each instrument gets an independent
  `Decision`. Context includes a brief "sibling positions in this basket" section so the
  panel is aware of, but not responsible for, the rest of the basket.
- `basket` — the panel runs once per cycle over the whole basket and returns a `Decision`
  *per instrument* in a single structured response (an allocation-style review). Costs more
  tokens, sees cross-asset structure. [Basket-level debate is the richer mode the spec
  asks for; per-asset is the cheaper, easier-to-validate default.]

---

## 5. High-Level Architecture

```
                        ┌────────────────────────────────────────────┐
                        │                 Dashboard / GUI            │
                        │  basket CRUD · panel config · risk config  │
                        │  monitoring · manual override · KILL SWITCH│
                        └───────────────┬────────────────────────────┘
                                        │ REST/WS (FastAPI)
┌───────────────────────────────────────▼───────────────────────────────────────┐
│                              Control Plane                                    │
│   ConfigStore (versioned, DB)   ·   Scheduler   ·   BasketRunner supervisor   │
└───────┬───────────────────────────────────────────────────────────────┬───────┘
        │ one runner per active basket                                  │
┌───────▼──────────────────────────────────────────────┐   ┌────────────▼───────┐
│                 BasketRunner (cycle loop)            │   │  Global Risk Mgr   │
│                                                      │   │  (Tier 2, per-venue)│
│  1. ContextBuilder ──► ContextSnapshot (frozen)      │   │  portfolio exposure │
│       ▲        ▲          ▲            ▲             │   │  drawdown budget    │
│  MarketData  Indicators  NewsHub     Ledger(RO)      │   │  correlation caps   │
│   providers   engine     + RAG                       │   │  circuit breakers   │
│                                                      │   │  KILL SWITCH        │
│  2. DecisionEngine (panel debate) ──► Decision(s)    │   └─────────▲──────────┘
│  3. Tier-1 Risk (basket/asset)   ──► OrderIntent(s)──┼─────────────┘ veto/resize
│  4. ExecutionService             ──► Orders          │
│  5. Persistence (event log + projections)            │
└───────────────┬──────────────────────────────────────┘
                │ BrokerAdapter interface
   ┌────────────┼──────────────────┐
   │            │                  │
┌──▼───┐   ┌────▼────┐   ┌─────────▼────────┐
│ CCXT │   │ Alpaca  │   │ SimBroker        │
│crypto│   │ equities│   │ (paper/backtest) │
└──────┘   └─────────┘   └──────────────────┘
        Reconciler runs against whichever adapter is live
```

Key structural choices:

- **One async process, one `BasketRunner` task per active basket.** Runners are independent
  (own schedule, own cycle) but share the MarketData cache, the Ledger, and the Global Risk
  Manager. Python asyncio is sufficient at minutes-scale cadence; no message broker needed.
- **The Global Risk Manager sits *between* Tier-1 approval and execution** and is consulted
  synchronously for every `OrderIntent`. It is also an independent watchdog: it monitors the
  reconciled portfolio continuously and can pause baskets or trip the kill switch without
  waiting for a cycle. [L3] Tier-2 enforcement is **per venue** (each venue portfolio has
  its own hard, synchronously checked limits); cross-venue rules run against the
  **PortfolioAggregate** summary and are enforced by the watchdog (pause/kill), since venues
  cannot block each other's orders synchronously — see §6.6.
- **Persistence is an append-only event log plus relational projections** (see §6.8). The
  event log gives the audit trail the spec demands; projections give the dashboard cheap
  queries.
- **Modes (live / paper / simulation) differ only in adapter wiring.** Same runners, same
  risk code, same persistence. `SimBroker` implements the exact `BrokerAdapter` interface
  with a matching-engine-lite (fills with configurable slippage/fees). This is what makes
  paper results predictive of live behavior. [L12]

---

## 6. Module Specifications

### 6.1 Control Plane: ConfigStore, Scheduler, Supervisor

**ConfigStore.** All user-editable configuration (Baskets, PanelConfigs, RiskPolicies,
news source lists, provider settings sans secrets) is stored in the DB with monotonically
increasing `version` per object; updates create new versions rather than overwrite. Each
`DecisionCycle` records the exact config versions used. Secrets (API keys) stay in env /
OS keyring and are referenced by name (`secret_ref: "BINANCE_API_KEY"`), never stored in DB.

**Scheduler.** Per-basket cron-like schedule (`every 10m`, `every 1h at :05`,
`market_open+15m` for equities). Uses the instrument's `trading_hours` calendar: an
equities basket simply doesn't cycle when the market is closed; crypto runs 24/7. Skips a
tick if the previous cycle for that basket is still running (no overlap, ever).

**Supervisor.** Starts/stops `BasketRunner` tasks in response to config changes (basket
activated/paused in GUI), restarts crashed runners with exponential backoff, and marks a
basket `halted` (requires human un-halt in GUI) after N consecutive cycle failures.

### 6.2 Market Data Layer

Interface:

```python
class MarketDataProvider(Protocol):
    async def get_candles(
        self, instrument: Instrument, timeframe: str, limit: int, end: datetime | None = None
    ) -> CandleSeries: ...
    async def get_quote(self, instrument: Instrument) -> Quote: ...
    def capabilities(self) -> DataCapabilities: ...  # timeframes, history depth, delay
```

- Implementations: `CcxtMarketData` (crypto), `AlpacaMarketData` (US equities), and
  `ReplayMarketData` (serves recorded/simulated series for backtest & simulation; honors
  `end=` for point-in-time slicing).
- **Normalization**: all providers return the same `CandleSeries` (UTC timestamps,
  `observed_at` stamped, gaps explicit). Equity series carry session metadata (regular vs.
  extended hours) so indicators don't compute across overnight gaps naively.
- **Staleness policy**: every `CandleSeries`/`Quote` has `max_age`; the ContextBuilder
  refuses to build a snapshot from stale data (→ cycle aborts as `DATA_STALE`, no trade).
  Fail closed.
- Pull-based REST fetching is the default (adequate at ≥1-minute cadence, far simpler than
  maintaining WebSocket state). WebSocket streaming is an optional provider capability
  added later only if a strategy needs it — practitioner reports make clear that silent
  WS disconnects and post-reconnect reconciliation are a major complexity tax. [L11]

### 6.3 Indicator / Feature Engine

- Pure functions over `CandleSeries` → `IndicatorSet` (RSI, MACD, EMA/SMA, ATR, Bollinger,
  volume profile; registry-extensible).
- Computed per timeframe (e.g. 1h/4h/1d), *by code, never by the LLM*. [L7]
- Output includes both values and pre-verbalized descriptions ("RSI(14)=71.3 —
  overbought territory") because the panel consumes text; verbalization is deterministic
  and unit-tested so wording can't drift between runs.
- ATR feeds risk sizing (§6.6), so the indicator engine is a dependency of risk, not just
  of the panel.

### 6.4 News & Context Layer

```python
class NewsSource(Protocol):
    source_id: str

    async def fetch_latest(self) -> list[RawNewsItem]: ...  # title, body, url, published_at


class RelevanceFilter(Protocol):
    def relevant(self, item: NewsItem, instruments: list[Instrument]) -> float: ...
```

- **Sources are plugins** registered by id: `cointelegraph` (RSS), `rss_generic` (arbitrary RSS
  feeds — cheap way to add many sources); future: SEC filings for equities (free EDGAR API).
  **No paid APIs in v1** (decided 2026-07-26): NewsAPI is dropped — its free tier delays
  articles 24 h and forbids commercial use, making it worthless as a trading signal, and paid
  tiers are out of scope; a `newsapi` plugin can be added later behind the same interface.
  A basket's config selects which sources feed it.
- **NewsHub pipeline**: fetch → normalize → dedupe (URL hash + embedding similarity in
  vector store) → relevance-score per instrument → store with `published_at` *and*
  `observed_at`. The ContextBuilder selects top-K relevant items for the snapshot,
  filtered by `observed_at <= cycle_start` (point-in-time discipline; in replay mode this
  is what prevents feeding tomorrow's news to yesterday's decision). [L12]
- Vector store (ChromaDB or sqlite-vec) serves two purposes: dedup and retrieval of
  *historical context* ("what did we know about X last week") for the panel's context.
  Embedding computation is CPU-bound and runs in a thread executor — the trading loops share
  one asyncio event loop and must never stall on it.
- **Sentiment is the panel's job, not the pipeline's** — the pipeline only filters and
  ranks; interpreting news is exactly what the LLM seats are for.

### 6.5 Decision Engine (the panel)

The heart of the system, and the part the research says to treat most skeptically.

**Provider abstraction:**

```python
class LLMProvider(Protocol):
    provider_id: str

    async def complete(self, req: CompletionRequest) -> CompletionResult: ...

    # CompletionResult: text, token_usage, latency_ms, model_fingerprint
```

Implementations: `openai_compat` (covers OpenAI, OpenRouter, Qwen, local vLLM — one
adapter, many endpoints), `anthropic`, `gemini`. Each **seat** in a panel binds
(role, provider, model, params). A panel is *data*, not code — fully GUI-configurable.

**Panel structure** (defaults informed by TradingAgents [L1] and sycophancy research [L5]):

| Seat | Role prompt focus | Evidence slice |
|---|---|---|
| Technical Analyst | trend/momentum from indicators | candles + indicators only |
| News/Sentiment Analyst | catalysts, narrative shifts | news items + historical RAG context |
| Macro/Risk Skeptic (devil's advocate) | what could go wrong; argues against the emerging majority | full context + explicit contrarian instruction |

Giving seats *different evidence slices* is deliberate: it manufactures genuine
disagreement, which debate research shows is what makes debate work.

**Debate protocol** (a pluggable `DebateProtocol`; default = "blind-then-debate"):

1. **Round 0 (blind)**: each seat produces an independent structured position without
   seeing the others. [L5]
2. **Rounds 1..N (N ≤ 2 by default)**: each seat sees the *anonymized* transcript
   ("Analyst A argued …") and may revise. Model names are never shown to other seats.
3. **Consensus rule (deterministic code, not an LLM)**, with explicit semantics:
   - **HOLD vs WAIT.** `HOLD` is an affirmative "keep the current position, do nothing"
     vote — it counts *against* acting. `WAIT` is the no-signal outcome (non-consensus,
     degraded panel, explicit uncertainty). Both produce no order, but they are distinct
     research signals and are recorded as such. A majority for `HOLD` yields a `HOLD`
     decision.
   - **Qualified majority is counted over the *original* seat count**, never over remaining
     voters: with 3 seats, BUY/SELL needs ≥ 2 of 3 — an abstention can never make a
     minority decisive.
   - **Conviction mapping.** Seats rate conviction 1–5 (§7.1); the Decision's conviction is
     normalized to 0–1 as `((mean of agreeing seats' ratings) − 1) / 4 × agreement_fraction`.
     All thresholds (e.g. Tier-1 min conviction) are stated on the 0–1 scale.
   - No qualified majority after N rounds → `WAIT` with the disagreement recorded as
     `dissent`. [L6]

**Hard output contract** [L8]: each seat must return JSON matching a published schema
(§7.1). Validation failures get one repair attempt (re-prompt with the error); a second
failure marks the seat's vote `ABSTAIN` for the round. A panel where >⅓ of seats abstain
yields `WAIT (PANEL_DEGRADED)`.

**Fallback & budget:**

- Per-seat fallback chain (e.g. OpenRouter slot → Anthropic) taken from config; a fallback
  model inherits the seat's role, and the substitution is recorded in the transcript.
  If fallbacks leave two or more seats on the same provider+model, the cycle is flagged
  `PANEL_HOMOGENEOUS` (event + dashboard) — heterogeneity is a design control [L5] and its
  silent loss must be visible; config may escalate the flag to `WAIT`.
- Per-cycle token/cost budget from `PanelConfig`; exceeding it truncates debate early and
  resolves with whatever rounds completed. Costs are persisted per cycle (the dashboard
  shows $/decision — essential for a research testbed comparing panel configurations).

**What the panel never does** [L3][L4][L7]:

- No tool use, no web access, no calculator tasks — the ContextSnapshot is its whole world.
- Never told account balance in raw form beyond a normalized "position: long 0.4 units,
  +2.3% unrealized; basket risk budget used: 40%" slice — enough for context, no ledger
  authority.
- Never sizes orders in absolute terms. It emits `size_hint ∈ {none, quarter, half, full}`
  relative to the *risk-allowed* maximum; deterministic sizing happens in risk (§6.6).

### 6.6 Risk Management (two-tier)

All rules are deterministic, unit-tested code evaluated against the *reconciled* ledger.
Each check produces a recorded `RiskCheckResult` (pass/fail/adjusted), so every order
carries its full risk provenance.

**Every limit is GUI-configurable data, and all risk state survives restart.** The Tier-1
and Tier-2 values in the tables below are seed defaults, not constants: they live as
versioned rows in the ConfigStore (§6.1), are edited from the dashboard (§6.10 — loosening
Tier-2 requires an extra typed confirmation), take effect at the next cycle boundary, and
persist across bot stops/restarts. Risk *state* persists too: a tripped kill switch, halted
baskets, the high-water mark, and day-start equity are stored in the DB and restored by the
startup sequence (§8.2) — a restart never silently un-halts anything or resets a limit.

**Tier 1 — Basket/Asset policy** (attached to each basket; per-asset overrides allowed):

| Rule | Example default |
|---|---|
| Max position size per instrument (% of basket budget) | 25% |
| Max basket allocation (% of portfolio equity) | 10% |
| Min conviction to act | 0.6 |
| Cooldown after a trade in the same instrument | 2 cycles |
| Max trades per day per basket | 6 |
| Stop-loss / take-profit policy (venue-native protective orders, §6.7) | 2×ATR / 3×ATR |
| Long-only: SELL is reduce-only (`qty ≤ current position`); SELL while flat ⇒ veto | always on in v1 |
| Max consecutive losses before basket auto-pause (a loss = a closed round-trip with realized PnL < 0, partial fills aggregated per position) | 4 |

Tier-1 output: reject, or approve with **deterministic position sizing** —
volatility-normalized, with explicit units (ATR is *absolute*, in quote currency per unit):

```
risk_amount   = basket_budget × risk_per_trade × size_hint_fraction   # quote currency
stop_distance = stop_multiple × ATR                                   # e.g. 2 × ATR
qty           = risk_amount / stop_distance
```

then clamped by max-position %, basket allocation, and exchange minimums from `Instrument`.
`risk_amount` is only a truthful "amount at risk" because the stop at `stop_distance` is held
by a venue-native protective order (§6.7); where the venue can't hold it, the
`unprotected_position` haircut applies (§6.7).

**Tier 2 — Global policy** (evaluated for every OrderIntent and continuously as a
watchdog). Enforcement is split in two, reflecting that venues cannot see each other:

- **Per-venue hard limits** — each venue portfolio has its own Tier-2 policy instance,
  checked synchronously before any order reaches that venue (exposure, order sanity,
  rate caps).
- **Cross-venue aggregate rules** — computed against the **PortfolioAggregate** (§4): all
  venue portfolios summed into one USD-valued summary, USD-stablecoins at par with a depeg
  sanity check (aggregation freezes — and halts new orders — if parity drifts beyond ±2%).
  Aggregate rules (total drawdown, daily loss, correlated clusters spanning venues) are
  enforced by the watchdog through basket pause or kill switch, since a single order to one
  venue cannot be blocked synchronously by another venue's state.

| Rule | Example default |
|---|---|
| Max gross exposure (% of equity) | 80% |
| Max single-instrument exposure across all baskets | 20% |
| Max correlated-cluster exposure (static clusters: e.g. {BTC, ETH, crypto basket} share one bucket) | 40% |
| Max daily loss (halt all new orders for the day) | 3% |
| Max drawdown from high-water mark (trip **kill switch**) | 10% |
| Order sanity: notional vs. 30-day average volume, price deviation vs. last quote | ±5% price collar |
| Rate limit: max orders/hour globally | 20 |

Tier 2 may **veto** or **shrink** (reduce qty to fit remaining headroom) an intent. Shrink
results below exchange minimums become vetoes.

**External flows never count as PnL.** Daily-loss and drawdown baselines are flow-adjusted:
the reconciler's `EXTERNAL_CHANGE` events (deposits, withdrawals, manual trades) adjust the
high-water mark and day-start equity, so a withdrawal can never trip the kill switch as a
phantom drawdown and a deposit can never mask a real loss. The daily-loss "day" boundary is
UTC for crypto and the exchange session for equities, computed on mark-to-market equity.

**Kill switch** (the one big red button; GUI-prominent and also automatic):

- Trips on: max drawdown breach, reconciliation mismatch above tolerance (§8.2), or manual
  GUI click.
- Effect: cancel all open orders, halt all runners, optionally flatten positions (config:
  `flatten_on_kill: bool`, default *false* — flattening into a broken market can be worse).
  Requires human re-arm with a typed confirmation in the GUI.

The correlation buckets are deliberately *static config* in v1 (crypto cluster, tech
equities cluster, …). Estimating live correlation matrices is future work; static buckets
already prevent the classic failure of two baskets independently maxing out on
near-identical assets — the exact "concentrated positions via component perturbation"
failure TradeTrap demonstrates. [L3]

### 6.7 Execution Layer

```python
class BrokerAdapter(Protocol):
    venue_id: str

    async def submit(self, intent: OrderIntent) -> OrderAck: ...
    async def cancel(self, order_ref: OrderRef) -> CancelAck: ...
    async def fetch_order(self, order_ref: OrderRef) -> OrderStatus: ...
    async def fetch_open_orders(self) -> list[OrderStatus]: ...
    async def fetch_positions_and_balances(self) -> AccountState: ...
    def capabilities(
        self,
    ) -> BrokerCapabilities: ...  # order types, protective orders (OCO/bracket), fractional, hours
```

Implementations: `CcxtBroker` (Binance live + testnet via one adapter), `AlpacaBroker`
(US equities, paper + live — the reference non-crypto integration), `SimBroker`
(deterministic fills with slippage/fee model, used for simulation *and* backtest).

**Order lifecycle state machine** [L9]:

```
PENDING_SUBMIT ─► SUBMITTED ─► OPEN ─► PARTIALLY_FILLED ─► FILLED
      │               │          │            │
      │               ▼          ▼            ▼
      └────────► SUBMIT_UNKNOWN  CANCELLED / EXPIRED / REJECTED
```

- **Idempotency**: every `OrderIntent` carries a deterministic `client_order_id`:
  `{env_prefix}-{base32(blake2s(basket_id|cycle_id|instrument|seq)[:10])}` — ≤ 36 chars and
  charset-safe for Binance's `newClientOrderId` rules (`^[\.A-Z\:/a-z0-9_-]{1,36}$`),
  recomputable from durable data (see IMPLEMENTATION_PLAN §2.2). On any
  timeout/5xx/disconnect during submit,
  the order enters `SUBMIT_UNKNOWN` and the *only* legal next step is querying the
  exchange by client order id — never blind resubmission. This is the single most
  important defense against the duplicate-order-after-retry failure practitioners report.
  [L9][L11]
- **Order policy**: default is limit orders at a marketable price with a TTL
  (`good_till = cycle_interval − buffer`); unfilled remainder is cancelled at TTL and the
  fill ratio recorded. TTL is **bot-enforced** (the ExecutionMonitor cancels at expiry) —
  Binance spot offers only GTC/IOC/FOK, there is no venue-side good-till-time. Market orders
  only where `BrokerCapabilities` says the book is deep enough and the risk policy allows.
- **Protective exits are venue-native and placed at entry.** A cycle-based system cannot
  babysit stops itself — between cycles the *venue* must hold the stop. Where
  `BrokerCapabilities` declares support (Binance spot OCO / `STOP_LOSS_LIMIT`, Alpaca
  bracket/stop orders), every entry fill is immediately followed by linked protective legs
  implementing the Tier-1 SL/TP policy; the state machine tracks entry + protective legs as
  one group (one leg fills ⇒ cancel the sibling; legs resize on partial entry fills).
- **Unprotected positions are an explicit, visible risk.** On venues/instruments without
  protective-order support, the position is flagged `unprotected_position`: Tier-1 applies a
  sizing haircut (config; default 50% of normal size), the flag is recorded in
  `RiskCheckResult`, and it is surfaced in the ContextSnapshot so the panel weighs it during
  deliberation.
- **Partial fills are normal**: position/ledger updates flow from *fills*, not from order
  terminal states.
- An **ExecutionMonitor** task polls open orders (REST; cadence ~10s while orders are
  open) and emits fill events. It owns each order end-to-end (Hummingbot's executor
  pattern).

### 6.8 Portfolio, Ledger & Reconciliation

- The **Ledger** maintains positions, balances, realized/unrealized PnL, high-water mark —
  updated only from fill events and reconciliation, exposed read-only to everything else. [L4]
- **Reconciler** (§8.2) runs at startup, after any connectivity gap, and every M minutes:
  fetches `AccountState` from each broker, diffs against the ledger, and classifies:
  match / explainable drift (fees, funding, dust) → auto-correct + log /
  **corporate action** (equities: splits, dividends — matched against the venue's
  corporate-announcements data) → adjust ledger + log `CORPORATE_ACTION`; unmatched ⇒ halt
  only the affected instrument / **venue reset** (testnet balances back at defaults, open
  orders gone) → `VENUE_RESET` → halt + notify, *not* kill switch / unexplained mismatch →
  `RECON_MISMATCH` risk event → halt affected baskets (above tolerance: kill switch). [L10]
- External changes (user manually trades on the exchange, deposits, withdrawals) are
  *expected*, detected by the reconciler, and absorbed as ledger adjustments with a
  logged `EXTERNAL_CHANGE` event.

### 6.9 Persistence

- **Append-only event log** (`events` table: `seq`, `ts`, `type`, `aggregate_id`,
  `payload_json`): cycle started, snapshot frozen, seat responded, decision made, risk
  checked, order submitted, fill received, recon completed, risk event, config changed.
  This is the audit trail — a past decision is replayed by reading its events.
- **Projections** (normal relational tables kept in the same transaction): current
  positions, order book, cycle summaries, cost per cycle, equity curve. The dashboard
  reads only projections.
- **SQLite + WAL mode is sufficient** for a single-process research bot (one writer
  thread/queue). The persistence interface is thin enough that Postgres is a config swap
  if the project ever needs concurrent writers or remote access.
- Raw LLM transcripts and ContextSnapshots are large → stored compressed, referenced from
  events, with a retention policy (e.g. full transcripts and full snapshots 90 days;
  summaries and snapshot hashes forever — the hash keeps replay verifiable after compaction).

### 6.10 Dashboard / GUI

FastAPI + server-rendered or light SPA frontend; WebSocket for live updates. Three jobs:

1. **Configure** — CRUD for Baskets (instrument picker with venue search, decision mode,
   schedule), PanelConfigs (seat editor: role/provider/model, protocol, budgets),
   RiskPolicies (tier-1 forms), GlobalRiskPolicy (tier-2, extra confirmation to loosen) —
   every limit and risk control in §6.6 is editable here and persisted in the DB; nothing
   risk-related is hardcoded.
   Validation server-side against the same pydantic schemas the engine uses. Publishing a
   config change = new version; runners pick it up at their next cycle boundary.
2. **Monitor** — portfolio & equity curve, per-basket cycle history, decision drill-down
   (the full debate transcript, the exact snapshot, risk check results, resulting orders —
   the "why did it do that" view, which is the core research artifact), cost tracking per
   panel config.
3. **Control** — pause/resume basket, un-halt, manual close position (goes through the
   same OrderIntent/risk/execution path — no side doors), and the **kill switch**.

Auth: single-user token/password is enough (research tool), but *mandatory* whenever the
dashboard binds to non-localhost — this thing can move real money.

---

## 7. Data Contracts

### 7.1 Seat response schema (per instrument)

```json
{
  "action":     "BUY | SELL | HOLD | WAIT",
  "conviction": "integer 1–5 (normalized to 0–1 by the consensus rule, §6.5)",
  "size_hint":  "none | quarter | half | full",
  "thesis":     "string, ≤ 200 words",
  "key_risks":  ["string", "..."],
  "invalidation": "what observable fact would change this view"
}
```

In `basket` decision mode the response is `{ "assessments": { "<symbol>": {…} }, "basket_view": "string" }`.
Schema-validated (pydantic); one repair attempt; then `ABSTAIN`. [L8]

### 7.2 ContextSnapshot (frozen input packet)

```json
{
  "snapshot_id": "uuid", "as_of": "ISO8601",
  "instruments": { "<symbol>": {
      "quote": {...}, "candles": {"1h": "…summarized…", "4h": "...", "1d": "..."},
      "indicators": {"1h": {...}, "4h": {...}, "1d": {...}},
      "position": {"qty": 0.4, "upl_pct": 2.3, "held_cycles": 5} | null
  }},
  "news": [ {"source": "...", "published_at": "...", "title": "...", "summary": "...", "relevance": 0.83} ],
  "basket_state": {"risk_budget_used_pct": 40, "recent_actions": [...]},
  "constraints": {"actions_allowed": ["BUY","SELL","HOLD","WAIT"], "note": "sizing is decided by risk mgmt"}
}
```

Serialized once, hashed, stored; every seat prompt embeds this same packet. [L7]

### 7.3 Decision → OrderIntent

`Decision` (panel, per instrument) + `RiskPolicy` ⇒ `OrderIntent`
(`client_order_id`, exact `qty`, `order_type`, `limit_price`, `ttl`, attached
`risk_checks[]` with every rule's pass/adjust/veto result and numbers).

---

## 8. Reliability Engineering

### 8.1 Failure-mode table (what fails, what we do)

| Failure | Detection | Response |
|---|---|---|
| Market data unavailable/stale | staleness policy in ContextBuilder | cycle aborts `DATA_STALE`; no decision, no trade |
| News source down | fetch error/timeout | proceed without that source; snapshot notes the gap so the panel knows news coverage is partial |
| LLM provider down/slow | per-call timeout (e.g. 120 s) | seat falls back per chain; else `ABSTAIN`; >⅓ abstain → `WAIT` |
| LLM returns junk | schema validation | one repair attempt → `ABSTAIN` [L8] |
| Order submit times out | no ack | `SUBMIT_UNKNOWN` → query by client order id; found ⇒ adopt; not found after bounded window ⇒ mark failed **and halt the basket for human review** — a vanished order is never routine [L9] |
| Partial fill at TTL | ExecutionMonitor | cancel remainder, book fills, record fill ratio |
| Exchange 5xx/429 | response codes | bounded retry with jittered backoff, honoring rate-limit budget; escalate to basket-halt on sustained failure |
| Process crash | startup sequence | recover from event log → full reconciliation → resolve all `SUBMIT_UNKNOWN`/open orders → only then resume runners (§8.2) |
| Ledger vs. exchange mismatch | reconciler | small/explainable ⇒ auto-correct + log; else halt baskets / kill switch [L10] |
| External deposit/withdrawal | reconciler `EXTERNAL_CHANGE` | absorb into ledger; flow-adjust HWM and day-start equity (§6.6) — never a phantom drawdown |
| Equity corporate action (split/dividend) | reconciler classification vs. venue announcements | adjust ledger + log `CORPORATE_ACTION`; unmatched ⇒ halt affected instrument |
| Venue reset (testnet) | reconciler: balances at defaults + open orders gone | `VENUE_RESET` → halt + notify; excluded from promotion-gate accounting (§9) |
| Drawdown breach | Tier-2 watchdog | kill switch (§6.6) |
| Config edited mid-cycle | version pinning | cycle finishes on its pinned versions; next cycle uses new ones |
| Clock skew | startup + periodic NTP-vs-exchange-time check | warn > 2 s, halt > 30 s (candle alignment and auth signatures both depend on it) |

### 8.2 Startup / recovery sequence (always the same, every start)

1. Load bootstrap env (mode, DB path, secret refs); open DB; replay/verify projections
   against the event log.
2. For each configured venue: fetch open orders + `AccountState`; adopt any orders that
   have our `client_order_id` prefix; reconcile ledger.
3. Resolve every non-terminal order in the DB to a terminal or monitored state.
4. Restore persisted risk state (kill-switch tripped/armed, halted baskets, high-water mark,
   day-start equity) — a tripped kill switch or halted basket stays that way until a human
   acts in the GUI. Arm the Tier-2 watchdog. Only then does the Supervisor start
   BasketRunners (halted ones excluded).
5. Any step failing → process stays up in "halted" state, dashboard shows why, nothing trades.

### 8.3 Security & operational hygiene

- API keys: trade-only permission, **withdrawals disabled at the exchange**, IP-allowlisted
  where supported. Keys never in DB, never in logs, never in LLM prompts.
- Prompt-injection surface: news text goes into prompts, and headlines are attacker-visible
  input. Mitigation: news is wrapped in clearly delimited data blocks with an instruction
  that content inside is data, not instructions — and, decisively, the panel has no tools
  and its output is schema-bound and risk-gated. Stated honestly: with a 3-seat panel the
  news seat's vote is pivotal whenever the other two split, so an injection *can* flip a
  marginal decision — but it can never size, route, or exceed risk limits on an order.
  [L3 defense-in-depth]
- Structured JSON logging with `cycle_id`/`basket_id` correlation ids throughout.
- Ops alerts (Telegram/email webhook) for: kill switch, basket halt, recon mismatch,
  repeated provider failures, daily summary.

---

## 9. Testing & Validation Pipeline

A change reaches live money only by climbing this ladder:

1. **Unit tests** — risk rules, sizing math, state-machine transitions, schema validation,
   consensus rules, indicator verbalization. The deterministic shell should approach 100%
   coverage; it's what stands between a hallucination and an order. [L3]
2. **Component tests with fakes** — BrokerAdapter contract tests run against `SimBroker`
   *and* recorded fixtures per real adapter (golden request/response), so all adapters
   provably honor identical semantics (esp. partial fills, `SUBMIT_UNKNOWN` recovery).
3. **Scenario/chaos simulation** — full loop against `ReplayMarketData` + `SimBroker` with
   fault injection: drop the data feed mid-cycle, return junk from a seat, time out a
   submit, inject a recon mismatch. Asserts the *response* table in §8.1, not PnL.
4. **Backtest (plumbing only)** — replay historical data through the whole loop. Purpose:
   verify point-in-time discipline, costs, and risk behavior over long horizons.
   **Explicitly not evidence of alpha** when the period predates the models' knowledge
   cutoffs — LLM memorization contaminates such backtests (published Sharpe decay >50%
   out-of-window). The report banner must say which periods are pre/post cutoff. [L12]
5. **Paper trading (the real evaluation)** — forward-only, for weeks. **Primary mode: live
   market data + `SimBroker`** — real data, deterministic fills, no venue-side test
   artifacts. Binance testnet / Alpaca paper run alongside as *adapter integration checks*,
   not as the evidence base: Binance's spot testnet resets to a blank state roughly monthly
   without notice (classified `VENUE_RESET`, §6.8, excluded from gate accounting), and
   testnet fill quality is unrealistically good. Panel configurations are compared here —
   preferably via the shadow A/B harness (two PanelConfigs on the same snapshots, §12),
   since a few weeks of forward PnL alone is statistically weak. Promotion gate examples:
   ≥ 200 cycles, zero unhandled risk events, recon always clean (venue resets excluded),
   then human review.
6. **Live with training wheels** — hard-capped budget (e.g. ≤ 1–2% of equity), tightest
   Tier-2 policy, alerts on every order, widened only manually and gradually.

CI runs 1–3 on every commit; 4 nightly.

---

## 10. Proposed Repository Layout

```
tradebot/
  core/            # domain models (pydantic), events, errors, clock, ids
  interfaces/      # MarketDataProvider, NewsSource, LLMProvider, BrokerAdapter,
                   # DebateProtocol, RiskRule — the plugin surface
  control/         # ConfigStore, Scheduler, Supervisor, BasketRunner
  marketdata/      # ccxt_, alpaca_, replay_ providers; normalization; cache
  indicators/      # feature registry + verbalization
  news/            # sources/, hub (dedupe, relevance), vectorstore
  decision/        # panel, seats, protocols/, consensus, providers/ (openai_compat, anthropic, gemini)
  risk/            # tier1 rules, tier2 global manager, sizing, kill switch
  execution/       # order state machine, execution monitor, brokers/ (ccxt, alpaca, sim)
  ledger/          # portfolio, positions, pnl, reconciler
  persistence/     # event log, projections, migrations
  dashboard/       # fastapi app, api/, ui/
  app.py           # composition root: wiring by mode (live|paper|sim)
tests/
  unit/  contract/  scenario/  fixtures/
```

Composition root (`app.py`) is the only place that knows concrete classes; everything else
imports from `interfaces/` and `core/`. Plugins register via entry points
(`[project.entry-points."tradebot.news_sources"]` etc.), so third-party packages can add
sources/brokers/providers without touching this repo — the extensibility story the spec
asks for.

---

## 11. Migration From the Current Prototype

The prototype validated the concept; the pieces migrate in roughly this order, each step
leaving a working system:

1. **Extract `core/` + `interfaces/`** (pydantic models, the six protocols). Port the
   existing CCXT and mock adapters onto `BrokerAdapter`/`MarketDataProvider`.
2. **Introduce the event log + projections** beside the current SQLite tables; start
   writing events; switch the dashboard to projections; drop old tables.
3. **Replace env-file config with ConfigStore + versioning**; env keeps secrets/bootstrap
   only. Build the basket CRUD GUI on top.
4. **Port the decision engine** onto seats/protocol/consensus; keep the current 3-model ×
   3-round behavior as the first `DebateProtocol` implementation, then add blind round &
   anonymization.
5. **Split risk into Tier 1 / Tier 2**, add the kill switch and reconciler (currently the
   biggest gap vs. this design).
6. **Add `AlpacaBroker` + trading-hours scheduling** — the proof of asset-agnosticism.
7. **Build the scenario/chaos test suite** and gate further work on it.

The existing tests carry over: conftest's dashboard suppression maps to simply not
mounting the dashboard in test wiring.

---

## 12. Open Questions & Future Work

- **Dynamic correlation** for Tier-2 clusters (v1 uses static buckets).
- **Learning from outcomes**: feed each decision's realized PnL back into the RAG store so
  seats can see "the last 5 times we bought on this pattern, results were …". High research
  value; risk of self-reinforcing bias — needs careful design.
- **Panel A/B harness**: run two PanelConfigs on the same snapshots in shadow mode and
  compare forward results — cheap and scientifically clean. *Promoted into
  IMPLEMENTATION_PLAN Phase 7 (no longer future work).*
- **Short selling**: v1 is long-only (SELL is reduce-only, §6.6); short exposure would
  ripple through the risk rules, borrow costs, and the ledger.
- **Options/futures**: the Instrument model deliberately excludes derivatives in v1
  (margin, expiry, and Greeks would ripple through risk and ledger).
- **Multi-venue same-instrument** (BTC on two exchanges): out of scope; one venue per
  instrument for now.

---

## 13. Sources

- [TradingAgents: Multi-Agents LLM Financial Trading Framework (arXiv 2412.20138)](https://arxiv.org/abs/2412.20138) · [project page](https://tradingagents-ai.github.io/)
- [TradeTrap: Are LLM-based Trading Agents Truly Reliable and Faithful? (arXiv 2512.02261)](https://arxiv.org/pdf/2512.02261)
- [Agentic Trading: When LLM Agents Meet Financial Markets (arXiv 2605.19337)](https://arxiv.org/pdf/2605.19337)
- [CONSENSAGENT: Sycophancy Mitigation in Multi-Agent LLM Consensus (ACL 2025)](https://aclanthology.org/2025.findings-acl.1141/)
- [The Cost of Consensus: Isolated Self-Correction vs. Homogeneous Multi-Agent Debate (arXiv 2605.00914)](https://arxiv.org/html/2605.00914v1)
- [Peacemaker or Troublemaker: How Sycophancy Shapes Multi-Agent Debate (OpenReview)](https://openreview.net/forum?id=hkBM5QkFVg)
- [Summoning the Oracle to Slay It: Mitigating Look-Ahead Bias in Financial Backtesting with LLMs (arXiv 2605.24564)](https://arxiv.org/html/2605.24564)
- [Look-Ahead-Freedom as Temporal Non-Interference (arXiv 2607.04958)](https://arxiv.org/html/2607.04958)
- [Look-Ahead Bias in LLM Trading: Why Your Backtest Is Lying (paperswithbacktest)](https://paperswithbacktest.com/course/look-ahead-bias-llm-trading)
- [Temporal Knowledge Leakage in LLM Backtesting — TradingAgents issue #805](https://github.com/TauricResearch/TradingAgents/issues/805)
- [Hummingbot Architecture — Part 1](https://hummingbot.org/blog/hummingbot-architecture---part-1/) · [V2 strategies/executors](https://hummingbot.org/strategies/v2-strategies/)
- [WebSocket closed with 1006: why trading bots lose connection without an error code (DEV)](https://dev.to/matrixtrak/websocket-closed-with-1006-why-trading-bots-lose-connection-without-an-error-code-26ld)
- [LLM hallucinations and failures: lessons from 5 examples (Evidently AI)](https://www.evidentlyai.com/blog/llm-hallucination-examples)

Informal practitioner sources (anecdotes that informed, but do not carry, design decisions):

- [Using LLMs as a sanity check in crypto trading pipelines (practitioner thread)](https://www.blackhatworld.com/seo/using-llms-as-a-sanity-check-in-crypto-trading-pipelines-what-actually-helps.1815069/)
- [Common Pitfalls When Building Your First Crypto Trading Bot (Coin Bureau)](https://coinbureau.com/guides/crypto-trading-bot-mistakes-to-avoid)
