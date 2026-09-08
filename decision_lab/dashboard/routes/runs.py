"""The §11 registry as a page, and the run a reader picked out of it (§12.2).

Selection is in the URL — the sort, the direction and the two runs being compared — so a reload,
a bookmark and the meta refresh that fires while a job runs all land on the same view.

Failure semantics: an absent registry is no runs, never an error. An unknown sort key is the
default sort rather than a 500: a mistyped query parameter must not take the page away from
someone reading it mid-run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from decision_lab import registry
from decision_lab.dashboard.views import render, state_of

router = APIRouter()

DEFAULT_SORT: Final = "recorded_at"

#: What a column header sorts by. Dispatch rather than `getattr`, so a query parameter can never
#: name a field that is not meant to be a sort — and the set is what the template renders.
SORTS: Final[dict[str, Callable[[registry.RunRow], Any]]] = {
    "recorded_at": lambda row: row.recorded_at,
    "accuracy": lambda row: row.accuracy,
    "net_profit": lambda row: row.net_profit,
    "cost_usd": lambda row: row.cost_usd,
    "scored": lambda row: row.scored,
    "scenario": lambda row: (row.scenario, row.candidate_id),
}

#: Never diffed: identity differs by construction between any two rows, and recording time is
#: not a property of the experiment.
NOT_COMPARED: Final = frozenset({"run_id", "recorded_at"})


def sorted_rows(
    rows: Sequence[registry.RunRow], *, sort: str, direction: str
) -> tuple[registry.RunRow, ...]:
    key = SORTS.get(sort, SORTS[DEFAULT_SORT])
    return tuple(sorted(rows, key=key, reverse=direction != "asc"))


def diff(left: registry.RunRow, right: registry.RunRow) -> tuple[tuple[str, str, str], ...]:
    """Every field the two runs disagree about. An identical field is noise on a comparison."""
    a = left.model_dump(mode="json")
    b = right.model_dump(mode="json")
    return tuple(
        (name, str(a[name]), str(b[name]))
        for name in a
        if name not in NOT_COMPARED and a[name] != b[name]
    )


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    sort: str = DEFAULT_SORT,
    dir: str = "desc",  # noqa: A002 — the query parameter's name is what appears in the URL
    compare: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI never mutates this default
) -> HTMLResponse:
    """Every run, and — when two are named — what differs between them."""
    state = state_of(request)
    rows = registry.read_all(workspace=state.workspace)
    chosen = [row for row in rows if row.run_id in set(compare)]
    return render(
        request,
        "runs.html",
        rows=sorted_rows(rows, sort=sort, direction=dir),
        sorts=tuple(SORTS),
        sort=sort if sort in SORTS else DEFAULT_SORT,
        direction=dir,
        selected=tuple(compare),
        comparison=diff(chosen[0], chosen[1]) if len(chosen) == 2 else (),
        chosen=tuple(chosen),
    )
