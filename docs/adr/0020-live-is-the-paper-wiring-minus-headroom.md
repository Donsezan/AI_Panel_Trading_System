# ADR 0020 — Live is the paper wiring minus headroom, gated on readiness rather than on paperwork

**Status:** accepted (2026-08-01) · **Phase:** 8 · **Supersedes:** the refusal in [ADR 0012](0012-live-is-four-independent-preconditions.md) §"Live still refuses"

## Context

Phase 5 built every live precondition and then made `build(Mode.LIVE)` raise anyway, because live
wiring was Phase 8's deliverable. This is Phase 8. The question it has to answer is not "may this
system trade live" — [ADR 0012](0012-live-is-four-independent-preconditions.md) answered that with
four facts a human puts in place — but the two that follow it:

1. What does live wiring *look like*, given DESIGN §5's claim that modes differ only in adapters?
2. What is the difference between being *permitted* to trade live and being *able* to?

The second question is the one that costs money. Every precondition in ADR 0012 can be satisfied by
an operator on a machine whose alerting was never configured, whose free model slot disappeared last
week, and whose market feed has been quietly returning a holed series since the last restart. All
four say yes. None of them is about whether the system works.

## Decision

### Live is the same objects, with limits subtracted

`build_live` calls the same `_assemble` as sim and paper. Same `BasketRunner`, same Tier-1 and
Tier-2 engines, same ledger, same event log, same reconciler. A separate live path would mean the
thing six weeks of soak validated is not the thing that trades — which would make the soak evidence
about a different program (DESIGN §5, §9 rung 5).

What live adds is **subtraction**, in two places:

* `control/live.py` clamps every Tier-2 magnitude to `LIVE_CEILING` — `min(published, ceiling)`,
  never a widening. A published policy that is already tighter keeps its own number. Raising the
  ceiling is a source change, reviewed and released, which is the only reading of DESIGN §9 rung 6's
  "widened only manually and gradually" that a config edit cannot defeat.
* The arming row's `max_live_notional` is folded in as the cap on `max_order_notional`, exactly as
  before — one more field on the same table, enforced by the same `OrderNotionalRule`.

The clamp is recorded as a `RISK_EVENT` (`rule="live_ceiling"`) at wiring. "What were the limits at
04:12" must be answerable from the log alone, not by joining two config documents against a constant
in a source file (PLAN §3.3).

### Permission is not readiness, so live checks both

`control/readiness.py` runs inside the startup sequence, live only, after the cheaper steps have
agreed there is a system worth checking. Four gates, each a way a live run fails quietly:

| Gate | Refuses when | Because |
|---|---|---|
| Alerting | no webhook and no Telegram in the environment | every control ends in "halt and tell someone"; live starting unheard means the telling never happens |
| Panel | any seat binds the stub, or no binding in a seat's chain answers a real 16-token completion | a stub panel places real orders from canned JSON; an unreachable model id is R11 happening now, not in theory |
| Market data | short, stale, or **holed** series for anything a basket decides on | ATR sizes every position, and an ATR across a hole is a stop distance from a bar the venue never published |
| Configuration | a stored basket does not build through the real factory | a missing `secret_ref` or unknown indicator should refuse now, not at 03:00 holding a position |

Sim and paper deliberately have none of these. An unreachable panel there is a `WAIT`, a holed
series is a `DATA_STALE` cycle, and running degraded is exactly what those modes are for.

Two asymmetries are deliberate. A seat answering on its **fallback** binding is a warning, not a
failure — the chain exists so an outage is survivable, and refusing over a healthy fallback would
make the fallback pointless. And a **paused** basket has its configuration checked but is neither
probed nor fetched for: it cannot cycle, so spending provider calls and venue weight on it would
make every live start cost more than it needs to.

### The probe is a real completion

A reachability check that only opened a socket would pass for a model id that no longer resolves and
for a key the endpoint rejects — R11's exact failure. Sixteen tokens per seat, once per start, buys
the answer to "can this seat get an answer at all". The reply is discarded; the probe deliberately
asks nothing about trading, so no unvalidated model opinion enters the log without a cycle to gate
it.

### A run of stale cycles is now an alert

The readiness gate covers the start. `ops/rules.py` covers the rest of the run: a streak of
`DATA_STALE` cycles alerts, counted and persisted exactly like the `PANEL_DEGRADED` streak it shares
a rule with (migration `0007`). Both are the same operational fact — cycle after cycle deciding
nothing — and a streak counted in memory is a streak a restart forgives ([ADR 0019](0019-alerts-are-a-log-tail-with-a-persisted-cursor.md)).

The distinction that matters for a live account: positions already open stay protected by their
venue-held legs, but nothing will be entered **or exited** while data is refused. That is in the
alert body, because it is what decides whether the operator gets out of bed.

## Consequences

* Live remains unreachable by accident: five refusals now (the four of ADR 0012, plus readiness),
  and `build_live` additionally refuses `--broker sim`, because an order not sent to a venue is not
  a live order.
* Equities cannot go live in v1. There is no equity market-data provider, so `_alpaca_stack` refuses
  — the same refusal as in paper, for the same reason, rather than a live-specific message.
* `LIVE_CEILING` is a constant in source, not config. That is the intent: it is the one limit an
  operator cannot loosen from the dashboard at 03:00 while an incident is running.
* The clamp is visible three ways — the log line, the `RISK_EVENT`, and `risk status` — so nobody
  has to infer which policy was in force from two documents and a release tag.
