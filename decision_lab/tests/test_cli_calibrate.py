"""`calibrate normal` and `calibrate shock` through `cli.main`, as an operator runs them.

Offline on the stub matrix, so the whole file is free and deterministic — which also means every
gate condition passes by construction here, and the refusals are asserted in `test_calibration.py`
against handmade evidence instead.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

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


def _long(data: Path, out: Path, *, skip_gate: bool = False) -> int:
    argv = [
        "calibrate",
        "long",
        "--data",
        str(data),
        "--configs",
        str(cd.STUB_MATRIX),
        "--candidate",
        _first_candidate(data),
        "--start-equity",
        "1000",
        "--every",
        "24h",
        "--window",
        "1m",
        "--out",
        str(out),
    ]
    return cli.main([*argv, "--skip-gate"] if skip_gate else argv)


def _first_candidate(data: Path) -> str:
    """The stub matrix's first candidate id, whatever it is called."""
    from tradebot.app import dataset_basket, select_panel
    from tradebot.core.clock import SystemClock
    from tradebot.marketdata.recorder import ReplayDataset

    dataset = ReplayDataset.load(data, SystemClock())
    reference = dataset_basket(dataset, select_panel("sim"), basket_id="reference")
    return cd.load_matrix(cd.STUB_MATRIX, reference=reference).candidates[0].candidate_id


def test_calibrate_long_refuses_without_the_gate(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§10.6: the sweep and the long run both refuse to start unless 1 and 2 have passed."""
    _, data = calibrated_corpus
    assert _long(data, tmp_path / "long.md") == cli.EXIT_GATE


def test_calibrate_long_runs_and_reports_both_halves_of_the_profit(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    corpus_id, data = calibrated_corpus
    for scenario in ("normal", "shock"):
        assert run(scenario, corpus_id, tmp_path / f"{scenario}.md") == cli.EXIT_OK

    out = tmp_path / "long.md"
    assert _long(data, out) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8").lower()
    assert "realized" in text and "unrealized" in text
    assert "net profit" in text
    assert "veto" in text


def test_a_skipped_gate_stamps_the_long_report(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    from decision_lab import render as rd

    _, data = calibrated_corpus
    out = tmp_path / "long.md"

    assert _long(data, out, skip_gate=True) == cli.EXIT_OK

    assert rd.GATE_SKIPPED in out.read_text(encoding="utf-8")


def test_a_long_run_is_reused_at_its_identity(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    """§5.5's rule one level over: identical parameters are one experiment, and a second
    invocation returns the first pass rather than appending a second into its log."""
    from decision_lab import longrun

    _, data = calibrated_corpus
    assert _long(data, tmp_path / "a.md", skip_gate=True) == cli.EXIT_OK
    directories = sorted((tmp_path / "workspace").glob("long-*"))
    assert len(directories) == 1
    first = (directories[0] / longrun.LONG_META).read_text(encoding="utf-8")

    assert _long(data, tmp_path / "b.md", skip_gate=True) == cli.EXIT_OK

    assert sorted((tmp_path / "workspace").glob("long-*")) == directories
    assert (directories[0] / longrun.LONG_META).read_text(encoding="utf-8") == first


def test_a_long_run_files_a_registry_row_carrying_its_window_and_equity(
    tmp_path: Path, calibrated_corpus: tuple[str, Path]
) -> None:
    _, data = calibrated_corpus
    assert _long(data, tmp_path / "long.md", skip_gate=True) == cli.EXIT_OK

    rows = [
        row
        for row in registry.read_all(workspace=tmp_path / "workspace")
        if row.scenario == "calibrate-long"
    ]
    assert len(rows) == 1
    assert rows[0].window == "1m"
    assert rows[0].start_equity == 1000
    assert rows[0].gate_skipped


def test_an_unknown_candidate_names_the_ones_that_exist(
    tmp_path: Path, calibrated_corpus: tuple[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """§15: a mistyped `--candidate` must not fall through to "nothing ran" — the matrix's own
    ids are the fix, and the operator cannot see them from the error otherwise."""
    _, data = calibrated_corpus
    # The handler directly, not `cli.main`: `main` calls `configure_logging`, which replaces the
    # root handlers and detaches caplog's. `test_cli_sweep` reaches for the same shape, and for
    # the same reason.
    args = cli.parse_args(
        [
            "calibrate",
            "long",
            "--data",
            str(data),
            "--configs",
            str(cd.STUB_MATRIX),
            "--candidate",
            "no-such-candidate",
            "--skip-gate",
            "--out",
            str(tmp_path / "long.md"),
        ]
    )

    with caplog.at_level(logging.ERROR, logger="decision_lab.cli"):
        code = asyncio.run(cli.calibrate_long(args))

    assert code == cli.EXIT_MISUSE
    named = [record for record in caplog.records if hasattr(record, "available")]
    assert named, "the refusal must name the ids that do exist"
    assert _first_candidate(data) in named[0].available
    assert not (tmp_path / "long.md").exists(), "a refusal writes no page"
