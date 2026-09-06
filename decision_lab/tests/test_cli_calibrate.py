"""`calibrate normal` and `calibrate shock` through `cli.main`, as an operator runs them.

Offline on the stub matrix, so the whole file is free and deterministic — which also means every
gate condition passes by construction here, and the refusals are asserted in `test_calibration.py`
against handmade evidence instead.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import cli, gate, registry
from decision_lab import corpus as cp
from decision_lab import dataset as ds


def run(scenario: str, corpus_id: str, out: Path) -> int:
    return cli.main(
        [
            "calibrate",
            scenario,
            "--corpus",
            corpus_id,
            "--configs",
            str(cd.STUB_MATRIX),
            "--budget",
            "1",
            "--out",
            str(out),
        ]
    )


def test_calibrate_normal_writes_a_report_and_half_a_gate(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, data = calibrated_corpus
    out = tmp_path / "normal.md"

    assert run("normal", corpus_id, out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "## Calibration — normal" in text
    assert "Gate: PASSED" in text

    record = _record(corpus_id, data, tmp_path)
    assert record is not None
    assert record.normal is not None and record.normal.passed
    assert record.shock is None, "one command fills one half"


def test_both_halves_open_the_gate(tmp_path: Path, calibrated_corpus: tuple[str, Path]) -> None:
    corpus_id, data = calibrated_corpus

    assert run("normal", corpus_id, tmp_path / "n.md") == cli.EXIT_OK
    assert run("shock", corpus_id, tmp_path / "s.md") == cli.EXIT_OK

    record = _record(corpus_id, data, tmp_path)
    assert record is not None and record.satisfied


def test_the_shock_report_never_pools_the_two_directions(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§8.3: SHOCK_UP and SHOCK_DOWN ask opposite questions of a long-only system."""
    corpus_id, _ = calibrated_corpus
    out = tmp_path / "s.md"

    assert run("shock", corpus_id, out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "SHOCK_UP" in text and "SHOCK_DOWN" in text
    assert "| SHOCK |" not in text


def test_a_calibration_files_a_registry_row_per_candidate(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus

    assert run("normal", corpus_id, tmp_path / "n.md") == cli.EXIT_OK

    rows = [
        row
        for row in registry.read_all(workspace=tmp_path / "workspace")
        if row.scenario == "calibrate-normal"
    ]
    assert rows, "§11: every calibration run appends a row"
    assert all(row.dayset_digest for row in rows), "the day set is part of the identity"


def test_a_dataset_with_no_pinned_day_set_refuses(tmp_path: Path, built_corpus_id: str) -> None:
    """§15: a missing pinned day set refuses a calibration, naming `dataset days`."""
    assert run("normal", built_corpus_id, tmp_path / "n.md") == cli.EXIT_DATASET


def test_the_report_carries_the_cost_projection(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus
    out = tmp_path / "n.md"

    assert run("normal", corpus_id, out) == cli.EXIT_OK

    assert "Projected spend" in out.read_text(encoding="utf-8")


def _record(corpus_id: str, data: Path, tmp_path: Path) -> gate.GateRecord | None:
    meta = cp.load(corpus_id, workspace=tmp_path / "workspace").meta
    matrix = cd.load_matrix(cd.STUB_MATRIX, reference=meta.reference_basket)
    key = gate.gate_key(
        dataset_digest=ds.require_verified(data).dataset_digest,
        matrix_digest=matrix.matrix_digest,
        dayset_digest=cday.require_pinned(data).dayset_digest,
    )
    return gate.read(key, workspace=tmp_path / "workspace")
