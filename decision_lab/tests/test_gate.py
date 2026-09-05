"""The §10.6 gate artifact: its key, its persistence, and every way it refuses.

The gate is the one thing standing between an operator and a six-month run paid for with a seat
that never answered, so each of its four conditions is asserted by the message it produces —
"which of the four failed, and which seat" is the whole value of the refusal (§15).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import gate
from tradebot.core.errors import ConfigError

KEYS = {"dataset_digest": "d1", "matrix_digest": "m1", "dayset_digest": "s1"}


def verdict(scenario: str, *, failures: tuple[str, ...] = ()) -> gate.ScenarioVerdict:
    return gate.ScenarioVerdict(
        scenario=scenario,
        ran_at=datetime(2026, 1, 1, tzinfo=UTC),
        cadence_seconds=14_400,
        corpus_id="c1",
        days=(date(2026, 1, 1),),
        candidates=(gate.CandidateEvidence(candidate_id="baseline", rows=3, scored=3),),
        report_path="reports/x.md",
        failures=failures,
    )


def record() -> gate.GateRecord:
    return gate.GateRecord(gate_key=gate.gate_key(**KEYS), **KEYS)


def test_the_key_is_stable_and_covers_all_three_parts() -> None:
    first = gate.gate_key(**KEYS)
    assert first == gate.gate_key(**KEYS), "the same triple is the same gate"
    assert first != gate.gate_key(dataset_digest="d2", matrix_digest="m1", dayset_digest="s1")
    assert first != gate.gate_key(dataset_digest="d1", matrix_digest="m2", dayset_digest="s1")
    assert first != gate.gate_key(dataset_digest="d1", matrix_digest="m1", dayset_digest="s2")


def test_the_key_is_exactly_the_three_parts_and_never_the_cadence() -> None:
    """§10.6: all four conditions are properties of the candidates, the seats and the days.
    Keying on the corpus — or on the cadence — would force a re-calibration every time §10.4
    varied `--every`, which is the one axis the long run exists to vary. Asserted on the
    signature, so adding a fourth part to the key fails here rather than silently doubling
    everyone's calibration bill."""
    import inspect

    assert set(inspect.signature(gate.gate_key).parameters) == {
        "dataset_digest",
        "matrix_digest",
        "dayset_digest",
    }


def test_a_record_round_trips(tmp_path: Path) -> None:
    written = gate.record_scenario(record(), verdict("normal"))
    gate.write(written, workspace=tmp_path)
    assert gate.read(written.gate_key, workspace=tmp_path) == written


def test_an_absent_record_reads_as_none_never_as_an_error(tmp_path: Path) -> None:
    assert gate.read("nothing-here", workspace=tmp_path) is None


def test_both_halves_are_needed(tmp_path: Path) -> None:
    """§10.6 names scenarios 1 *and* 2. One half is not a pass."""
    gate.write(gate.record_scenario(record(), verdict("normal")), workspace=tmp_path)
    with pytest.raises(ConfigError, match="shock"):
        gate.require_satisfied(**KEYS, workspace=tmp_path)


def test_a_failing_half_refuses_and_names_the_failure(tmp_path: Path) -> None:
    both = gate.record_scenario(record(), verdict("normal"))
    both = gate.record_scenario(
        both, verdict("shock", failures=("baseline / analyst: never answered on its primary",))
    )
    gate.write(both, workspace=tmp_path)
    with pytest.raises(ConfigError, match="never answered on its primary"):
        gate.require_satisfied(**KEYS, workspace=tmp_path)


def test_a_missing_record_names_the_command_that_produces_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="calibrate normal"):
        gate.require_satisfied(**KEYS, workspace=tmp_path)


def test_a_satisfied_gate_returns_its_record(tmp_path: Path) -> None:
    both = gate.record_scenario(record(), verdict("normal"))
    both = gate.record_scenario(both, verdict("shock"))
    gate.write(both, workspace=tmp_path)
    assert gate.require_satisfied(**KEYS, workspace=tmp_path).satisfied


def test_recording_a_scenario_replaces_that_half_and_leaves_the_other() -> None:
    both = gate.record_scenario(record(), verdict("normal", failures=("stale",)))
    both = gate.record_scenario(both, verdict("shock"))
    fixed = gate.record_scenario(both, verdict("normal"))
    assert fixed.normal is not None and fixed.normal.passed
    assert fixed.shock is not None and fixed.shock.passed
    assert fixed.satisfied


def test_a_seat_that_only_answered_on_a_fallback_is_not_on_its_primary() -> None:
    seat = gate.SeatEvidence(
        candidate_id="baseline",
        seat_id="analyst",
        primary="openrouter:model-a",
        answered_on=("openrouter:model-b",),
    )
    assert not seat.answered_on_primary


def test_evidence_defaults_are_zero_not_none() -> None:
    """A candidate nobody measured must read as zero decisions, never as a missing field."""
    evidence = gate.CandidateEvidence(candidate_id="x")
    assert evidence.rows == 0
    assert evidence.cost_per_cycle == Decimal(0)


def test_an_unknown_scenario_is_refused_rather_than_stored() -> None:
    with pytest.raises(ConfigError, match="not a calibration scenario"):
        gate.record_scenario(record(), verdict("sideways"))
