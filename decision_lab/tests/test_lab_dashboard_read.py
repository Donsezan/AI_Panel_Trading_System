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
from decision_lab import sweep as sw
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


def _sweep_meta(workspace: Path) -> sw.SweepResult:
    """The one sweep under this workspace, whatever corpus it belongs to."""
    found = sorted(workspace.rglob("sweep.json"))
    assert found, "no sweep meta was written"
    return sw.SweepResult.model_validate_json(found[0].read_text(encoding="utf-8"))


def test_run_detail_ranks_the_candidates_that_swept(
    lab_client: TestClient, swept_workspace: tuple[str, Path]
) -> None:
    """The fixture runs a real stub sweep, so this asserts the whole read path end to end."""
    run_id, _ = swept_workspace

    page = lab_client.get(f"/runs/{run_id}").text

    assert "NORMAL" in page
    assert "varied-three" in page, "the stub matrix's candidate is ranked"
    assert "Agreement" in page


def test_run_detail_for_an_unknown_run_is_a_404_that_says_so(lab_client: TestClient) -> None:
    response = lab_client.get("/runs/deadbeef")
    assert response.status_code == 404
    assert "no run" in response.text.lower()


def test_run_detail_says_when_the_matrix_on_disk_has_changed(
    lab_client: TestClient, swept_workspace: tuple[str, Path]
) -> None:
    """The same refusal `cli.report` makes, rendered instead of raised (finding 2).

    A page that re-derived from an edited matrix would attribute one experiment's rows to another
    experiment's candidates.
    """
    run_id, workspace = swept_workspace
    meta = _sweep_meta(workspace)
    matrix_path = Path(meta.matrix_source)
    # Still valid TOML, still parseable — exactly "one edited prompt" (§7.1's own example) and
    # the same scenario `test_report_refuses_when_the_matrix_no_longer_matches_the_sweep_that_ran`
    # exercises for `cli.report`. Overwriting with something that fails `Basket` validation would
    # hit the *other* refusal (`context_for`'s `ConfigError` branch) instead of this one.
    matrix_path.write_text(
        matrix_path.read_text(encoding="utf-8").replace('id = "varied-three"', 'id = "renamed"'),
        encoding="utf-8",
    )

    page = lab_client.get(f"/runs/{run_id}").text

    assert "no longer matches" in page
    assert "NORMAL" not in page, "nothing derived is shown when the matrix cannot be trusted"


def test_a_long_run_row_shows_its_profit_block_and_no_ranking(
    lab_client: TestClient, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    registry.record(
        a_row(
            scenario="calibrate-long",
            total_profit=Decimal("120"),
            realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal("20"),
            net_profit=Decimal("118.50"),
        ),
        workspace=workspace,
    )
    run_id = registry.read_all(workspace=workspace)[0].run_id

    page = lab_client.get(f"/runs/{run_id}").text

    assert "118.50" in page and "Realized" in page and "Unrealized" in page
    assert "Agreement" not in page, "scenario 3 has no cross-candidate tables"


def test_an_unvaluable_long_run_reports_no_figure_at_all(
    lab_client: TestClient, tmp_path: Path
) -> None:
    """§10.4: freezing is ignorance, and a partial number produced in ignorance is worse."""
    workspace = tmp_path / "workspace"
    registry.record(
        a_row(scenario="calibrate-long", realized_pnl=Decimal("100"), unvaluable=True),
        workspace=workspace,
    )
    run_id = registry.read_all(workspace=workspace)[0].run_id

    page = lab_client.get(f"/runs/{run_id}").text

    assert "UNVALUABLE" in page
    assert "100" not in page, "not even the realized half is quoted"
