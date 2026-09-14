"""Starting, watching and stopping a run (spec §12.4).

Every start builds an argv and hands it to `jobs.start`, which spawns this tool's own CLI. There
is no second implementation of a sweep here, and there is no code path from this module to a
`DecisionEngine`: what the page runs is what a terminal would run, and every refusal it reports
is an exit code the CLI already had.

Failure semantics: a missing field, an unknown command and a held workspace are all rendered
refusals — 400, 400 and 409 — carrying the reason above the page's own history and log rather
than a bare status code. The submitted values are deliberately not echoed back into the form:
every field a builder requires carries `required` on its input too, so this path is the
belt-and-braces one a hand-made POST reaches rather than the one an operator types into. Nothing
is spawned until the argv is complete, so a refused start never takes the lock and writes nothing
to the workspace.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import gate, jobs
from decision_lab.dashboard.views import LabState, render, state_of
from tradebot.core.errors import ConfigError
from tradebot.core.schema import Money

router = APIRouter()

#: The commands whose CLI takes `--budget`. `corpus build` and `calibrate long` are absent
#: because their commands have no such flag — and a form offering one would imply a ceiling that
#: does not exist (§12.4).
BUDGETED: Final = frozenset({"calibrate-normal", "calibrate-shock", "sweep"})


def _required(fields: Mapping[str, str], name: str) -> str:
    value = (fields.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required, and nothing was started")
    return value


def _budget(fields: Mapping[str, str]) -> list[str]:
    """No default. A start here spends real credit, and a ceiling nobody chose is not a ceiling."""
    return ["--budget", _required(fields, "budget")]


def _corpus_build(fields: Mapping[str, str]) -> list[str]:
    return [
        "corpus",
        "build",
        "--data",
        _required(fields, "data"),
        "--every",
        _required(fields, "every"),
        "--reference-panel",
        _required(fields, "reference_panel"),
    ]


def _calibrate(scenario: str) -> Callable[[Mapping[str, str]], list[str]]:
    def build(fields: Mapping[str, str]) -> list[str]:
        return [
            "calibrate",
            scenario,
            "--corpus",
            _required(fields, "corpus"),
            "--configs",
            _required(fields, "configs"),
            *_budget(fields),
        ]

    return build


def _sweep(fields: Mapping[str, str]) -> list[str]:
    argv = [
        "sweep",
        "--corpus",
        _required(fields, "corpus"),
        "--configs",
        _required(fields, "configs"),
        *_budget(fields),
    ]
    if fields.get("seed", "").strip():
        argv += ["--seed", fields["seed"].strip()]
    if fields.get("full"):
        argv.append("--full")
    if fields.get("skip_gate"):
        argv.append("--skip-gate")
    return argv


def _calibrate_long(fields: Mapping[str, str]) -> list[str]:
    argv = [
        "calibrate",
        "long",
        "--data",
        _required(fields, "data"),
        "--configs",
        _required(fields, "configs"),
        "--candidate",
        _required(fields, "candidate"),
        "--start-equity",
        _required(fields, "start_equity"),
        "--every",
        _required(fields, "every"),
        "--window",
        _required(fields, "window"),
    ]
    if fields.get("skip_gate"):
        argv.append("--skip-gate")
    return argv


#: One builder per launchable command. Dispatch rather than branching, and the page renders one
#: form per key — a command on the page with no builder here would 500 on submit, which
#: `test_a_builder_exists_for_every_launchable_command` is there to prevent.
BUILDERS: Final[dict[str, Callable[[Mapping[str, str]], list[str]]]] = {
    "corpus-build": _corpus_build,
    "calibrate-normal": _calibrate("normal"),
    "calibrate-shock": _calibrate("shock"),
    "sweep": _sweep,
    "calibrate-long": _calibrate_long,
}


@dataclass(frozen=True, slots=True)
class ProjectionRow:
    """What one candidate cost per cycle when it was calibrated, and what this run would cost."""

    candidate_id: str
    cost_per_cycle: Money
    entries: int
    projected: Money


async def projection_for(
    request: Request, *, corpus_id: str, configs: str
) -> tuple[ProjectionRow, ...] | str:
    """§10.2's measured cost per cycle, over the entries this run would buy.

    A string is returned when there is nothing to project from — which is itself the answer worth
    showing, because an uncalibrated seat set is one the gate will refuse anyway.
    """
    state = state_of(request)
    if not corpus_id or not configs:
        return "Name a corpus and a seat set to see what a run would cost."
    try:
        corpus = cp.load(corpus_id, workspace=state.workspace)
        matrix = cd.load_matrix(Path(configs), reference=corpus.meta.reference_basket)
        pinned = cday.require_pinned(Path(corpus.meta.dataset_directory))
    except ConfigError as error:
        return str(error)
    record = gate.read(
        gate.gate_key(
            dataset_digest=corpus.meta.dataset_digest,
            matrix_digest=matrix.matrix_digest,
            dayset_digest=pinned.dayset_digest,
        ),
        workspace=state.workspace,
    )
    if record is None or record.normal is None:
        return (
            "No calibration on record for this seat set and dataset, so there is nothing to "
            "project from — and a sweep will refuse (exit 6) until `calibrate normal` and "
            "`calibrate shock` have run over the nine pinned days."
        )
    entries = len(corpus.entries)
    return tuple(
        ProjectionRow(
            candidate_id=found.candidate_id,
            cost_per_cycle=found.cost_per_cycle,
            entries=entries,
            projected=found.cost_per_cycle * entries,
        )
        for found in record.normal.candidates
    )


def _history_context(state: LabState) -> dict[str, Any]:
    """The history table, refreshed, and which row (if any) this process can stop.

    Shared by `index` and `_refusal` so a refusal never tells the operator something different
    about a job's status than the normal page would — before this was pulled out, `_refusal` built
    the same two values without the `jobs.refresh` call `index` makes first, so a job that finished
    in the window between a refused start and the read rendered as "ended without being recorded"
    on the refusal page while the index page, reading the same record, would have reported its
    real exit code.
    """
    history = tuple(
        jobs.refresh(one, clock=state.clock, workspace=state.workspace)
        for one in jobs.history(workspace=state.workspace)
    )
    statuses = {one.job_id: jobs.status_of(one, workspace=state.workspace) for one in history}
    return {
        "history": history,
        "statuses": statuses,
        # The one record (there can be at most one, the lock enforces it) whose status is
        # "running" *is* the current holder when the holder is a job this page recorded — the
        # dashboard restart case `status_of` documents reports every stale record as "ended
        # without being recorded" instead, so a Stop button here always names a job `jobs.stop`
        # can actually act on rather than one merely read off the sidecar.
        "stoppable": next((one.job_id for one in history if statuses[one.job_id] == "running"), ""),
    }


@router.get("/jobs", response_class=HTMLResponse)
async def index(request: Request, job: str = "", corpus: str = "", configs: str = "") -> Response:
    state = state_of(request)
    return render(
        request,
        "jobs.html",
        **_history_context(state),
        selected=job,
        log=jobs.log_tail(job, workspace=state.workspace) if job else "",
        commands=tuple(BUILDERS),
        budgeted=BUDGETED,
        projection=await projection_for(request, corpus_id=corpus, configs=configs),
        corpus=corpus,
        configs=configs,
        error="",
    )


@router.post("/jobs/start")
async def start(request: Request) -> Response:
    """Build the argv, then spawn. Nothing is spawned until the argv is complete."""
    state = state_of(request)
    fields = {key: str(value) for key, value in (await request.form()).items()}
    command = fields.get("command", "")
    builder = BUILDERS.get(command)
    if builder is None:
        return await _refusal(request, f"{command!r} is not a command this page can start", 400)
    try:
        argv = builder(fields)
    except ConfigError as error:
        return await _refusal(request, str(error), 400)
    try:
        record = jobs.start(argv, label=command, clock=state.clock, workspace=state.workspace)
    except jobs.Busy as error:
        # 409, and the message names the holder: "something else is running" without saying what
        # is a refusal an operator cannot act on.
        return await _refusal(request, str(error), 409)
    return RedirectResponse(f"/jobs?job={record.job_id}", status_code=303)


@router.post("/jobs/stop")
async def stop(request: Request, job_id: str = Form(default="")) -> Response:
    state = state_of(request)
    jobs.stop(job_id, workspace=state.workspace)
    return RedirectResponse(f"/jobs?job={job_id}", status_code=303)


async def _refusal(request: Request, reason: str, status: int) -> Response:
    state = state_of(request)
    page = render(
        request,
        "jobs.html",
        **_history_context(state),
        selected="",
        log="",
        commands=tuple(BUILDERS),
        budgeted=BUDGETED,
        projection="",
        corpus="",
        configs="",
        error=reason,
    )
    page.status_code = status
    return page
