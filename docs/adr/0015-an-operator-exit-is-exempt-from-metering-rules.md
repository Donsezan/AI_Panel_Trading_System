# 15. An operator's exit is exempt from the metering rules, and every exemption is recorded

Date: 2026-07-31
Status: accepted

## Context

DESIGN §6.10 requires the dashboard's manual close to go "through the same OrderIntent/risk/
execution path — no side doors", and PLAN §5 repeats it. Phase 6 pass 2 built exactly that, and
it immediately produced a result nobody had reasoned about: **the risk engine refuses an
operator's close.**

Four rules veto a reduce-only SELL:

| Rule | Tier | Why it fires on a close |
|---|---|---|
| `CooldownRule` | 1 | The entry being closed *was* the last trade |
| `DailyTradeCapRule` | 1 | The close spends the same 6/day budget the panel spends |
| `ConsecutiveLossRule` | 1 | After 4 losing round trips — precisely when flattening matters most |
| `OrderRateRule` | 2 | 20 orders/hour, shared with the panel |

Every one of them exists to stop the **panel** over-trading. DESIGN §6.6 describes them as
"Cooldown after a trade", "Max trades per day per basket", "Max consecutive losses before basket
auto-pause" — metering of a decision loop. None was written with a human exit in mind.

So a system built exactly to spec could not be flattened by its operator during a loss streak.
That is the control backwards: the state in which exiting is most valuable is the state in which
the rules forbid it.

Three ways out were considered.

**Leave it and document an unlock.** The operator raises the limit in Configure — versioned and
attributable — closes, then restores it. It works today and changes no code. It also makes
someone edit a *global* limit to fix a *single* position, during an incident, with a real chance
of never restoring it. That is a worse safety outcome than the problem.

**Exempt every SELL.** The exposure rules already do this: `MaxPositionSizeRule` and
`MaxBasketAllocationRule` both open with `if side is SELL: return PASS`. Simple, consistent, no
new state. But it also exempts the *panel's* sells, and a panel that can sell freely while its
buys are metered is a panel whose churn is half-metered — which is what the cooldown exists to
prevent.

**Exempt an operator's exit specifically.** Narrower, and needs one new piece of state.

## Decision

**`RiskProposal.operator_initiated`, and a predicate that requires three things at once.**

```python
@property
def is_operator_exit(self) -> bool:
    return (
        self.operator_initiated
        and self.decision.action.side is Side.SELL
        and self.position.qty > ZERO
    )
```

Each condition removes a different abuse. `operator_initiated` alone would let a human *open* a
position past the daily cap. The SELL test alone would exempt the panel's churn. The holding
test alone would exempt a sell into a flat book. Together they describe an act that is
monotonically risk-reducing in a long-only system.

**Only the metering rules stand aside.** `CooldownRule`, `DailyTradeCapRule`,
`ConsecutiveLossRule` and Tier-2's `OrderRateRule`. Ban avoidance does not rest on the last of
these — the transports' token bucket is what keeps the venue budget (PLAN §3.1).

**Nothing about correctness or venue legality is exempt.** `LongOnlyRule` still clamps quantity
to the holding and still vetoes a sell while flat, quantization still enforces lot size and
minimum notional, and Tier-2's price collar still refuses a fat finger. Those rules stop a close
being *wrong*; the metering rules only stop it being *frequent*.

**A rule that stands aside still answers, and says so.** It returns
`PASS` with `detail = "stood aside: an operator exit reduces exposure and is not metered"`, which
lands in the `RISK_CHECKED` event and on the order's `risk_checks`. The event log therefore shows
which rules stood aside and why. This is the property that makes the exemption a *decision by the
risk layer* rather than a bypass around it: the rule is asked, in deterministic unit-tested code,
and its answer is auditable six weeks later.

**`ManualCloser` is the only writer of the flag**, and the invariant is asserted from the log:
`test_no_cycle_ever_records_a_stand_aside` fails if a runner-built proposal ever carries it.

**A tripped kill switch does not block a manual close.** Previously true by omission; now stated
and tested. The switch stops the bot from trading and must not trap a human's exit —
`flatten_on_kill` existing at all (DESIGN §6.6) shows the design contemplates leaving positions at
kill time, and this is its manual equivalent.

## Consequences

- An operator can always exit a position that the venue will accept an order for. The remaining
  refusals are all ones where closing would be wrong or impossible, not merely unscheduled.
- The panel is metered exactly as before. `test_the_panel_is_still_metered_by_the_same_rules`
  re-runs a cycle immediately after a manual close and asserts no second order.
- A dust position still cannot be closed, because `min_notional` refuses it. That is venue truth,
  not policy, and the honest answer is that it must be closed at the venue.
- Tier-2's `max_order_notional` **shrinks** rather than vetoes, so a capped close can be partial.
  Reporting that as "closed" would leave an operator believing they are flat when they are not, so
  `CloseOutcome.partial` exists and the Control page says "you are not flat".
- A manual close is still recorded with `role = entry`, so it consumes the basket's daily trade
  budget for subsequent *panel* decisions. Deliberately left: an `OrderRole.MANUAL_EXIT` would
  change an on-disk enum and the meaning of every history query for a second-order effect.
