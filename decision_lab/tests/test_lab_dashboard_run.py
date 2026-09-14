"""§12.4 — a start is the CLI, under a lock, with a ceiling nobody defaulted for you."""

from __future__ import annotations

import time
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
from decision_lab.params import HOLDER_FILE, JOBS_DIR
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
    """`"configs" in response.text` alone is vacuous: every rendering of `jobs.html` already
    contains that literal string via `name="configs"` on the calibrate-normal, calibrate-shock and
    sweep forms, which are always rendered — so that assertion would pass even if `_required`'s
    refusal were never rendered at all. Assert on the actual message `_required` raises instead."""
    response = lab_client.post("/jobs/start", data={"command": "sweep", "corpus": "abc"})
    assert response.status_code == 400
    assert "configs is required, and nothing was started" in response.text


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


def test_a_refusal_page_reports_the_same_status_as_the_index_page(
    lab_client: TestClient, tmp_path: Path
) -> None:
    """Finding 2 (Task 9 fix round 1): before `_history_context` was pulled out, `_refusal` built
    its history table without the `jobs.refresh` call `index` makes first, so a job that finished
    in the window between a refused start and the read rendered as "ended without being recorded"
    on the refusal page while the index page, reading the very same record, would have reported
    its real exit code. Both must now agree.
    """
    workspace = tmp_path / "workspace"
    lab_client.post(
        "/jobs/start",
        data={
            "command": "corpus-build",
            "data": str(tmp_path / "nothing"),
            "every": "8h",
            "reference_panel": "stub",
        },
        follow_redirects=False,
    )
    started = jobs.history(workspace=workspace)
    assert started, "the job was recorded"
    record = started[0]

    # Wait for the real child - which refuses almost immediately against a dataset directory that
    # does not exist - to actually exit. `jobs.status_of` is the non-mutating probe: unlike
    # `jobs.refresh`, it never writes the exit code back to disk, so polling it is how the test
    # catches the record in the exact window this bug lived in — the process has exited but
    # nothing has yet noticed and persisted its exit code. (A lock-release probe is not the same
    # signal: the child releases the workspace lock in its own `finally`, slightly *before* the
    # OS reports the process as exited, which raced this loop when it was written against
    # `jobs.holder` instead.)
    deadline = time.monotonic() + 10
    status = jobs.status_of(record, workspace=workspace)
    while status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        status = jobs.status_of(record, workspace=workspace)
    assert status != "running", "the child did not exit in time"

    refusal = lab_client.post("/jobs/start", data={"command": "rm -rf"})
    assert refusal.status_code == 400
    assert "ended without being recorded" not in refusal.text, (
        "a finished job must report its real exit code on a refusal page, not read as still-live"
    )

    finished = jobs.history(workspace=workspace)[0]
    assert finished.exit_code is not None, "the refusal's own refresh should have recorded it"
    status_line = f"exit {finished.exit_code}"
    assert status_line in refusal.text
    assert status_line in lab_client.get("/jobs").text


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


def test_an_unreadable_job_record_does_not_take_the_page_down(
    lab_client: TestClient, tmp_path: Path
) -> None:
    """Important 3, at the surface: one bad sidecar must cost its own row, not the whole page.

    `jobs.history` is read by `GET /jobs`, and `jobs.holder` by `views.render` on *every* page —
    so parsed bare, a truncated `jobs/*.json` or an unreadable `.run.holder.json` made the entire
    dashboard a 500 until someone found and deleted the file by hand.

    The lock is genuinely held for the duration, because `holder` probes the OS lock first and
    returns before it ever opens the sidecar on a free workspace: corrupting that file without
    holding the lock would assert nothing at all about the parse this test exists to cover.
    """
    from datetime import UTC, datetime

    from tradebot.core.clock import ManualClock

    workspace = tmp_path / "workspace"
    (workspace / JOBS_DIR).mkdir(parents=True, exist_ok=True)
    (workspace / JOBS_DIR / "truncated.json").write_text('{"job_id": "tr', encoding="utf-8")

    lock = jobs.WorkspaceLock(workspace=workspace)
    lock.acquire(("sweep",), clock=ManualClock(datetime(2026, 9, 6, tzinfo=UTC)))
    try:
        (workspace / HOLDER_FILE).write_text("{ not json at all", encoding="utf-8")
        assert jobs.holder(workspace=workspace) is not None, "the fixture must hold the lock"

        listing = lab_client.get("/jobs")
        elsewhere = lab_client.get("/")
    finally:
        lock.release()

    assert listing.status_code == 200, listing.text
    assert "Name a corpus and a seat set" in listing.text, "the page rendered, not an error body"
    assert elsewhere.status_code == 200, "every page calls `holder()` through `views.render`"
