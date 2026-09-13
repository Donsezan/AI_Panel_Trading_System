"""§12.4 — a start is the CLI, under a lock, with a ceiling nobody defaulted for you."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

# See test_lab_dashboard_auth.py's own note: starlette's TestClient now prefers `httpx2`, which is
# not one of this repo's pinned dependencies, and falls back to `httpx` with a
# `StarletteDeprecationWarning` the root `filterwarnings = ["error"]` would otherwise turn into a
# collection failure of this module, unrelated to anything decision_lab does.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from decision_lab import jobs
from decision_lab.dashboard.routes import jobs as jobs_routes
from tradebot.core.errors import ConfigError


def test_a_builder_exists_for_every_launchable_command() -> None:
    """§12.4 names five. A sixth added to the page without a builder would 500 on submit."""
    assert set(jobs_routes.BUILDERS) == {
        "corpus-build",
        "calibrate-normal",
        "calibrate-shock",
        "sweep",
        "calibrate-long",
    }


def test_the_sweep_builder_produces_the_command_a_terminal_would_run() -> None:
    argv = jobs_routes.BUILDERS["sweep"](
        {"corpus": "abc", "configs": "config/sweep.toml", "budget": "40"}
    )
    assert argv == ["sweep", "--corpus", "abc", "--configs", "config/sweep.toml", "--budget", "40"]


def test_a_budgeted_command_refuses_without_a_ceiling() -> None:
    """No default: a start here spends real credit, and a defaulted ceiling is one nobody chose."""
    with pytest.raises(ConfigError, match="budget"):
        jobs_routes.BUILDERS["sweep"]({"corpus": "abc", "configs": "config/sweep.toml"})

    with pytest.raises(ConfigError, match="budget"):
        jobs_routes.BUILDERS["calibrate-normal"]({"corpus": "a", "configs": "b", "budget": ""})


def test_the_long_run_takes_no_budget_because_its_cli_has_no_ceiling() -> None:
    """Scenario 3 drives BacktestHarness with no engine seam to meter, so the form must not
    imply a ceiling that does not exist."""
    assert "calibrate-long" not in jobs_routes.BUDGETED
    argv = jobs_routes.BUILDERS["calibrate-long"](
        {
            "data": "data/history",
            "configs": "config/sweep.toml",
            "candidate": "baseline",
            "start_equity": "1000",
            "every": "4h",
            "window": "6m",
        }
    )
    assert "--budget" not in argv
    assert argv[:2] == ["calibrate", "long"]


def test_the_page_says_there_is_no_mid_run_ceiling_for_a_long_run(lab_client: TestClient) -> None:
    page = lab_client.get("/jobs").text
    assert "no mid-run ceiling" in page
    assert "delete" in page.lower(), "stopping one means deleting its directory before a re-run"


def test_starting_while_the_workspace_is_held_is_refused_on_the_page(
    lab_client: TestClient, tmp_path: Path
) -> None:
    from tradebot.core.clock import SystemClock

    workspace = tmp_path / "workspace"
    lock = jobs.WorkspaceLock(workspace=workspace)
    lock.acquire(("sweep", "--corpus", "held"), clock=SystemClock())
    try:
        response = lab_client.post(
            "/jobs/start",
            data={"command": "sweep", "corpus": "abc", "configs": "x.toml", "budget": "1"},
        )
        assert response.status_code == 409
        assert "another run holds this workspace" in response.text
        assert "held" in response.text, "the refusal names what is holding it"
    finally:
        lock.release()


def test_a_missing_field_is_a_refusal_on_the_page_not_a_500(lab_client: TestClient) -> None:
    response = lab_client.post("/jobs/start", data={"command": "sweep", "corpus": "abc"})
    assert response.status_code == 400
    assert "configs" in response.text


def test_an_unknown_command_is_refused(lab_client: TestClient) -> None:
    assert lab_client.post("/jobs/start", data={"command": "rm -rf"}).status_code == 400


def test_starting_and_stopping_a_real_child(lab_client: TestClient, tmp_path: Path) -> None:
    """`corpus build` against a dataset directory that does not exist: a real child, exiting
    quickly on the dataset refusal, entirely under `tmp_path`.

    `jobs.start` hands the route's workspace to the child as `DECISION_LAB_WORKSPACE`, so the
    lock it takes and the log it writes land in the sandbox rather than in the operator's real
    `decision_lab/workspace/`. The `finally` is not ceremony: an assertion failing between the
    start and the stop would otherwise leave a real process running past the session.
    """
    workspace = tmp_path / "workspace"
    response = lab_client.post(
        "/jobs/start",
        data={
            "command": "corpus-build",
            "data": str(tmp_path / "nothing"),
            "every": "8h",
            "reference_panel": "stub",
        },
        follow_redirects=True,
    )
    started = jobs.history(workspace=workspace)
    try:
        assert response.status_code == 200
        assert started, "the job was recorded"

        stopped = lab_client.post(
            "/jobs/stop", data={"job_id": started[0].job_id}, follow_redirects=True
        )
        assert stopped.status_code == 200
    finally:
        for record in started:
            jobs.stop(record.job_id, workspace=workspace)


def test_the_projection_says_so_when_nothing_has_been_calibrated(
    lab_client: TestClient, calibrated_corpus: tuple[str, Path]
) -> None:
    """An uncalibrated seat set has nothing to project from, and the gate will refuse it anyway."""
    corpus_id, _ = calibrated_corpus
    stub = Path(__file__).resolve().parents[1] / "config" / "sweep-stub.toml"

    page = lab_client.get(f"/jobs?corpus={corpus_id}&configs={stub}").text

    assert "no calibration on record" in page.lower()
    assert "calibrate normal" in page and "calibrate shock" in page


def test_the_projection_with_no_corpus_named_asks_for_one(lab_client: TestClient) -> None:
    """The bare page must not read as 'nothing has been calibrated' — it asked nothing yet."""
    assert "Name a corpus and a seat set" in lab_client.get("/jobs").text
