"""The §11 registry as a page, and the run a reader picked out of it (§12.2).

Selection is in the URL — the sort, the direction and the two runs being compared — so a reload,
a bookmark and the meta refresh that fires while a job runs all land on the same view.

Failure semantics: an absent registry is no runs, never an error. An unknown sort key is the
default sort rather than a 500: a mistyped query parameter must not take the page away from
someone reading it mid-run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from decision_lab import analysis as an
from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import registry
from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab import sweep as sw
from decision_lab.dashboard.views import render, state_of
from tradebot.core.errors import ConfigError

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


@dataclass(frozen=True, slots=True)
class RunContext:
    """One registry row, and everything derivable from it — or why nothing is.

    `problem` is a rendered refusal rather than an exception on purpose: the row itself is worth
    showing even when the sweep it names can no longer be re-derived, and that is exactly the
    case an operator needs explained.
    """

    row: registry.RunRow
    corpus: cp.Corpus | None = None
    matrix: cd.Matrix | None = None
    analysis: an.MatrixAnalysis | None = None
    problem: str = ""


async def context_for(request: Request, run_id: str) -> RunContext | None:
    """Re-derive a run from what it recorded. `None` when no row has that id."""
    state = state_of(request)
    row = next(
        (one for one in registry.read_all(workspace=state.workspace) if one.run_id == run_id),
        None,
    )
    if row is None:
        return None
    # Scenario 3 has no corpus of frozen snapshots to re-score: its numbers are the profit block
    # on the row itself (§10.4), and there is nothing to reload.
    if row.scenario == "calibrate-long" or not row.corpus_id:
        return RunContext(row=row)

    result = sw.read_meta(row.corpus_id, row.matrix_digest, workspace=state.workspace)
    if result is None:
        return RunContext(row=row, problem="no sweep files remain under this corpus for it")
    corpus = cp.load(row.corpus_id, workspace=state.workspace)
    try:
        matrix = cd.load_matrix(Path(result.matrix_source), reference=corpus.meta.reference_basket)
    except ConfigError as error:
        return RunContext(
            row=row,
            corpus=corpus,
            problem=f"the matrix this sweep ran could not be reloaded: {error}",
        )
    if matrix.matrix_digest != result.matrix_digest:
        # finding 2, one level over: re-deriving from an edited matrix would attribute one
        # experiment's rows to another experiment's candidates.
        return RunContext(
            row=row,
            corpus=corpus,
            problem=(
                f"the matrix at {result.matrix_source} no longer matches the one this sweep ran "
                f"({result.matrix_digest} recorded, {matrix.matrix_digest} on disk), so nothing "
                "derived from it would describe this run"
            ),
        )
    _, analysed = await state.cache.matrix_analysis(
        corpus, matrix, result.matrix_digest, workspace=state.workspace
    )
    return RunContext(row=row, corpus=corpus, matrix=matrix, analysis=analysed)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def detail(request: Request, run_id: str) -> HTMLResponse:
    """§12.2's drill-down: one run, its per-regime ranking, and what was not measured."""
    found = await context_for(request, run_id)
    if found is None:
        page = render(request, "run_detail.html", missing=run_id, context=None, regimes=())
        page.status_code = 404
        return page
    regimes = (
        sc.by_regime(tuple(row for rows in found.analysis.by_candidate.values() for row in rows))
        if found.analysis
        else ()
    )
    return render(request, "run_detail.html", missing="", context=found, regimes=regimes)


@router.get("/runs/{run_id}/seats/{candidate_id}", response_class=HTMLResponse)
async def seat_detail(request: Request, run_id: str, candidate_id: str) -> HTMLResponse:
    """§9.7 for one candidate. Round 0 is reported beside the final vote, never instead of it."""
    found = await context_for(request, run_id)
    if found is None or found.analysis is None:
        page = render(request, "seats.html", missing=run_id, found=None, candidate=None)
        page.status_code = 404
        return page
    candidate = found.analysis.find(candidate_id)
    if candidate is None or not candidate.measured:
        page = render(
            request,
            "seats.html",
            missing="",
            found=found,
            candidate=candidate,
            reason=(
                candidate.not_measured_reason
                if candidate is not None
                else f"no candidate {candidate_id!r} ran in this sweep"
            ),
        )
        page.status_code = 404
        return page
    return render(
        request,
        "seats.html",
        missing="",
        found=found,
        candidate=candidate,
        # §9.7: `single_round` reports the two rounds as identical rather than duplicating the
        # table, and the page must say which it is rather than showing one column twice.
        identical_rounds=st.rounds_are_identical(candidate.seats),
    )


@router.get("/runs/{run_id}/decision", response_class=HTMLResponse)
async def decision_detail(
    request: Request, run_id: str, cycle: str, instrument: str, candidate: str
) -> HTMLResponse:
    """One decision, with the evidence the panel saw and why the verdict landed as it did.

    `instrument` is a query parameter because an instrument key carries a `/`
    (`binance:BTC/USDT`) and a path segment would either split it or need escaping on both sides.
    """
    found = await context_for(request, run_id)
    block = found.analysis.find(candidate) if found and found.analysis else None
    if block is None:
        page = render(
            request, "decision.html", missing=run_id, run_id=run_id, scored=None, record=None
        )
        page.status_code = 404
        return page
    scored = next(
        (one for one in block.scored if one.cycle_id == cycle and one.instrument_key == instrument),
        None,
    )
    record = next((one for one in block.records if one.cycle_id == cycle), None)
    page = render(
        request,
        "decision.html",
        missing="" if scored and record else run_id,
        run_id=run_id,
        cycle=cycle,
        scored=scored,
        record=record,
        instrument=instrument,
        candidate=candidate,
        round_zero=record.round_zero_for(instrument) if record else (),
        final_round=record.final_round_for(instrument) if record else (),
    )
    if scored is None or record is None:
        page.status_code = 404
    return page
