"""Slice D pass 1 end to end: calibrate → gate → sweep, offline and free (§16).

The slice's exit criterion, driven through `cli.main` exactly as an operator would — the same
shape slices A, B and C take, and for the same reason: a handler called directly proves the
handler, while the operator's failure is usually in the wiring between them.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab import candidates as cd
from decision_lab import cli, registry


def test_the_gate_stands_between_an_operator_and_a_sweep(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus

    # Nothing calibrated: the expensive command refuses, and nothing is spent.
    assert _sweep(corpus_id) == cli.EXIT_GATE

    # Half calibrated is not calibrated.
    assert _calibrate("normal", corpus_id, tmp_path) == cli.EXIT_OK
    assert _sweep(corpus_id) == cli.EXIT_GATE

    # Both halves open it.
    assert _calibrate("shock", corpus_id, tmp_path) == cli.EXIT_OK
    assert _sweep(corpus_id) == cli.EXIT_OK

    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert {"calibrate-normal", "calibrate-shock", "sweep"} <= {row.scenario for row in rows}
    assert all(row.run_id for row in rows)
    # §11: `record` replaces the row with this identity rather than appending, so the three
    # `_sweep` calls above — two refusals and the run they were fixed into — are one experiment
    # and one row. `gate_skipped` and `status` are deliberately outside the identity, or a
    # refusal and its retry would show as two.
    assert len({row.run_id for row in rows}) == len(rows)


def test_a_calibration_report_names_the_days_the_gate_and_the_projection(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, _ = calibrated_corpus
    out = tmp_path / "shock.md"

    assert _calibrate("shock", corpus_id, tmp_path, out=out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "PLUMBING CHECK — NOT AN EVALUATION" in text, "the stub matrix is never an evaluation"
    assert "## Calibration — shock" in text
    assert "SHOCK_UP" in text and "SHOCK_DOWN" in text
    assert "Projected spend" in text
    assert "Gate: PASSED" in text


def _sweep(corpus_id: str) -> int:
    return cli.main(
        ["sweep", "--corpus", corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "1"]
    )


def _calibrate(scenario: str, corpus_id: str, tmp_path: Path, *, out: Path | None = None) -> int:
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
            str(out or tmp_path / f"{scenario}.md"),
        ]
    )
