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
| 2 | Deterministic shell to full depth | ⬜ next |
| 3–8 | Data layers → decision engine → brokers → control plane → validation → live (locked) | ⬜ |

## Quick start

```powershell
python -m uv venv --python 3.11 .venv
python -m uv pip install --python .venv\Scripts\python.exe -r requirements-dev.lock
python -m uv pip install --python .venv\Scripts\python.exe -e . --no-deps

.venv\Scripts\python.exe -m tradebot run --mode sim --once   # one full cycle, simulated
.\check.ps1                                                   # format, lint, types, tests
```

`--mode` is required and has no default. Each mode writes to its own database (`data/{mode}.db`)
so a paper ledger can never be read as a live one.

## Layout

```
tradebot/
  core/         domain models, money, clock, ids, errors, events, logging
  interfaces/   the plugin surface — protocols only, no implementations
  persistence/  append-only event log + projections, single writer
  marketdata/   providers (replay in v1)
  indicators/   feature registry + deterministic verbalization
  news/         sources, relevance, hub
  decision/     seats, debate protocols, consensus, LLM providers
  risk/         tier-1 rules, sizing, tier-2 global manager, kill switch
  execution/    order state machine, execution monitor, brokers
  ledger/       positions, PnL, reconciler
  control/      basket runner, scheduler, supervisor
  app.py        composition root — the only module that knows concrete classes
tests/
  unit/ contract/ scenario/ fixtures/
```

## License

Unlicensed research code. Not investment advice.
