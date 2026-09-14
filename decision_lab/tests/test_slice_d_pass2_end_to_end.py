"""The slice exit criterion: a seat set built on the page, run from the page, read on the page.

Offline and free — the stub matrix, so the whole loop is exercised and nothing is measured, which
is what a plumbing check is for (§7.2). It is slow by construction: it spawns a real child
process running a real sweep, because "the page is the CLI" is the claim being tested.

The read-back is asserted two ways, deliberately not the single `rows[0].candidate_id in listing`
line the plan first proposed: `_registry_row` (`cli.py`) never sets `candidate_id` on a sweep row,
so that assertion is `"" in listing` — true of every possible page, and the least useful test in
the suite would have proven nothing. Part (a) below asserts the run is findable on the Runs list
by its `run_id` (real: `runs.html` renders it into the compare and detail links, never
unconditionally elsewhere). Part (b) follows the detail link and asserts one of the *expanded*
candidate ids the sweep actually produced (`varied-three~max_rounds=...`) appears in the ranking
table — real too: nothing in `run_detail.html` or the macros it imports names "varied-three"
outside `_tables.regime_table`'s per-row loop, and that loop renders only when a candidate was
actually ranked in that regime.
"""

from __future__ import annotations

import time
import tomllib
import warnings
from pathlib import Path

# See test_lab_dashboard_auth.py's own note: starlette's TestClient now prefers `httpx2`, which is
# not one of this repo's pinned dependencies, and falls back to `httpx` with a
# `StarletteDeprecationWarning` the root `filterwarnings = ["error"]` would otherwise turn into a
# collection failure of this module, unrelated to anything decision_lab does.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from decision_lab import jobs, matrices, registry
from decision_lab.tests.test_lab_dashboard_edit import _flatten
from tradebot.core.clock import Clock, SystemClock

SHIPPED = Path(__file__).resolve().parents[1] / "config"
TIMEOUT_SECONDS = 180


def _await_finish(job_id: str, workspace: Path, clock: Clock) -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        found = next(one for one in jobs.history(workspace=workspace) if one.job_id == job_id)
        refreshed = jobs.refresh(found, clock=clock, workspace=workspace)
        if refreshed.exit_code is not None:
            return refreshed.exit_code
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not finish within {TIMEOUT_SECONDS}s")


def test_build_a_seat_set_run_it_and_read_its_ranking(
    lab_client: TestClient, calibrated_corpus: tuple[str, Path], tmp_path: Path
) -> None:
    corpus_id, _ = calibrated_corpus
    workspace = tmp_path / "workspace"

    # 1. Build a seat set from the shipped plumbing-check template, through the form.
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    saved = lab_client.post(
        "/matrices/mine", data=_flatten(document) | {"action": "save"}, follow_redirects=True
    )
    assert saved.status_code == 200
    assert "uncalibrated" in saved.text.lower()
    assert matrices.versions("mine", workspace=workspace) == (1,)

    # 2. Run it, from the page, with an explicit ceiling and the gate skipped — a stub matrix can
    #    never satisfy a real gate, and this is a plumbing check by construction.
    started = lab_client.post(
        "/jobs/start",
        data={
            "command": "sweep",
            "corpus": corpus_id,
            "configs": str(matrices.path_for("mine", 1, workspace=workspace)),
            "budget": "1",
            "skip_gate": "on",
        },
        follow_redirects=False,
    )
    assert started.status_code == 303
    job_id = started.headers["location"].split("job=")[1]

    assert _await_finish(job_id, workspace, SystemClock()) == 0

    # 3. Read what it scored, on the page.
    rows = registry.read_all(workspace=workspace)
    assert rows, "the child process recorded no registry row"

    # (a) the run is findable on the Runs list, by the id `runs.html` renders into every row's
    #     compare and detail links — real, because an empty registry renders no run_id at all.
    listing = lab_client.get("/").text
    assert rows[0].run_id in listing

    # (b) following that link, the ranking names one of the expanded candidates the stub matrix's
    #     `[expand] max_rounds = [1, 3]` actually produced — not `rows[0].candidate_id`, which a
    #     sweep row never sets, and not rendered anywhere on this page outside the per-candidate
    #     row of `_tables.regime_table`, which is empty unless a candidate was actually ranked.
    detail = lab_client.get(f"/runs/{rows[0].run_id}").text
    assert "NORMAL" in detail
    assert "gate skipped" in detail.lower()
    assert "plumbing check" in detail.lower()
    assert "varied-three~max_rounds=" in detail, "the stub matrix's expanded candidate is ranked"
