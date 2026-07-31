# Phase 6, pass 2 — the dashboard

> **Status: delivered.** Kept as the record of what was decided and why. The §10 open questions
> are settled — see [§11](#11-how-it-was-settled). Authoritative specs remain
> [DESIGN.md](../DESIGN.md) §6.10 and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md) §5
> Phase 6; the conventions that outlived this document are in [CLAUDE.md](../CLAUDE.md) and
> [ADR 0014](adr/0014-the-dashboard-is-vendored-and-always-authenticated.md).

## 1. Where pass 1 left things

Landed and tested:

| Piece | Module | What it gives the dashboard |
|---|---|---|
| Versioned `ConfigStore` | [control/config_store.py](../tradebot/control/config_store.py) | Read and publish configuration; every edit is a new version |
| `Scheduler` | [control/scheduler.py](../tradebot/control/scheduler.py) | Next-fire computation; nothing for the UI to call directly |
| `Supervisor` / `BasketWorker` | [control/supervisor.py](../tradebot/control/supervisor.py) | Start/stop, per-basket status, `run_once`, `serve` |
| Multi-basket composition root | [app.py](../tradebot/app.py) | `Application` holds `configs`, `supervisor`, `watchdog`, `states`, `store` |
| Migration `0005` | [migrations/versions/0005_config_store.py](../migrations/versions/0005_config_store.py) | `config_versions` table, `cycles.config_versions_json` |
| Decision record | [ADR 0013](adr/0013-configuration-is-versioned-and-pinned-per-cycle.md) | Why a basket is one versioned document, and why pins matter |

Not started: anything under `tradebot/dashboard/`. There is no HTTP surface at all yet.

## 2. Decisions already made (2026-07-31)

These were chosen deliberately before any code was written. Follow them rather than the defaults.

1. **Stack:** FastAPI + Jinja2 + HTMX, no JS build step, `htmx.min.js` **vendored into the repo**
   under `tradebot/dashboard/static/`. No CDN, no npm — the repo hash-pins everything for
   supply-chain reasons and must work offline. Record the vendored version and its SRI hash in a
   new ADR (0014).
2. **Auth is mandatory always, including on localhost.** This is *stricter* than DESIGN §6.10,
   which only demands it on a non-loopback bind. Rationale accepted at the time: anything that can
   reach localhost otherwise gets the kill switch and config CRUD for free. Token read from
   `TRADEBOT_DASHBOARD_TOKEN`, plus a signed `HttpOnly` session cookie; the server **refuses to
   start** without the token, the same way live mode refuses without its preconditions.
3. **Multi-basket is already in** — the supervisor runs N baskets over one shared venue portfolio.
4. **Staged delivery** — build, run the full gate, report, then continue.

## 3. Dependencies

Add to `[project] dependencies` in `pyproject.toml`, then recompile both locks:

```powershell
python -m uv pip compile pyproject.toml --generate-hashes -o requirements.lock
python -m uv pip compile pyproject.toml --extra dev --generate-hashes -o requirements-dev.lock
python -m uv pip install --python .venv\Scripts\python.exe -r requirements-dev.lock
```

- `fastapi` — the app
- `uvicorn` — ASGI server, run in the same event loop as the supervisor
- `jinja2` — templates
- `python-multipart` — form POSTs
- `itsdangerous` — signed session cookie

`httpx` is already a dependency and doubles as FastAPI's test client transport, so the suite stays
offline. Add a `dashboard` package to `[tool.coverage]`/mypy expectations as needed; the coverage
gate for a non-money package is 80%.

## 4. Proposed layout

```
tradebot/dashboard/
  app.py          # FastAPI factory: create_dashboard(application, *, token) -> FastAPI
  auth.py         # token check, signed session cookie, the refuse-to-start assertion
  routes/
    configure.py  # basket / panel / risk CRUD
    monitor.py    # equity, cycle history, decision drill-down, cost
    control.py    # pause/resume, un-halt, manual close, kill switch
  queries.py      # read-only projection queries; the only place SQL for the UI lives
  templates/      # Jinja2; base.html carries the mode banner
  static/         # htmx.min.js (vendored), one stylesheet
```

`app.py` (the composition root) stays the only module naming concrete adapters — the dashboard
takes a wired `Application` and never builds one. Serving it is a new CLI subcommand
(`tradebot serve --mode sim`) that runs `uvicorn` and `supervisor.serve()` on one event loop.

## 5. The three jobs (DESIGN §6.10)

### Configure

Every Tier-1 and Tier-2 limit from DESIGN §6.6 is editable and DB-persisted; nothing risk-related
is hardcoded. **Server-side validation is the existing pydantic models, unchanged** — the form
surfaces their messages rather than reimplementing the rules. `PanelConfig`'s validators already
prove every seat binding resolves to a declared provider, that a chain repeats no binding, and
that a panel is not all devil's advocates.

- Basket editor: instruments, decision mode, `Schedule` (`every_seconds`, `offset_seconds`,
  `open_delay_seconds`), timeframes, indicators, news sources, `RiskPolicy` form.
- Panel editor: a provider list (endpoint, kind, `secret_ref` **by name**, per-model prices) and a
  seat list where each seat picks its primary provider+model and builds its own ordered fallback
  chain **from the declared providers — a picker, never free text**.
- Tier-2 form: loosening any limit requires an extra typed confirmation (DESIGN §6.10).
- Publishing calls `configs.put(config_id, document, actor=..., note=...)`. That is the whole
  write path; it emits `CONFIG_CHANGED` and the runner picks it up at its next cycle boundary.

### Monitor

Reads **only projections** (DESIGN §6.9). Available today:

| Table | Feeds |
|---|---|
| `cycles` | history, outcome, `cost_usd`, `snapshot_digest`, `config_versions_json` |
| `decisions` | action, conviction, size hint, reasoning summary, dissent, flags |
| `orders`, `fills` | order lifecycle and fill ratio |
| `positions`, `round_trips` | holdings, realized PnL, the equity curve |
| `risk_events` | every veto, halt, and kill-switch trip |
| `reconciliations` | classification per sweep |

The **decision drill-down is the core research artifact**: the full debate transcript, the exact
snapshot, the risk-check results, and the resulting orders. Seat responses, risk-check provenance,
protective placements and config changes are *audit-only* — they have no projector and are read
from the `events` table via `EventStore.read_all()` / a new cycle-scoped query.

Show the pinned config versions on every cycle row and resolve them with `configs.at(ref)`, so a
six-week-old decision is displayed against the limits that produced it.

### Control

- **Pause / resume a basket** = publish a new basket version with `status` `PAUSED` / `ACTIVE`.
- **Un-halt a basket** = `watchdog.resume_basket(basket_id, actor=...)` — a *database state*, not
  configuration. These are two different mechanisms and the UI must not conflate them: a halt is
  the system protecting itself, a pause is the operator's intent.
- **Kill switch** = `watchdog.trip(rule, detail)`; re-arm = `watchdog.rearm(equity, actor=...)`
  behind `assert_rearm_phrase` (`"RE-ARM TRADING"`).
- **Manual close** must go through the normal `OrderIntent` → Tier-1 → Tier-2 → `ExecutionService`
  path. No side doors. This is the one genuinely new piece of logic in pass 2; everything else is
  a view over existing calls.

Every action must appear in the event log with an actor. Pass 1 uses `composition_root` and `cli`
as actor strings — pick a distinct one for the dashboard.

## 6. API surface pass 1 exposes

```python
Application: mode, store, ledger, supervisor, configs, startup, watchdog, states,
             quote_currency, baskets, recover(), equity(), shutdown()

ConfigStore: put(config_id, document, *, actor, note="") -> ConfigRecord
             retire(kind, config_id, *, actor, reason="") -> ConfigRecord
             latest(kind, config_id) / at(ref) / current(kind) / history(kind, config_id)
             baskets() -> tuple[ConfigRecord[Basket], ...]
             global_risk() -> ConfigRecord[GlobalRiskPolicy] | None      # SINGLETON_ID == "global"

Supervisor:  workers, baskets(), worker_for(id), run_once(), serve(resync_seconds=30), sync(), stop()
BasketWorker: basket_id, failures, stopped, cycle(), run(), stop()

Watchdog:    check(equity), trip(rule, detail), rearm(equity, actor=), use_policy(policy),
             halt_basket(id, reason), resume_basket(id, actor=), record_flow(amount, reason)

RiskStateStore: load(), halted_baskets(), status_of(id)
```

## 7. Gotchas from pass 1

- **A basket is one versioned document**, panel and Tier-1 policy included. There is no separate
  `panel` kind yet — editing a shared panel means editing it in each basket. Adding a `panel` kind
  is a registry entry in `DOCUMENTS`, not a redesign, and Phase 7's A/B harness will want it.
- **`config_versions` is not a projection.** Never truncate it in a rebuild; the log's pins
  resolve against it.
- **Secrets:** `put` refuses any document a registered secret value or known key *shape* can be
  found in. A form field that accepts a pasted API key will be rejected at publish time — the UI
  should say so up front and only ever collect a `secret_ref` name.
- **A new basket created while `serve()` runs** is picked up by the resync sweep (30s default);
  *edits* to an existing basket are picked up by its own worker at its next cycle boundary.
- **The watchdog is long-lived** and takes a new Tier-2 policy through `use_policy`, which
  `RunnerBuilder.build` already calls. A dashboard edit needs no extra plumbing for this.
- **Money is `Decimal` and renders as a string.** Do not let a template coerce a limit to float.
- **Mode must be unmissable** in the header and colour-coded (PLAN §2.4).

## 8. Testing

- Unit: auth (missing token refuses to start; bad token 401; cookie signing), form validation
  surfacing pydantic messages, the queries module.
- Contract/integration: FastAPI `TestClient` over a wired sim `Application` — create a basket,
  edit a limit, pause, un-halt, trip the kill switch, and assert **each produced its event**.
- Scenario: the PLAN §6 exit criterion end to end — a basket created, configured, run, paused and
  killed entirely through HTTP, with the event chain asserted.
- The suite must stay offline and free: no CDN fetch, no real server bind.

## 9. Exit criterion

> A basket can be created, configured, run, paused, and killed entirely from the GUI, with every
> action appearing in the event log. (PLAN §5, Phase 6)

## 10. Open questions to settle before or during the build

- Session lifetime and logout behaviour for the signed cookie.
- Whether `tradebot serve` should also accept `--once`-style no-supervisor mode (dashboard only,
  useful for inspecting a stopped system without it trading).
- Whether the equity curve is computed on read from `round_trips` + `positions`, or gets its own
  projection. A projection is cheaper for a long soak but is another thing a rebuild must
  reproduce exactly.

---

## 11. How it was settled

The §10 open questions, and the three decisions taken during the build that a reader of this
document would otherwise be surprised by.

| Question | Answer |
|---|---|
| Session lifetime and logout | **No expiry; logout ends it.** A single-user tool whose operator watches a soak for weeks should not be logged out mid-incident. The mitigation is that the cookie's signing key is derived from `TRADEBOT_DASHBOARD_TOKEN`, so rotating the token and restarting revokes every session at once (ADR 0014). |
| `serve` without the supervisor | **Yes — `--observe`.** Runs uvicorn and the startup recovery, never `supervisor.serve()`. Reads of every kind work; a manual close is refused, because nothing would be polling the order it placed. |
| Equity curve: on read or projected | **On read.** `Queries.equity_curve` accumulates `round_trips.realized_pnl`, which a rebuild reproduces by definition. A projection would be one more table a replay must reproduce byte-identically, and a drift there is a silently wrong research artifact. |

Decided during the build:

- **Startup recovery halting does not stop the dashboard.** DESIGN §8.2 step 5 says the process
  stays up, the dashboard shows why, and nothing trades. So `serve` starts the HTTP surface and
  *not* the supervisor, and exits `3` when it finally stops. Refusing to serve would leave the
  operator with no way to read the reason.
- **A manual close was refused by the metering rules — resolved in `risk/rules.py`, as
  predicted.** Cooldown, the daily trade cap, the loss streak and Tier-2's hourly order rate all
  vetoed a reduce-only SELL, because every one of them was written to meter the *panel*. The fix
  is a narrow `RiskProposal.is_operator_exit` predicate that those four rules stand aside for,
  each recording that it did — a decision by the risk layer, not a bypass around it. Correctness
  and venue legality stay in force. See
  [ADR 0015](adr/0015-an-operator-exit-is-exempt-from-metering-rules.md).
- **Per-model prices are edited as rows, not as a mapping.** Model ids contain `.`, `/` and `:`,
  all of which the field-path parser splits on, so the form carries the id as a *value* and
  `fold_prices` / `unfold_prices` convert between the two shapes. They move a shape, never a rule.

## 12. What landed

```
tradebot/dashboard/
  app.py        create_dashboard(application, *, token, observe_only)
  auth.py       token, refuse-to-start, signed cookie, middleware, --allow-remote bind guard
  forms.py      flat form ⇄ nested document; the models are the only validation
  queries.py    read-only projections; equity curve; CycleDetail for the drill-down
  views.py      render shell, DashboardState, Decimal-exact display filters
  routes/       monitor.py · configure.py · control.py
  templates/    base + login + _macros + _fields + monitor/ configure/ control/
  static/       htmx.min.js (vendored, hash-pinned) · app.css
tradebot/control/manual_close.py    the one genuinely new piece of logic
tradebot/risk/loosening.py          which Tier-2 edits need the typed confirmation
```

Also: `EventStore.read_cycle` (scoped audit read), `Application.manual_close`, and the
`tradebot serve` subcommand.
