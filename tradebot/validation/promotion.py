"""The promotion gates: what a paper soak must show before a human may consider live.

DESIGN §9 rung 5 states them, and they are enforced here rather than in a checklist because a
gate a human evaluates by reading a dashboard is a gate that gets waved through at 2am:

1. **enough cycles** — ≥ 200 completed cycles on the evidence base;
2. **no unhandled incidents** — nothing that needed a person: no kill switch trip, no basket
   halt, no failed cycle, no unexplained reconciliation, no order stranded in `SUBMIT_UNKNOWN`;
3. **clean reconciliation** — every pass matched or was explained, venue resets excluded (R15).

Two things this module deliberately does **not** do.

It does not gate on PnL. Weeks of forward returns are statistically weak, the panel comparison
that *is* meaningful is the shadow A/B harness, and a gate on profit would reward a soak that
got lucky over one that proved the plumbing (DESIGN §9, [L12]).

It does not sign itself off. Every automatic gate passing produces a report that says a human
may now review it — that is the last rung, and nothing here can climb it (PLAN Phase 7 exit).

The **evidence base is the `sim` venue**: live market data through `SimBroker`. Cycles run
against Binance testnet or Alpaca paper are adapter integration checks and are counted, shown,
and excluded from the gates — their fills are unrealistically good and their state resets
without notice (DESIGN §9 rung 5).

Failure semantics: a fact the log does not establish fails its gate. A report can only ever be an
argument *for* promotion built from what was recorded; silence is never taken as consent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from tradebot.core.enums import Mode
from tradebot.validation.evidence import Evidence, ReconcileFacts

#: DESIGN §9's figure. Enough cycles that a rare path — a partial fill at TTL, a venue 5xx, a
#: degraded panel — has had a chance to happen at least once.
DEFAULT_MIN_CYCLES = 200

#: The venue whose cycles count as evidence: live data, simulated fills, nothing to reset.
DEFAULT_EVIDENCE_VENUES = frozenset({"sim"})


@dataclass(frozen=True, slots=True)
class Criteria:
    """The bar a soak is held to. Data, so a stricter run can raise it."""

    min_cycles: int = DEFAULT_MIN_CYCLES
    evidence_venues: frozenset[str] = DEFAULT_EVIDENCE_VENUES


@dataclass(frozen=True, slots=True)
class Gate:
    """One criterion, its verdict, and the numbers behind it."""

    name: str
    passed: bool
    observed: str
    required: str
    detail: str = ""


def _cycles_gate(evidence: Evidence, criteria: Criteria) -> Gate:
    counted = evidence.for_venues(criteria.evidence_venues)
    excluded = len([c for c in evidence.cycles if c.completed]) - len(counted)
    return Gate(
        name="completed_cycles",
        passed=len(counted) >= criteria.min_cycles,
        observed=str(len(counted)),
        required=f"≥ {criteria.min_cycles}",
        detail=(
            f"on venue(s) {', '.join(sorted(criteria.evidence_venues))}; "
            f"{excluded} completed cycle(s) elsewhere are adapter checks and do not count"
        ),
    )


def _incidents_gate(evidence: Evidence, _criteria: Criteria) -> Gate:
    incidents = evidence.incidents
    kinds = sorted({incident.kind.value for incident in incidents})
    return Gate(
        name="no_unhandled_incidents",
        passed=not incidents,
        observed=str(len(incidents)),
        required="0",
        detail=", ".join(kinds) or "nothing needed a human",
    )


def _reconciliation_gate(evidence: Evidence, _criteria: Criteria) -> Gate:
    unclean = evidence.unclean_reconciliations
    resets = [pass_ for pass_ in evidence.reconciliations if pass_.excluded]
    return Gate(
        name="reconciliation_clean",
        passed=not unclean and bool(evidence.reconciliations),
        observed=_reconciliation_summary(evidence.reconciliations, unclean),
        required="every pass matched or explained",
        detail=(
            f"{len(resets)} venue reset(s) excluded from accounting (R15)"
            if resets
            else "no venue resets in the window"
        ),
    )


def _reconciliation_summary(
    passes: tuple[ReconcileFacts, ...], unclean: tuple[ReconcileFacts, ...]
) -> str:
    """An empty history is reported as such, because it is not the same as a clean one.

    A soak with no reconciliation at all has not shown the ledger agreeing with the venue even
    once, which is exactly what this gate exists to establish.
    """
    if not passes:
        return "no reconciliation recorded"
    return f"{len(passes) - len(unclean)}/{len(passes)} clean"


#: Evaluated in order and all of them always, so a report lists every failure rather than the
#: first one — an operator fixing a soak needs the whole list.
GATES: tuple[Callable[[Evidence, Criteria], Gate], ...] = (
    _cycles_gate,
    _incidents_gate,
    _reconciliation_gate,
)


@dataclass(frozen=True, slots=True)
class PromotionReport:
    """The argument for promotion, and the numbers a human reviews it against."""

    mode: Mode
    generated_at: datetime
    criteria: Criteria
    evidence: Evidence
    gates: tuple[Gate, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """Whether every *automatic* gate passed. Never a promotion — a human still decides."""
        return all(gate.passed for gate in self.gates)

    @property
    def failures(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.gates if not gate.passed)


def evaluate(
    evidence: Evidence,
    *,
    mode: Mode,
    generated_at: datetime,
    criteria: Criteria | None = None,
) -> PromotionReport:
    """Run every gate over the gathered evidence."""
    resolved = criteria or Criteria()
    return PromotionReport(
        mode=mode,
        generated_at=generated_at,
        criteria=resolved,
        evidence=evidence,
        gates=tuple(gate(evidence, resolved) for gate in GATES),
    )
