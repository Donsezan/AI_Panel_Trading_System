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
.venv\Scripts\python.exe -m tradebot run --mode sim --once
.venv\Scripts\python.exe -m tradebot risk status --mode sim
```

Clearing a safety state is a human act and needs the typed phrase:

```powershell
.venv\Scripts\python.exe -m tradebot risk rearm  --mode sim --confirm "RE-ARM TRADING"
.venv\Scripts\python.exe -m tradebot risk unhalt demo --mode sim --confirm "RE-ARM TRADING"
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

Phases 0–2 are complete: guardrails and money primitives, the sim-only walking skeleton, and the
deterministic shell to full depth (order lifecycle with venue-held protective groups, the
`ExecutionMonitor`, the fills-driven ledger with round trips, the reconciler, both risk tiers,
the kill switch, and the DESIGN §8.2 startup sequence).

Phase 3 (data layers — real market data, the full indicator registry, RSS news) is next; see
IMPLEMENTATION_PLAN §5. Paper and live modes still have no wiring and refuse to start.

The pieces most likely to surprise a reader are recorded as decisions:
[ADR 0004](docs/adr/0004-protective-orders-are-venue-held.md) (protective legs),
[ADR 0005](docs/adr/0005-risk-state-and-history-are-persisted.md) (risk state and history),
[ADR 0006](docs/adr/0006-reconciliation-classifies-before-it-reacts.md) (reconciliation).
