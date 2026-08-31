"""The `sweep` command's refusals, each with its own exit code (spec §13, §15).

A distinct code per distinct refusal, so a script can tell "you forgot a key" from "you ran out of
budget" without parsing a log line — the convention the bot's own CLI follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab import cli, registry


def test_an_unreachable_evaluation_exits_4_and_leaves_a_registry_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, built_corpus_id: str
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code = cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(cd.DEFAULT_MATRIX)])

    assert code == cli.EXIT_CANDIDATE
    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert rows[-1].status == "provider_unavailable"
    assert "OPENROUTER_API_KEY" in rows[-1].note


def test_an_invalid_matrix_exits_4_before_any_spend(tmp_path: Path, built_corpus_id: str) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[[candidates]]\nid = "x"\nqualified_majority = "9"\n\n'
        '  [[candidates.seats]]\n  seat_id = "s"\n  role = "r"\n'
        '  provider_id = "stub"\n  model = "varied-technical"\n',
        encoding="utf-8",
    )

    assert cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(bad)]) == (
        cli.EXIT_CANDIDATE
    )


def test_a_budget_of_zero_exits_5_with_partial_results_written(
    tmp_path: Path, built_corpus_id: str
) -> None:
    code = cli.main(
        ["sweep", "--corpus", built_corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "0"]
    )

    assert code == cli.EXIT_BUDGET


def test_a_plumbing_sweep_runs_and_the_report_says_what_it_was(
    tmp_path: Path, built_corpus_id: str
) -> None:
    assert (
        cli.main(
            [
                "sweep",
                "--corpus",
                built_corpus_id,
                "--configs",
                str(cd.STUB_MATRIX),
                "--budget",
                "1",
            ]
        )
        == cli.EXIT_OK
    )

    out = tmp_path / "report.md"
    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "PLUMBING CHECK" in text
    assert "## Candidates, by regime" in text


def test_report_without_a_sweep_still_scores_the_reference_pass(
    tmp_path: Path, built_corpus_id: str
) -> None:
    out = tmp_path / "report.md"

    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")
    assert "## Panel, by regime" in text
    assert "## Candidates, by regime" not in text
