"""The promotion gates (DESIGN §9 rung 5).

These are the checks standing between a soak and a human being asked to consider real money, so
each one is tested for what it refuses as much as for what it passes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradebot.core.enums import CycleOutcome, Mode, ReconcileClass
from tradebot.validation.evidence import (
    CycleFacts,
    Evidence,
    Incident,
    IncidentKind,
    ReconcileFacts,
)
from tradebot.validation.promotion import Criteria, evaluate

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def cycles(count: int, *, venue: str = "sim", completed: bool = True) -> tuple[CycleFacts, ...]:
    return tuple(
        CycleFacts(
            cycle_id=f"c{index}",
            basket_id="demo",
            venue=venue,
            started_at=NOW,
            completed_at=NOW if completed else None,
            outcome=CycleOutcome.NO_ACTION if completed else None,
            cost_usd=Decimal("0.01"),
        )
        for index in range(count)
    )


def clean_pass(classification: ReconcileClass = ReconcileClass.MATCH) -> ReconcileFacts:
    return ReconcileFacts(at=NOW, venue="sim", classification=classification)


def evidence(**overrides: object) -> Evidence:
    """A soak that would pass every gate, unless a test spoils one."""
    defaults: dict[str, object] = {
        "since": None,
        "until": None,
        "cycles": cycles(200),
        "reconciliations": (clean_pass(),),
    }
    return Evidence(**{**defaults, **overrides})  # type: ignore[arg-type]


def gates(source: Evidence, criteria: Criteria | None = None) -> dict[str, bool]:
    report = evaluate(source, mode=Mode.PAPER, generated_at=NOW, criteria=criteria)
    return {gate.name: gate.passed for gate in report.gates}


class TestCycleCount:
    def test_a_full_soak_passes(self) -> None:
        assert gates(evidence())["completed_cycles"]

    def test_one_cycle_short_does_not(self) -> None:
        assert not gates(evidence(cycles=cycles(199)))["completed_cycles"]

    def test_an_unfinished_cycle_does_not_count(self) -> None:
        """A cycle with no recorded outcome proves nothing about the loop that started it."""
        assert not gates(evidence(cycles=cycles(200, completed=False)))["completed_cycles"]

    def test_testnet_cycles_are_reported_but_never_counted(self) -> None:
        """Binance testnet and Alpaca paper are adapter checks, not the evidence base."""
        mixed = evidence(cycles=(*cycles(150), *cycles(60, venue="binance")))

        report = evaluate(mixed, mode=Mode.PAPER, generated_at=NOW)
        (cycle_gate,) = [gate for gate in report.gates if gate.name == "completed_cycles"]
        assert not cycle_gate.passed
        assert cycle_gate.observed == "150"
        assert "60 completed cycle(s) elsewhere" in cycle_gate.detail

    def test_the_evidence_venue_is_configurable(self) -> None:
        soak = evidence(cycles=cycles(200, venue="binance"))
        criteria = Criteria(evidence_venues=frozenset({"binance"}))

        assert gates(soak, criteria)["completed_cycles"]

    def test_the_bar_is_configurable(self) -> None:
        assert gates(evidence(cycles=cycles(10)), Criteria(min_cycles=10))["completed_cycles"]


class TestIncidents:
    def test_a_quiet_soak_passes(self) -> None:
        assert gates(evidence())["no_unhandled_incidents"]

    def test_any_incident_fails_the_gate(self) -> None:
        halted = evidence(
            incidents=(
                Incident(
                    kind=IncidentKind.BASKET_HALTED,
                    at=NOW,
                    scope="demo",
                    detail="3 consecutive failed cycles",
                ),
            )
        )

        report = evaluate(halted, mode=Mode.PAPER, generated_at=NOW)
        assert not report.passed
        assert [gate.name for gate in report.failures] == ["no_unhandled_incidents"]


class TestReconciliation:
    def test_a_clean_history_passes(self) -> None:
        assert gates(evidence())["reconciliation_clean"]

    def test_explainable_drift_still_passes(self) -> None:
        drifted = evidence(reconciliations=(clean_pass(ReconcileClass.DRIFT),))

        assert gates(drifted)["reconciliation_clean"]

    def test_an_unexplained_mismatch_fails(self) -> None:
        mismatched = evidence(
            reconciliations=(clean_pass(), clean_pass(ReconcileClass.MISMATCH)),
        )

        assert not gates(mismatched)["reconciliation_clean"]

    def test_a_venue_reset_is_excluded_and_reported(self) -> None:
        reset = evidence(reconciliations=(clean_pass(), clean_pass(ReconcileClass.VENUE_RESET)))

        report = evaluate(reset, mode=Mode.PAPER, generated_at=NOW)
        (recon,) = [gate for gate in report.gates if gate.name == "reconciliation_clean"]
        assert recon.passed
        assert "1 venue reset(s) excluded" in recon.detail

    def test_no_reconciliation_at_all_is_not_a_clean_one(self) -> None:
        """Silence is never taken as consent: the ledger has not been shown to agree once."""
        never = evidence(reconciliations=())

        report = evaluate(never, mode=Mode.PAPER, generated_at=NOW)
        (recon,) = [gate for gate in report.gates if gate.name == "reconciliation_clean"]
        assert not recon.passed
        assert recon.observed == "no reconciliation recorded"


class TestVerdict:
    def test_every_gate_is_evaluated_so_the_operator_sees_the_whole_list(self) -> None:
        broken = evidence(
            cycles=cycles(1),
            reconciliations=(clean_pass(ReconcileClass.MISMATCH),),
            incidents=(
                Incident(kind=IncidentKind.KILL_SWITCH, at=NOW, scope="watchdog", detail="dd"),
            ),
        )

        report = evaluate(broken, mode=Mode.PAPER, generated_at=NOW)
        assert len(report.failures) == 3

    def test_passing_every_gate_is_not_a_promotion(self) -> None:
        """The last rung is a human's; `passed` only ever means "worth reviewing"."""
        report = evaluate(evidence(), mode=Mode.PAPER, generated_at=NOW)

        assert report.passed
        assert report.failures == ()
