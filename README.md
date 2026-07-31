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
| 6 | Control plane, ConfigStore, dashboard | 🟨 control plane done; dashboard next |
| 7–8 | Validation ladder → live (locked) | ⬜ |

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

Live mode evaluates all four of its preconditions and then still refuses: the wiring is Phase 8's
deliverable and arming it is a human act.

## Layout

```
tradebot/
  core/         domain models, money, clock, ids, errors, events, logging
  interfaces/   the plugin surface — protocols only, no implementations
  persistence/  append-only event log + projections, single writer
  venues/       one raw transport per venue, shared by market data and execution
  marketdata/   providers: replay, and Binance spot over ccxt
  indicators/   feature registry + deterministic verbalization
  news/         sources, relevance, hub
  decision/     seats, debate protocols, consensus, LLM providers
  risk/         tier-1 rules, sizing, tier-2 global manager, kill switch
  execution/    order state machine, execution monitor, brokers/ (sim, binance, alpaca)
  ledger/       positions, PnL, reconciler
  control/      basket runner, startup recovery, venue preflight, live arming
  app.py        composition root — the only module that knows concrete classes
tests/
  unit/ contract/ scenario/ smoke/
```

## License

Unlicensed research code. Not investment advice.
