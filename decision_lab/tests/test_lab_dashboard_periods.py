# ruff: noqa: F811 - the imported fixtures are used by name as test parameters
"""The period page (period spec §4, §5.7, §7.3), over real period runs read back from the log.

The runs are `test_period_pause`'s own scenarios, paid for once per module; each test reads a copy
of their workspace, so doctoring a meta for one ending never leaks into another test.
"""

from __future__ import annotations

import re
import shutil
import warnings
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from decision_lab import analysis, jobs
from decision_lab import period as pd
from decision_lab.dashboard.app import create_lab_dashboard
from decision_lab.dashboard.routes import periods as periods_routes
from decision_lab.records import records_from_store
from decision_lab.regimes import RegimeLabel
from decision_lab.spikes import SpikeEvent
from decision_lab.tests.test_analysis_period_report import feed_of, story, with_news
from decision_lab.tests.test_newsfeed import archive
from decision_lab.tests.test_period_pause import (  # noqa: F401 - pytest fixtures
    Ran,
    Scenarios,
    dropping_data,
    flat_data,
    scenarios,
)
from tradebot.core.clock import SystemClock

TOKEN = "decision-lab-token-0123456789"


def client_over(workspace: Path) -> TestClient:
    client = TestClient(create_lab_dashboard(workspace=workspace, token=TOKEN))
    client.post("/login", data={"token": TOKEN})
    return client


def limit_of_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load every seat set as the scenarios' candidates were built: a loss limit of two.

    A matrix cannot express a risk limit, and the page matches a run's candidate on the whole
    basket it ran (fix round 1, item 3), so without this no file on disk is the scenarios' basket.
    """
    from decision_lab import candidates as cd
    from decision_lab.tests.test_period_run import with_loss_limit

    real = cd.load_matrix

    def limited(path: Path, *, reference: object) -> cd.Matrix:
        matrix = real(path, reference=reference)  # type: ignore[arg-type]
        return replace(
            matrix, candidates=tuple(with_loss_limit(one, 2) for one in matrix.candidates)
        )

    monkeypatch.setattr(cd, "load_matrix", limited)


def copied(ran: Ran, tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(ran.database.parent.parent, workspace)
    return workspace


@pytest.fixture
def paused(
    scenarios: Scenarios, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Ran, Path]]:
    """A copy of the January-paused quarter's workspace, and a signed-in client over it."""
    import asyncio

    ran = asyncio.run(scenarios.paused_in_january())
    limit_of_two(monkeypatch)
    workspace = copied(ran, tmp_path)
    yield client_over(workspace), ran, workspace


def with_meta(workspace: Path, ran: Ran, name: str, **changes: object) -> str:
    """Copy the run to a sibling id whose meta carries `changes`; return the new id."""
    source = pd.period_dir(ran.meta.run_id, workspace=workspace)
    target = pd.period_dir(name, workspace=workspace)
    shutil.copytree(source, target)
    meta = ran.meta.model_copy(update={"run_id": name, **changes})
    (target / pd.PERIOD_META).write_text(meta.model_dump_json(), encoding="utf-8")
    return name


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 1, day, hour, tzinfo=UTC)


def spike(start: datetime, end: datetime, move: str = "5") -> SpikeEvent:
    return SpikeEvent(
        start=start,
        end=end,
        direction=RegimeLabel.SHOCK_UP,
        move_pct=Decimal(move),
        peak_volatility=Decimal(1),
    )


def recording(started: list[list[str]], job_id: str):  # type: ignore[no-untyped-def]
    """A `jobs.start` that records the argv instead of spawning the CLI."""

    def start(argv: list[str], **_: object) -> jobs.JobRecord:
        started.append(list(argv))
        return jobs.JobRecord(
            job_id=job_id,
            label="evaluate-period",
            argv=tuple(argv),
            pid=1,
            started_at=SystemClock().now(),
        )

    return start


def input_named(page: str, name: str) -> str:
    found = re.findall(rf'<input[^>]*name="{name}"[^>]*>', page)
    assert found, f"no input named {name}"
    return str(found[0])


# ------------------------------------------------------------------------------- auth


def test_auth_covers_every_new_route(tmp_path: Path) -> None:
    """The shared walk (`test_lab_dashboard_auth`), narrowed to this router's routes."""
    from decision_lab.tests.test_lab_dashboard_auth import filled, protected_routes

    client = TestClient(create_lab_dashboard(workspace=tmp_path, token=TOKEN))
    routes = [
        (path, methods)
        for path, methods, module in protected_routes(client.app)
        if module == periods_routes.__name__
    ]
    assert len(routes) >= 7, routes
    for path, methods in routes:
        for method in methods - {"HEAD"}:
            response = client.request(method, filled(path), follow_redirects=False)
            if method == "GET":
                assert response.status_code == 303, path
                assert response.headers["location"] == "/login", path
            else:
                assert response.status_code == 401, path


# ------------------------------------------------------------------------------- the form


def test_the_form_offers_only_covered_periods(flat_data: Path, tmp_path: Path) -> None:
    client = client_over(tmp_path / "workspace")
    months = client.get("/periods", params={"data": str(flat_data), "kind": "month"}).text
    offered = re.findall(r'<option value="(20\d\d-\d\d)"', months)
    assert offered == ["2024-01", "2024-02", "2024-03"]

    quarters = client.get("/periods", params={"data": str(flat_data), "kind": "quarter"}).text
    assert re.findall(r'<option value="(20\d\d-Q\d)"', quarters) == ["2024-Q1"]

    years = client.get("/periods", params={"data": str(flat_data), "kind": "year"}).text
    assert "fully covers no calendar year" in years


def test_the_form_names_the_candidates_of_the_seat_set(flat_data: Path, tmp_path: Path) -> None:
    from decision_lab.tests.factories import stub_matrix_file

    page = (
        client_over(tmp_path / "workspace")
        .get("/periods", params={"data": str(flat_data), "configs": str(stub_matrix_file())})
        .text
    )
    assert '<select name="candidate">' in page


def test_budget_has_no_default_and_is_required(tmp_path: Path) -> None:
    page = client_over(tmp_path / "workspace").get("/periods").text
    budget_input = input_named(page, "budget")
    assert 'name="budget"' in page and "required" in budget_input and "value=" not in budget_input
    assert "a run can overshoot by up to a month" in page


FIELDS = {
    "data": "data/history",
    "configs": "config/sweep.toml",
    "candidate": "baseline",
    "kind": "month",
    "period": "2025-03",
    "start_equity": "1000",
    "every": "4h",
    "budget": "40",
}


def test_run_builds_the_cli_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    monkeypatch.setattr(jobs, "start", recording(started, "j1"))
    response = client_over(tmp_path / "workspace").post(
        "/periods/run",
        data={**FIELDS, "news_source": "cryptoslate, bitcoincom", "skip_gate": "on"},
        follow_redirects=False,
    )

    def started_argv() -> list[str]:
        return started[0]

    assert response.status_code == 303 and "job=j1" in response.headers["location"]
    assert started_argv()[:2] == ["evaluate", "period"]
    argv = started_argv()
    assert argv[argv.index("--budget") + 1] == "40"
    assert argv[argv.index("--period") + 1] == "2025-03"
    assert argv.count("--news-source") == 2 and "--skip-gate" in argv
    assert "--continue" not in argv


def test_a_refusal_rerenders_with_the_reason(tmp_path: Path) -> None:
    client = client_over(tmp_path / "workspace")
    missing = client.post("/periods/run", data={k: v for k, v in FIELDS.items() if k != "budget"})
    assert missing.status_code == 400
    assert "budget is required, and nothing was started" in missing.text
    assert "Evaluate a seat set over a period" in missing.text, "the form, not a bare error"

    lock = jobs.WorkspaceLock(workspace=tmp_path / "workspace")
    lock.acquire(("sweep", "--corpus", "held"), clock=SystemClock())
    try:
        busy = client.post("/periods/run", data=FIELDS)
    finally:
        lock.release()
    assert busy.status_code == 409 and "held" in busy.text


# ------------------------------------------------------------------------------- the result


def test_a_lift_is_on_the_page_beside_the_profit(paused: tuple[TestClient, Ran, Path]) -> None:
    client, ran, _ = paused
    assert ran.meta.lifts, "the scenario must lift a pause for this to bite"
    page = client.get(f"/periods/{ran.meta.run_id}").text
    assert "PAUSE LIFTED" in page
    profit = page[page.index("Net profit after model costs") :][:600]
    assert "lifted pause" in profit, "the lift is stated where the profit figure is"
    assert "⚠ Paused" in page


def test_all_spike_rows_render_with_no_events(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ran, _ = paused
    monkeypatch.setattr(analysis, "find_events", lambda *_a, **_k: ())
    page = client.get(f"/periods/{ran.meta.run_id}").text
    assert "REPORT INCOMPLETE" not in page, page[page.find("REPORT INCOMPLETE") :][:400]
    assert all(x in page for x in ("Normal", "Spike up", "Spike down", "No spikes in this period"))


def test_months_not_run_read_not_run(paused: tuple[TestClient, Ran, Path]) -> None:
    client, ran, workspace = paused
    name = with_meta(
        workspace,
        ran,
        "short",
        months=ran.meta.months[:1],
        ending=pd.RunEnding.INTERRUPTED,
        ending_reason="ValueError: boom",
    )
    page = client.get(f"/periods/{name}").text
    assert page.count(">not run<") == 2


def test_model_failed_has_no_continue_and_interrupted_does(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ran, workspace = paused
    failed = with_meta(
        workspace,
        ran,
        "failed",
        ending=pd.RunEnding.RULE_ENDED,
        ending_reason="out of money on 2024-02-03",
    )
    budget = with_meta(
        workspace,
        ran,
        "budget",
        ending=pd.RunEnding.INTERRUPTED,
        ending_reason="budget of 1 passed at the end of 2024-01",
        budget_stopped=True,
    )

    failed_page = client.get(f"/periods/{failed}").text
    assert "Model failed: out of money on 2024-02-03" in failed_page
    assert "/continue" not in failed_page
    refused = client.post(f"/periods/{failed}/continue", data={"budget": "10"})
    assert refused.status_code == 409 and "Model failed, requires tuning" in refused.text

    budget_page = client.get(f"/periods/{budget}").text
    assert f'action="/periods/{budget}/continue"' in budget_page
    assert "needs a larger budget" in budget_page

    started: list[list[str]] = []
    monkeypatch.setattr(jobs, "start", recording(started, "j2"))
    assert client.post(f"/periods/{budget}/continue", data={}).status_code == 400, "no budget"
    went = client.post(f"/periods/{budget}/continue", data={"budget": "9"}, follow_redirects=False)
    assert went.status_code == 303
    argv = started[0]
    assert argv[:2] == ["evaluate", "period"] and "--continue" in argv
    assert argv[argv.index("--period") + 1] == ran.meta.period.label
    assert argv[argv.index("--every") + 1] == "24h"
    assert argv[argv.index("--budget") + 1] == "9"


def test_a_running_job_with_half_written_meta_is_not_a_500(
    paused: tuple[TestClient, Ran, Path],
) -> None:
    """Review Focus 4: a running job's record is absent, cut off or ahead of its log."""
    client, ran, workspace = paused
    half = pd.period_dir("half", workspace=workspace)
    half.mkdir()
    (half / pd.PERIOD_META).write_text('{"run_id": "half", "period": {"lab', encoding="utf-8")
    empty = pd.period_dir("starting", workspace=workspace)
    empty.mkdir()
    running = with_meta(workspace, ran, "running", ending=pd.RunEnding.RUNNING)
    (pd.period_dir(running, workspace=workspace) / pd.PERIOD_DB).unlink()

    listing = client.get("/periods")
    assert listing.status_code == 200 and "cannot be read" in listing.text
    for run_id in ("half", "starting"):
        response = client.get(f"/periods/{run_id}")
        assert response.status_code == 200, run_id
    page = client.get(f"/periods/{running}")
    assert page.status_code == 200 and "REPORT INCOMPLETE" in page.text
    assert "Running, or a process died" in page.text
    assert client.get("/periods/nothing-here").status_code == 404
    assert client.get(f"/periods/{running}/months/2024-01").status_code == 404


def test_a_frozen_run_shows_spend_and_no_valuation(paused: tuple[TestClient, Ran, Path]) -> None:
    client, ran, workspace = paused
    profit = ran.meta.profit.model_copy(update={"unvaluable": True, "freeze_reason": "no mark"})
    frozen = with_meta(workspace, ran, "frozen", profit=profit)
    page = client.get(f"/periods/{frozen}").text
    assert "UNVALUABLE" in page and "Net profit after model costs" not in page
    assert "Model costs" in page


def test_a_cycle_shows_dispute_result_market_and_grades(
    paused: tuple[TestClient, Ran, Path],
) -> None:
    client, ran, workspace = paused
    month = client.get(
        f"/periods/{ran.meta.run_id}/months/2024-01", headers={"HX-Request": "true"}
    ).text
    assert "<html" not in month, "htmx gets the fragment alone"
    assert "<svg" in month and "quarantined (paused on losses)" in month
    cycle_ids = re.findall(r"/cycles/([^?\"]+)", month)
    assert cycle_ids

    import asyncio

    from decision_lab.tests.test_analysis_period import scoring_for

    analysed = analysis.analyse_period(
        ran.meta.run_id,
        asyncio.run(scoring_for(Path(ran.meta.dataset_directory))),
        panel=_panel(ran),
        archive=None,
        workspace=workspace,
    )
    bought = next(r for r in analysed.records if r.as_of == at(10))
    page = client.get(f"/periods/{ran.meta.run_id}/cycles/{bought.cycle_id}")
    assert page.status_code == 200 and "<html" in page.text, "a plain navigation gets a page"
    text = page.text
    for heading in ("The dispute", "Round 0, blind", "The result", "How the market performed"):
        assert heading in text, heading
    assert "Grades" in text and "entry" in text and "buy" in text
    assert text.index("The dispute") < text.index("The result") < text.index("Grades")


def _panel(ran: Ran):  # type: ignore[no-untyped-def]
    from decision_lab.tests.test_period_pause import candidate_over

    return candidate_over(Path(ran.meta.dataset_directory), 2).panel


def test_news_blind_page_says_the_seats_had_no_news(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ran, _ = paused
    monkeypatch.setattr(analysis, "find_events", lambda *_a, **_k: (spike(at(20), at(25)),))
    page = client.get(f"/periods/{ran.meta.run_id}").text
    assert "NEWS-BLIND" in page and "No spikes in this period" not in page
    event = client.get(f"/periods/{ran.meta.run_id}/events/0").text
    assert "the seats had no news, so this says nothing about reading it" in event
    assert "Lead-in" in event


def test_a_retracing_event_is_labelled_by_its_own_move(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merged spike-up event can end lower than it began; its move is printed as it is."""
    client, ran, _ = paused
    monkeypatch.setattr(analysis, "find_events", lambda *_a, **_k: (spike(at(20), at(25), "-3"),))
    page = client.get(f"/periods/{ran.meta.run_id}").text
    assert "↑ spike up" in page and "move -3.00%" in page and "+-3" not in page


def test_could_see_and_published_later_are_separate_blocks(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ran, workspace = paused
    fed = with_meta(workspace, ran, "fed", archive_digest="fed-digest", news_blind=False)
    feed = feed_of(archive(story(1, "early"), story(24, "the later story"), story(28, "late")))
    monkeypatch.setattr(periods_routes, "_feed_for", lambda *_a, **_k: (feed, ""))
    monkeypatch.setattr(analysis, "find_events", lambda *_a, **_k: (spike(at(20), at(25)),))
    real = records_from_store

    def served(store):  # type: ignore[no-untyped-def]
        records = real(store)
        seen = max((r for r in records if r.decisions and r.as_of <= at(22)), key=lambda r: r.as_of)
        return tuple(
            with_news(r, "the served story") if r.cycle_id == seen.cycle_id else r for r in records
        )

    monkeypatch.setattr(analysis, "records_from_store", served)

    page = client.get(f"/periods/{fed}/events/0").text
    could, later = page.index("What the panel could see"), page.index("Published later")
    assert could < page.index("the served story") < later
    assert page.index("the later story") > later
    assert "NEWS-BLIND" not in page


def test_the_nav_reaches_the_page(tmp_path: Path) -> None:
    page = client_over(tmp_path / "workspace").get("/").text
    assert 'href="/periods"' in page


def test_the_vendored_htmx_is_the_bots_own_copy() -> None:
    """One vendored file and one provenance (ADR 0014): the lab's copy is byte-identical."""
    root = Path(__file__).resolve().parents[2]
    assert (root / "decision_lab/dashboard/static/htmx.min.js").read_bytes() == (
        root / "tradebot/dashboard/static/htmx.min.js"
    ).read_bytes()


# --------------------------------------------------------- fix round 1: provenance (items 3, 4)


def test_a_candidate_with_the_same_panel_and_another_basket_is_not_this_run(
    scenarios: Scenarios, tmp_path: Path
) -> None:
    """The shipped stub matrix has these seats under the default loss limit, not the run's two."""
    import asyncio

    ran = asyncio.run(scenarios.paused_in_january())
    client = client_over(copied(ran, tmp_path))
    page = client.get(f"/periods/{ran.meta.run_id}").text
    assert "REPORT INCOMPLETE" in page and "not on file" in page


def test_the_recorded_seat_set_file_is_tried_first(
    paused: tuple[TestClient, Ran, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decision_lab import candidates as cd
    from decision_lab.tests.factories import stub_matrix_file

    client, ran, workspace = paused
    recorded = tmp_path / "recorded.toml"
    shutil.copyfile(stub_matrix_file(), recorded)
    name = with_meta(workspace, ran, "sourced", matrix_source=str(recorded))
    loaded: list[Path] = []
    real = cd.load_matrix
    monkeypatch.setattr(
        cd, "load_matrix", lambda path, **k: loaded.append(Path(path)) or real(path, **k)
    )

    page = client.get(f"/periods/{name}").text

    assert "REPORT INCOMPLETE" not in page
    assert loaded == [recorded], "the recorded file answered, so nothing else was opened"
    assert str(recorded) in page


def test_the_stored_version_scan_is_cached(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from decision_lab import candidates as cd

    client, ran, _ = paused
    client.get(f"/periods/{ran.meta.run_id}")
    calls: list[Path] = []
    real = cd.load_matrix
    monkeypatch.setattr(
        cd, "load_matrix", lambda path, **k: calls.append(Path(path)) or real(path, **k)
    )
    client.get(f"/periods/{ran.meta.run_id}")
    client.get(f"/periods/{ran.meta.run_id}/months/2024-01")
    assert calls == [], "an unchanged seat-set file is not re-read"


def test_a_fed_run_reads_its_recorded_sources_and_the_url_overrides_them(
    paused: tuple[TestClient, Ran, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ran, workspace = paused
    fed = with_meta(
        workspace,
        ran,
        "fed-sources",
        archive_digest="right",
        news_blind=False,
        news_sources=("cryptoslate",),
    )
    asked: list[tuple[str, ...]] = []

    class Feed:
        def __init__(self, digest: str) -> None:
            self.digest, self.items = digest, ()

        @staticmethod
        def covers_span(*_: object) -> bool:
            return False

    def load(_data: Path, sources: tuple[str, ...], _policy: object) -> Feed:
        asked.append(sources)
        return Feed("right" if sources == ("cryptoslate",) else "wrong")

    monkeypatch.setattr(periods_routes, "load_feed", load)

    bookmarked = client.get(f"/periods/{fed}").text
    assert asked[-1] == ("cryptoslate",) and "REPORT INCOMPLETE" not in bookmarked

    overridden = client.get(f"/periods/{fed}", params={"news_source": "bitcoincom"}).text
    assert asked[-1] == ("bitcoincom",)
    assert "REPORT INCOMPLETE" in overridden and "is not the one this run read" in overridden
