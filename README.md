# AI Panel Trading System

A research testbed in which a configurable panel of LLMs analyses market data, indicators and
news, debates over several rounds, and converges on a trading decision per asset — while every
path between that decision and a venue stays deterministic, unit-tested code.

See [DESIGN.md](DESIGN.md) for the architecture and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
for the phase plan and the money-safety rules this code is held to.

> **This system can move real money.** Live mode is opt-in, capped, and cannot be reached by a
> default, a typo, or a missing environment variable. It ships disabled.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Repo, guardrails, money primitives | ✅ done |
| 1 | Walking skeleton, simulation only | ✅ done |
| 2 | Deterministic shell to full depth | ✅ done |
| 3 | Data layers — market data, indicators, news | ✅ done |
| 4 | Decision engine (real LLM providers, debate protocol) | ✅ done |
| 5 | Broker adapters — Binance spot, Alpaca, one contract suite | ✅ done |
| 6 | Control plane, ConfigStore, dashboard | ✅ done |
| 7 | Validation ladder | 🟨 code complete; the paper soak itself is wall-clock time |
| 8 | Live, locked | ⬜ |

## Quick start

```powershell
python -m uv venv --python 3.11 .venv
python -m uv pip install --python .venv\Scripts\python.exe -r requirements-dev.lock
python -m uv pip install --python .venv\Scripts\python.exe -e . --no-deps

.venv\Scripts\python.exe -m tradebot run --mode sim --once   # one full cycle per basket
.venv\Scripts\python.exe -m tradebot run --mode sim          # supervised, on each basket's schedule
.venv\Scripts\python.exe -m tradebot config list --mode sim  # what is configured, and at which version
.\check.ps1                                                   # format, lint, types, tests
```

Configuration lives in the database as versioned rows: a fresh database is seeded with a demo
basket and the default limits, every edit creates a new version, and each cycle records the exact
versions it ran on so a past decision is re-read against the limits that produced it.

`--mode` is required and has no default. Each mode writes to its own database (`data/{mode}.db`)
so a paper ledger can never be read as a live one.

News ingestion is **opt-in** and reaches the public internet, so it is off unless a source is
named — a default that fetches on the first simulated cycle would be a surprise:

```powershell
.venv\Scripts\python.exe -m tradebot run --mode sim --once --news cointelegraph --news coindesk
```

Feeds are read over RSS only, honouring `robots.txt`, with a real `User-Agent`, conditional GET,
and excerpt-only retention (never full article bodies).

The LLM panel is opt-in for the same reason. The default `stub` panel is scripted and offline, so
the demo and the whole test suite run free. `--panel` selects a real one:

```powershell
$env:OPENROUTER_API_KEY = "..."
.venv\Scripts\python.exe -m tradebot run --mode sim --once --panel free
.venv\Scripts\python.exe -m tradebot run --mode sim --once --panel local   # no key, no egress
```

### Backtests and the promotion gates

A backtest replays recorded history through the real loop. It validates **plumbing and risk
behaviour**, never alpha: the models memorized this period, so every report carries a banner
saying so and a per-model cutoff table stating how much of the window each of them may have seen.

```powershell
# record public history (unauthenticated, read-only — no key, no order)
.venv\Scripts\python.exe -m tradebot backtest fetch --symbol BTC/USDT `
    --since 2026-01-01 --until 2026-06-01 --out data\history

.venv\Scripts\python.exe -m tradebot backtest run --mode sim --data data\history
.venv\Scripts\python.exe -m tradebot report promotion --mode paper
.venv\Scripts\python.exe -m tradebot report shadow --mode paper
```

All three write a Markdown report under `reports\` rather than printing: a promotion report is
filed with the decision it justified. `report promotion` exits 5 when a gate fails — ≥200
completed cycles on the evidence base, zero incidents that needed a human, every reconciliation
clean. The last gate is a human's signature, and nothing in the code can supply it.

### Comparing two panels honestly

A basket may carry a **shadow panel**: a challenger deliberated on the *same frozen snapshot* as
the panel that trades, every cycle. That removes the market from the comparison, which is the only
way a few weeks of data can tell two panels apart. The challenger never trades, its cost is
accounted separately, and a failure of it leaves the cycle untouched
([ADR 0018](docs/adr/0018-a-challenger-panel-is-evaluated-on-the-champions-snapshot.md)). Set it in
the dashboard's basket editor, then read `report shadow`.

### Ops alerts

Kill switch, basket halt, reconciliation mismatch, repeated provider failure, and a daily summary
reach a webhook or Telegram. Alerting **tails the event log** rather than hooking into it, so it
can never delay or fail an order, and its cursor advances only after delivery
([ADR 0019](docs/adr/0019-alerts-are-a-log-tail-with-a-persisted-cursor.md)). It is on exactly
when a destination is configured — there is no flag to forget:

```powershell
$env:TRADEBOT_ALERT_WEBHOOK_URL  = "https://hooks.example/..."
$env:TRADEBOT_TELEGRAM_BOT_TOKEN = "..."   # both, or neither
$env:TRADEBOT_TELEGRAM_CHAT_ID   = "..."
```

### Panels and per-seat fallbacks

A panel is **data**, and it declares both the endpoints it may reach and the seats that reach
them — so one form (Phase 6's dashboard) edits the whole thing, and every binding is validated
before anything runs. Each seat has **its own ordered fallback chain**, because a chain that stays
inside one vendor does not survive that vendor's outage, and free slots disappear without notice:

| Seat | Primary | Falls back to |
|---|---|---|
| Technical Analyst | OpenRouter (DeepSeek, free) | LM Studio (local Qwen) |
| News/Sentiment Analyst | OpenRouter (Llama, free) | Gemini |
| Macro/Risk Skeptic | OpenRouter (Qwen, free) | LM Studio (local Mistral) |

Three seats, three *different* backups — sharing one would collapse the panel onto a single model
at exactly the moment it is already degraded. A fallback is a `(provider, model)` pair, so the
same LM Studio instance can back two seats with two different local models. Editing any of this
today means editing [presets.py](tradebot/decision/presets.py); the shapes are already the ones
the GUI will write.

Two mistakes are rejected at configuration time rather than discovered mid-cycle: a chain that
repeats a binding (a retry, not a fallback), and a binding naming a provider the panel does not
declare. Keys are referenced by environment-variable *name*, registered with the log redactor, and
never stored, logged, or put in a prompt.

**Verify the seeded model ids** in [presets.py](tradebot/decision/presets.py) against
openrouter.ai/models before a real run; free slots change without notice.

## Venues

Paper's default venue is **`SimBroker` fed by live market data** — real prices, deterministic
fills, and nothing at a venue that can reset underneath a multi-week soak. A real adapter is opt-in
per venue and runs as an *integration check*, not as the evidence base:

```powershell
$env:BINANCE_TESTNET_API_KEY = "..."; $env:BINANCE_TESTNET_API_SECRET = "..."
.venv\Scripts\python.exe -m tradebot run --mode paper --once --broker binance
```

Keys are read from **mode-specific variable names** (`BINANCE_TESTNET_API_KEY` for paper,
`BINANCE_API_KEY` only for live), so a live key sitting in a paper machine's environment is not
merely unused — it is unreachable.

All three brokers — Binance spot, Alpaca, and `SimBroker` — pass one identical contract suite
covering partial fills, cancel races, `SUBMIT_UNKNOWN` recovery, rejections, linked exit legs and
precision handling. An adapter that diverges fails CI, which is what makes a paper result predictive
of live behaviour. `pytest -m smoke` additionally runs read-only checks against the real test
venues; it skips without keys and is never part of CI.

## Live

Live is wired and **ships disarmed**. It is the paper wiring with the same objects — a separate
live path would mean the thing the soak validated is not the thing that trades — plus two
subtractions: Tier-2 limits clamped to a ceiling that can only tighten, and a readiness gate that
refuses to start unless alerting is configured, the panel is real and reachable, market data
arrives complete, and every stored basket builds for this venue.

Reaching it takes four deliberate acts, in four different places: `--mode live`, the typed phrase,
an armed row in live's own database, and live-only credentials. Any one missing and the process
refuses, listing all of them.

```powershell
.venv\Scripts\python.exe -m tradebot risk arm-live --mode live `
    --max-notional 50 --confirm "I ACCEPT REAL MONEY RISK"
.venv\Scripts\python.exe -m tradebot risk status --mode live   # state, arming, limits in force
```

**Read [docs/OPERATIONS.md](docs/OPERATIONS.md) first** — the pre-live checklist, the arming
procedure, and the incident runbook. Arming is a human act; nothing here does it for you.

## Layout

```
tradebot/
  core/         domain models, money, clock, ids, errors, events, logging
  interfaces/   the plugin surface — protocols only, no implementations
  persistence/  append-only event log + projections, single writer
  venues/       one raw transport per venue, shared by market data and execution
  marketdata/   providers: replay, and Binance spot over ccxt; the history recorder
  indicators/   feature registry + deterministic verbalization
  news/         sources, relevance, hub
  decision/     seats, debate protocols, consensus, LLM providers
  risk/         tier-1 rules, sizing, tier-2 global manager, kill switch
  execution/    order state machine, execution monitor, brokers/ (sim, binance, alpaca)
  ledger/       positions, PnL, reconciler
  control/      config store, scheduler, supervisor, basket runner, startup recovery
  dashboard/    FastAPI + Jinja2 + vendored HTMX; configure, monitor, control
  validation/   backtest harness, promotion gates, reports — all read from the log
  app.py        composition root — the only module that knows concrete classes
tests/
  unit/ contract/ scenario/ smoke/
```

## License

Unlicensed research code. Not investment advice.
