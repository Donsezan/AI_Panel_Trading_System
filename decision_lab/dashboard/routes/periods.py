"""Evaluate one seat set over one calendar period, and read the result (period spec §4, §5, §7).

`GET /periods` is the form and the list of runs; Run posts the form, which builds the argv of
`evaluate period` and hands it to `jobs.start` — the page *is* the CLI, so every refusal is an exit
code the command already has. `GET /periods/{run_id}` is the result, profit first; the month, cycle
and event drill-downs are their own routes, fetched by a `<details>` when it opens (htmx), and each
renders as a whole page when reached without htmx, so the drill-down works with scripting off and
a year never renders two thousand cycles at once. Selection is the URL.

Every figure comes from `analysis.analyse_period`, the one read assembly (through the cache).
The panel it needs is the run's own: the seat set named in the URL, or a stored or shipped seat
set whose candidate carries the run's `panel_digest`. The news archive is the run's own too, named
by `news_source` and checked against the run's `archive_digest`.

Failure semantics: nothing here writes, except `jobs.start` spawning the CLI. A run whose meta is
missing, unreadable or half-written renders as such (never a 500). A run whose log cannot be read,
whose seat set is no longer on file or whose archive is not named renders a meta-only page under a
`REPORT INCOMPLETE` banner, as the CLI's page does. A dataset that cannot be read leaves the form
up with the reason in place of the period list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from decision_lab import analysis as an
from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import dataset as ds
from decision_lab import gate, jobs, matrices
from decision_lab import period as pd
from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab.archive.config import DEFAULT_POLICY_FILE, load_policy
from decision_lab.corpus import config_digest
from decision_lab.dashboard import charts
from decision_lab.dashboard.views import render, state_of
from decision_lab.newsfeed import ArchiveNewsFeed, load_feed
from decision_lab.params import CADENCE_SECONDS, workspace_root
from decision_lab.records import CycleRecord
from decision_lab.spikes import AFTER, LEAD_IN, stretches
from tradebot.app import dataset_basket, select_panel
from tradebot.core.decision import SeatResponse
from tradebot.core.enums import Action
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

router = APIRouter()

KINDS: Final = ("month", "quarter", "year")
#: The page's cost note, verbatim from the plan (§5.5).
COST_NOTE: Final = (
    "a run can overshoot by up to a month's spend, and a one-month run has no ceiling while it runs"
)
#: §7.2's question per pool, in the operator's words from the mockup. Keyed by `Pool` value.
REGIME_ROWS: Final = (
    ("NORMAL", "Normal", "Did it stay sensible?"),
    ("SHOCK_UP", "Spike up", "Did it catch the move?"),
    ("SHOCK_DOWN", "Spike down", "Did it protect the money?"),
)
LETTER: Final[dict[str, str]] = {"BUY": "B", "SELL": "S", "HOLD": "H", "WAIT": "W"}
_CADENCE_LABEL: Final = {seconds: label for label, seconds in CADENCE_SECONDS.items()}
_MONTH_NAMES: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


# ------------------------------------------------------------------------------- the command


def _required(fields: Mapping[str, str], name: str) -> str:
    value = (fields.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required, and nothing was started")
    return value


def sources_of(text: str) -> tuple[str, ...]:
    """`news_source` as typed: comma- or space-separated, each named once."""
    return tuple(dict.fromkeys(one for one in text.replace(",", " ").split() if one))


def evaluate_period_argv(fields: Mapping[str, str]) -> list[str]:
    """`evaluate period`'s argv. The budget has no default: a defaulted ceiling is none (§5.5)."""
    argv = [
        "evaluate",
        "period",
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
        "--period",
        _required(fields, "period"),
        "--budget",
        _required(fields, "budget"),
    ]
    for source in sources_of(fields.get("news_source", "")):
        argv += ["--news-source", source]
    if fields.get("skip_gate"):
        argv.append("--skip-gate")
    if fields.get("continue"):
        argv.append("--continue")
    return argv


# ----------------------------------------------------------------------------- the runs list


@dataclass(frozen=True, slots=True)
class RunEntry:
    """One `period-*` directory: its meta, or why it has none a reader can use."""

    run_id: str
    meta: pd.PeriodMeta | None
    problem: str = ""


def _read_meta(run_id: str, workspace: Path | None) -> RunEntry:
    path = pd.period_dir(run_id, workspace=workspace) / pd.PERIOD_META
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RunEntry(
            run_id, None, "no record yet: a run is starting, or it died before its first write"
        )
    except OSError as error:
        return RunEntry(run_id, None, f"its record cannot be read: {error}")
    try:
        return RunEntry(run_id, pd.PeriodMeta.model_validate_json(text))
    except ValueError as error:
        first = str(error).splitlines()[0]
        return RunEntry(
            run_id, None, f"its record cannot be read (being written, or another version): {first}"
        )


def period_runs(workspace: Path | None) -> tuple[RunEntry, ...]:
    """Every period run in the workspace, newest first; unreadable ones last, never dropped."""
    root = workspace or workspace_root()
    if not root.is_dir():
        return ()
    found = [
        _read_meta(entry.name.removeprefix("period-"), workspace)
        for entry in sorted(root.iterdir())
        if entry.is_dir() and entry.name.startswith("period-")
    ]
    readable = sorted((e for e in found if e.meta), key=lambda e: e.meta.built_at, reverse=True)  # type: ignore[union-attr]
    return (*readable, *(e for e in found if e.meta is None))


# ------------------------------------------------------------------------------- the form


@dataclass(frozen=True, slots=True)
class Selection:
    """The form's state, read off the URL so a reload lands on the same view."""

    data: str = ""
    configs: str = ""
    candidate: str = ""
    kind: str = "month"
    period: str = ""
    start_equity: str = "1000"
    every: str = "4h"
    news_source: str = ""
    skip_gate: bool = False

    @classmethod
    def of(cls, values: Mapping[str, str]) -> Selection:
        kind = values.get("kind", "month")
        return cls(
            data=values.get("data", "").strip(),
            configs=values.get("configs", "").strip(),
            candidate=values.get("candidate", "").strip(),
            kind=kind if kind in KINDS else "month",
            period=values.get("period", "").strip(),
            start_equity=values.get("start_equity", "1000").strip() or "1000",
            every=values.get("every", "4h") if values.get("every") in CADENCE_SECONDS else "4h",
            news_source=values.get("news_source", "").strip(),
            skip_gate=bool(values.get("skip_gate")),
        )

    def query(self, **more: str) -> str:
        """The read-side parameters a result link carries, so it reads the same panel and news."""
        carried = {"configs": self.configs, "news_source": self.news_source, **more}
        return urlencode({k: v for k, v in carried.items() if v})


@dataclass(frozen=True, slots=True)
class FormFacts:
    """What the chosen dataset and seat set allow, or why they allow nothing yet."""

    periods: tuple[str, ...] = ()
    data_problem: str = ""
    candidates: tuple[str, ...] = ()
    configs_problem: str = ""
    projection: Decimal | None = None
    projection_note: str = ""


def _reference(dataset: ReplayDataset, cadence_seconds: int) -> Any:
    """The reference basket `evaluate period` expands a matrix against (`cli._open_candidate`)."""
    return dataset_basket(
        dataset, select_panel("sim"), basket_id="reference", every_seconds=cadence_seconds
    )


def _form_facts(selection: Selection, request: Request) -> FormFacts:
    state = state_of(request)
    if not selection.data:
        return FormFacts(data_problem="Name a dataset directory to see which periods it covers.")
    data = Path(selection.data)
    try:
        audit = ds.require_verified(data)
        dataset = ReplayDataset.load(data, state.clock)
    except (ConfigError, OSError, ValueError) as error:
        return FormFacts(data_problem=f"This dataset cannot be read: {error}")
    first, last = dataset.coverage
    periods = pd.covered_periods(first, last - timedelta(hours=1))[selection.kind]
    if not selection.configs:
        return FormFacts(periods=periods, configs_problem="Name a seat set to choose a candidate.")
    try:
        matrix = cd.load_matrix(
            Path(selection.configs),
            reference=_reference(dataset, CADENCE_SECONDS[selection.every]),
        )
    except (ConfigError, OSError) as error:
        return FormFacts(periods=periods, configs_problem=f"This seat set will not load: {error}")
    candidates = tuple(one.candidate_id for one in matrix.candidates)
    projection, note = None, ""
    if selection.candidate and selection.period in periods:
        try:
            pinned = cday.require_pinned(data)
        except ConfigError as error:
            note = f"No projection: {error}. The gate will refuse (exit 3) until `dataset days`."
        else:
            record = gate.read(
                gate.gate_key(
                    dataset_digest=audit.dataset_digest,
                    matrix_digest=matrix.matrix_digest,
                    dayset_digest=pinned.dayset_digest,
                ),
                workspace=state.workspace,
            )
            projection = pd.projected_cost(
                record,
                selection.candidate,
                pd.parse_period(selection.period),
                CADENCE_SECONDS[selection.every],
            )
            if projection is None:
                note = (
                    "No projection: this seat set has no calibration on record for this dataset, "
                    "so the gate will refuse (exit 6) until `calibrate normal` and `calibrate "
                    "shock` have run — or the run goes ahead under skip-gate with no projection."
                )
    return FormFacts(
        periods=periods,
        candidates=candidates,
        projection=projection,
        projection_note=note,
    )


def _form_page(
    request: Request, selection: Selection, *, error: str = "", job: str = "", status: int = 200
) -> HTMLResponse:
    state = state_of(request)
    page = render(
        request,
        "periods.html",
        selection=selection,
        facts=_form_facts(selection, request),
        runs=period_runs(state.workspace),
        kinds=KINDS,
        cadences=tuple(CADENCE_SECONDS),
        cost_note=COST_NOTE,
        error=error,
        job=job,
    )
    page.status_code = status
    return page


@router.get("/periods", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    values = dict(request.query_params)
    return _form_page(request, Selection.of(values), job=values.get("job", ""))


@router.post("/periods/run")
async def start(request: Request) -> Response:
    """Build the argv, then spawn. A refusal re-renders the form with the reason and the choices."""
    state = state_of(request)
    fields = {key: str(value) for key, value in (await request.form()).items()}
    selection = Selection.of(fields)
    try:
        argv = evaluate_period_argv(fields)
    except ConfigError as error:
        return _form_page(request, selection, error=str(error), status=400)
    try:
        record = jobs.start(
            argv, label="evaluate-period", clock=state.clock, workspace=state.workspace
        )
    except jobs.Busy as error:
        return _form_page(request, selection, error=str(error), status=409)
    query = {
        "data": selection.data,
        "configs": selection.configs,
        "candidate": selection.candidate,
        "kind": selection.kind,
        "period": selection.period,
        "start_equity": selection.start_equity,
        "every": selection.every,
        "news_source": selection.news_source,
        "job": record.job_id,
    }
    return RedirectResponse(
        f"/periods?{urlencode({k: v for k, v in query.items() if v})}", status_code=303
    )


# ------------------------------------------------------------------------------- one run


@dataclass(frozen=True, slots=True)
class RunView:
    """One run as the page shows it: the analysis, or the meta alone and the reason why."""

    run_id: str
    meta: pd.PeriodMeta | None
    analysis: an.PeriodAnalysis | None = None
    #: Set when only the meta can be shown. Rendered under the `REPORT INCOMPLETE` banner.
    incomplete: str = ""
    #: Set when there is no meta at all.
    problem: str = ""
    configs: str = ""
    sources: tuple[str, ...] = ()
    panel_seats: tuple[str, ...] = ()
    #: Kept for the drill-downs, which read prices and scoring parameters off it.
    scoring: an.Scoring | None = None


def _candidate_files(meta: pd.PeriodMeta, configs: str, workspace: Path | None) -> Iterable[Path]:
    """Where a run's seat set may be: the file it recorded, the URL's, every stored version, then
    the shipped templates (fix round 1, item 3)."""
    if meta.matrix_source:
        yield Path(meta.matrix_source)
    if configs:
        yield Path(configs)
    for name in matrices.names(workspace=workspace):
        for version in reversed(matrices.versions(name, workspace=workspace)):
            yield matrices.path_for(name, version, workspace=workspace)
    yield from (matrices.CONFIG_DIR / f"{name}.toml" for name in matrices.templates())


def _is_this_run(
    meta: pd.PeriodMeta, matrix: cd.Matrix, candidate: cd.Candidate, dataset: ReplayDataset
) -> bool:
    """The candidate this run ran: its id, its whole basket and, when recorded, its matrix.

    Never the panel alone. A seat set edited since the run, or one whose candidate carries the same
    seats under another risk limit, would read this run's votes against a basket that did not run.
    """
    if candidate.candidate_id != meta.candidate_id:
        return False
    if meta.matrix_digest and matrix.matrix_digest != meta.matrix_digest:
        return False
    basket = pd.period_basket(candidate, dataset, meta.cadence_seconds)
    return config_digest(basket) == meta.basket_digest


def _find_panel(
    meta: pd.PeriodMeta, dataset: ReplayDataset, configs: str, request: Request
) -> tuple[cd.Candidate | None, str]:
    """The run's own candidate, and the file it was found in, or `None` and no file."""
    state = state_of(request)
    reference = _reference(dataset, meta.cadence_seconds)
    reference_key = f"{meta.dataset_digest}|{meta.cadence_seconds}"
    seen: set[Path] = set()
    for path in _candidate_files(meta, configs, state.workspace):
        if path in seen:
            continue
        seen.add(path)
        matrix = state.cache.matrix_for(path, reference=reference, reference_key=reference_key)
        if matrix is None:
            continue
        for one in matrix.candidates:
            if _is_this_run(meta, matrix, one, dataset):
                return one, str(path)
    return None, ""


def _feed_for(meta: pd.PeriodMeta, sources: tuple[str, ...]) -> tuple[ArchiveNewsFeed | None, str]:
    """The run's own archive, or `None` for a news-blind run, or the reason it cannot be had."""
    if not meta.archive_digest:
        return None, ""
    if not sources:
        return None, (
            "this run read a news archive, so its events need the same archive: name its news "
            "sources in the form above (news_source) to read them"
        )
    try:
        feed = load_feed(Path(meta.dataset_directory), sources, load_policy(DEFAULT_POLICY_FILE))
    except (ConfigError, OSError, ValueError) as error:
        return None, f"the news archive could not be read: {error}"
    if feed.digest != meta.archive_digest:
        return None, (
            f"the archive named ({', '.join(sources)}) is not the one this run read "
            f"({meta.archive_digest})"
        )
    return feed, ""


async def run_view(
    request: Request, run_id: str, configs: str = "", news_source: str = ""
) -> RunView:
    state = state_of(request)
    entry = _read_meta(run_id, state.workspace)
    if entry.meta is None:
        return RunView(run_id=run_id, meta=None, problem=entry.problem, configs=configs)
    meta = entry.meta
    # The URL may override the recorded sources; the digest check below holds either way.
    sources = sources_of(news_source) or meta.news_sources
    # §8's ruling: a page read is one boundary where anything may fail (a moved dataset, a
    # corrupt row, a log mid-write); the run's own record is still worth showing, so this catches
    # broadly and says why, exactly as the CLI's page does.
    try:
        scoring = await state.cache.scoring_for(Path(meta.dataset_directory))
        candidate, found_in = _find_panel(meta, scoring.dataset, configs, request)
        if candidate is None:
            return RunView(
                run_id=run_id,
                meta=meta,
                incomplete=(
                    f"the seat set this run ran (candidate {meta.candidate_id}, basket "
                    f"{meta.basket_digest}) is not on file: name its TOML as configs"
                ),
                configs=configs,
                sources=sources,
            )
        feed, why = _feed_for(meta, sources)
        if why:
            return RunView(
                run_id=run_id, meta=meta, incomplete=why, configs=found_in, sources=sources
            )
        analysed = await state.cache.period_analysis(
            run_id, scoring, panel=candidate.panel, archive=feed, workspace=state.workspace
        )
    except Exception as error:
        return RunView(
            run_id=run_id,
            meta=meta,
            incomplete=f"{type(error).__name__}: {error}",
            configs=configs,
            sources=sources,
        )
    return RunView(
        run_id=run_id,
        meta=meta,
        analysis=analysed,
        configs=found_in,
        sources=sources,
        panel_seats=tuple(seat.seat_id for seat in candidate.panel.seats),
        scoring=scoring,
    )


def _query(view: RunView) -> str:
    return urlencode(
        {
            k: v
            for k, v in {"configs": view.configs, "news_source": " ".join(view.sources)}.items()
            if v
        }
    )


# ------------------------------------------------------------------------- page assembly
# Pure arrangement of `PeriodAnalysis` into rows. Nothing below reads anything.


@dataclass(frozen=True, slots=True)
class Summary:
    graded: int
    correct: int
    decisions: int
    accuracy: Decimal | None
    ran: int
    planned: int
    paused_months: int
    late_start: bool


def summary_of(view: RunView) -> Summary:
    meta = view.meta
    assert meta is not None
    analysed = view.analysis
    graded = sum(row.scored for row in analysed.regimes) if analysed else 0
    correct = sum(row.correct for row in analysed.regimes) if analysed else 0
    return Summary(
        graded=graded,
        correct=correct,
        decisions=sum(row.decisions for row in analysed.regimes) if analysed else 0,
        accuracy=sc.ratio(correct, graded) if graded else None,
        ran=sum(m.ran_cycles for m in meta.months),
        planned=sum(m.planned_cycles for m in meta.months),
        paused_months=sum(1 for m in meta.months if m.paused_at),
        late_start=meta.first_cycle_at > meta.period.start,
    )


def _month_name(label: str, *, with_year: bool) -> str:
    year, month = label.split("-")
    name = _MONTH_NAMES[int(month) - 1]
    return f"{name} {year}" if with_year else name


def equity_chart(meta: pd.PeriodMeta) -> str:
    """One point per month end (§4 item 2); an unvaluable or unrun month is a gap, never zero."""
    rows = {m.label: m for m in meta.months}
    points = []
    for month in meta.period.months:
        row = rows.get(month.label)
        name = _month_name(month.label, with_year=False)
        if row is None:
            points.append(charts.Point(label=name, value=None, title=f"{month.label}: not run"))
        elif row.unvaluable:
            points.append(charts.Point(label=name, value=None, title=f"{month.label}: UNVALUABLE"))
        else:
            paused = ", paused on losses" if row.paused_at else ""
            points.append(
                charts.Point(
                    label=name,
                    value=row.end_equity,
                    title=f"{month.label}: {row.end_equity}{paused}",
                    flag="⚠" if row.paused_at else "",
                )
            )
    return charts.line_svg(points, width=960, height=210)


@dataclass(frozen=True, slots=True)
class RegimeRow:
    key: str
    name: str
    question: str
    events: int
    graded: int
    accuracy: Decimal | None
    seats: tuple[Decimal | None, ...]
    unscored: int


def regime_rows(view: RunView) -> tuple[RegimeRow, ...]:
    """All three rows always, never pooled (§7.2). A seat column is its final vote's accuracy."""
    analysed = view.analysis
    assert analysed is not None
    by_key = {row.regime: row for row in analysed.regimes}
    rows = []
    for key, name, question in REGIME_ROWS:
        metrics = by_key.get(key, sc.RegimeMetrics(regime=key))
        seat_cells = []
        for seat_id in view.panel_seats:
            found = next(
                (
                    s
                    for s in analysed.seats
                    if s.seat_id == seat_id and s.regime == key and s.round_label == st.FINAL
                ),
                None,
            )
            seat_cells.append(
                sc.ratio(found.correct, found.scored) if found and found.scored else None
            )
        rows.append(
            RegimeRow(
                key=key,
                name=name,
                question=question,
                events=sum(1 for e in analysed.events if e.direction.value == key),
                graded=metrics.scored,
                accuracy=metrics.accuracy if metrics.scored else None,
                seats=tuple(seat_cells),
                unscored=metrics.decisions - metrics.scored,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class SeatRow:
    seat_id: str
    final: Decimal | None
    first: Decimal | None
    changed: Decimal | None
    trend: tuple[Decimal | None, ...]


def _accuracy(rows: Iterable[st.SeatMetrics]) -> Decimal | None:
    listed = list(rows)
    scored = sum(r.scored for r in listed)
    return sc.ratio(sum(r.correct for r in listed), scored) if scored else None


def changed_votes(records: Iterable[CycleRecord], seat_id: str) -> Decimal | None:
    """How often a seat's final vote differs from its blind one, over cycles it voted in both."""
    voted = changed = 0
    for record in records:
        for decision in record.decisions:
            first = _vote_of(record.round_zero_for(decision.instrument_key), seat_id)
            last = _vote_of(record.final_round_for(decision.instrument_key), seat_id)
            if first is None or last is None:
                continue
            voted += 1
            changed += first != last
    return sc.ratio(changed, voted) if voted else None


def _vote_of(responses: Sequence[SeatResponse], seat_id: str) -> str | None:
    found = next((r for r in responses if r.seat_id == seat_id), None)
    return found.vote.action.value if found is not None and found.vote is not None else None


def seat_rows(view: RunView) -> tuple[tuple[str, ...], tuple[SeatRow, ...]]:
    analysed = view.analysis
    assert analysed is not None
    buckets = tuple(next(iter(analysed.seat_trend.values()), {}).keys())
    rows = tuple(
        SeatRow(
            seat_id=seat_id,
            final=_accuracy(
                s for s in analysed.seats if s.seat_id == seat_id and s.round_label == st.FINAL
            ),
            first=_accuracy(
                s for s in analysed.seats if s.seat_id == seat_id and s.round_label == st.ROUND_ZERO
            ),
            changed=changed_votes(analysed.records, seat_id),
            trend=tuple(analysed.seat_trend.get(seat_id, {}).get(b) for b in buckets),
        )
        for seat_id in view.panel_seats
    )
    return buckets, rows


@dataclass(frozen=True, slots=True)
class MonthRow:
    label: str
    name: str
    row: pd.MonthResult | None
    accuracy: Decimal | None
    graded: int


def month_rows(view: RunView) -> tuple[MonthRow, ...]:
    """Every month of the period: the ones that ran, then those that did not (§5.3, never zero)."""
    meta = view.meta
    assert meta is not None
    rows = {m.label: m for m in meta.months}
    by_month = view.analysis.by_month if view.analysis else {}
    out = []
    for month in meta.period.months:
        decisions = by_month.get(month.label, ())
        graded = sum(1 for d in decisions if d.verdict.is_scored)
        correct = sum(1 for d in decisions if d.verdict is sc.Verdict.CORRECT)
        out.append(
            MonthRow(
                label=month.label,
                name=_month_name(month.label, with_year=True),
                row=rows.get(month.label),
                accuracy=sc.ratio(correct, graded) if graded else None,
                graded=graded,
            )
        )
    return tuple(out)


def _detail_context(view: RunView) -> dict[str, Any]:
    context: dict[str, Any] = {
        "view": view,
        "meta": view.meta,
        "query": _query(view),
        "cost_note": COST_NOTE,
    }
    if view.meta is None:
        return context
    context.update(
        summary=summary_of(view),
        equity_svg=equity_chart(view.meta) if len(view.meta.period.months) > 1 else "",
        months=month_rows(view),
        shown_month_cost=sum((m.cost_usd for m in view.meta.months), start=Decimal(0)),
    )
    if view.analysis is not None:
        buckets, seats = seat_rows(view)
        context.update(regime_rows=regime_rows(view), buckets=buckets, seat_table=seats)
    return context


@router.get("/periods/{run_id}", response_class=HTMLResponse)
async def detail(
    request: Request, run_id: str, configs: str = "", news_source: str = "", job: str = ""
) -> HTMLResponse:
    """The result. A run with no directory at all is a 404; one mid-write is a page saying so."""
    view = await run_view(request, run_id, configs, news_source)
    page = render(request, "period_detail.html", error="", job=job, **_detail_context(view))
    if (
        view.meta is None
        and not pd.period_dir(run_id, workspace=state_of(request).workspace).is_dir()
    ):
        page.status_code = 404
    return page


# ------------------------------------------------------------------------- the drill-downs


def _fragment(request: Request, template: str, title: str, **context: Any) -> HTMLResponse:
    """The fragment for htmx; the same fragment inside the page shell for a plain navigation."""
    if request.headers.get("HX-Request"):
        return render(request, template, **context)
    return render(request, "period_part.html", part=template, part_title=title, **context)


def _missing(request: Request, run_id: str, why: str) -> HTMLResponse:
    page = _fragment(request, "period_part_missing.html", "not found", run_id=run_id, why=why)
    page.status_code = 404
    return page


def _key(view: RunView) -> str | None:
    if view.scoring is None:
        return None
    return next(
        (one.key for one in view.scoring.dataset.instruments if one.symbol == pd.PERIOD_SYMBOL),
        None,
    )


def _closes(view: RunView, start: datetime, end: datetime) -> tuple[tuple[datetime, Decimal], ...]:
    key = _key(view)
    if key is None or view.scoring is None:
        return ()
    return tuple(
        (candle.close_time, candle.close)
        for candle in view.scoring.index.candles.get(key, ())
        if start <= candle.close_time <= end
    )


def _grade(action: str | None, truth: sc.Truth | None, verdict: sc.Verdict | None) -> str:
    if action is None:
        return "none"
    if truth is None or verdict is None or not verdict.is_scored:
        return "unscored"
    return "correct" if Action(action) in sc.CORRECT_ACTIONS[truth] else "wrong"


def lanes_for(view: RunView, records: Sequence[CycleRecord], start: datetime, end: datetime) -> str:
    """Price above each seat's final vote and the panel's, graded, spikes shaded (§4 item 6)."""
    analysed = view.analysis
    assert analysed is not None
    scored = {d.cycle_id: d for d in analysed.scored}
    lanes: dict[str, list[charts.Mark]] = {seat: [] for seat in view.panel_seats}
    lanes["Panel"] = []
    for record in records:
        decision = scored.get(record.cycle_id)
        if not record.decisions:
            continue
        key = record.decisions[0].instrument_key
        final = record.final_round_for(key)
        truth = decision.truth if decision else None
        verdict = decision.verdict if decision else None
        stamp = record.as_of.strftime("%b %d %H:%M")
        for seat_id in view.panel_seats:
            action = _vote_of(final, seat_id)
            grade = _grade(action, truth, verdict)
            lanes[seat_id].append(
                charts.Mark(
                    at=record.as_of,
                    letter=LETTER.get(action or "", "–"),
                    grade=grade,
                    title=f"{seat_id}, {stamp}: {action or 'abstained'}, {grade}",
                )
            )
        panel_action = record.decisions[0].action.value
        grade = _grade(panel_action, truth, verdict)
        lanes["Panel"].append(
            charts.Mark(
                at=record.as_of,
                letter=LETTER.get(panel_action, "?"),
                grade=grade,
                title=f"Panel, {stamp}: {panel_action}, {grade}",
            )
        )
    shades = [
        charts.Shade(start=e.start, end=e.end, direction=e.direction.value)
        for e in analysed.events
        if e.end >= start and e.start <= end
    ]
    return charts.lanes_svg(_closes(view, start, end), lanes, shades)


@router.get("/periods/{run_id}/months/{label}", response_class=HTMLResponse)
async def month(
    request: Request, run_id: str, label: str, configs: str = "", news_source: str = ""
) -> HTMLResponse:
    view = await run_view(request, run_id, configs, news_source)
    if view.meta is None or view.analysis is None:
        return _missing(request, run_id, view.problem or view.incomplete)
    found = next((m for m in view.meta.period.months if m.label == label), None)
    if found is None:
        return _missing(request, run_id, f"{label} is not a month of {view.meta.period.label}")
    records = tuple(r for r in view.analysis.records if found.start <= r.as_of < found.end)
    scored = {d.cycle_id: d for d in view.analysis.scored}
    return _fragment(
        request,
        "_period_month.html",
        f"{label}",
        view=view,
        query=_query(view),
        label=label,
        lanes_svg=lanes_for(view, records, found.start, found.end),
        cycles=tuple(reversed(records)),
        scored=scored,
        not_run=label in view.analysis.not_run,
    )


@dataclass(frozen=True, slots=True)
class GradeRow:
    seat_id: str
    first: str | None
    final: str | None
    grade: str


def grade_rows(
    view: RunView, record: CycleRecord, decision: sc.ScoredDecision | None
) -> tuple[GradeRow, ...]:
    if not record.decisions:
        return ()
    key = record.decisions[0].instrument_key
    truth = decision.truth if decision else None
    verdict = decision.verdict if decision else None
    return tuple(
        GradeRow(
            seat_id=seat_id,
            first=_vote_of(record.round_zero_for(key), seat_id),
            final=_vote_of(record.final_round_for(key), seat_id),
            grade=_grade(_vote_of(record.final_round_for(key), seat_id), truth, verdict),
        )
        for seat_id in view.panel_seats
    )


def rounds_of(record: CycleRecord) -> tuple[tuple[int, tuple[SeatResponse, ...]], ...]:
    """Each round in order, blind round first (§4 item 7)."""
    indices = sorted({r.round_index for r in record.responses})
    return tuple(
        (index, tuple(r for r in record.responses if r.round_index == index)) for index in indices
    )


def tally(record: CycleRecord) -> dict[str, int]:
    if not record.decisions:
        return {}
    counted: dict[str, int] = {}
    for response in record.final_round_for(record.decisions[0].instrument_key):
        action = response.vote.action.value if response.vote else "abstained"
        counted[action] = counted.get(action, 0) + 1
    return counted


@router.get("/periods/{run_id}/cycles/{cycle_id}", response_class=HTMLResponse)
async def cycle(
    request: Request, run_id: str, cycle_id: str, configs: str = "", news_source: str = ""
) -> HTMLResponse:
    view = await run_view(request, run_id, configs, news_source)
    if view.meta is None or view.analysis is None:
        return _missing(request, run_id, view.problem or view.incomplete)
    record = next((r for r in view.analysis.records if r.cycle_id == cycle_id), None)
    if record is None:
        return _missing(request, run_id, f"no cycle {cycle_id} in this run")
    decision = next((d for d in view.analysis.scored if d.cycle_id == cycle_id), None)
    after = ""
    if decision is not None and view.scoring is not None:
        horizon = view.scoring.params.horizon_bars
        bar = view.scoring.index.candles.get(decision.instrument_key, ())
        step = bar[1].close_time - bar[0].close_time if len(bar) > 1 else timedelta(hours=1)
        prices = _closes(view, decision.as_of - step, decision.as_of + step * horizon)
        anchor = next((p for t, p in prices if t <= decision.as_of), None)
        band = (
            (anchor - decision.band, anchor + decision.band)
            if anchor is not None and decision.band is not None
            else None
        )
        after = charts.after_svg(prices, band=band, decision_at=decision.as_of)
    return _fragment(
        request,
        "_period_cycle.html",
        record.as_of.strftime("%Y-%m-%d %H:%M"),
        view=view,
        query=_query(view),
        record=record,
        decision=decision,
        rounds=rounds_of(record),
        tally=tally(record),
        result=view.analysis.outcomes.get(cycle_id, an.CycleResult()),
        after_svg=after,
        grades=grade_rows(view, record, decision),
        horizon=view.scoring.params.horizon_bars if view.scoring else None,
        band_k=view.scoring.params.band_k if view.scoring else None,
    )


@dataclass(frozen=True, slots=True)
class StretchRow:
    name: str
    decisions: tuple[sc.ScoredDecision, ...]


@dataclass(frozen=True, slots=True)
class SeatCall:
    seat_id: str
    action: str | None
    grade: str
    thesis: str


def seat_calls(
    view: RunView, records: Mapping[str, CycleRecord], decisions: Sequence[sc.ScoredDecision]
) -> tuple[tuple[sc.ScoredDecision, tuple[SeatCall, ...]], ...]:
    """Per decision, each seat's final call, its grade and the argument it gave (§7.3)."""
    out = []
    for decision in decisions:
        record = records.get(decision.cycle_id)
        final = record.final_round_for(decision.instrument_key) if record else ()
        calls = []
        for seat_id in view.panel_seats:
            found = next((r for r in final if r.seat_id == seat_id), None)
            action = found.vote.action.value if found and found.vote else None
            calls.append(
                SeatCall(
                    seat_id=seat_id,
                    action=action,
                    grade=_grade(action, decision.truth, decision.verdict),
                    thesis=found.vote.thesis if found and found.vote else "",
                )
            )
        out.append((decision, tuple(calls)))
    return tuple(out)


@router.get("/periods/{run_id}/events/{index}", response_class=HTMLResponse)
async def event(
    request: Request, run_id: str, index: int, configs: str = "", news_source: str = ""
) -> HTMLResponse:
    view = await run_view(request, run_id, configs, news_source)
    if view.meta is None or view.analysis is None:
        return _missing(request, run_id, view.problem or view.incomplete)
    if not 0 <= index < len(view.analysis.events):
        return _missing(request, run_id, f"no event {index} in this run")
    found = view.analysis.events[index]
    news = view.analysis.event_news[index]
    split = stretches(found, view.analysis.scored, period_start=view.meta.period.start)
    records = {r.cycle_id: r for r in view.analysis.records}
    window = tuple(
        r for r in view.analysis.records if found.start - LEAD_IN <= r.as_of <= found.end + AFTER
    )
    return _fragment(
        request,
        "_period_event.html",
        f"event {found.start:%Y-%m-%d}",
        view=view,
        query=_query(view),
        event=found,
        news=news,
        clipped=split.clipped,
        stretches=(
            ("Lead-in (48 hours before)", seat_calls(view, records, split.lead_in)),
            ("During", seat_calls(view, records, split.during)),
            ("After (48 hours)", seat_calls(view, records, split.after)),
        ),
        lanes_svg=lanes_for(view, window, found.start - LEAD_IN, found.end + AFTER),
    )


# ---------------------------------------------------------------------------- Continue


@router.post("/periods/{run_id}/continue")
async def continue_run(request: Request, run_id: str) -> Response:
    """§5.7: Continue is for an interrupted run only, and it is the CLI's `--continue`."""
    state = state_of(request)
    fields = {key: str(value) for key, value in (await request.form()).items()}
    view = await run_view(request, run_id, fields.get("configs", ""), fields.get("news_source", ""))

    def refuse(reason: str, status: int) -> HTMLResponse:
        page = render(request, "period_detail.html", error=reason, job="", **_detail_context(view))
        page.status_code = status
        return page

    meta = view.meta
    if meta is None:
        return refuse(f"no run {run_id} to continue: {view.problem}", 404)
    refusals: dict[pd.RunEnding, str] = {
        pd.RunEnding.RULE_ENDED: (
            f"Model failed, requires tuning: {meta.ending_reason}. A rule's verdict is the "
            "result, and there is nothing to continue."
        ),
        pd.RunEnding.COMPLETED: "This run completed; there is nothing to continue.",
        pd.RunEnding.RUNNING: (
            "This run is still marked running: a process is writing it, or one died. Wait for "
            "it to end (or stop it on Runs in progress) before continuing."
        ),
    }
    if meta.ending in refusals:
        return refuse(refusals[meta.ending], 409)
    if not view.configs:
        return refuse(
            "The seat set this run ran is not on file, so the command cannot be built: name its "
            "TOML in the configs field.",
            400,
        )
    try:
        argv = evaluate_period_argv(
            {
                "data": meta.dataset_directory,
                "configs": view.configs,
                "candidate": meta.candidate_id,
                "start_equity": str(meta.start_equity),
                "every": _CADENCE_LABEL.get(meta.cadence_seconds, ""),
                "period": meta.period.label,
                "budget": fields.get("budget", ""),
                "news_source": " ".join(view.sources),
                "skip_gate": "1" if meta.gate_skipped else "",
                "continue": "1",
            }
        )
    except ConfigError as error:
        return refuse(str(error), 400)
    try:
        record = jobs.start(
            argv, label="evaluate-period", clock=state.clock, workspace=state.workspace
        )
    except jobs.Busy as error:
        return refuse(str(error), 409)
    query = _query(view)
    return RedirectResponse(
        f"/periods/{run_id}?{query}{'&' if query else ''}job={record.job_id}", status_code=303
    )
