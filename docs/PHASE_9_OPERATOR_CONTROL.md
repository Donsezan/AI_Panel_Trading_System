# Phase 9 — operator control: GUI arm/start/stop, and quarantine

> Authoritative specs remain [DESIGN.md](../DESIGN.md) and [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).
> This records what was decided, why, and what it took to build. Conventions that outlive it move to
> [CLAUDE.md](../CLAUDE.md); decisions move to `docs/adr/`. Written and reviewed before any code
> changed, per the standing rule that a change touching the live-arming gate gets a design pass
> first.

Two independent deliverables, planned together because both are about giving an operator more
control from the dashboard without loosening anything the CLI already guarantees:

| Deliverable | State |
|---|---|
| Live arming, and start/stop of supervision, from the GUI | **built** (2026-08-02) |
| Quarantine: exclude one instrument or a whole basket from automated trading | **built** (2026-08-02) |

Part B shipped first and alone, as the sequencing below intended: it is self-contained, does not
touch the composition root, and Part A's refactor got its own review pass afterwards.

## Why now

Today the bot is controlled entirely from the command line. Live arming
(`tradebot risk arm-live`) writes a database row before the process ever starts; the confirmation
phrase is a CLI flag typed at every invocation; starting or stopping trading means restarting the
process with or without `--observe`. The dashboard is read-mostly, with a few narrow actions
(pause/resume, un-halt, kill switch, manual close) — it cannot arm, start, or stop anything.

The ask is full control from the GUI: view current state, arm/disarm live trading, start/stop the
bot, all visibly and reliably, without freezing during long operations, and without weakening any
safety property `docs/OPERATIONS.md` and `CLAUDE.md` currently guarantee — this is money-moving
code and CLAUDE.md's non-negotiables (fail closed, live is opt-in loud and capped) apply to the GUI
exactly as they apply to the CLI. A second, independent request followed: a way to mark one
instrument or a whole basket as excluded from automated trading — a "quarantine" — while everything
else about it (market data, indicators) keeps running, distinct from both pause (stops the whole
cycle) and halt (the system's own doing, not an operator's judgment call).

## Decisions

Confirmed with the operator via targeted questions before any design was written, because each one
changes a safety-relevant boundary and CLAUDE.md's prime directives make guessing wrong here
expensive:

1. **Start/Stop toggles the Supervisor inside the already-running dashboard process.** Not a new
   process-lifecycle manager launching/killing OS processes — that would abandon DESIGN §5's "one
   async process" principle and add a component (something to watch the watcher) for no safety
   benefit.
2. **Live's four preconditions move from a build-time gate to a runtime gate.** Today
   `_assemble` raises before the `Application` — and therefore the dashboard — exists, so an
   unarmed live process cannot be opened to arm it. The gate moves to the moment supervision is
   asked to start, so a live dashboard can come up disarmed and say so, and be armed from there.
   Full rationale: [ADR 0021](adr/0021-live-arming-and-supervision-move-to-a-runtime-gate.md).
3. **The confirmation phrase is retyped fresh in the GUI, every time**, for both arming and
   starting — never cached in the session, exactly as `docs/OPERATIONS.md` already requires of the
   CLI's `--confirm` flag ("transient by design — an armed database alone must not be enough to
   start").
4. **Stop = pause supervision only**, the GUI equivalent of today's `--observe`. It stays distinct
   from the kill switch (which cancels working orders and requires the typed re-arm phrase).
5. **`tradebot serve --mode live` changes behaviour**: it will no longer refuse outright when
   unarmed. It serves the dashboard showing "NOT ARMED", the same way a halted recovery already
   serves today. `tradebot run --mode live` (the headless path) is unaffected — it keeps refusing
   immediately, since there is no GUI for an unarmed headless process to be armed from.
6. **The GUI's Disarm also stops supervision**, if running — diverging deliberately from the CLI's
   `disarm-live`, which only prevents the next start and never reaches into a running process (it
   has none to reach into). Leaving a live basket cycling against a cap that was just revoked is
   the one silent-drift state this feature must never produce.
7. **Quarantine is versioned configuration**, not persisted risk state like a halt — an ordinary,
   reversible edit through the same `ConfigStore`/`RiskPolicy` path every other Tier-1 limit uses,
   no typed phrase.
8. **Quarantine is a Tier-1 veto rule**, not a scheduling change. The cycle keeps running — market
   data, indicators, and the panel's deliberation are untouched — only the resulting order is
   refused. A quarantined instrument's held position stays closable by the operator, exempted from
   the veto exactly the way the metering rules already exempt a manual close (ADR 0015). Full
   rationale: [ADR 0022](adr/0022-quarantine-is-a-tier-1-veto-rule.md).
9. **A re-check-readiness GUI action is out of scope for this phase.** It would re-run real panel
   probes (up to ~120 s per seat) and needs a background-task-plus-polling pattern nothing in the
   dashboard has today. Readiness stays boot-only, exactly as it is now; an operator who fixes
   something (e.g. adds an alert webhook) restarts the process to re-validate it.

## Part A — GUI arm/start/stop for live, plus start/stop everywhere · **built**

### Mechanism

A small controller owns the supervisor's task explicitly, replacing today's fixed choice (made
once, at boot, inside `serve_command`) of whether `application.supervisor.serve()` is one of the
tasks `_race` watches for process lifetime:

```python
class SupervisionController:
    def __init__(self, application: Application) -> None: ...
    @property
    def running(self) -> bool: ...
    async def start(self) -> tuple[str, ...]:  # unmet preconditions; empty means it started
        ...
    async def stop(self) -> None: ...
```

`start()` cancels nothing; `stop()` cancels the controller's own task directly (not
`Supervisor.stop()` called from outside), which matters — calling `stop()` while the old `serve()`
task is mid-sleep and then starting a new one quickly would otherwise leave two loops alive against
one `Supervisor`. Cancelling the task is what routes execution through `Supervisor.serve()`'s own
`try/finally`, the same idiom `_race`'s shutdown path already uses.

Live's permission check moves out of `tradebot/app.py::_assemble` (today: raises, so
`build_live` never returns an `Application` when unarmed) into a non-raising
`live_permission(...) -> LivePermission` in `tradebot/control/arming.py`, called by
`SupervisionController.start()` for live mode before it creates the task. `RunnerBuilder`
(`tradebot/app.py`) stops closing over a fixed notional cap computed once at boot and instead
re-reads the armed cap fresh every time a basket's runner is rebuilt — which, because
`Supervisor.stop()` tears down every runner and a Stop→Start always rebuilds them, means live
permission is only ever consulted at a Stop→Start transition. It cannot silently drift mid-run.

### Why this needs no background-job/polling pattern

The long operation the "don't freeze" requirement worries about is the live readiness probe — real
LLM completions, up to ~120 s per seat. It already runs once, at process boot, inside
`StartupSequence.recover()`, before the dashboard starts listening — not inside any HTTP request.
Once arming becomes a runtime gate, `Start` itself is a database read, an environment read, and
`asyncio.create_task` — nothing slow enough to block a request. No route handler in this plan does
real I/O in the request path.

### Files (as built)

```
tradebot/control/arming.py         live_permission(), LivePermission, LiveArming.cap;
                                     assert_live_preconditions is now `.require()` over it
tradebot/control/supervision.py    SupervisionController (new)
tradebot/app.py                    Application.clock / .broker / .live_permission() /
                                     .record_limits(), .policy computed rather than held;
                                     enforced_policy() as the single answer to "which limits";
                                     _assemble no longer gates live and no longer takes a
                                     confirmation; RunnerBuilder reads arming fresh per build
tradebot/__main__.py               serve_command builds the controller, _run_server races the
                                     dashboard alone, run_command keeps the headless refusal
tradebot/dashboard/views.py        DashboardState holds the controller; `trading` /
                                     `observe_only` derived from it
tradebot/dashboard/app.py          create_dashboard takes the controller (never `observe_only`)
tradebot/dashboard/routes/control.py   POST /control/start, /control/stop, /control/live/arm,
                                        /control/live/disarm
tradebot/dashboard/templates/control/index.html   supervision + live arming sections; the
                                                    working-orders warning while stopped
tradebot/dashboard/templates/base.html            the header pill is "not trading", not
                                                    "observe-only" — a runtime fact now
docs/OPERATIONS.md                 §2.5 the GUI procedure; the disarm/stop divergence stated
                                     explicitly; §3.8 ordered so Stop cannot strand a close
```

Three things came out differently from the plan, each recorded because a reader would otherwise
wonder:

- **The confirmation phrase left `build()` entirely.** With the gate at the start, nothing about
  wiring needs it, and a `confirmation=` parameter that no longer gates anything is exactly the
  argument someone later assumes is a control.
- **Credentials stay a build-time precondition.** A venue transport cannot be constructed without a
  key, and keys are environment-only — so no dashboard could have supplied one anyway. Live's other
  three facts moved; this one could not, and `docs/OPERATIONS.md` §2.2 says so.
- **`Application.policy` became a computed property**, and the `live_ceiling` clamp event moved
  from wiring to `record_limits()` at each start. A cap armed after boot would otherwise be
  enforced by the runners while the CLI, the dashboard and the event log all reported the boot-time
  number.

Decisions: [ADR 0021](adr/0021-live-arming-and-supervision-move-to-a-runtime-gate.md).

## Part B — Quarantine · **built**

### Mechanism

`RiskProposal.policy` (a `RiskPolicy`) already flows unchanged into every automated decision and
every manual close (`control/basket_runner.py`, `control/manual_close.py`), so the safety-critical
core is two small, additive changes:

```python
# tradebot/core/config.py — RiskPolicy
quarantined: bool = False  # whole basket
quarantined_instruments: tuple[str, ...] = ()  # specific instrument keys


# tradebot/risk/rules.py — new rule, added to DEFAULT_TIER1_RULES right after LongOnlyRule
class QuarantineRule:
    rule_id = "quarantine"

    def evaluate(self, proposal, requested_qty):
        if proposal.is_operator_exit:
            return _stand_aside(self.rule_id, requested_qty)
        if (
            proposal.policy.quarantined
            or proposal.instrument.key in proposal.policy.quarantined_instruments
        ):
            return _block(
                self.rule_id, "instrument is quarantined; no automated order may act on it"
            )
        return _allow(self.rule_id, requested_qty)
```

`proposal.is_operator_exit` is the existing ADR-0015 exemption (`operator_initiated` + SELL + qty
> 0) — reused verbatim, so a quarantined instrument's held position stays closable by hand, with
the same "stood aside" provenance the metering rules already produce, while correctness rules
(`LongOnlyRule`, quantization, Tier-2's collar) stay fully enforced. This alone guarantees no
automated order ever reaches a quarantined scope, with no changes needed to the runner, the manual
closer, or the risk interfaces.

**On top of the core rule**, a whole-basket quarantine also skips the panel entirely (a new
`CycleOutcome.QUARANTINED`, short-circuiting `BasketRunner._run` right after the snapshot is built
— so market data and indicators keep flowing but no LLM call is spent proposing trades that would
only be vetoed downstream).

Per-instrument prompt awareness — telling the panel via `ContextSnapshot.constraints
.actions_allowed` that one instrument inside an otherwise-active basket is under review — is
explicitly **not** part of this phase. It hasn't been verified that field is actually enforced by
the seat/prompt code today versus being descriptive-only, and getting that wrong is a correctness
risk for a pure cost optimization the Tier-1 veto already makes safe regardless.

### Files (as built)

```
tradebot/core/config.py               RiskPolicy.quarantined / .quarantined_instruments, plus
                                        .excludes() / .with_quarantine() / .quarantine, and a
                                        Basket validator refusing a key the basket does not hold
tradebot/risk/rules.py                QuarantineRule, in DEFAULT_TIER1_RULES after LongOnlyRule
tradebot/core/enums.py                CycleOutcome.QUARANTINED
tradebot/control/basket_runner.py     whole-basket short-circuit, after the snapshot is frozen
tradebot/dashboard/routes/control.py  POST /control/baskets/{id}/quarantine, PendingQuarantine
tradebot/dashboard/templates/control/index.html      per-basket and per-instrument toggles, with
                                                       the held-position warning + confirm click
tradebot/dashboard/templates/configure/basket.html   both fields, in the Tier-1 section
tradebot/__main__.py                  quarantine state in `config list` and `config history`
```

One addition beyond the plan: **a `RiskPolicy` may not quarantine an instrument its `Basket` does
not hold.** A key matching nothing excludes nothing, and an operator who typed one would believe an
instrument is out of service while the panel kept trading it — which is the single way this feature
could fail silently, and `core/config.py` already refuses to carry a limit nothing enforces.

No new CLI mutation command — every other Tier-1 limit is dashboard-only per CLAUDE.md's Configure
section, and quarantine follows that. `tradebot config list`/`history basket` (already read-only)
gained quarantine state in their output for visibility.

Decisions: [ADR 0022](adr/0022-quarantine-is-a-tier-1-veto-rule.md).

## Sequencing

Two mostly-independent slices, built and tested separately:

1. **Quarantine (Part B)** first — smaller, self-contained (one config change, one risk rule, two
   templates), doesn't touch the composition root. **Done 2026-08-02.**
2. **GUI arm/start/stop (Part A)** second — the composition-root refactor, reviewed on its own once
   B had landed. **Done 2026-08-02.**

## Verification

Part B, done:

- `tests/unit/test_quarantine.py` — the rule's truth table, the ADR 0015 exemption reused for a
  human's exit (and standing aside *only* when quarantine would otherwise have bitten, so no
  ordinary manual close gains a rule that never applied), `with_quarantine` round-trips, and the
  `Basket` validator refusing a key the basket does not hold.
- `tests/unit/test_dashboard_control.py` — the toggle route: a new version per act, the
  held-position second click, releasing without one, no order placed for a quarantined
  instrument, a quarantined basket cycling to `QUARANTINED` with a snapshot and no seat response,
  and a manual close still working on a quarantined instrument.
- `tests/scenario/test_quarantine_cycles.py` — several cycles with data arriving and no order,
  for both scopes.
- `.\check.ps1` green.

Part A, done:

- `tests/unit/test_supervision.py` — start/stop lifetime (one task per start, a restart is a new
  task, stop is never refused), and the three refusals: an unrecovered process, a halted recovery,
  and a tripped kill switch, with a re-arm clearing it.
- `tests/unit/test_live_wiring.py` — `TestRefusals` moved from asserting `build()` raises to
  asserting `live_permission()`'s `unmet` tuple; `TestTheRuntimeGate` proves arming in the same
  process is enough to start, that the phrase is demanded at *every* start, and that a cap armed
  after boot is the one in force; `TestTheLiveControlPage` drives the real HTTP walkthrough —
  unarmed dashboard → arm → start → disarm-also-stops.
- `tests/unit/test_dashboard_control.py` — the Stop/Start routes, the refusal that names its
  reason, the working-orders warning, and arming refused outside live.
- `tests/scenario/test_full_cycle.py::TestModeSafety` — a wired live process still will not cycle
  unarmed.
- `tests/unit/test_cli.py::TestTheAlertTailDoesNotEndTheProcess` — see below.
- `.\check.ps1` green, plus the manual walkthrough over a real socket: `serve --mode sim` → Stop →
  the working-orders warning appears and a manual close is refused → Start → cycling resumes, all
  without restarting the process; arming refused in sim mode.

**One pre-existing bug the walkthrough found, and fixed here.** `_race` waited for the *first* of
its tasks to finish, and the alert tail is one of them — but `AlertDispatcher.run` returns
immediately when no destination is configured, which is the default for sim and paper (ADR 0019).
So `tradebot serve` exited before a browser could reach it and `tradebot run` exited before a
basket cycled: both documented commands were unusable with alerting off. The tail is now a
companion rather than a racer — started and cancelled with the rest, but never deciding the
process's lifetime. It predates this phase; it surfaced here because Part A is the first work whose
verification requires the dashboard to actually stay up.

The open decision was settled with the operator before building: **Stop is allowed, warns, and
lists the working orders.** Stop must work during an incident, which is precisely when it must not
be refused. Its consequence — nothing polls open orders — is carried consistently: `observe_only`
became "supervision is not running", so a manual close is refused while stopped for exactly the
reason `--observe` already refused one, and `docs/OPERATIONS.md` §3.8 orders the getting-out steps
so that Stop cannot strand a close. `--observe` is the state the process *starts* in, not a lock.

## Deferred, not forgotten

- Re-check-readiness from the GUI (decision 9) — needs the background-task/polling pattern this
  phase otherwise avoids building.
- Per-instrument prompt awareness of quarantine (Part B) — needs verifying `constraints
  .actions_allowed` is actually enforced by the seat/prompt code first.
- CLI mutation command for quarantine, for parity with `risk arm-live` — deliberately not built,
  to stay consistent with every other Tier-1 limit being dashboard-only.
