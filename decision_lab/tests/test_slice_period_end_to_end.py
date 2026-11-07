"""The period evaluation's exit criterion: one quarter run from the page's argv, read on the page.

Offline and free: the stub matrix, so the loop is exercised and nothing is measured (a plumbing
check, §7.2 of the lab spec). Nothing here is patched on the page side — the run goes through
`cli.main` with exactly the argv `routes.periods.evaluate_period_argv` builds from the form, and
the page is then opened over the workspace that run wrote, with no `configs` hint at all.

The seat set is a *copy with one seat's role reworded*, so no shipped template and no stored
seat set carries its digest: the page can only find the run's seats through `matrix_source`, which
the meta recorded at the first build (Task 9's ruling). Were it matched by a template instead,
a regression that dropped `matrix_source` would go unseen, and the page would read REPORT
INCOMPLETE only on an operator's machine.
"""

from __future__ import annotations

import re
import tomllib
import warnings
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")

from decision_lab import candidates as cd
from decision_lab import cli, gate, longrun, matrices, registry
from decision_lab import period as pd
from decision_lab.dashboard.routes.periods import evaluate_period_argv
from decision_lab.tests import factories as f
from decision_lab.tests.test_lab_dashboard_periods import client_over
from decision_lab.tests.test_slice_b_end_to_end import seeded_stub
from tradebot.app import dataset_basket, select_panel
from tradebot.core.clock import SystemClock
from tradebot.marketdata.recorder import ReplayDataset

QUARTER = "2024-Q1"


def _own_seat_set(tmp_path: Path) -> Path:
    """The stub matrix with one seat's role reworded: a digest nothing else holds.

    (Not `instruction`:
    a seat that names a library prompt has it overridden, so the digest would not move.)"""
    document = tomllib.loads(f.stub_matrix_file().read_text(encoding="utf-8"))
    document["candidates"][0]["seats"][0]["role"] += " (this copy)"
    path = tmp_path / "my-seats.toml"
    path.write_text(matrices.dumps(document), encoding="utf-8")
    return path


def test_a_quarter_run_from_the_pages_argv_opens_on_the_unpatched_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(workspace))
    for module in (registry, gate, longrun, pd):
        monkeypatch.setattr(module, "workspace_root", lambda: workspace)
    # `varied-*` seats draw from an unseeded generator: pinned, as slice B's own fixtures do.
    seeded_stub(monkeypatch)

    data = f.write_dataset(
        tmp_path / "history",
        {
            (f.instrument(), "1h"): f.shocked_walk(
                days=91, shock_up=(10, 40, 70), shock_down=(25, 55)
            )
        },
    )
    assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK

    configs = _own_seat_set(tmp_path)
    reference = dataset_basket(
        ReplayDataset.load(data, SystemClock()), select_panel("sim"), basket_id="reference"
    )
    candidate = cd.load_matrix(configs, reference=reference).candidates[0].candidate_id

    argv = evaluate_period_argv(
        {
            "data": str(data),
            "configs": str(configs),
            "candidate": candidate,
            "start_equity": "1000",
            "every": "24h",
            "period": QUARTER,
            "budget": "5",
            "skip_gate": "on",
        }
    )
    code = cli.main([*argv, "--out", str(tmp_path / "period.md")])
    assert code == cli.EXIT_OK, "a stub quarter must finish, not end on a rule or a budget"

    (run_dir,) = workspace.glob("period-*")
    run_id = run_dir.name.removeprefix("period-")
    meta = pd.PeriodMeta.model_validate_json((run_dir / pd.PERIOD_META).read_text("utf-8"))
    assert meta.matrix_source == str(configs), "the page finds its seat set through this"

    client = client_over(workspace)
    page = client.get(f"/periods/{run_id}")
    assert page.status_code == 200
    assert "REPORT INCOMPLETE" not in page.text, page.text[page.text.find("REPORT INCOMPLETE") :][
        :400
    ]
    assert "PLUMBING CHECK" in page.text

    months = re.findall(rf"/periods/{run_id}/months/(2024-\d\d)", page.text)
    assert sorted(set(months)) == ["2024-01", "2024-02", "2024-03"]
    events = re.findall(rf"/periods/{run_id}/events/(\d+)", page.text)
    assert events, "shocked_walk must leave at least one spike event to open"

    month = client.get(f"/periods/{run_id}/months/{months[0]}")
    assert month.status_code == 200
    cycles = re.findall(rf"/periods/{run_id}/cycles/([^?\"]+)", month.text)
    assert cycles, "a month with no cycle to open is a month that did not run"
    assert client.get(f"/periods/{run_id}/cycles/{cycles[0]}").status_code == 200
    assert client.get(f"/periods/{run_id}/events/{events[0]}").status_code == 200
