# ADR 0021 — Live arming and supervision move to a runtime gate, controllable from the GUI

**Status:** proposed (2026-08-01) · **Phase:** 9 (planned) · **Depends on:** [ADR 0012](0012-live-is-four-independent-preconditions.md), [ADR 0020](0020-live-is-the-paper-wiring-minus-headroom.md)

## Context

[ADR 0012](0012-live-is-four-independent-preconditions.md) established four independent facts a
human must put in place before live trading is reachable: `--mode live`, the typed confirmation
phrase, an armed database row, and a positive notional cap (plus credentials, asserted elsewhere).
[ADR 0020](0020-live-is-the-paper-wiring-minus-headroom.md) wired all four to be checked inside
`tradebot/app.py::_assemble`, before the `Application` object — and therefore before any dashboard
— exists. An unarmed `tradebot serve --mode live` refuses outright.

That was the right design for a CLI-only control surface: arming happens with `risk arm-live`
against the database directly, and the process is only ever started once the operator already
knows it will succeed. It stops being sufficient the moment "view current state, and arm it or
not" is asked for as a GUI capability, because there is nothing to view — the process that would
show the dashboard never starts.

Two designs were considered.

**Keep the build-time gate; GUI only disarms.** The live process still has to be armed and
confirmed from the CLI before it — and its dashboard — can exist at all. Once up, the dashboard can
show arming status and offer Disarm (safe: it only removes permission). This is a smaller change,
but it does not deliver what was asked for: an operator cannot arm live trading from the GUI,
because there is no GUI to arm it from until the CLI has already done the equivalent.

**Move the whole gate to a runtime action.** A live-mode dashboard can come up disarmed, clearly
showing "NOT ARMED / NOT TRADING". Arming and starting become separate GUI actions, each
re-validating the same four facts at the moment supervision is actually asked to start. This is
what "arm it or not" from the GUI requires, and it was confirmed as the intended design.

## Decision

### The gate is a predicate, callable from two places

`assert_live_preconditions` (ADR 0012) stops being the only shape of this check. A non-raising
`live_permission(mode, *, confirmation, arming, credentials) -> LivePermission` carries the same
four-fact predicate, returning every unmet reason rather than raising on the first. Two callers:

- `tradebot run --mode live` (headless) still calls the raising wrapper immediately, exactly as
  today — there is no dashboard for an unarmed headless process to be armed from, so failing fast
  remains strictly better than an idle, unusable process.
- A new `SupervisionController.start()` calls the non-raising form, at the moment supervision is
  asked to begin — whether that's `serve_command` at boot (unless `--observe`), or a click on the
  dashboard's Start button.

`tradebot serve --mode live` therefore changes behaviour: instead of refusing outright when
unarmed, it serves the dashboard the same way a halted startup recovery already does today — up,
visibly not trading, and able to say why.

### The cap is read fresh, not fixed at boot

Today `RunnerBuilder` closes over a notional cap computed once, at `_assemble` time. That was
correct when arming could only ever precede the process. It stops being correct once arming can
happen *during* the process's life: a cap set after boot would be invisible to every runner built
from the stale closure.

`RunnerBuilder` instead re-reads the armed cap from the `ArmingStore` inside `build()`, every time.
Because `Supervisor.stop()` already tears down every basket's runner, and a subsequent `Start`
always rebuilds them from scratch, this has a clean consequence: **live permission is consulted
exactly at a Stop→Start transition, never mid-run.** There is no window in which a basket keeps
cycling against a cap, or an arming state, that has since changed underneath it.

### Disarm from the GUI also stops

The CLI's `disarm-live` only prevents the *next* start — documented as such, and unchanged by this
decision, because it edits a database file with no running process to reach into. The GUI's Disarm
is different: it is issued from inside the same process that might currently be trading, so it also
cancels the supervision task if one is running. Leaving a live basket cycling against a cap that
was just revoked, silently, is exactly the failure mode this whole feature exists to not introduce.
This is a deliberate, GUI-only divergence from the CLI's documented behaviour, not an oversight —
`docs/OPERATIONS.md` states both explicitly.

### The confirmation phrase is never cached

Both the Arm form and the Start form require the operator to type
`LIVE_CONFIRMATION_PHRASE` — the same string the CLI's `--confirm` flag already uses — fresh, every
time, never pre-filled and never stored in the dashboard session. The phrase's whole point, per
ADR 0012, is that "an armed database alone must not be enough to start"; caching it in a session
that (per [ADR 0014](0014-the-dashboard-is-vendored-and-always-authenticated.md)) has no expiry
would quietly undo that property for anyone holding a valid session cookie.

### Readiness is unaffected

[ADR 0020](0020-live-is-the-paper-wiring-minus-headroom.md)'s readiness gates
(`control/readiness.py`) check none of the four arming facts — alerting, panel reachability, market
data, and configuration are orthogonal to permission. They keep running exactly where they already
run: once, at boot, inside `StartupSequence.recover()`, before the dashboard starts listening.
"Permission is not readiness" stays true, and is now shown on the live Control page as two
independent statuses rather than only in the log.

## Consequences

- An operator running the CLI exactly as documented today sees no behavioural change: arm, then
  `serve --confirm ...`, starts trading immediately, same as before.
- An operator who starts `serve --mode live` unarmed now gets a dashboard instead of a refusal —
  the entire point of the change — and arms and starts from there, retyping the phrase both times.
- `tests/unit/test_live_wiring.py::TestRefusals` moves from asserting `build()` raises to asserting
  `live_permission()`'s `unmet` tuple, plus a new case proving the runtime gate refuses an unarmed
  `SupervisionController.start()` and succeeds once armed, in the same process, without a rebuild.
- `docs/OPERATIONS.md` §2 needs both paths documented, and the disarm/stop divergence stated
  plainly so it is never discovered mid-incident.
- Nothing about *what* is required to trade live changes — all four facts of ADR 0012 still have to
  hold, together, exactly as before. Only *when* they are checked moves.
