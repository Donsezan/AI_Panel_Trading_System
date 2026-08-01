# ADR 0022 — Quarantine is a Tier-1 veto rule, not a scheduling change

**Status:** accepted (2026-08-02) · **Phase:** 9 · **Depends on:** [ADR 0015](0015-an-operator-exit-is-exempt-from-metering-rules.md)

## Context

An operator asked for a way to exclude one instrument, or a whole basket, from automated trading
on their own judgement — "I have doubt or information I'm acting on, so exclude this, but keep
market data flowing so I can put it back into processing later." Two existing mechanisms look
similar and are both wrong for this:

- **Pause** (`BasketStatus.PAUSED`) stops the whole cycle for a basket — no market data fetch, no
  indicators, nothing. The operator explicitly wants data to keep flowing.
- **Halt** is the system protecting itself after repeated failures. It is persisted risk state,
  cleared by a typed phrase, and it is never the operator's own judgement call about a healthy
  instrument they simply don't want traded right now.

Quarantine needed to be a third thing: a per-instrument-or-per-basket "don't act on this," that
leaves everything else about the cycle — data, indicators, the panel's deliberation — running.

Two questions had to be settled before any code shape made sense: what happens to an existing
position in a quarantined scope, and how heavyweight should the mechanism be. Both were answered
directly by the operator: existing positions should be left alone by the bot ("fully hands-off"),
but a manual close should still work, with a warning, since inaction can itself compound a loss;
and the mechanism should be ordinary versioned configuration, not persisted safety state — the
whole point is that this is a reversible, low-ceremony judgement call, not an incident.

## Decision

### A new field on `RiskPolicy`, read by a new Tier-1 rule

```python
# tradebot/core/config.py — RiskPolicy
quarantined: bool = False
quarantined_instruments: tuple[str, ...] = ()
```

`RiskProposal.policy` already carries a basket's `RiskPolicy` into every automated decision
(`control/basket_runner.py::_build_proposal`) *and* every manual close
(`control/manual_close.py::_proposal`) unchanged. A new `QuarantineRule`, added to
`DEFAULT_TIER1_RULES` immediately after `LongOnlyRule`, needs nothing else wired through:

```python
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

This is deliberately the smallest change that guarantees the safety property. No changes to
`BasketRunner`, `ManualCloser`, or `interfaces/risk.py` are required for the veto itself to be
complete and auditable — the rule's `RiskCheckResult` lands in the same `RISK_CHECKED` event every
other Tier-1 rule already writes.

### Reuse, don't reinvent, the operator-exit exemption

`proposal.is_operator_exit` ([ADR 0015](0015-an-operator-exit-is-exempt-from-metering-rules.md))
already means "a human, through the dashboard's manual close, reducing an existing long" — the
exact carve-out quarantine needs, for the exact reason ADR 0015 gives: a system that cannot be
exited by its operator has the control backwards. `QuarantineRule` stands aside for it identically
to `CooldownRule`, `DailyTradeCapRule`, and `ConsecutiveLossRule`, and records that it did.
Correctness rules — `LongOnlyRule`, quantization, Tier-2's price collar — are never exempt, exactly
as ADR 0015 already established for every other case.

### Cycles keep running; only the order is refused

Because the veto lives in Tier-1 rather than in the scheduler, `ContextBuilder` still fetches
market data and computes indicators for a quarantined instrument every cycle, and the panel still
deliberates over it (in `per_asset` mode) or sees it as part of the basket (in `basket` mode) — it
is `_act`'s Tier-1 check that refuses the resulting order, not `_gate`'s pre-panel short-circuit
that `BLOCKED`/paused/halted baskets use. This is what satisfies "keep market data flowing" without
inventing a new scheduling state.

One optimization sits on top, for a **whole-basket** quarantine only: the panel call is skipped
entirely (`CycleOutcome.QUARANTINED`, short-circuiting `BasketRunner._run` immediately after the
snapshot is frozen), since asking an LLM panel to deliberate on every instrument in a basket the
operator has already excluded, only to veto the result downstream, is a pure cost with no
offsetting benefit. It was safe to add or omit independently of the veto rule — the veto is what
makes it safe either way — and the cycle is still *recorded*, because a basket that stops
appearing in the log is a basket nobody can audit.

### Versioned configuration, not persisted state

Unlike a halt, quarantine needs no typed phrase and no persisted-state table. It publishes through
the same `ConfigStore.put()` every other Tier-1 limit already uses, versioned, attributable, and
reversible by editing it again — because it is, definitionally, an ordinary judgement call an
operator should be able to make and reverse quickly, not an incident.

## Consequences

- No automated `BUY` or `SELL` can ever reach a quarantined instrument or basket — verified by unit
  test on the rule in isolation (`tests/unit/test_quarantine.py`) and by a scenario test proving
  several cycles produce data and no orders (`tests/scenario/test_quarantine_cycles.py`).
- A `RiskPolicy` may not name an instrument its `Basket` does not hold. A key matching nothing
  excludes nothing, and the operator who typed it would believe an instrument is out of service
  while the panel keeps trading it — the one way this feature could fail silently.
- A quarantined position is not orphaned: the operator's manual close still works, with the
  dashboard additionally warning when a held position exists, since leaving a position unmanaged
  can itself be the riskier choice — that warning is a GUI courtesy, not a rule change.
- Cost is not free: a per-instrument (not whole-basket) quarantine inside an otherwise-active
  basket still pays for a panel call that will be vetoed. Teaching the panel's prompt about it via
  `ContextSnapshot.constraints.actions_allowed` is deferred — not because it's unsafe to add, but
  because it hasn't been verified that field is actually enforced by the seat/prompt code today,
  and getting that wrong risks a correctness bug for a cost optimization the veto already makes
  safe without it.
- `tradebot config list`/`history basket` (read-only) gain quarantine state in their output; no CLI
  mutation command is added, consistent with every other Tier-1 limit being dashboard-only.
