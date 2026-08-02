# ADR 0023 — A missing provider key degrades the panel; it does not refuse to start

**Status:** accepted (2026-08-02) · **Phase:** 9 · **Depends on:** [ADR 0009](0009-llm-providers-over-plain-http.md), [ADR 0020](0020-live-is-the-paper-wiring-minus-headroom.md), [ADR 0021](0021-live-arming-and-supervision-move-to-a-runtime-gate.md)

## Context

An operator ran the demo with the free panel and got this, after a clean startup recovery:

```
ERROR tradebot.cli  refusing to run
  error: provider 'gemini' needs GEMINI_API_KEY in the environment; refusing to start a panel
         whose seat cannot reach its model
  kind: ConfigError
```

`OPENROUTER_API_KEY` *was* set. Every seat's **primary** binding was reachable. `gemini` appears
in `FREE_PANEL` exactly once, as the `news` seat's **fallback** — so the process refused to start
because a backup was missing.

That is wrong three times over.

- **DESIGN §8.1 already prescribes the response** to a provider that cannot be reached: *"seat
  falls back per chain; else `ABSTAIN`; >⅓ abstain → `WAIT`."* A key that is absent is not a
  different kind of fact from a vendor that is down — it is the same fact, discovered earlier.
- **Fail closed means no trade, not no process.** The prime directive is that uncertainty resolves
  to *no trade* (PLAN §1.1). Refusing to boot is a strictly worse response than booting and
  declining to trade: it costs the operator the dashboard, the event log and the ledger view,
  which are the three things they need in order to diagnose and fix it.
- **The composition root already claimed the opposite behaviour.** `app.py::_readiness_for` says
  *"Sim and paper are allowed to run degraded — an unreachable panel is a `WAIT`, and a holed
  series is a `DATA_STALE` cycle. That is what those modes are for."* The registry did not honour
  that; it refused in every mode, including the two that exist to tolerate this.

The machinery to degrade gracefully was already present and tested. `SeatRunner._complete` skips a
binding whose provider is absent from the pool, logs `seat binding names an unconfigured
provider`, and walks to the next binding; a seat that exhausts its chain raises `ProviderError`,
abstains, and the panel resolves `WAIT (PANEL_DEGRADED)`. The eager `ConfigError` in
`resolve_secret` pre-empted a path the system was built for.

A second defect surfaced while tracing this, and is fixed with it. `BasketWorker._cycle` called
`_runner_for` *outside* its `try/except`, so any build failure escaped the worker. Under `--once`
that is the exit code above. Under `serve` it is worse: the worker's task dies with an unretrieved
exception and `Supervisor.sync` recreates it at every resync — a crash loop with no backoff, no
failure count, and never the three-strike auto-halt that exists for exactly this.

## Decision

### A provider with no key is *unreachable*, not *invalid*

`resolve_secret` returns `None` instead of raising. `build_providers` partitions the declared
endpoints, wires the reachable ones, and reports the rest on `ProviderPool.unconfigured` — so an
absent key produces the same pool shape as a provider that was never declared, which is the shape
`SeatRunner` already handles.

One rule, `unconfigured_providers`, reads the environment. Everything that asks "can this panel
work" resolves through it, so the wiring, live readiness, the dashboard banner and the Start button
cannot answer differently from one another.

`build_provider` — the single-endpoint entry point — still raises. It is unreachable from the
composition root, because `build_providers` filters first; it exists so that a direct caller cannot
silently obtain a provider that would call a paid endpoint with no credentials.

### The consequence is reported per seat, because the two cases differ

`reach_of(panel, environ) -> PanelReach` answers from configuration and the environment alone. No
call is made, so it costs nothing and the dashboard asks it on every page render.
`decision/probe.py` remains the stronger and far more expensive question of whether a *reachable*
model id still resolves and is still accepted.

It distinguishes two consequences, because an operator needs to act on them differently:

- **degraded** — the seat lost part of its chain but keeps a binding it can answer on. The panel
  still works; it has less cover if the primary fails.
- **silenced** — the seat has no reachable binding at all. It abstains on *every* cycle, so the
  panel is permanently short a voice rather than transiently. Heterogeneity is a design control
  against sycophantic convergence (DESIGN [L5]), and a panel quietly deciding with fewer seats than
  it was configured with is the failure that control exists to prevent.

### It is loud in sim and paper, and refusing in live

| | sim / paper | live |
|---|---|---|
| Wiring | one `WARNING` + one `RISK_EVENT` (`rule="panel_unconfigured"`) | same |
| Startup | runs | `LiveReadiness` refuses; process stays **up and halted** |
| Start | permitted | `SupervisionController.blockers` refuses, at *every* start |
| Cycle | seats fall back or abstain → `WAIT (PANEL_DEGRADED)` | never reached |
| Dashboard | banner on every page | banner on every page |

The event is written **once, at wiring**, not per cycle. A cycle that decided nothing already
records `PANEL_DEGRADED`; what the startup event adds is the *cause* — a key absent from this
machine's environment — which no cycle event carries, and which months later is the difference
between "the vendor was down" and "this was never configured".

The live refusal is evaluated at every Start rather than trusted from the startup gate, for the
same reason ADR 0021 retypes the phrase: a panel edited in the dashboard while the process was
stopped would otherwise be started against a check that ran on the previous version.

### The banner cannot offer to take the key

Keys are environment-only and referenced by *name* (`secret_ref`, PLAN §3.2, DESIGN §6.1). So the
banner names the variable to set and offers the only two fixes that exist: set it and restart, or
edit the panel in Configure so no seat binds that provider. A dashboard field that accepted an API
key would put a credential in the database, which is the one place this design has always refused
to put one.

## Consequences

**Good.** The reported failure is gone: the process comes up, the panel degrades exactly as
DESIGN §8.1 says it should, and the operator can see and fix the problem from the GUI. A whole
class of build failures — not just this one — now counts as a failed cycle with backoff and
auto-halt instead of killing a supervisor task. `--panel` no longer chooses nothing silently on a
database that already holds baskets; it says so and names the panel that will actually run.

**Costs.** A degraded panel is now a state the system will sit in indefinitely in sim and paper.
That is deliberate, and it is why the finding appears on every dashboard page and in the log rather
than only in a startup line that scrolls away. The existing `PANEL_DEGRADED` alert streak
(ADR 0019) is the operational backstop: three consecutive degraded cycles reach a human.

**Rejected: keep refusing, but only for a seat's primary binding.** It fixes the reported case and
leaves the same landmine one config edit away — the operator who moves `gemini` from a fallback to
a primary gets the boot refusal back, and the mode that was supposed to tolerate degradation still
does not.

**Rejected: wire an "unconfigured" provider object that raises `ProviderError` on every call.**
Behaviourally equivalent, since the seat falls back either way, but it spends a call to discover
something known at wiring time and puts a fake provider in a pool whose whole purpose is to hold
things that can be reached.
