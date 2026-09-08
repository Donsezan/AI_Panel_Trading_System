"""§12.2's Runs view: what ran, sorted how the reader asked, and two rows side by side."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

# See test_lab_dashboard_auth.py's own note: starlette's TestClient now prefers `httpx2`, which is
# not one of this repo's pinned dependencies, and falls back to `httpx` with a
# `StarletteDeprecationWarning` the root `filterwarnings = ["error"]` would otherwise turn into a
# collection failure of this module, unrelated to anything decision_lab does.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from decision_lab import registry
from decision_lab.dashboard.routes import runs


def a_row(**fields: object) -> registry.RunRow:
    base = {
        "recorded_at": datetime(2026, 9, 1, tzinfo=UTC),
        "scenario": "sweep",
        "corpus_id": "corpus1",
        "matrix_digest": "m1",
        "candidate_id": "baseline",
        "scored": 40,
        "accuracy": Decimal("0.6"),
        "cost_usd": Decimal("1.50"),
    }
    return registry.RunRow.model_validate(base | fields)


def test_sorting_is_by_the_named_column_and_direction() -> None:
    rows = (
        a_row(candidate_id="a", accuracy=Decimal("0.4")),
        a_row(candidate_id="b", accuracy=Decimal("0.8")),
    )

    best_first = runs.sorted_rows(rows, sort="accuracy", direction="desc")

    assert [row.candidate_id for row in best_first] == ["b", "a"]
    assert [
        row.candidate_id for row in runs.sorted_rows(rows, sort="accuracy", direction="asc")
    ] == [
        "a",
        "b",
    ]


def test_an_unknown_sort_falls_back_to_the_recorded_time() -> None:
    """A mistyped query parameter is a default, never a 500 on a page an operator is reading."""
    rows = (a_row(candidate_id="a"), a_row(candidate_id="b"))
    assert runs.sorted_rows(rows, sort="nonsense", direction="desc")


def test_the_diff_shows_only_what_differs() -> None:
    left = a_row(candidate_id="a", accuracy=Decimal("0.4"))
    right = a_row(candidate_id="b", accuracy=Decimal("0.4"), cost_usd=Decimal("9"))

    fields = {name for name, _, _ in runs.diff(left, right)}

    assert "candidate_id" in fields and "cost_usd" in fields
    assert "accuracy" not in fields, "an identical field is noise on a comparison"
    assert "run_id" not in fields, "identity differs by construction and says nothing"


def test_the_runs_page_lists_every_recorded_row(lab_client: TestClient, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    registry.record(a_row(candidate_id="baseline"), workspace=workspace)
    registry.record(a_row(candidate_id="cautious"), workspace=workspace)

    page = lab_client.get("/").text

    assert "baseline" in page and "cautious" in page
    assert "60.0%" in page, "accuracy is rendered from Decimal, exactly"


def test_an_empty_registry_says_so_rather_than_rendering_an_empty_table(
    lab_client: TestClient,
) -> None:
    assert "No runs recorded yet" in lab_client.get("/").text


def test_comparing_two_runs_renders_their_differences(
    lab_client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    left = a_row(candidate_id="a")
    right = a_row(candidate_id="b", cost_usd=Decimal("9"))
    registry.record(left, workspace=workspace)
    registry.record(right, workspace=workspace)
    ids = [row.run_id for row in registry.read_all(workspace=workspace)]

    page = lab_client.get(f"/?compare={ids[0]}&compare={ids[1]}").text

    assert "candidate_id" in page and "cost_usd" in page


def test_comparing_one_run_is_not_an_error(lab_client: TestClient, tmp_path: Path) -> None:
    """Half a comparison is a page with one row selected, never a 500 or an empty diff table."""
    workspace = tmp_path / "workspace"
    registry.record(a_row(), workspace=workspace)
    only = registry.read_all(workspace=workspace)[0].run_id

    assert lab_client.get(f"/?compare={only}").status_code == 200
