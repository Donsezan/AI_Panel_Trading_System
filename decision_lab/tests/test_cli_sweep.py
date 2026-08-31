"""The `sweep` command's refusals, each with its own exit code (spec §13, §15).

A distinct code per distinct refusal, so a script can tell "you forgot a key" from "you ran out of
budget" without parsing a log line — the convention the bot's own CLI follows.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import cli, registry, sampling
from decision_lab import sweep as sw
from decision_lab.tests.test_candidates import STUB_MATRIX


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


def test_report_refuses_when_the_matrix_no_longer_matches_the_sweep_that_ran(
    tmp_path: Path, built_corpus_id: str
) -> None:
    """finding 2: renaming the one candidate after the sweep ran moves `matrix_digest` (§7.1) —
    reloading blindly would read zero rows for the renamed candidate and stamp a fresh registry
    row with the old experiment's numbers under the new digest. `report` must refuse instead."""
    configs = tmp_path / "m.toml"
    configs.write_text(STUB_MATRIX, encoding="utf-8")
    assert (
        cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(configs), "--budget", "1"])
        == cli.EXIT_OK
    )

    # Still valid TOML, still parseable — exactly "one edited prompt" (§7.1's own example).
    configs.write_text(STUB_MATRIX.replace('id = "baseline"', 'id = "renamed"'), encoding="utf-8")

    out = tmp_path / "report.md"
    code = cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)])

    assert code == cli.EXIT_CANDIDATE
    assert not out.exists()


def test_report_refuses_when_the_recorded_matrix_file_is_gone(
    tmp_path: Path, built_corpus_id: str
) -> None:
    """finding 2: a `--configs` path recorded by `sweep` can stop resolving by the time `report`
    runs (a relative path, a different cwd, a moved file) — this must refuse cleanly rather than
    escape as the dataset exit code, which is what an uncaught `ConfigError` here used to do."""
    configs = tmp_path / "m.toml"
    configs.write_text(STUB_MATRIX, encoding="utf-8")
    assert (
        cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(configs), "--budget", "1"])
        == cli.EXIT_OK
    )

    configs.unlink()

    out = tmp_path / "report.md"
    code = cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)])

    assert code == cli.EXIT_CANDIDATE
    assert not out.exists()


def test_report_marks_a_candidate_the_sweep_never_reached_as_not_measured(
    tmp_path: Path, built_corpus_id: str
) -> None:
    """finding 3: deleting the second candidate's rows file after a completed sweep reproduces
    exactly the on-disk state a real halt leaves behind for a candidate it never got to."""
    configs = tmp_path / "m.toml"
    configs.write_text(
        STUB_MATRIX + '\n[expand]\ndecision_mode = ["per_asset", "basket"]\n', encoding="utf-8"
    )
    assert (
        cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(configs), "--budget", "1"])
        == cli.EXIT_OK
    )

    result = sw.latest_meta(built_corpus_id)
    assert result is not None
    assert len(result.candidate_ids) == 2
    second_id = result.candidate_ids[1]
    sw.rows_path(built_corpus_id, result.matrix_digest, second_id).unlink()

    out = tmp_path / "report.md"
    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert f"| NORMAL | {second_id} |" not in text, "unreached must never be a scored row"
    assert f"**Not measured:** {second_id}" in text


def test_report_warns_when_two_sweeps_are_ambiguous_and_names_the_digests(
    tmp_path: Path, built_corpus_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    """finding 4: with two sweeps and no `--matrix`, `report` must say which digests it found
    rather than silently rendering a page with no candidate sections at all.

    Calls the coroutines directly rather than going through `cli.main`, which calls
    `configure_logging` and would replace the root logger's handlers — including caplog's own —
    between the two `sweep` invocations and the `report` one.
    """
    first = tmp_path / "a.toml"
    first.write_text(STUB_MATRIX, encoding="utf-8")
    second = tmp_path / "b.toml"
    second.write_text(STUB_MATRIX.replace('id = "baseline"', 'id = "other"'), encoding="utf-8")

    for configs in (first, second):
        args = cli.parse_args(
            ["sweep", "--corpus", built_corpus_id, "--configs", str(configs), "--budget", "1"]
        )
        assert asyncio.run(cli.sweep_command(args)) == cli.EXIT_OK

    out = tmp_path / "report.md"
    report_args = cli.parse_args(["report", "--corpus", built_corpus_id, "--out", str(out)])
    with caplog.at_level(logging.WARNING, logger="decision_lab.cli"):
        assert asyncio.run(cli.report(report_args)) == cli.EXIT_OK

    assert any("more than one sweep" in record.message for record in caplog.records)
    assert "## Candidates, by regime" not in out.read_text(encoding="utf-8")


def test_the_sample_stratifies_on_the_pinned_day_sets_reference_instrument(
    tmp_path: Path, built_corpus_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finding 6: §7.3's stratum is the regime of the instrument §4.5 already drew the day set
    from — `sweep` must read it off the pinned `CalibrationDays`, not assume
    `dataset.instruments[0]`. The stub reference instrument here need not exist in the dataset:
    `sampling.stratified` itself is replaced, so nothing downstream ever looks it up."""
    pinned = cday.CalibrationDays(
        selected_at=datetime(2024, 1, 1, tzinfo=UTC),
        seed=1,
        reference_instrument="binance:ETH/USDT",
        scoring_timeframe="1h",
        dataset_digest="d",
        dayset_digest="p",
        days={"normal": (date(2024, 1, 1),)},
    )
    monkeypatch.setattr(cday, "require_pinned", lambda directory: pinned)

    captured: dict[str, object] = {}

    def fake_stratified(*args: object, **kwargs: object) -> sampling.Sample:
        captured.update(kwargs)
        return sampling.Sample()

    monkeypatch.setattr(sampling, "stratified", fake_stratified)

    configs = tmp_path / "m.toml"
    configs.write_text(STUB_MATRIX, encoding="utf-8")
    assert (
        cli.main(["sweep", "--corpus", built_corpus_id, "--configs", str(configs), "--budget", "1"])
        == cli.EXIT_OK
    )

    assert captured["reference_instrument"] == "binance:ETH/USDT"
    assert captured["pinned"] == pinned.all_days
