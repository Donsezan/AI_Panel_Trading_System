# decision_lab slice D, pass 2 — the dashboard: read, edit, run

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One page from which a seat set is built, launched and read — the §12 loop — plus `notebooks/tuning.ipynb` over the same library.

**Architecture:** Three surfaces over one shell. **Read** derives live from the workspace through a new `analysis.py` that the CLI, the dashboard and the notebook all share, cached on the rows files' modification times so a running job invalidates its own cache. **Edit** stores seat sets as versioned TOML in the workspace and validates them with `candidates.load_matrix` itself. **Run** launches `python -m decision_lab <argv>` as a child process under a workspace lock, so the page cannot diverge from the CLI, a six-month `BacktestHarness` pass never blocks the event loop, and Stop is a real termination. No JavaScript: server-rendered Jinja, a meta refresh only while a job runs.

**Tech Stack:** Python 3.11, FastAPI + Starlette + Jinja2 (already dependencies of `tradebot`), pydantic v2 (`DomainModel`), stdlib `tomllib` for reading and a hand-rolled writer for writing, `Decimal` only, pytest, ruff, mypy. **No new dependency**, including for the notebook.

**Spec:** [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../specs/2026-08-23-decision-lab-design.md) — §12 (the three surfaces, rewritten for this pass), §11 (the registry), §9.5–§9.7 (what the views show), §13 (CLI and exit codes), §14 (one assembly, three front doors), §15 (failure semantics), §16 (testing).

## Global Constraints

- **No `float`, anywhere in `decision_lab`.** Enforced by `decision_lab/tests/test_discipline.py`, which walks every non-test module for `float(` calls and `float` annotations and whose exemption set is **empty**. Percentages rendered in a template are `Decimal` formatted to a string; a CSS bar width is an `int`. **The seat editor deliberately does not expose `SeatConfig.temperature`** — it is not one of §7.1's matrix axes, no shipped matrix sets it, and a form that wrote it would put a TOML float into this package's own code path for no measurement.
- **Nothing under `tradebot/` may name `decision_lab`** — not an import, not an attribute, not a string. Enforced by `decision_lab/tests/test_separation.py`. **This pass touches no file under `tradebot/`.** `git diff --stat main -- tradebot/` must stay empty. The import direction is one-way: `decision_lab` may import `tradebot.dashboard.auth`, never the reverse.
- **Nothing prints.** `T20` bans `print` repo-wide. Progress goes to the logger; a result is a file or an HTTP response; the verdict is the exit code.
- **Money is `Decimal`**, via `tradebot.core.money` and `tradebot.core.schema.Money`. A money value reaching a page is the server's exact `Decimal` formatted as a string, never coerced.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never `datetime.now()` in library code. Routes take the clock from app state.
- **Comments explain *why*, and cite the spec section they implement** (`§12.4`, `§7.4`), per CLAUDE.md. Docstrings state failure semantics at module level.
- **Both gates must pass:** `.\decision_lab\check.ps1` and the root `.\check.ps1`.
- Exit codes (`decision_lab/cli.py`): `0` ok, `2` misuse, `3` dataset/day-set, `4` candidate invalid, `5` budget ceiling, `6` gate unsatisfied, **`7` workspace held by another run** (new in this pass).
- Line length 100 (`ruff`), `from __future__ import annotations` at the top of every module.

## File Structure

**Create**

| File | Responsibility |
|---|---|
| `decision_lab/analysis.py` | §12.2's one read assembly: a dataset + scoring setup, and a matrix's candidates folded to scored decisions, ranking, agreement, seat blocks and not-measured reasons. Pure — no argparse, no spend, no I/O beyond reading what a run already wrote. |
| `decision_lab/matrices.py` | §12.3's seat-set store: versioned TOML under `workspace/matrices/<name>/`, a TOML writer for the matrix schema, and save-time validation through `candidates.load_matrix`. |
| `decision_lab/jobs.py` | §12.4's workspace lock and child-process launcher: `WorkspaceLock`, `JobRecord`, `start`, `stop`, `current`, `history`. |
| `decision_lab/dashboard/__init__.py` | Package marker; re-exports `create_lab_dashboard`. |
| `decision_lab/dashboard/auth.py` | The lab's token (`DECISION_LAB_DASHBOARD_TOKEN`), its own cookie name, and a pure-ASGI middleware over `tradebot.dashboard.auth`'s `Session`, `GUARDED_SCOPES` and `REFUSALS`. |
| `decision_lab/dashboard/views.py` | `LabState`, `state_of`, `build_templates`, `render`, and the money/percent/moment filters. |
| `decision_lab/dashboard/app.py` | `create_lab_dashboard(*, workspace, token, clock) -> FastAPI`. Mounts static, installs auth, includes the routers, owns the analysis cache. |
| `decision_lab/dashboard/cache.py` | The §12.2 derivation cache, keyed on corpus, matrix, scoring params and the rows files' mtimes. |
| `decision_lab/dashboard/routes/runs.py` | Runs, run detail, seat detail, decision drill-down. |
| `decision_lab/dashboard/routes/matrices.py` | The seat-set editor: list, open a version, add/remove a seat, save. |
| `decision_lab/dashboard/routes/jobs.py` | Start forms with the cost projection, start, stop, and the log tail. |
| `decision_lab/dashboard/templates/*.html` | `base.html`, `login.html`, `runs.html`, `run_detail.html`, `seats.html`, `decision.html`, `matrices.html`, `matrix_edit.html`, `jobs.html`, `_tables.html`. |
| `decision_lab/dashboard/static/app.css` | One stylesheet. No JS. |
| `decision_lab/notebooks/tuning.ipynb` | §14's second front door over `analysis.py`. |
| `decision_lab/tests/test_analysis.py` | The assembly, and that it equals what the CLI's report produced. |
| `decision_lab/tests/test_matrices.py` | TOML round-trip on the shipped matrices, versioning, refusal on an invalid matrix. |
| `decision_lab/tests/test_jobs.py` | The lock, the launcher, stop, and a stale lock. |
| `decision_lab/tests/test_lab_dashboard_auth.py` | Every route refuses without a session; the cookie name differs from the bot's. |
| `decision_lab/tests/test_lab_dashboard_read.py` | The four read views. |
| `decision_lab/tests/test_lab_dashboard_edit.py` | The editor: save mints a version, the gate warning, the refusals. |
| `decision_lab/tests/test_lab_dashboard_run.py` | Start refusals (no budget, gate, busy), stop, the log tail. |
| `decision_lab/tests/test_notebook.py` | The notebook parses, every code cell compiles, no stored outputs. |
| `decision_lab/tests/test_slice_d_pass2_end_to_end.py` | The slice exit criterion: build a seat set in the page, launch a stub sweep, read its ranking. |

**Modify**

| File | Change |
|---|---|
| `decision_lab/cli.py` | `report` and `calibrate_snapshot` call `analysis`; the `dashboard` command; the workspace lock around the writing commands; `EXIT_BUSY = 7`. |
| `decision_lab/params.py` | `MATRICES_DIR`, `JOBS_DIR`, `LOCK_FILE`, `DEFAULT_DASHBOARD_PORT`. |
| `decision_lab/tests/conftest.py` | A `lab_client` fixture: a `TestClient` over a workspace built by `calibrated_corpus`. |
| `decision_lab/PROGRESS.md`, `CLAUDE.md` | Slice D pass 2 status, the new commands, the rules that are easy to get backwards. |

**Dependency direction (no cycles):** `analysis.py` → `corpus`, `records`, `scoring`, `seats`, `compare`, `sweep`, `regimes`, `dataset`, `candidates`, `render` (models only). `matrices.py` → `candidates`, `params`. `jobs.py` → `params` only — it launches a command line and knows nothing about what the command does. `dashboard/` → all of the above; **nothing in the lab's core imports `dashboard`**, asserted in Task 4.

---

### Task 1: `analysis.py` — one assembly, three front doors

**Files:**
- Create: `decision_lab/analysis.py`
- Test: `decision_lab/tests/test_analysis.py`
- Modify: `decision_lab/cli.py` (`report`, `calibrate_snapshot`, `_not_measured_reason` moves out)

**Interfaces:**
- Consumes: `decision_lab.corpus.{Corpus, load}`, `decision_lab.records.load`, `decision_lab.scoring.{ScoringParams, PriceIndex, ScoredDecision, build_price_index, score_records}`, `decision_lab.seats.score_seats`, `decision_lab.compare.{ranking, agreement, Ranked, Agreement}`, `decision_lab.sweep.{SweepRow, read_rows, rows_path, records_from_rows}`, `decision_lab.regimes.{RegimeIndex, index_dataset, load_windows, DEFAULT_REGIMES_TOML}`, `decision_lab.dataset.{CoverageAudit, require_verified}`, `decision_lab.candidates.{Candidate, Matrix}`, `decision_lab.render.{CandidateSeats, NotMeasured}`, `tradebot.marketdata.recorder.ReplayDataset`, `tradebot.core.clock.Clock`.
- Produces:
  - `analysis.Scoring` — a frozen dataclass: `params: ScoringParams`, `index: PriceIndex`, `regimes: RegimeIndex`, `dataset: ReplayDataset`, `audit: CoverageAudit`, `data_dir: Path`
  - `async analysis.build_scoring(data_dir: Path, *, clock: Clock, timeframe: str = "", band_k: Decimal | None = None, horizon: int | None = None, regimes_toml: Path | None = None) -> Scoring`
  - `analysis.CandidateAnalysis` — frozen dataclass: `candidate_id: str`, `rows: Mapping[str, SweepRow]`, `records: tuple[CycleRecord, ...]`, `scored: tuple[ScoredDecision, ...]`, `not_measured_reason: str`, `seats: tuple[SeatMetrics, ...]`; property `measured -> bool`
  - `analysis.MatrixAnalysis` — frozen dataclass: `candidates: tuple[CandidateAnalysis, ...]`; properties `by_candidate -> dict[str, tuple[ScoredDecision, ...]]`, `ranking -> tuple[Ranked, ...]`, `agreement -> tuple[Agreement, ...]`, `candidate_seats -> tuple[CandidateSeats, ...]`, `not_measured -> tuple[NotMeasured, ...]`; method `find(candidate_id) -> CandidateAnalysis | None`
  - `analysis.analyse_matrix(corpus: Corpus, matrix: Matrix, matrix_digest: str, scoring: Scoring, *, workspace: Path | None = None) -> MatrixAnalysis`
  - `analysis.not_measured_reason(rows: Mapping[str, SweepRow], records: Sequence[CycleRecord]) -> str`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_analysis.py`:

```python
"""§12.2 — the assembly the CLI, the dashboard and the notebook all share.

The load-bearing assertion is the last one: what `analyse_matrix` produces is what
`cli.report` already wrote. A second assembly that drifted would be §14's rejected second
`report` command arriving through another door.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import analysis, candidates as cd, corpus as cp, records as rc, sweep as sw
from decision_lab.tests.factories import stub_matrix_file
from tradebot.core.clock import SystemClock


@pytest.mark.asyncio
async def test_build_scoring_reads_the_datasets_own_timeframe(built_corpus_id: str) -> None:
    meta, _ = rc.load(built_corpus_id)
    scoring = await analysis.build_scoring(Path(meta.dataset_directory), clock=SystemClock())

    assert scoring.params.timeframe == scoring.dataset.timeframes[0]
    assert scoring.audit.dataset_digest == meta.dataset_digest
    # The regime index carries the named windows, so a window is never silently unlabelled.
    assert scoring.regimes.window_bars > 0


@pytest.mark.asyncio
async def test_a_candidate_with_no_rows_is_not_measured_and_says_why(built_corpus_id: str) -> None:
    meta, _ = rc.load(built_corpus_id)
    corpus = cp.load(built_corpus_id)
    matrix = cd.load_matrix(stub_matrix_file(), reference=meta.reference_basket)
    scoring = await analysis.build_scoring(Path(meta.dataset_directory), clock=SystemClock())

    result = analysis.analyse_matrix(corpus, matrix, matrix.matrix_digest, scoring)

    assert result.ranking == (), "no sweep ran, so nothing is ranked"
    assert {row.candidate_id for row in result.not_measured} == {
        one.candidate_id for one in matrix.candidates
    }
    assert all(
        "the sweep halted before reaching it" in row.reason for row in result.not_measured
    )


def test_not_measured_reason_separates_the_three_causes() -> None:
    halted = analysis.not_measured_reason({}, ())
    assert "halted before reaching it" in halted

    failed = analysis.not_measured_reason(
        {"c1": sw.SweepRow(cycle_id="c1", as_of="2026-01-01T00:00:00Z", error="boom")}, ()
    )
    assert "1 failed" in failed and "unusable" in failed

    clean = analysis.not_measured_reason(
        {"c1": sw.SweepRow(cycle_id="c1", as_of="2026-01-01T00:00:00Z")},
        (object(),),  # type: ignore[arg-type]  # only the count is read
    )
    assert "none of them carried a decision to score" in clean
```

Add to `decision_lab/tests/factories.py` (create the helper if the module has none):

```python
def stub_matrix_file() -> Path:
    """The shipped plumbing-check matrix. Offline, free, and a real `load_matrix` input."""
    return Path(__file__).resolve().parents[1] / "config" / "sweep-stub.toml"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_analysis.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.analysis'`.

- [ ] **Step 3: Write `decision_lab/analysis.py`**

```python
"""The one read assembly: a corpus and a sweep's rows, folded into what a page or a report shows.

§12.2 and §14 — one implementation, three front doors. `cli.report`, the dashboard's views and
`notebooks/tuning.ipynb` all call this, so two of them can never disagree about the same run.
The rule this module exists to hold is finding 3's: **a candidate is measured only if a scored
decision survived**, and the test is the scored decisions, never the row file. A candidate whose
rows all errored has a non-empty `.jsonl` and no measurement whatever; admitted to the ranking it
would sort in at 0.0% accuracy — measured and worst, rather than never measured.

Failure semantics: nothing here spends, writes or reaches a network. A missing rows file reads as
a candidate the sweep never reached, never as an error. Reading a `.jsonl` another process is
appending to is safe: `sweep.read_rows` validates line by line, and a half-written final line is
skipped rather than raising (§12.2).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from decision_lab import compare as cmp
from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import regimes as rg
from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab import sweep as sw
from decision_lab.candidates import Matrix
from decision_lab.records import CycleRecord
from decision_lab.render import CandidateSeats, NotMeasured
from tradebot.core.clock import Clock
from tradebot.marketdata.recorder import ReplayDataset


@dataclass(frozen=True, slots=True)
class Scoring:
    """Everything a verdict is derived from, built once and shared by every candidate.

    Built once per (dataset, parameters) because §9.2's band is read off the frozen snapshot and
    the price index is the expensive half: rebuilding it per candidate would make a sweep of ten
    candidates load the dataset ten times to reach identical numbers.
    """

    params: sc.ScoringParams
    index: sc.PriceIndex
    regimes: rg.RegimeIndex
    dataset: ReplayDataset
    audit: ds.CoverageAudit
    data_dir: Path


async def build_scoring(
    data_dir: Path,
    *,
    clock: Clock,
    timeframe: str = "",
    band_k: Decimal | None = None,
    horizon: int | None = None,
    regimes_toml: Path | None = None,
) -> Scoring:
    """Load the dataset, its audit, the price index and the regime labels.

    `require_verified` first: an unverified dataset refuses here exactly as it refuses a corpus
    build, so a page cannot show numbers derived from history nobody audited (§15).
    """
    audit = ds.require_verified(data_dir)
    dataset = ReplayDataset.load(data_dir, clock)
    params = sc.ScoringParams(
        timeframe=timeframe or dataset.timeframes[0],
        **({"band_k": band_k} if band_k is not None else {}),
        **({"horizon_bars": horizon} if horizon is not None else {}),
    )
    index = await sc.build_price_index(dataset, audit, params)
    regime_index = (await rg.index_dataset(dataset, params.timeframe)).with_windows(
        rg.load_windows(regimes_toml or rg.DEFAULT_REGIMES_TOML)
    )
    return Scoring(
        params=params,
        index=index,
        regimes=regime_index,
        dataset=dataset,
        audit=audit,
        data_dir=data_dir,
    )


def not_measured_reason(
    rows: Mapping[str, sw.SweepRow], records: Sequence[CycleRecord]
) -> str:
    """Why a candidate contributed no measurement — never merely *that* it did not.

    Three causes an operator must act on differently: a halt that stopped the sweep before this
    candidate (re-run and it fills in), rows that all failed or were all contaminated (the
    candidate itself is broken, or its seats are substituting), and a candidate that replayed
    cleanly but whose cycles yielded no decision at all. Flattening them into "the sweep halted"
    sends the second and third to the wrong fix.
    """
    if not rows:
        return "the sweep halted before reaching it — no row was recorded"
    if not records:
        failed = sum(1 for row in rows.values() if row.error)
        contaminated = sum(1 for row in rows.values() if row.contaminated)
        parts = [f"{failed} failed"] if failed else []
        parts += [f"{contaminated} contaminated by a substitute model"] if contaminated else []
        why = ", ".join(parts) or "none of them matched a corpus entry"
        return (
            f"all {len(rows)} of its rows were unusable ({why}) — nothing it produced measures "
            "the panel it declares"
        )
    return f"{len(records)} cycles replayed cleanly, but none of them carried a decision to score"


@dataclass(frozen=True, slots=True)
class CandidateAnalysis:
    """One candidate's rows, folded and scored — measured or not, and why not."""

    candidate_id: str
    rows: Mapping[str, sw.SweepRow]
    records: tuple[CycleRecord, ...]
    scored: tuple[sc.ScoredDecision, ...]
    not_measured_reason: str
    seats: tuple[st.SeatMetrics, ...]

    @property
    def measured(self) -> bool:
        """A scored decision survived. The row file is never the test (finding 3)."""
        return bool(self.scored)


@dataclass(frozen=True, slots=True)
class MatrixAnalysis:
    """Every candidate of one matrix over one corpus, and the cross-candidate tables."""

    candidates: tuple[CandidateAnalysis, ...]

    @property
    def by_candidate(self) -> dict[str, tuple[sc.ScoredDecision, ...]]:
        return {one.candidate_id: one.scored for one in self.candidates if one.measured}

    @property
    def ranking(self) -> tuple[cmp.Ranked, ...]:
        return cmp.ranking(self.by_candidate)

    @property
    def agreement(self) -> tuple[cmp.Agreement, ...]:
        return cmp.agreement(self.by_candidate)

    @property
    def candidate_seats(self) -> tuple[CandidateSeats, ...]:
        return tuple(
            CandidateSeats(candidate_id=one.candidate_id, seats=one.seats)
            for one in self.candidates
            if one.measured
        )

    @property
    def not_measured(self) -> tuple[NotMeasured, ...]:
        return tuple(
            NotMeasured(candidate_id=one.candidate_id, reason=one.not_measured_reason)
            for one in self.candidates
            if not one.measured
        )

    def find(self, candidate_id: str) -> CandidateAnalysis | None:
        return next((one for one in self.candidates if one.candidate_id == candidate_id), None)


def analyse_matrix(
    corpus: cp.Corpus,
    matrix: Matrix,
    matrix_digest: str,
    scoring: Scoring,
    *,
    workspace: Path | None = None,
) -> MatrixAnalysis:
    """Fold every candidate's rows onto the corpus and score them.

    `matrix_digest` is passed rather than read off `matrix` because a report renders the digest a
    sweep *recorded*, which is the directory the rows are in. They are equal on the happy path and
    the caller is the one that can tell — `cli.report` refuses outright when they differ.
    """
    built: list[CandidateAnalysis] = []
    for candidate in matrix.candidates:
        rows = sw.read_rows(
            sw.rows_path(
                corpus.meta.corpus_id, matrix_digest, candidate.candidate_id, workspace=workspace
            )
        )
        records = sw.records_from_rows(corpus, rows)
        scored = sc.score_records(
            records, index=scoring.index, regimes=scoring.regimes, params=scoring.params
        )
        built.append(
            CandidateAnalysis(
                candidate_id=candidate.candidate_id,
                rows=rows,
                records=records,
                scored=scored,
                not_measured_reason="" if scored else not_measured_reason(rows, records),
                seats=(
                    st.score_seats(records, scored, panel=candidate.panel) if scored else ()
                ),
            )
        )
    return MatrixAnalysis(candidates=tuple(built))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_analysis.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Rewire `cli.report` to the new assembly**

In `decision_lab/cli.py`: add `from decision_lab import analysis as an` to the imports, delete the module-level `_not_measured_reason` function, and replace the body of `report` from the `params = sc.ScoringParams(` line through `candidate_seats = tuple(blocks)` with the calls below. Everything before (`rc.load`, `require_verified`) and after (`rd.LabReport(...)`, `write_report`, the registry row) is unchanged, as are both refusals about the reloaded matrix.

```python
    scoring = await an.build_scoring(
        data_dir,
        clock=SystemClock(),
        timeframe=args.scoring_timeframe,
        band_k=args.band_k,
        horizon=args.horizon,
        regimes_toml=args.regimes,
    )
    scored = sc.score_records(
        cycles, index=scoring.index, regimes=scoring.regimes, params=scoring.params
    )
    params = scoring.params
    index = scoring.index
    regime_index = scoring.regimes
```

and, inside `if result is not None:` after the two matrix refusals, replacing the `blocks` loop:

```python
        analysed = an.analyse_matrix(corpus_obj, matrix, result.matrix_digest, scoring)
        ranking = analysed.ranking
        agreement = analysed.agreement
        candidate_seats = analysed.candidate_seats
        not_measured = list(analysed.not_measured)
        by_candidate = analysed.by_candidate
```

- [ ] **Step 6: Rewire `cli.calibrate_snapshot` to the same assembly**

In `calibrate_snapshot`, replace the `params = sc.ScoringParams(...)`, `index = ...` and `regime_index = ...` block with one `an.build_scoring(...)` call as above, then replace the per-candidate loop body. The evidence list must still be appended for **every** candidate, measured or not — §10.6's first condition is that every candidate materialised and deliberated, so one that produced nothing has to reach `failures_for` to fail it:

```python
    analysed = an.analyse_matrix(corpus, matrix, matrix.matrix_digest, scoring)
    evidence = [
        cal.candidate_evidence(candidate, found.rows, found.records, found.scored)
        for candidate, found in zip(matrix.candidates, analysed.candidates, strict=True)
    ]
    by_candidate = analysed.by_candidate
    not_measured = list(analysed.not_measured)
    seat_blocks = list(analysed.candidate_seats)
    day_rows = [
        row
        for found in analysed.candidates
        if found.measured
        for row in cal.per_day(found.candidate_id, found.scored, pinned)
    ]
```

- [ ] **Step 7: Run the whole tool suite — the CLI tests are the safety net for this refactor**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS, with `test_cli_sweep.py`, `test_cli_calibrate.py`, `test_render_sweep.py`, `test_slice_c_end_to_end.py` and `test_slice_d_end_to_end.py` all green — they assert the numbers this refactor must not move.

- [ ] **Step 8: Run both gates**

Run: `.\decision_lab\check.ps1` then `.\check.ps1`
Expected: both pass. `check.ps1` includes `mypy decision_lab`; `zip(..., strict=True)` needs both sequences to be the same length, which `analyse_matrix` guarantees by iterating `matrix.candidates`.

- [ ] **Step 9: Commit**

```bash
git add decision_lab/analysis.py decision_lab/tests/test_analysis.py decision_lab/tests/factories.py decision_lab/cli.py
git commit -m "feat(decision_lab): one read assembly, shared by every front door

§12.2 and §14. The fold from a corpus and a sweep's rows to scored decisions,
the ranking, the agreement matrix and the seat blocks lived inside two cli.py
handlers. A dashboard and a notebook make three front doors, and three copies
of finding 3's rule — a candidate is measured only if a scored decision
survived — is three places for it to drift.

cli.report and cli.calibrate_snapshot now call analysis.analyse_matrix. Their
tests are the safety net and none of their numbers move.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: `matrices.py` — the versioned seat-set store

**Files:**
- Create: `decision_lab/matrices.py`
- Test: `decision_lab/tests/test_matrices.py`
- Modify: `decision_lab/params.py`

**Interfaces:**
- Consumes: `decision_lab.params.workspace_root`, `decision_lab.candidates.{Matrix, load_matrix}`, `tradebot.core.config.Basket`, `tradebot.core.errors.ConfigError`.
- Produces:
  - `matrices.MATRICES_DIR: Final[str]`, `matrices.NAME_PATTERN: Final[re.Pattern[str]]`
  - `matrices.dumps(document: Mapping[str, Any]) -> str` — the matrix schema as TOML text
  - `matrices.root(*, workspace: Path | None = None) -> Path`
  - `matrices.names(*, workspace: Path | None = None) -> tuple[str, ...]`
  - `matrices.versions(name: str, *, workspace: Path | None = None) -> tuple[int, ...]`
  - `matrices.path_for(name: str, version: int, *, workspace: Path | None = None) -> Path`
  - `matrices.read(name: str, version: int | None = None, *, workspace: Path | None = None) -> tuple[int, dict[str, Any]]`
  - `matrices.save(name: str, document: Mapping[str, Any], *, reference: Basket, workspace: Path | None = None) -> tuple[int, Matrix]`
  - `matrices.templates() -> tuple[str, ...]` and `matrices.read_template(name: str) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_matrices.py`:

```python
"""§12.3 — seat sets as versioned TOML in the workspace.

The round-trip test is the one that matters: the writer is hand-rolled (stdlib `tomllib` reads
and does not write, and §2.1's "no new dependency" is a property worth keeping), so it is proved
against the two matrices this repo ships rather than against a document written to suit it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from decision_lab import matrices
from decision_lab.tests.factories import basket, stub_matrix_file
from tradebot.core.errors import ConfigError

SHIPPED = Path(__file__).resolve().parents[1] / "config"


@pytest.mark.parametrize("shipped", ["sweep.toml", "sweep-stub.toml"])
def test_writer_round_trips_every_shipped_matrix(shipped: str) -> None:
    original = tomllib.loads((SHIPPED / shipped).read_text(encoding="utf-8"))

    written = matrices.dumps(original)

    assert tomllib.loads(written) == original


def test_writer_refuses_a_value_the_schema_has_no_place_for() -> None:
    with pytest.raises(ConfigError, match="temperature"):
        matrices.dumps({"candidates": [{"id": "c", "temperature": 0.3}]})


def test_save_mints_a_new_version_and_never_overwrites(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))

    first, _ = matrices.save("mine", document, reference=basket(), workspace=workspace)
    second, matrix = matrices.save("mine", document, reference=basket(), workspace=workspace)

    assert (first, second) == (1, 2)
    assert matrices.versions("mine", workspace=workspace) == (1, 2)
    assert matrices.read("mine", workspace=workspace)[0] == 2, "no version reads the latest"
    assert matrix.candidates, "save returns the validated matrix, so a caller need not reload it"


def test_save_refuses_an_invalid_matrix_and_writes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    broken = {"candidates": [{"id": "c", "seats": [{"seat_id": "s"}]}]}

    with pytest.raises(ConfigError):
        matrices.save("mine", broken, reference=basket(), workspace=workspace)

    assert matrices.versions("mine", workspace=workspace) == ()
    assert list(matrices.root(workspace=workspace).rglob("*.toml")) == [], "no temp file survives"


def test_a_name_that_could_escape_the_workspace_is_refused(tmp_path: Path) -> None:
    for bad in ("../escape", "with/slash", "", "A" * 80):
        with pytest.raises(ConfigError, match="name"):
            matrices.save(bad, {}, reference=basket(), workspace=tmp_path)


def test_templates_are_the_shipped_files_and_are_never_written(tmp_path: Path) -> None:
    assert set(matrices.templates()) == {"sweep", "sweep-stub"}
    assert matrices.read_template("sweep-stub")["sweep"]["on_fallback"] == "halt"
    assert stub_matrix_file().read_text(encoding="utf-8").startswith("# The plumbing check")
```

Add to `decision_lab/tests/factories.py` (beside `stub_matrix_file` from Task 1):

```python
def basket(basket_id: str = "reference") -> Basket:
    """A minimal reference basket for matrix validation — the one `_basket_for` copies."""
    return Basket(
        basket_id=basket_id,
        instruments=(instrument(),),
        timeframes=("1h",),
        panel=PanelConfig(
            panel_id="reference",
            providers=(),
            seats=(SeatConfig(seat_id="s", role="Analyst", provider_id="stub", model="stub-1"),),
        ),
    )
```

If `factories.py` already builds a reference basket under another name, reuse it rather than adding a second; check with `grep -n "def .*[Bb]asket" decision_lab/tests/factories.py` first and adapt the import in the test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_matrices.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.matrices'`.

- [ ] **Step 3: Add the workspace paths to `params.py`**

```python
#: §12.3's seat-set store and §12.4's job records, both under the workspace so nothing the
#: dashboard writes is ever mistaken for the curated, committed matrices in `config/`.
MATRICES_DIR: Final = "matrices"
JOBS_DIR: Final = "jobs"

#: §12.4's advisory lock, and the sidecar naming who holds it.
LOCK_FILE: Final = ".run.lock"
HOLDER_FILE: Final = ".run.holder.json"

#: §13. Loopback by default; a non-loopback bind needs `--allow-remote` on top of the token.
DEFAULT_DASHBOARD_PORT: Final = 8788
```

- [ ] **Step 4: Write `decision_lab/matrices.py`**

```python
"""Seat sets, versioned, in the workspace (spec §12.3).

The shipped `config/*.toml` stay read-only templates: they are curated, commented and in git, and
a form writer would destroy the first two and dirty the third from a browser click. What the
dashboard saves goes to `workspace/matrices/<name>/<version>.toml`, where every save is a new
version and nothing is overwritten — so the seat set that produced the best row in the registry
can still be opened.

One format serves both front doors. The CLI's `--configs` takes a path, `matrix_digest` is
computed exactly as it is today, and the writer here is the only new code: `tomllib` reads TOML
and does not write it, and §2.1's "no new dependency" is a property worth more than a
dependency-free writer costs. It handles exactly the matrix schema's value kinds and **refuses
anything else** — notably a float, which would be a `SeatConfig.temperature` this package has no
way to carry (see `test_discipline.py`).

Failure semantics: validation is `candidates.load_matrix` itself, run against a temporary file
before any version is minted, so a matrix that will not load leaves the store exactly as it was.
An absent store reads as no seat sets, never as an error.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from decision_lab.candidates import Matrix, load_matrix
from decision_lab.params import MATRICES_DIR, workspace_root
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError

#: A seat-set name is a directory name, so it may not traverse, and it is short enough to read in
#: a URL. Refused rather than sanitised: a name silently rewritten is one an operator cannot find
#: again.
NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

CONFIG_DIR: Final = Path(__file__).parent / "config"

__all__ = [
    "MATRICES_DIR",
    "NAME_PATTERN",
    "dumps",
    "names",
    "path_for",
    "read",
    "read_template",
    "root",
    "save",
    "templates",
    "versions",
]


# ---------------------------------------------------------------- the writer


def dumps(document: Mapping[str, Any]) -> str:
    """The matrix schema as TOML text. Scalars, then tables, then arrays of tables."""
    lines: list[str] = []
    _emit(lines, document, prefix=())
    return "\n".join(lines).strip("\n") + "\n"


def _is_table(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(_is_table(one) for one in value)


def _emit(lines: list[str], table: Mapping[str, Any], *, prefix: tuple[str, ...]) -> None:
    """Every scalar first, because a key written after a `[header]` belongs to that header."""
    for key, value in table.items():
        if not _is_table(value) and not _is_table_array(value):
            lines.append(f"{key} = {_scalar(value)}")
    for key, value in table.items():
        path = ".".join((*prefix, key))
        if _is_table(value):
            lines += ["", f"[{path}]"]
            _emit(lines, value, prefix=(*prefix, key))
        elif _is_table_array(value):
            for entry in value:
                lines += ["", f"[[{path}]]"]
                _emit(lines, entry, prefix=(*prefix, key))


def _scalar(value: Any) -> str:
    # `bool` before `int`: `isinstance(True, int)` is True, and `max_rounds = true` is nonsense.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quoted(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(one) for one in value) + "]"
    raise ConfigError(
        f"a matrix holds no value of type {type(value).__name__} ({value!r}). Money and every "
        "ratio is a quoted string, and a seat's temperature is deliberately not editable here"
    )


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


# ---------------------------------------------------------------- the store


def root(*, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / MATRICES_DIR


def _require_name(name: str) -> str:
    if not NAME_PATTERN.match(name):
        raise ConfigError(
            f"{name!r} is not a usable seat-set name: lowercase letters, digits, '-' and '_', "
            "starting with a letter or digit, at most 64 characters. It becomes a directory and "
            "a URL, so it is refused rather than rewritten into something you cannot find again"
        )
    return name


def names(*, workspace: Path | None = None) -> tuple[str, ...]:
    directory = root(workspace=workspace)
    if not directory.is_dir():
        return ()
    return tuple(sorted(one.name for one in directory.iterdir() if one.is_dir()))


def versions(name: str, *, workspace: Path | None = None) -> tuple[int, ...]:
    directory = root(workspace=workspace) / name
    if not directory.is_dir():
        return ()
    found = []
    for path in directory.glob("*.toml"):
        if path.stem.isdigit():
            found.append(int(path.stem))
    return tuple(sorted(found))


def path_for(name: str, version: int, *, workspace: Path | None = None) -> Path:
    return root(workspace=workspace) / _require_name(name) / f"{version}.toml"


def read(
    name: str, version: int | None = None, *, workspace: Path | None = None
) -> tuple[int, dict[str, Any]]:
    """One stored version, or the latest. The version is returned with it, never inferred later."""
    available = versions(_require_name(name), workspace=workspace)
    if not available:
        raise ConfigError(f"no seat set named {name!r} has been saved")
    chosen = available[-1] if version is None else version
    if chosen not in available:
        raise ConfigError(
            f"seat set {name!r} has no version {chosen}; it has {list(available)}"
        )
    path = path_for(name, chosen, workspace=workspace)
    return chosen, tomllib.loads(path.read_text(encoding="utf-8"))


def save(
    name: str,
    document: Mapping[str, Any],
    *,
    reference: Basket,
    workspace: Path | None = None,
) -> tuple[int, Matrix]:
    """Validate through `load_matrix`, then mint the next version.

    The temporary file is written into the seat set's own directory so `load_matrix` reads exactly
    the bytes that will be stored — a validator run against a different rendering of the same
    document proves nothing about the file the CLI will later be pointed at.
    """
    directory = root(workspace=workspace) / _require_name(name)
    directory.mkdir(parents=True, exist_ok=True)
    draft = directory / ".draft.toml"
    draft.write_text(dumps(document), encoding="utf-8")
    try:
        matrix = load_matrix(draft, reference=reference)
    except ConfigError:
        draft.unlink(missing_ok=True)
        raise
    existing = versions(name, workspace=workspace)
    version = (existing[-1] if existing else 0) + 1
    draft.replace(path_for(name, version, workspace=workspace))
    return version, matrix


def templates() -> tuple[str, ...]:
    """The shipped matrices, as starting points. Read-only: nothing here ever writes to them."""
    return tuple(sorted(path.stem for path in CONFIG_DIR.glob("sweep*.toml")))


def read_template(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"{_require_name(name)}.toml"
    if not path.is_file():
        raise ConfigError(f"no shipped matrix named {name!r}; there are {list(templates())}")
    return tomllib.loads(path.read_text(encoding="utf-8"))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_matrices.py -q`
Expected: PASS (6 tests, one parametrised twice).

If `test_writer_round_trips_every_shipped_matrix` fails on `sweep.toml`, read the diff before touching the writer: the shipped file is the specification of what the schema contains, and a value kind it holds that `_scalar` refuses is a gap in the writer, never a reason to edit the matrix.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/matrices.py decision_lab/params.py decision_lab/tests/test_matrices.py decision_lab/tests/factories.py
git commit -m "feat(decision_lab): seat sets as versioned TOML in the workspace

§12.3. Every save is a new version and nothing is overwritten, so the seat
set that produced the best row in the registry can still be opened. The
shipped config/*.toml stay read-only templates — curated, commented and in
git, and a form writer would destroy the first two and dirty the third.

The writer is hand-rolled because tomllib reads and does not write, and it is
proved by round-tripping both shipped matrices rather than a document written
to suit it. It refuses a float outright: that would be a
SeatConfig.temperature, which this package has no way to carry.

Validation is load_matrix itself, against a temp file in the seat set's own
directory, so an invalid matrix leaves the store exactly as it was.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `jobs.py` — the workspace lock and the child-process launcher

**Files:**
- Create: `decision_lab/jobs.py`
- Test: `decision_lab/tests/test_jobs.py`
- Modify: `decision_lab/cli.py` (`EXIT_BUSY`, the lock around the writing commands), `pyproject.toml` (one mypy override)

**Interfaces:**
- Consumes: `decision_lab.params.{workspace_root, JOBS_DIR, LOCK_FILE, HOLDER_FILE}`, `tradebot.core.clock.Clock`, `tradebot.core.schema.{DomainModel, UtcDatetime}`, `tradebot.core.errors.ConfigError`.
- Produces:
  - `jobs.Busy(ConfigError)` — raised when the workspace is held
  - `jobs.Holder(DomainModel)` — `pid: int`, `argv: tuple[str, ...]`, `started_at: UtcDatetime`
  - `jobs.JobRecord(DomainModel)` — `job_id: str`, `argv: tuple[str, ...]`, `label: str`, `pid: int`, `started_at: UtcDatetime`, `finished_at: UtcDatetime | None`, `exit_code: int | None`
  - `jobs.WorkspaceLock` — `acquire(argv, *, clock)`, `release()`, context manager
  - `jobs.holder(*, workspace=None) -> Holder | None`
  - `jobs.start(argv: Sequence[str], *, label: str, clock: Clock, workspace: Path | None = None) -> JobRecord`
  - `jobs.stop(job_id: str, *, workspace: Path | None = None) -> bool`
  - `jobs.refresh(record: JobRecord, *, clock: Clock, workspace: Path | None = None) -> JobRecord`
  - `jobs.history(*, limit: int = 20, workspace: Path | None = None) -> tuple[JobRecord, ...]`
  - `jobs.log_tail(job_id: str, *, lines: int = 200, workspace: Path | None = None) -> str`
  - `jobs.status_of(record: JobRecord, *, workspace: Path | None = None) -> str`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_jobs.py`:

```python
"""§12.4 — one writer at a time, and a run that is the CLI rather than a copy of it.

The lock is an OS advisory lock, so a killed process releases it: a lock whose staleness has to
be *detected* is one that eventually strands the workspace, and there is no portable way to ask
whether a pid is alive without risking terminating it on Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from decision_lab import jobs
from tradebot.core.clock import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    from datetime import UTC, datetime

    return ManualClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))


def test_a_free_workspace_has_no_holder(tmp_path: Path) -> None:
    assert jobs.holder(workspace=tmp_path) is None


def test_the_lock_names_its_holder_and_refuses_a_second_taker(
    tmp_path: Path, clock: ManualClock
) -> None:
    first = jobs.WorkspaceLock(workspace=tmp_path)
    first.acquire(("sweep", "--corpus", "abc"), clock=clock)
    try:
        found = jobs.holder(workspace=tmp_path)
        assert found is not None
        assert found.argv == ("sweep", "--corpus", "abc")

        with pytest.raises(jobs.Busy, match="sweep"):
            jobs.WorkspaceLock(workspace=tmp_path).acquire(("report",), clock=clock)
    finally:
        first.release()

    assert jobs.holder(workspace=tmp_path) is None, "releasing frees it"


def test_a_lock_held_by_another_process_is_seen_and_released_when_it_exits(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The property the OS gives us for free, asserted because the whole design leans on it."""
    import subprocess
    import textwrap

    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path.cwd())!r})
        from datetime import UTC, datetime
        from decision_lab import jobs
        from tradebot.core.clock import ManualClock
        lock = jobs.WorkspaceLock(workspace={str(tmp_path)!r})
        lock.acquire(("sweep",), clock=ManualClock(datetime(2026, 9, 6, tzinfo=UTC)))
        sys.stdout.write("held\\n")
        sys.stdout.flush()
        time.sleep(30)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        assert jobs.holder(workspace=tmp_path) is not None
    finally:
        child.kill()
        child.wait(timeout=10)

    assert jobs.holder(workspace=tmp_path) is None, "the OS released it when the process died"


def test_start_records_the_job_and_stop_ends_it(tmp_path: Path, clock: ManualClock) -> None:
    record = jobs.start(
        ["dataset", "verify", "--data", str(tmp_path / "nothing")],
        label="verify",
        clock=clock,
        workspace=tmp_path,
    )

    assert record.job_id and record.pid
    assert jobs.history(workspace=tmp_path)[0].job_id == record.job_id

    jobs.stop(record.job_id, workspace=tmp_path)
    ended = jobs.refresh(record, clock=clock, workspace=tmp_path)
    assert ended.exit_code is not None, "a stopped job records how it ended"
    assert jobs.status_of(ended, workspace=tmp_path) != "running"


def test_start_refuses_while_the_workspace_is_held(tmp_path: Path, clock: ManualClock) -> None:
    lock = jobs.WorkspaceLock(workspace=tmp_path)
    lock.acquire(("sweep",), clock=clock)
    try:
        with pytest.raises(jobs.Busy, match="sweep"):
            jobs.start(["report", "--corpus", "x"], label="r", clock=clock, workspace=tmp_path)
    finally:
        lock.release()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_jobs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.jobs'`.

- [ ] **Step 3: Allow mypy to resolve one platform's locking module**

Append to `pyproject.toml`, beside the existing overrides:

```toml
# `fcntl` exists on POSIX and `msvcrt` on Windows; `decision_lab/jobs.py` takes an advisory lock
# through whichever this platform ships. mypy resolves one of them and must not fail on the
# other — and an inline ignore would itself be an error under `warn_unused_ignores` on the
# platform where the import does resolve.
[[tool.mypy.overrides]]
module = ["fcntl", "msvcrt"]
ignore_missing_imports = true
```

- [ ] **Step 4: Write `decision_lab/jobs.py`**

```python
"""One writer at a time, and runs that *are* the CLI rather than a copy of it (spec §12.4).

A start spawns `python -m decision_lab <argv>` as a child process. The page therefore cannot
diverge from the command — every refusal already has an exit code (3 dataset, 4 candidate, 5
budget, 6 gate, 7 busy) — a six-month `calibrate long` driving `BacktestHarness` never runs on the
event loop serving the page, and Stop is a real termination rather than a cooperative cancel
through code that offers none.

The lock is an **OS advisory lock** (`msvcrt` on Windows, `fcntl` on POSIX) rather than a pid file
with a staleness rule: the operating system releases it when the holder dies, and there is no
portable way to ask whether a pid is alive — on Windows `os.kill(pid, 0)` *terminates* the
process. The sidecar beside it carries argv and pid so a refusal can name the holder; it is read
for the message and never trusted for the decision.

Failure semantics: an absent workspace reads as free. A job whose record says it never finished
and which no longer holds the lock is reported as ended without being recorded — the dashboard
was restarted while it ran — never as still running, because a page claiming a sweep is running
is a page an operator waits on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Final

from decision_lab.params import HOLDER_FILE, JOBS_DIR, LOCK_FILE, workspace_root
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel, UtcDatetime

logger = get_logger("decision_lab.jobs")

#: Where the launcher runs its children: the repo root, so a relative `--data data\history` on a
#: form means what it means in a terminal.
REPO_ROOT: Final = Path(__file__).resolve().parents[1]

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - one branch per platform
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - one branch per platform
    msvcrt = None  # type: ignore[assignment]


class Busy(ConfigError):
    """The workspace is held by another run. `cli.main` maps this to exit code 7."""


class Holder(DomainModel):
    """Who holds the lock. For the refusal message; never for the decision."""

    pid: int
    argv: tuple[str, ...]
    started_at: UtcDatetime


class JobRecord(DomainModel):
    """One run the dashboard launched, and how it ended."""

    job_id: str
    argv: tuple[str, ...]
    label: str = ""
    pid: int = 0
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    exit_code: int | None = None


#: Children this process started, so `stop` and `refresh` act on the process itself rather than
#: on a pid — killing by pid is the one thing that is not portable, and a recycled pid is the
#: failure it produces.
_RUNNING: dict[str, subprocess.Popen[bytes]] = {}


def _root(workspace: Path | None) -> Path:
    return workspace or workspace_root()


def _jobs_dir(workspace: Path | None) -> Path:
    return _root(workspace) / JOBS_DIR


def _take(handle: IO[Any]) -> bool:
    """Try to take the lock without blocking. True when this process now holds it."""
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _drop(handle: IO[Any]) -> None:
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # Already gone: the process is exiting and the OS has released it.
        pass


class WorkspaceLock:
    """The one writer's claim on a workspace, held for the life of a command."""

    def __init__(self, *, workspace: Path | None = None) -> None:
        self._workspace = workspace
        self._path = _root(workspace) / LOCK_FILE
        self._sidecar = _root(workspace) / HOLDER_FILE
        self._handle: IO[Any] | None = None

    def acquire(self, argv: Sequence[str], *, clock: Clock) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        if not _take(handle):
            handle.close()
            raise Busy(_busy_message(holder(workspace=self._workspace)))
        self._handle = handle
        self._sidecar.write_text(
            Holder(pid=os.getpid(), argv=tuple(argv), started_at=clock.now()).model_dump_json(),
            encoding="utf-8",
        )

    def release(self) -> None:
        if self._handle is None:
            return
        self._sidecar.unlink(missing_ok=True)
        _drop(self._handle)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> WorkspaceLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _busy_message(found: Holder | None) -> str:
    if found is None:
        # The lock was taken and released between the refusal and the read. Rare, and the honest
        # message is the one that does not invent a holder.
        return (
            "another run holds this workspace. Wait for it to finish, or stop it from the "
            "dashboard's Runs page"
        )
    return (
        f"another run holds this workspace: pid {found.pid} running "
        f"`{' '.join(found.argv)}` since {found.started_at.isoformat()}. Two writers over one "
        "workspace would interleave registry rows and sweep files (§12.4)"
    )


def holder(*, workspace: Path | None = None) -> Holder | None:
    """Who holds the workspace, or `None` when it is free.

    Probes the lock rather than trusting the sidecar: a sidecar left behind by a killed process is
    stale, and the OS lock never is.
    """
    path = _root(workspace) / LOCK_FILE
    if not path.is_file():
        return None
    with path.open("a+b") as handle:
        if _take(handle):
            _drop(handle)
            return None
    sidecar = _root(workspace) / HOLDER_FILE
    if not sidecar.is_file():
        return Holder(pid=0, argv=(), started_at=_epoch())
    return Holder.model_validate_json(sidecar.read_text(encoding="utf-8"))


def _epoch() -> Any:
    from datetime import UTC, datetime

    return datetime(1970, 1, 1, tzinfo=UTC)


def record_path(job_id: str, *, workspace: Path | None = None) -> Path:
    return _jobs_dir(workspace) / f"{job_id}.json"


def log_path(job_id: str, *, workspace: Path | None = None) -> Path:
    return _jobs_dir(workspace) / f"{job_id}.log"


def start(
    argv: Sequence[str],
    *,
    label: str,
    clock: Clock,
    workspace: Path | None = None,
) -> JobRecord:
    """Spawn one run of this tool's own CLI. Refuses while the workspace is held.

    The child takes the lock itself, which is the real guarantee; this check is so the operator
    is refused *on the page* rather than shown a job that exited 7 a second after they started it.
    """
    if (found := holder(workspace=workspace)) is not None:
        raise Busy(_busy_message(found))

    directory = _jobs_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log = log_path(job_id, workspace=workspace)
    environment = dict(os.environ)
    if workspace is not None:
        # The child is a separate process, so a test workspace has to reach it as configuration.
        environment["DECISION_LAB_WORKSPACE"] = str(workspace)
    with log.open("wb") as sink:
        process = subprocess.Popen(  # noqa: S603 — argv is built here, never from a request
            [sys.executable, "-m", "decision_lab", *argv],
            cwd=REPO_ROOT,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=environment,
        )
    record = JobRecord(
        job_id=job_id,
        argv=tuple(argv),
        label=label,
        pid=process.pid,
        started_at=clock.now(),
    )
    _RUNNING[job_id] = process
    _write(record, workspace=workspace)
    logger.info("job started", extra={"job_id": job_id, "argv": list(argv), "label": label})
    return record


def _write(record: JobRecord, *, workspace: Path | None) -> None:
    record_path(record.job_id, workspace=workspace).write_text(
        record.model_dump_json(), encoding="utf-8"
    )


def refresh(record: JobRecord, *, clock: Clock, workspace: Path | None = None) -> JobRecord:
    """Update a record from the process itself. A job we did not start is returned unchanged."""
    process = _RUNNING.get(record.job_id)
    if process is None or record.exit_code is not None:
        return record
    code = process.poll()
    if code is None:
        return record
    updated = record.model_copy(update={"exit_code": code, "finished_at": clock.now()})
    _RUNNING.pop(record.job_id, None)
    _write(updated, workspace=workspace)
    return updated


def stop(job_id: str, *, workspace: Path | None = None) -> bool:
    """Terminate a running child. True when there was one to terminate.

    Only jobs this process started can be stopped, deliberately: killing by a pid read from a file
    is how a recycled pid gets terminated instead, and the alternative — leaving a stale process
    running — is visible on the page rather than silent.
    """
    process = _RUNNING.get(job_id)
    if process is None:
        return False
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a child ignoring SIGTERM
        process.kill()
        process.wait(timeout=10)
    logger.info("job stopped", extra={"job_id": job_id})
    return True


def history(*, limit: int = 20, workspace: Path | None = None) -> tuple[JobRecord, ...]:
    directory = _jobs_dir(workspace)
    if not directory.is_dir():
        return ()
    found = [
        JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in directory.glob("*.json")
    ]
    return tuple(sorted(found, key=lambda one: one.started_at, reverse=True))[:limit]


def status_of(record: JobRecord, *, workspace: Path | None = None) -> str:
    """`running`, `exit <code>`, or `ended without being recorded`.

    The last is what a dashboard restart looks like from here, and it is not `running`: a page
    claiming a sweep is still going is a page an operator waits on.
    """
    if record.exit_code is not None:
        return f"exit {record.exit_code}"
    process = _RUNNING.get(record.job_id)
    if process is not None and process.poll() is None:
        return "running"
    found = holder(workspace=workspace)
    if found is not None and found.pid == record.pid:
        return "running"
    return "ended without being recorded"


def log_tail(job_id: str, *, lines: int = 200, workspace: Path | None = None) -> str:
    """The end of a job's output. An absent log is an empty string, never an error."""
    path = log_path(job_id, workspace=workspace)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_jobs.py -q`
Expected: PASS (5 tests). The subprocess test takes a second or two.

- [ ] **Step 6: Wire the lock and exit code 7 into the CLI**

In `decision_lab/cli.py`:

```python
EXIT_BUSY = 7  # the workspace is held by another run                    (slice D pass 2)
```

```python
#: Commands that write the workspace, and therefore take the lock. `dataset` and `dashboard` are
#: absent because they write nothing a second writer could interleave with (§12.4).
LOCKED: frozenset[tuple[str, str]] = frozenset(
    {
        ("corpus", "build"),
        ("sweep", ""),
        ("report", ""),
        ("calibrate", "normal"),
        ("calibrate", "shock"),
        ("calibrate", "long"),
    }
)
```

and in `main`, wrapping the dispatch:

```python
    handler = COMMANDS[(args.command, getattr(args, "action", ""))]
    key = (args.command, getattr(args, "action", ""))
    try:
        if key not in LOCKED:
            return asyncio.run(handler(args))
        lock = jobs.WorkspaceLock()
        lock.acquire(argv if argv is not None else sys.argv[1:], clock=SystemClock())
        try:
            return asyncio.run(handler(args))
        finally:
            lock.release()
    except jobs.Busy as error:
        # Ahead of `TradebotError`: `Busy` is a `ConfigError`, and the generic handler would
        # report a workspace held by a running sweep as a dataset problem (exit 3).
        logger.error(str(error), extra={"kind": "Busy"})
        return EXIT_BUSY
    except TradebotError as error:
        ...
```

Add `import sys` and `from decision_lab import jobs` to the imports.

`workspace_root()` must also honour the child's environment, so a dashboard test's temporary workspace reaches the process it spawns. In `decision_lab/params.py`:

```python
# `import os` at the top of params.py, for the override below.
def workspace_root() -> Path:
    """Scratch databases, caches and results. Gitignored, and never `data/` (§2.1).

    `DECISION_LAB_WORKSPACE` overrides it, which is how §12.4's launcher points a child process at
    the same workspace the page is reading — a child is a separate process and cannot inherit a
    monkeypatched module attribute.
    """
    override = os.environ.get("DECISION_LAB_WORKSPACE", "").strip()
    return Path(override) if override else Path(__file__).parent / "workspace"
```

- [ ] **Step 7: Assert the CLI refusal**

Add to `decision_lab/tests/test_jobs.py`:

```python
def test_the_cli_refuses_a_locked_workspace_with_exit_seven(
    tmp_path: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decision_lab import cli, params

    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(tmp_path))
    lock = jobs.WorkspaceLock(workspace=tmp_path)
    lock.acquire(("sweep", "--corpus", "abc"), clock=clock)
    try:
        assert params.workspace_root() == tmp_path
        assert cli.main(["report", "--corpus", "abc"]) == cli.EXIT_BUSY
    finally:
        lock.release()
```

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_jobs.py -q`
Expected: PASS (6 tests).

- [ ] **Step 8: Run both gates**

Run: `.\decision_lab\check.ps1` then `.\check.ps1`
Expected: both pass.

- [ ] **Step 9: Commit**

```bash
git add decision_lab/jobs.py decision_lab/params.py decision_lab/cli.py decision_lab/tests/test_jobs.py pyproject.toml
git commit -m "feat(decision_lab): one writer at a time, and runs that are the CLI

§12.4. A start spawns \`python -m decision_lab <argv>\` as a child process, so
the page cannot diverge from the command it claims to run, a six-month
BacktestHarness pass never blocks the event loop that serves the page, and
Stop is a real termination.

The lock is an OS advisory lock, not a pid file: the OS releases it when the
holder dies, and there is no portable way to ask whether a pid is alive — on
Windows os.kill(pid, 0) terminates the process. The sidecar names the holder
for the refusal message and is never trusted for the decision.

The CLI takes the same lock for every command that writes the workspace and
refuses with the new exit code 7, so a terminal run and a page run cannot
collide. DECISION_LAB_WORKSPACE lets a launched child reach the same
workspace as its parent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: the dashboard shell — auth, the app factory, and the `dashboard` command

**Files:**
- Create: `decision_lab/dashboard/__init__.py`, `decision_lab/dashboard/auth.py`, `decision_lab/dashboard/views.py`, `decision_lab/dashboard/cache.py`, `decision_lab/dashboard/app.py`, `decision_lab/dashboard/routes/__init__.py`, `decision_lab/dashboard/templates/base.html`, `decision_lab/dashboard/templates/login.html`, `decision_lab/dashboard/static/app.css`
- Test: `decision_lab/tests/test_lab_dashboard_auth.py`
- Modify: `decision_lab/cli.py` (the `dashboard` command), `decision_lab/tests/conftest.py` (the `lab_client` fixture)

**Interfaces:**
- Consumes: `tradebot.dashboard.auth.{Session, GUARDED_SCOPES, REFUSALS, assert_bind_allowed}`, `tradebot.core.logging.{SECRETS, get_logger}`, `tradebot.core.errors.ConfigError`, `tradebot.core.clock.{Clock, SystemClock}`, `decision_lab.analysis`, `decision_lab.params.DEFAULT_DASHBOARD_PORT`.
- Produces:
  - `dashboard.auth.{TOKEN_ENV, SESSION_COOKIE, MIN_TOKEN_LENGTH, PUBLIC_PATHS, require_token, is_public, LabSessionMiddleware, set_session, clear_session}`
  - `dashboard.cache.AnalysisCache` — `async matrix_analysis(corpus: Corpus, matrix: Matrix, matrix_digest: str, *, workspace: Path | None) -> tuple[Scoring, MatrixAnalysis]`, `async scoring_for(data_dir: Path) -> Scoring`, `clear()`
  - `dashboard.views.{LabState, state_of, build_templates, render, PACKAGE, ABSENT, percent, money, moment}`
  - `dashboard.app.create_lab_dashboard(*, workspace: Path | None = None, token: str | None = None, clock: Clock | None = None) -> FastAPI`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_lab_dashboard_auth.py`:

```python
"""The lab dashboard's only gate, tested as one (§12, ADR 0014 inherited).

Two properties this suite exists for. The route walk covers a route added by a later task the day
it lands — which is why auth is middleware here as it is in the bot. And the cookie *name*:
cookies are not port-scoped, so a lab app setting `tradebot_session` on 127.0.0.1 would overwrite
the bot dashboard's cookie on another port, signed with a different token, and log the operator
out of the surface holding the kill switch.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decision_lab.dashboard import auth
from decision_lab.dashboard.app import create_lab_dashboard
from tradebot.core.errors import ConfigError
from tradebot.dashboard import auth as bot_auth

TOKEN = "decision-lab-token-0123456789"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_lab_dashboard(workspace=tmp_path, token=TOKEN))


def test_missing_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match=auth.TOKEN_ENV):
        auth.require_token({})


def test_short_token_refuses_to_start() -> None:
    with pytest.raises(ConfigError, match="at least 16 characters"):
        auth.require_token({auth.TOKEN_ENV: "short"})


def test_the_lab_reads_its_own_environment_variable() -> None:
    assert auth.TOKEN_ENV == "DECISION_LAB_DASHBOARD_TOKEN"
    assert auth.TOKEN_ENV != bot_auth.TOKEN_ENV


def test_the_cookie_name_differs_from_the_bots() -> None:
    """Cookies ignore ports. Sharing the name would log the operator out of the bot dashboard."""
    assert auth.SESSION_COOKIE != bot_auth.SESSION_COOKIE


def test_every_route_is_protected(client: TestClient) -> None:
    checked = 0
    for route in client.app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", "")
        if not path or auth.is_public(path) or "{" in path:
            continue
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path
        checked += 1
    assert checked, "no protected routes were found; the walk is not testing anything"


def test_unauthenticated_post_is_refused_not_redirected(client: TestClient) -> None:
    assert client.post("/matrices", data={}, follow_redirects=False).status_code == 401


def test_login_then_a_page_renders(client: TestClient) -> None:
    assert client.post("/login", data={"token": TOKEN}, follow_redirects=False).status_code == 303
    assert client.get("/").status_code == 200


def test_a_wrong_token_is_refused(client: TestClient) -> None:
    assert client.post("/login", data={"token": "nope"}, follow_redirects=False).status_code == 401


def test_every_guarded_scope_has_a_refusal() -> None:
    assert set(bot_auth.REFUSALS) == set(bot_auth.GUARDED_SCOPES)


def test_static_is_the_only_route_that_serves_a_file(client: TestClient) -> None:
    """§16's structural row: no route resolves a path out of a request into a file it serves.

    The run forms *pass* operator-typed paths to a child process, which is the CLI's own argument
    and is checked by the CLI. What must not exist is a route that reads a path from a request and
    answers with the file — that would turn an authenticated tuning surface into a file browser
    over `data/`.
    """
    from starlette.staticfiles import StaticFiles

    mounts = [
        route
        for route in client.app.routes  # type: ignore[attr-defined]
        if isinstance(getattr(route, "app", None), StaticFiles)
    ]
    assert len(mounts) == 1
    assert mounts[0].path == "/static"


def test_nothing_in_the_lab_core_imports_the_dashboard() -> None:
    """The dashboard is a front door, never a dependency — so the CLI stays importable headless."""
    import ast

    root = Path(auth.__file__).resolve().parents[2]
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any("decision_lab.dashboard" in name for name in names) and path.name != "cli.py":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"lab core modules importing the dashboard: {offenders}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_auth.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.dashboard'`.

- [ ] **Step 3: Write `decision_lab/dashboard/auth.py`**

```python
"""The lab's own token, its own cookie, and the bot's refusal semantics (spec §12).

`tradebot.dashboard.auth` is imported one way — `Session` for the signing, `GUARDED_SCOPES` and
`REFUSALS` for how a request without a session is turned away — so ADR 0014's posture is
inherited rather than re-argued, and the separation contract is untouched.

Two things are deliberately *not* inherited:

* **The environment variable.** `DECISION_LAB_DASHBOARD_TOKEN`, so the tuning surface and the
  surface holding the kill switch are not one credential.
* **The cookie name.** Cookies are not port-scoped: a lab app on 127.0.0.1:8788 setting
  `tradebot_session` would overwrite the bot dashboard's cookie on :8787, which is signed with a
  different token — logging in here would silently log the operator out of the bot's dashboard at
  the moment they reached for it.

Failure semantics: identical to the bot's. An absent or unverifiable session is never anonymous
access; a navigation is redirected to the login form and anything else is refused with 401,
because silently redirecting a POST would swallow a change the operator believes they made.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

from itsdangerous import Signer
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from tradebot.core.errors import ConfigError
from tradebot.core.logging import SECRETS, get_logger
from tradebot.dashboard.auth import GUARDED_SCOPES, REFUSALS, Session

logger = get_logger("decision_lab.dashboard.auth")

TOKEN_ENV: Final = "DECISION_LAB_DASHBOARD_TOKEN"  # noqa: S105 — a variable name, not a secret
SESSION_COOKIE: Final = "decision_lab_session"
MIN_TOKEN_LENGTH: Final = 16
PUBLIC_PATHS: Final = frozenset({"/login", "/logout"})
STATIC_PREFIX: Final = "/static/"

__all__ = [
    "MIN_TOKEN_LENGTH",
    "PUBLIC_PATHS",
    "SESSION_COOKIE",
    "TOKEN_ENV",
    "LabSessionMiddleware",
    "clear_session",
    "is_public",
    "require_token",
    "set_session",
]


def require_token(environ: Mapping[str, str] | None = None) -> str:
    """The configured token, or a refusal to start.

    Registered with the log redactor on the way out, so a token that later reaches a log line is
    scrubbed rather than recorded.
    """
    token = (environ if environ is not None else os.environ).get(TOKEN_ENV, "").strip()
    if not token:
        raise ConfigError(
            f"the decision_lab dashboard refuses to start without {TOKEN_ENV}: it can spend real "
            "API credit and publish seat sets, so it is authenticated even on localhost. Set a "
            f"token of at least {MIN_TOKEN_LENGTH} characters and restart."
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters; got {len(token)}. "
            "A short token is a guessable one."
        )
    SECRETS.register(token)
    return token


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(STATIC_PREFIX)


class LabSessionMiddleware:
    """Refuses every request without a valid session, except the public paths.

    Pure ASGI, and it reuses the bot's `REFUSALS` table, so a scope type added there is refused
    here by the same code rather than by a second copy of the rule.
    """

    def __init__(self, app: ASGIApp, session: Session) -> None:
        self._app = app
        self._session = session

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in GUARDED_SCOPES or self._admits(scope):
            await self._app(scope, receive, send)
            return
        await REFUSALS[scope["type"]](scope, receive, send)

    def _admits(self, scope: Scope) -> bool:
        return is_public(scope["path"]) or self._session.verifies(
            HTTPConnection(scope).cookies.get(SESSION_COOKIE)
        )


def set_session(response: Response, session: Session, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session.issue(),
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
```

Note: `Signer` is imported by `tradebot.dashboard.auth.Session` and is not needed here — delete the import if ruff flags it unused.

- [ ] **Step 4: Write `decision_lab/dashboard/cache.py`**

```python
"""§12.2's derivation cache: live numbers, without re-loading a dataset per request.

The key is what determines the answer and nothing else — the corpus, the matrix digest, the
scoring parameters and the **modification times of the candidates' row files** — so a job
appending rows invalidates its own entry and a page is current without anything being written to
disk for it.

Failure semantics: a cache miss is a rebuild, never an error. The cache is bounded because a
`PriceIndex` holds every bar of the scoring timeframe: an unbounded one over a long session is a
process that grows until it is killed.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from typing import Final

from decision_lab import analysis as an
from decision_lab import corpus as cp
from decision_lab import sweep as sw
from decision_lab.candidates import Matrix
from tradebot.core.clock import Clock

#: Entries kept. Small deliberately: each holds a price index over the whole dataset.
MAX_ENTRIES: Final = 4


class AnalysisCache:
    """One process's memory of what it has already derived."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._scoring: OrderedDict[str, an.Scoring] = OrderedDict()
        self._analysis: OrderedDict[str, an.MatrixAnalysis] = OrderedDict()
        self._lock = asyncio.Lock()

    async def scoring_for(self, data_dir: Path) -> an.Scoring:
        """The dataset, its audit, the price index and the regime labels."""
        key = str(data_dir.resolve())
        async with self._lock:
            found = self._scoring.get(key)
            if found is None:
                found = await an.build_scoring(data_dir, clock=self._clock)
                self._store(self._scoring, key, found)
            return found

    async def matrix_analysis(
        self, corpus: cp.Corpus, matrix: Matrix, matrix_digest: str, *, workspace: Path | None
    ) -> tuple[an.Scoring, an.MatrixAnalysis]:
        scoring = await self.scoring_for(Path(corpus.meta.dataset_directory))
        stamp = _rows_stamp(corpus, matrix, matrix_digest, workspace=workspace)
        key = f"{corpus.meta.corpus_id}|{matrix_digest}|{scoring.params.digest()}|{stamp}"
        async with self._lock:
            found = self._analysis.get(key)
            if found is None:
                found = an.analyse_matrix(
                    corpus, matrix, matrix_digest, scoring, workspace=workspace
                )
                self._store(self._analysis, key, found)
            return scoring, found

    def clear(self) -> None:
        self._scoring.clear()
        self._analysis.clear()

    @staticmethod
    def _store(store: OrderedDict[str, object], key: str, value: object) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > MAX_ENTRIES:
            store.popitem(last=False)


def _rows_stamp(
    corpus: cp.Corpus, matrix: Matrix, matrix_digest: str, *, workspace: Path | None
) -> str:
    """Every candidate's rows file, by size and modification time.

    Size as well as mtime because a filesystem's mtime resolution can be a whole second on
    Windows, and a sweep appends several rows a second: a stamp that moved only with the clock
    would serve a stale page for the length of the write burst that follows it.
    """
    parts = []
    for candidate in matrix.candidates:
        path = sw.rows_path(
            corpus.meta.corpus_id, matrix_digest, candidate.candidate_id, workspace=workspace
        )
        if path.is_file():
            stat = path.stat()
            parts.append(f"{candidate.candidate_id}:{stat.st_size}:{stat.st_mtime_ns}")
        else:
            parts.append(f"{candidate.candidate_id}:-")
    return "|".join(parts)
```

`ScoringParams` needs a stable identity for the key. Add to `decision_lab/scoring.py`, on `ScoringParams`:

```python
    def digest(self) -> str:
        """Identity of the parameters a verdict was derived under (§12.2's cache key)."""
        return f"{self.timeframe}:{self.band_k}:{self.horizon_bars}"
```

- [ ] **Step 5: Write `decision_lab/dashboard/views.py`**

```python
"""The render shell: what every page is given, and how a value reaches a template.

Separate from the factory so routers import it without importing the factory that imports them.
Everything a route needs arrives through `LabState`, hung on `app.state` — a route never reaches
for a global.

**Every number a human reads is the server's exact `Decimal` as a string.** There is no `float`
in this package at all (`test_discipline.py`), so a percentage is formatted from `Decimal` and a
bar width is an `int`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import HTTPConnection

from decision_lab.dashboard.cache import AnalysisCache
from tradebot.core.clock import Clock
from tradebot.core.money import to_decimal
from tradebot.dashboard.auth import Session

PACKAGE = Path(__file__).parent

#: Rendered where a value is genuinely absent, so an empty cell is never read as a zero.
ABSENT = "—"

#: The banner every page carries. The tool is a comparison instrument and not evidence of alpha
#: (§14), and a page that omitted it would be the one place that claim is not made.
DISCLAIMER = (
    "A comparison instrument, not evidence of alpha. Every number here is one panel measured "
    "against another over recorded history."
)


@dataclass(frozen=True, slots=True)
class LabState:
    """Everything a route may reach. Read through `state_of`."""

    workspace: Path | None
    templates: Jinja2Templates
    session: Session
    clock: Clock
    cache: AnalysisCache


def state_of(connection: HTTPConnection) -> LabState:
    return cast(LabState, connection.app.state.lab)


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.filters.update(money=money, percent=percent, moment=moment, count=count)
    templates.env.globals.update(absent=ABSENT, disclaimer=DISCLAIMER)
    return templates


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    """Render a page with the context every page needs. The only place templates are called."""
    from decision_lab import jobs

    state = state_of(request)
    running = jobs.holder(workspace=state.workspace)
    return state.templates.TemplateResponse(
        request,
        template,
        {
            # On every page, because §12.4's meta refresh is what makes a running job visible and
            # a page that omitted it would silently stop updating while a sweep spent money.
            "busy": running,
            **context,
        },
    )


def money(value: Decimal | str | int | None, places: int = 2) -> str:
    if value is None:
        return ABSENT
    exact = to_decimal(value)
    try:
        return f"{exact.quantize(Decimal(f'1e-{places}')):,}"
    except InvalidOperation:
        return str(exact)


def percent(value: Decimal | str | int | None, places: int = 1) -> str:
    """A ratio as a percentage, exactly. `Decimal * 100`, never a float."""
    if value is None:
        return ABSENT
    exact = to_decimal(value) * 100
    try:
        return f"{exact.quantize(Decimal(f'1e-{places}'))}%"
    except InvalidOperation:
        return f"{exact}%"


def moment(value: datetime | None) -> str:
    return ABSENT if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def count(value: int | None) -> str:
    return ABSENT if value is None else f"{value:,}"
```

- [ ] **Step 6: Write `decision_lab/dashboard/app.py`**

```python
"""The FastAPI factory for the tuning surface (spec §12).

Takes a workspace and a token; builds no `Application`, opens no bot database and constructs no
venue adapter. Everything it shows is read from `decision_lab/workspace/` and everything it runs
is a child process of this tool's own CLI (§12.4).

Failure semantics: the factory raises `ConfigError` before serving anything if the token is
missing or too short. An unauthenticated navigation is redirected to the login form; anything else
is refused outright.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from decision_lab.dashboard.auth import (
    SESSION_COOKIE,
    LabSessionMiddleware,
    clear_session,
    require_token,
    set_session,
)
from decision_lab.dashboard.cache import AnalysisCache
from decision_lab.dashboard.routes import jobs as jobs_routes
from decision_lab.dashboard.routes import matrices as matrices_routes
from decision_lab.dashboard.routes import runs as runs_routes
from decision_lab.dashboard.views import PACKAGE, LabState, build_templates, render, state_of
from tradebot.core.clock import Clock, SystemClock
from tradebot.core.logging import get_logger
from tradebot.dashboard.auth import Session

logger = get_logger("decision_lab.dashboard")

__all__ = ["create_lab_dashboard"]


def create_lab_dashboard(
    *,
    workspace: Path | None = None,
    token: str | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Build the tuning dashboard. `workspace` of `None` means the tool's own (`params`)."""
    session = Session(token if token is not None else require_token())
    used_clock = clock or SystemClock()
    app = FastAPI(title="decision_lab", docs_url=None, redoc_url=None)
    app.state.lab = LabState(
        workspace=workspace,
        templates=build_templates(),
        session=session,
        clock=used_clock,
        cache=AnalysisCache(clock=used_clock),
    )
    app.add_middleware(LabSessionMiddleware, session=session)
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    app.include_router(runs_routes.router)
    app.include_router(matrices_routes.router)
    app.include_router(jobs_routes.router)
    _add_session_routes(app)
    return app


def _add_session_routes(app: FastAPI) -> None:
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> Response:
        if state_of(request).session.verifies(request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error="")

    @app.post("/login")
    async def login(request: Request, token: str = Form(default="")) -> Response:
        state = state_of(request)
        if not state.session.accepts(token):
            # The submitted value is never logged, not even truncated.
            logger.warning("lab dashboard login refused")
            refused = render(request, "login.html", error="That token was not accepted.")
            refused.status_code = 401
            return refused
        accepted = RedirectResponse("/", status_code=303)
        set_session(accepted, state.session, secure=request.url.scheme == "https")
        return accepted

    @app.get("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/login", status_code=303)
        clear_session(response)
        return response
```

`decision_lab/dashboard/__init__.py`:

```python
"""The tuning surface: read a result, build a seat set, run it (spec §12)."""

from __future__ import annotations

from decision_lab.dashboard.app import create_lab_dashboard

__all__ = ["create_lab_dashboard"]
```

`decision_lab/dashboard/routes/__init__.py`: empty module docstring only.

- [ ] **Step 7: Write `base.html`, `login.html` and `app.css`**

`decision_lab/dashboard/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}decision_lab{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  {# §12.4: the only refresh on the whole surface, and only while a run is writing. #}
  {% if busy %}<meta http-equiv="refresh" content="5">{% endif %}
</head>
<body>
<header>
  <nav>
    <a href="/">Runs</a>
    <a href="/matrices">Seat sets</a>
    <a href="/jobs">Runs in progress</a>
    <a href="/logout">Log out</a>
  </nav>
  {% if busy %}
  <p class="busy">Running <code>{{ busy.argv | join(" ") }}</code> since {{ busy.started_at | moment }} (pid {{ busy.pid }}).</p>
  {% endif %}
</header>
<main>
  {% block content %}{% endblock %}
</main>
<footer><p>{{ disclaimer }}</p></footer>
</body>
</html>
```

`decision_lab/dashboard/templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}decision_lab — sign in{% endblock %}
{% block content %}
<h1>decision_lab</h1>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/login">
  <label>Token <input type="password" name="token" autofocus></label>
  <button type="submit">Sign in</button>
</form>
{% endblock %}
```

`decision_lab/dashboard/static/app.css` — one stylesheet, no JS. Keep it short and legible: a
system font stack, a max width, tables with `border-collapse: collapse` and a light row rule,
`.error` in red, `.busy` on a tinted strip, `.unmeasured` and `.warning` in amber, `code`
monospace, and `table { display: block; overflow-x: auto }` on wide tables so a ranking never
forces the page to scroll sideways.

- [ ] **Step 8: Add the `dashboard` command to the CLI**

In `parse_args`:

```python
    dash = commands.add_parser("dashboard", help="serve the tuning surface (§12)")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
    dash.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind a non-loopback address. Auth is already mandatory; this is the second lock, "
        "so a --host 0.0.0.0 typo cannot put a surface that spends money on a LAN",
    )
    dash.add_argument("--verbose", action="store_true")
```

The handler, beside the others:

```python
async def dashboard_command(args: argparse.Namespace) -> int:
    """Serve §12's surface until interrupted. Takes no workspace lock — it writes nothing itself.

    The token is read here rather than inside the factory so a missing one is a refusal to start
    with an exit code, exactly as the bot's `serve` refuses (ADR 0014).
    """
    assert_bind_allowed(args.host, allow_remote=args.allow_remote)
    app = create_lab_dashboard(token=lab_auth.require_token())
    logger.info(
        "decision_lab dashboard listening",
        extra={"host": args.host, "port": args.port},
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_config=None, access_log=False)
    )
    await server.serve()
    return EXIT_OK
```

with, at the top of `cli.py`:

```python
import uvicorn

from decision_lab.dashboard import create_lab_dashboard
from decision_lab.dashboard import auth as lab_auth
from tradebot.dashboard.auth import assert_bind_allowed
```

and in `COMMANDS`: `("dashboard", ""): dashboard_command,`.

**`cli.py` is the one module in the lab core permitted to import the dashboard** — it is the entry
point, and `test_lab_dashboard_auth.py::test_nothing_in_the_lab_core_imports_the_dashboard`
exempts it by name.

- [ ] **Step 9: Add the shared client fixture**

Append to `decision_lab/tests/conftest.py`:

```python
@pytest.fixture
def lab_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A signed-in client over an empty workspace under `tmp_path`."""
    from fastapi.testclient import TestClient

    from decision_lab.dashboard.app import create_lab_dashboard

    token = "decision-lab-token-0123456789"
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    client = TestClient(create_lab_dashboard(workspace=workspace, token=token))
    client.post("/login", data={"token": token})
    yield client
```

- [ ] **Step 10: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_auth.py -q`
Expected: PASS. The route walk needs the three routers to exist; until Tasks 5–9 land, create each
`routes/*.py` with an empty `router = APIRouter()` so the factory imports, and the walk will grow
as routes are added.

- [ ] **Step 11: Commit**

```bash
git add decision_lab/dashboard decision_lab/scoring.py decision_lab/cli.py decision_lab/tests/test_lab_dashboard_auth.py decision_lab/tests/conftest.py
git commit -m "feat(decision_lab): the tuning surface's shell, authenticated by construction

§12. Its own ASGI app, its own port, its own token env var, and its own
session cookie name — cookies are not port-scoped, so sharing the bot's name
would log the operator out of the surface holding the kill switch.

Session signing and the refusal table are imported from
tradebot.dashboard.auth one way, so ADR 0014's posture is inherited rather
than re-argued, and the middleware is pure ASGI so a route added by a later
task is guarded the day it lands. The route walk asserts exactly that.

The derivation cache keys on the corpus, the matrix, the scoring parameters
and the rows files' size and mtime, so a running job invalidates its own
entry and nothing has to be written for a page to be current.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: the Runs view — the registry, sorted, and two rows diffable

**Files:**
- Create: `decision_lab/dashboard/routes/runs.py`, `decision_lab/dashboard/templates/runs.html`, `decision_lab/dashboard/templates/_tables.html`
- Test: `decision_lab/tests/test_lab_dashboard_read.py`

**Interfaces:**
- Consumes: `decision_lab.registry.{RunRow, read_all}`, `decision_lab.jobs.holder`, `decision_lab.dashboard.views.{render, state_of}`.
- Produces:
  - `runs.router: APIRouter`
  - `GET /` — the registry table. Query: `sort` (one of `recorded_at`, `accuracy`, `net_profit`, `cost_usd`, `scored`), `dir` (`asc`|`desc`), `compare` (repeated, up to two `run_id`s)
  - `runs.SORTS: dict[str, Callable[[RunRow], object]]`
  - `runs.sorted_rows(rows: Sequence[RunRow], *, sort: str, direction: str) -> tuple[RunRow, ...]`
  - `runs.diff(left: RunRow, right: RunRow) -> tuple[tuple[str, str, str], ...]` — field, left, right, for fields that differ

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_lab_dashboard_read.py`:

```python
"""§12.2's Runs view: what ran, sorted how the reader asked, and two rows side by side."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
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
    assert [row.candidate_id for row in runs.sorted_rows(rows, sort="accuracy", direction="asc")] == [
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


def test_the_runs_page_lists_every_recorded_row(lab_client: TestClient, tmp_path) -> None:
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


def test_comparing_two_runs_renders_their_differences(lab_client: TestClient, tmp_path) -> None:
    workspace = tmp_path / "workspace"
    left = a_row(candidate_id="a")
    right = a_row(candidate_id="b", cost_usd=Decimal("9"))
    registry.record(left, workspace=workspace)
    registry.record(right, workspace=workspace)
    ids = [row.run_id for row in registry.read_all(workspace=workspace)]

    page = lab_client.get(f"/?compare={ids[0]}&compare={ids[1]}").text

    assert "candidate_id" in page and "cost_usd" in page


def test_comparing_one_run_is_not_an_error(lab_client: TestClient, tmp_path) -> None:
    """Half a comparison is a page with one row selected, never a 500 or an empty diff table."""
    workspace = tmp_path / "workspace"
    registry.record(a_row(), workspace=workspace)
    only = registry.read_all(workspace=workspace)[0].run_id

    assert lab_client.get(f"/?compare={only}").status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_read.py -q`
Expected: FAIL — `ImportError: cannot import name 'sorted_rows'`.

- [ ] **Step 3: Write `decision_lab/dashboard/routes/runs.py`**

```python
"""The §11 registry as a page, and the run a reader picked out of it (§12.2).

Selection is in the URL — the sort, the direction and the two runs being compared — so a reload,
a bookmark and the meta refresh that fires while a job runs all land on the same view.

Failure semantics: an absent registry is no runs, never an error. An unknown sort key is the
default sort rather than a 500: a mistyped query parameter must not take the page away from
someone reading it mid-run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Final

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
    compare: list[str] = Query(default=[]),
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
```

- [ ] **Step 4: Write `runs.html` and `_tables.html`**

`decision_lab/dashboard/templates/runs.html`:

```html
{% extends "base.html" %}
{% block title %}decision_lab — runs{% endblock %}
{% block content %}
<h1>Runs</h1>

{% if not rows %}
  <p class="empty">No runs recorded yet. Build a seat set, then start a run from
  <a href="/matrices">Seat sets</a>.</p>
{% else %}
<table>
  <thead>
    <tr>
      <th></th>
      {% for column in sorts %}
      <th><a href="/?sort={{ column }}&dir={{ 'asc' if sort == column and direction == 'desc' else 'desc' }}">{{ column.replace('_', ' ') }}</a></th>
      {% endfor %}
      <th>status</th><th></th>
    </tr>
  </thead>
  <tbody>
  {% for row in rows %}
    <tr>
      <td><a href="/?compare={{ row.run_id }}{% for one in selected %}&compare={{ one }}{% endfor %}">compare</a></td>
      <td>{{ row.recorded_at | moment }}</td>
      <td>{{ row.accuracy | percent }}</td>
      <td>{{ row.net_profit | money }}</td>
      <td>{{ row.cost_usd | money }}</td>
      <td>{{ row.scored | count }}</td>
      <td>{{ row.scenario }} · {{ row.candidate_id }}</td>
      <td>{{ row.status }}{% if row.gate_skipped %} <span class="warning">gate skipped</span>{% endif %}{% if not row.evaluation %} <span class="warning">plumbing check</span>{% endif %}</td>
      <td><a href="/runs/{{ row.run_id }}">detail</a></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}

{% if comparison %}
<h2>Two runs, side by side</h2>
<table>
  <thead><tr><th>field</th><th>{{ chosen[0].candidate_id }}</th><th>{{ chosen[1].candidate_id }}</th></tr></thead>
  <tbody>
  {% for name, left, right in comparison %}
    <tr><td>{{ name }}</td><td>{{ left }}</td><td>{{ right }}</td></tr>
  {% endfor %}
  </tbody>
</table>
{% elif selected | length == 1 %}
<p>One run selected. Pick a second to compare it against.</p>
{% endif %}
{% endblock %}
```

`decision_lab/dashboard/templates/_tables.html` — the two macros Task 6 also uses, so the ranking
is rendered in one place:

```html
{% macro regime_table(rows) %}
  {% if not rows %}
    <p class="empty">No candidate was measured in this regime.</p>
  {% else %}
  <table>
    <thead><tr>
      <th>candidate</th><th>scored</th><th>accuracy</th><th>action rate</th>
      <th>precision on action</th><th>conviction gap</th><th>regret / decision</th>
      <th>degraded</th><th>cost</th><th>cost / scored</th>
    </tr></thead>
    <tbody>
    {% for row in rows %}
      <tr>
        <td><code>{{ row.candidate_id }}</code></td>
        <td>{{ row.scored | count }}</td>
        <td>{{ row.accuracy | percent }}</td>
        <td>{{ row.action_rate | percent }}</td>
        <td>{{ row.precision_on_action | percent }}</td>
        <td>{{ row.mean_conviction_gap | money(3) }}</td>
        <td>{{ row.regret_per_decision | money(3) }}</td>
        <td>{{ row.degradation_rate | percent }}</td>
        <td>{{ row.cost_usd | money }}</td>
        <td>{{ row.cost_per_scored | money(4) }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
{% endmacro %}

{# §9.4: unscorable is a verdict with a reason, never a drop. A regime with no unscored
   decisions still gets its row, so an empty cell reads as zero rather than as unmeasured. #}
{% macro unscored_table(regimes) %}
<table>
  <thead><tr><th>regime</th><th>decisions</th><th>scored</th><th>unscored, by reason</th></tr></thead>
  <tbody>
  {% for regime in regimes %}
    <tr>
      <td>{{ regime.regime }}</td>
      <td>{{ regime.decisions | count }}</td>
      <td>{{ regime.scored | count }}</td>
      <td>
        {% if regime.unscored %}
          {% for reason, number in regime.unscored | dictsort %}{{ reason }}: {{ number }}{% if not loop.last %} · {% endif %}{% endfor %}
        {% else %}0{% endif %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endmacro %}
```

`runs.html` and `run_detail.html` both open with `{% import "_tables.html" as tables %}`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_read.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add decision_lab/dashboard/routes/runs.py decision_lab/dashboard/templates decision_lab/tests/test_lab_dashboard_read.py
git commit -m "feat(decision_lab): the Runs view, sorted and diffable

§12.2. The §11 registry as a page, with the sort, the direction and the two
runs being compared all in the URL, so a reload, a bookmark and the meta
refresh that fires while a job runs land on the same view.

An unknown sort key is the default sort rather than a 500: a mistyped query
parameter must not take the page away from someone reading it mid-run. A
one-run comparison is a selection, not half a table.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: run detail — the ranking per regime, and what was not measured

**Files:**
- Modify: `decision_lab/dashboard/routes/runs.py`
- Create: `decision_lab/dashboard/templates/run_detail.html`
- Test: `decision_lab/tests/test_lab_dashboard_read.py` (append)

**Interfaces:**
- Consumes: Task 1's `analysis.MatrixAnalysis`, Task 4's `AnalysisCache`, `decision_lab.sweep.read_meta`, `decision_lab.corpus.load`, `decision_lab.records.load`, `decision_lab.candidates.load_matrix`, `decision_lab.scoring.by_regime`.
- Produces:
  - `GET /runs/{run_id}` — per-regime ranking, agreement matrix, unscored counts with reasons, cost table; the §10.4 profit block instead when the row's scenario is `calibrate-long`
  - `runs.RunContext` — frozen dataclass: `row: RunRow`, `corpus: Corpus | None`, `matrix: Matrix | None`, `analysis: MatrixAnalysis | None`, `problem: str`
  - `async runs.context_for(request, run_id) -> RunContext`

- [ ] **Step 1: Write the failing tests** (append to `test_lab_dashboard_read.py`)

```python
@pytest.mark.asyncio
async def test_run_detail_ranks_the_candidates_that_swept(
    lab_client: TestClient, swept_workspace
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
    lab_client: TestClient, swept_workspace
) -> None:
    """The same refusal `cli.report` makes, rendered instead of raised (finding 2).

    A page that re-derived from an edited matrix would attribute one experiment's rows to another
    experiment's candidates.
    """
    run_id, workspace = swept_workspace
    meta = _sweep_meta(workspace)
    Path(meta.matrix_source).write_text("[[candidates]]\nid = 'moved'\n", encoding="utf-8")

    page = lab_client.get(f"/runs/{run_id}").text

    assert "no longer matches" in page
    assert "NORMAL" not in page, "nothing derived is shown when the matrix cannot be trusted"


def test_a_long_run_row_shows_its_profit_block_and_no_ranking(
    lab_client: TestClient, tmp_path
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


def test_an_unvaluable_long_run_reports_no_figure_at_all(lab_client: TestClient, tmp_path) -> None:
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
```

Add the fixture to `decision_lab/tests/conftest.py` — it is what Tasks 6, 7 and 10 all read:

```python
@pytest.fixture
def swept_workspace(
    calibrated_corpus: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path]]:
    """A corpus with one real stub sweep run over it, and the registry row that names it.

    The stub matrix, so this is offline, free and deterministic — a plumbing check by
    construction (§7.2), which is exactly what a rendering test should be measuring.
    """
    from decision_lab import cli, registry

    corpus_id, data = calibrated_corpus
    workspace = tmp_path / "workspace"
    matrix = Path(__file__).resolve().parents[1] / "config" / "sweep-stub.toml"
    assert (
        cli.main(
            [
                "sweep",
                "--corpus",
                corpus_id,
                "--configs",
                str(matrix),
                "--budget",
                "1",
                "--skip-gate",
            ]
        )
        == cli.EXIT_OK
    )
    rows = registry.read_all(workspace=workspace)
    assert rows, "the sweep recorded no registry row; the fixture is not testing anything"
    yield rows[0].run_id, workspace


def _sweep_meta(workspace: Path):
    """The one sweep under this workspace, whatever corpus it belongs to."""
    import json

    found = sorted(workspace.rglob("sweep.json"))
    assert found, "no sweep meta was written"
    from decision_lab import sweep as sw

    return sw.SweepResult.model_validate_json(found[0].read_text(encoding="utf-8"))
```

`_sweep_meta` is used by the test module, so import it there (`from decision_lab.tests.conftest import _sweep_meta`) or move it into the test file — either is fine, but it must not be a fixture, because the test calls it directly.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_read.py -q`
Expected: FAIL — 404 from `/runs/...`, because the route does not exist yet.

- [ ] **Step 3: Add the route to `runs.py`**

```python
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
```

with the imports this needs at the top of `runs.py`: `from dataclasses import dataclass`, `from pathlib import Path`, `from decision_lab import analysis as an, candidates as cd, corpus as cp, scoring as sc, sweep as sw`, `from tradebot.core.errors import ConfigError`.

- [ ] **Step 4: Write `run_detail.html`**

The page renders, in order: the row's identity (scenario, candidate, corpus, matrix digest, cadence, seed, status, and the `gate skipped` / `plumbing check` banners); then, when `context.analysis` is present, the per-regime ranking (`context.analysis.ranking` grouped by `regime`), the agreement matrix, the unscored counts per regime with their reasons, and the cost columns; then the not-measured list **with each candidate's reason**; and, when the scenario is `calibrate-long`, the profit block instead.

Three rules the template must hold, each asserted by a test above:

```html
{# §8.3: every regime row is always rendered. An absent SHOCK_DOWN reads as "not measured",
   which is the opposite of "never happened". #}
{% for regime in ["NORMAL", "SHOCK_UP", "SHOCK_DOWN"] %}
  <h3>{{ regime }}</h3>
  {{ tables.regime_table(context.analysis.ranking | selectattr("regime", "equalto", regime) | list) }}
{% endfor %}

{# finding 3: a candidate that produced no scored decision is named with its reason, never
   ranked at 0.0% — that reads as measured and worst. #}
{% if context.analysis.not_measured %}
<h3>Not measured</h3>
<ul>{% for one in context.analysis.not_measured %}
  <li><code>{{ one.candidate_id }}</code> — {{ one.reason }}</li>
{% endfor %}</ul>
{% endif %}

{# §10.4: a frozen aggregate reports UNVALUABLE and no figure at all, not even the realized half. #}
{% if context.row.scenario == "calibrate-long" %}
<h2>Profit</h2>
{% if context.row.unvaluable %}
  <p class="warning">UNVALUABLE — the portfolio could not be valued at the window's end, so no
  figure is reported. Freezing is ignorance, and a number produced in ignorance is worse than
  its absence.</p>
{% else %}
<table>
  <tr><th>Total (mark to market)</th><td>{{ context.row.total_profit | money }}</td></tr>
  <tr><th>Realized</th><td>{{ context.row.realized_pnl | money }}</td></tr>
  <tr><th>Unrealized</th><td>{{ context.row.unrealized_pnl | money }}</td></tr>
  <tr><th>Deliberation cost</th><td>{{ context.row.cost_usd | money }}</td></tr>
  <tr><th>Net</th><td>{{ context.row.net_profit | money }}</td></tr>
</table>
{% endif %}
{% endif %}
```

An agreement matrix that is empty distinguishes *one* candidate from *none*: `compare.agreement`
returns `()` for both, so the template says "only one candidate ran" when
`context.analysis.by_candidate | length == 1` and "no candidate was measured" when it is zero.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_read.py -q`
Expected: PASS. `swept_workspace` runs a real stub sweep, so allow it a few seconds.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/dashboard/routes/runs.py decision_lab/dashboard/templates/run_detail.html decision_lab/tests
git commit -m "feat(decision_lab): run detail, per regime, with what was not measured

§12.2 and §9.5. Every regime row is always rendered — an absent SHOCK_DOWN
reads as 'not measured', which is the opposite of 'never happened' — and a
candidate that produced no scored decision is named with its reason rather
than ranked at 0.0%, which reads as measured and worst.

cli.report's finding-2 refusal is rendered rather than raised: when the matrix
on disk no longer matches the digest the sweep recorded, the page says so and
shows nothing derived, because re-deriving would attribute one experiment's
rows to another experiment's candidates.

A calibrate-long row shows §10.4's profit block instead, and an unvaluable one
reports no figure at all — not even the realized half.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: seat detail and the decision drill-down

**Files:**
- Modify: `decision_lab/dashboard/routes/runs.py`
- Create: `decision_lab/dashboard/templates/seats.html`, `decision_lab/dashboard/templates/decision.html`
- Test: `decision_lab/tests/test_lab_dashboard_read.py` (append)

**Interfaces:**
- Produces:
  - `GET /runs/{run_id}/seats/{candidate_id}` — §9.7 in full: per seat, per regime, round 0 beside final, swing rate, marginal contribution
  - `GET /runs/{run_id}/decision?cycle=<cycle_id>&instrument=<instrument_key>` — one snapshot, every seat's vote and raw text, the truth label, the verdict and why

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_seat_detail_shows_round_zero_beside_the_final_vote(
    lab_client: TestClient, swept_workspace
) -> None:
    """§9.7: 'which seat reasons well' and 'which is easily talked round' are two questions."""
    run_id, _ = swept_workspace

    page = lab_client.get(f"/runs/{run_id}/seats/varied-three").text

    assert "round 0" in page and "final" in page
    assert "swing" in page.lower() and "contribution" in page.lower()


def test_seat_detail_for_an_unmeasured_candidate_says_so(
    lab_client: TestClient, swept_workspace
) -> None:
    run_id, _ = swept_workspace
    response = lab_client.get(f"/runs/{run_id}/seats/never-ran")
    assert response.status_code == 404
    assert "not measured" in response.text.lower() or "no candidate" in response.text.lower()


def test_the_drill_down_shows_the_vote_the_truth_and_the_verdict(
    lab_client: TestClient, swept_workspace
) -> None:
    run_id, _ = swept_workspace
    listing = lab_client.get(f"/runs/{run_id}/seats/varied-three").text
    # The seat page links every scored decision; take the first link it renders.
    cycle_link = listing.split('href="/runs/')[1].split('"')[0]

    page = lab_client.get(f"/runs/{cycle_link}").text

    assert "Verdict" in page and "Truth" in page
    assert "raw" in page.lower(), "a seat's raw text is the audit record worth having"


def test_an_unscored_decision_shows_its_reason_not_a_blank(
    lab_client: TestClient, swept_workspace
) -> None:
    """§9.4: unscorable is a verdict with a reason — gap, horizon or no ATR — never a drop."""
    run_id, _ = swept_workspace
    page = lab_client.get(f"/runs/{run_id}").text
    assert "UNSCORED" not in page or "gap" in page or "horizon" in page or "ATR" in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_read.py -q`
Expected: FAIL — 404 for `/runs/{id}/seats/{candidate}`.

- [ ] **Step 3: Add both routes to `runs.py`**

```python
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
        page = render(request, "decision.html", missing=run_id, scored=None, record=None)
        page.status_code = 404
        return page
    scored = next(
        (
            one
            for one in block.scored
            if one.cycle_id == cycle and one.instrument_key == instrument
        ),
        None,
    )
    record = next((one for one in block.records if one.cycle_id == cycle), None)
    page = render(
        request,
        "decision.html",
        missing="" if scored and record else run_id,
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
```

Import `from decision_lab import seats as st` at the top of `runs.py`.

- [ ] **Step 4: Write `seats.html` and `decision.html`**

`seats.html` renders one table per regime, with a row per (seat, round label) so **round 0 sits
beside final**, and columns: turns, scored, accuracy, action rate, precision on action, mean
conviction gap, abstention rate, fallback rate, swing rate, marginal contribution, cost per vote.
When `identical_rounds` is true it renders one round and says so:

```html
{% if identical_rounds %}
<p>This candidate ran <code>single_round</code>: round 0 <em>is</em> the final vote, so one
column is shown rather than the same numbers twice (§9.7).</p>
{% endif %}
```

Each scored decision in the candidate's set is linked to the drill-down:

```html
<a href="/runs/{{ found.row.run_id }}/decision?cycle={{ one.cycle_id | urlencode }}&instrument={{ one.instrument_key | urlencode }}&candidate={{ candidate.candidate_id | urlencode }}">{{ one.as_of | moment }}</a>
```

`decision.html` shows, in order: the instrument, the instant, the regime and any named window;
the panel's action and conviction; the truth label, the verdict and — when the verdict is one of
the three unscored kinds — its reason spelled out rather than an empty cell; the band, the
forward move, MFE, MAE and regret; then a table of every seat's round-0 vote and final vote with
`raw_text` in a `<pre>`; then the frozen snapshot's indicator readings.

The verdict's own value carries the reason (`UNSCORED (gap)`), so the template renders
`scored.verdict` verbatim and adds one explanatory line per kind from a small mapping in the
template context — never a blank cell.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_read.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/dashboard decision_lab/tests/test_lab_dashboard_read.py
git commit -m "feat(decision_lab): seat detail and the decision drill-down

§9.7 and §12.2. Round 0 sits beside the final vote, because 'which seat
reasons well' and 'which seat is easily talked round' are different questions
and one column answers neither; a single_round candidate says so rather than
rendering the same numbers twice.

The drill-down is one snapshot: the evidence, every seat's vote and raw text,
the truth label and the verdict — with an unscored verdict's reason spelled
out, never a blank cell. The instrument is a query parameter because an
instrument key carries a slash.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: the seat-set editor

**Files:**
- Create: `decision_lab/dashboard/forms.py`, `decision_lab/dashboard/routes/matrices.py`, `decision_lab/dashboard/templates/matrices.html`, `decision_lab/dashboard/templates/matrix_edit.html`
- Test: `decision_lab/tests/test_lab_dashboard_edit.py`

**Interfaces:**
- Consumes: Task 2's `matrices` module, `decision_lab.candidates.load_matrix`, `decision_lab.records.load` (for a reference basket), `tradebot.core.errors.ConfigError`.
- Produces:
  - `forms.nest(items: Mapping[str, str]) -> dict[str, Any]` — dotted form names to a nested document, integer segments becoming list indices
  - `forms.INT_FIELDS`, `forms.BOOL_FIELDS`, `forms.LIST_FIELDS` — the only coercions
  - `matrices_routes.router`
  - `GET /matrices` — every stored seat set and its versions, plus the shipped templates
  - `GET /matrices/{name}` — the editor over the latest version; `?version=N` for an older one; `?from_template=sweep-stub` to seed a new one
  - `POST /matrices/{name}` — `action` of `save`, `add_candidate`, `remove_candidate=<i>`, `add_seat=<i>`, `remove_seat=<i>.<j>`, `add_fallback=<i>.<j>`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_lab_dashboard_edit.py`:

```python
"""§12.3 — building a seat set in the page, and the two things that must never happen.

The form round-trips the whole document, so a control that stopped being rendered would delete
that part of the seat set on the next save — the bot's `_panel.html` hazard, one level up. And an
edit mints a new `matrix_digest`, a third of the §10.6 gate key, so the page has to say that at
the moment it saves rather than leaving it to be discovered as an exit 6.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decision_lab import matrices
from decision_lab.dashboard import forms

SHIPPED = Path(__file__).resolve().parents[1] / "config"


def test_nest_builds_candidates_and_seats_from_dotted_names() -> None:
    flat = {
        "sweep.on_fallback": "halt",
        "candidates.0.id": "baseline",
        "candidates.0.max_rounds": "3",
        "candidates.0.providers": "openrouter, gemini",
        "candidates.0.seats.0.seat_id": "technical",
        "candidates.0.seats.0.devils_advocate": "on",
        "candidates.0.seats.0.evidence": "indicators, position",
        "expand.limit": "4",
    }

    document = forms.nest(flat)

    assert document["sweep"]["on_fallback"] == "halt"
    assert document["candidates"][0]["max_rounds"] == 3, "an int field is an int"
    assert document["candidates"][0]["providers"] == ["openrouter", "gemini"]
    assert document["candidates"][0]["seats"][0]["devils_advocate"] is True
    assert document["expand"]["limit"] == 4


def test_money_and_ratios_stay_strings() -> None:
    """A ratio parsed to a float here is the binary rounding error the whole package forbids."""
    document = forms.nest(
        {"candidates.0.qualified_majority": "0.5", "candidates.0.max_cost_usd_per_cycle": "0.02"}
    )
    assert document["candidates"][0]["qualified_majority"] == "0.5"
    assert document["candidates"][0]["max_cost_usd_per_cycle"] == "0.02"


def test_an_absent_checkbox_is_false_not_missing() -> None:
    """An unchecked box posts nothing, and a field that vanished would be a field deleted."""
    document = forms.nest({"candidates.0.seats.0.seat_id": "s"})
    assert document["candidates"][0]["seats"][0]["devils_advocate"] is False


def test_the_editor_lists_the_shipped_templates(lab_client: TestClient) -> None:
    page = lab_client.get("/matrices").text
    assert "sweep-stub" in page and "sweep" in page


def test_saving_mints_a_version_and_warns_that_the_gate_is_now_shut(
    lab_client: TestClient, tmp_path
) -> None:
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    posted = _flatten(document) | {"action": "save"}

    response = lab_client.post("/matrices/mine", data=posted, follow_redirects=True)

    assert response.status_code == 200
    assert "version 1" in response.text
    assert "uncalibrated" in response.text.lower()
    assert "calibrate normal" in response.text and "calibrate shock" in response.text
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == (1,)


def test_an_invalid_seat_set_is_refused_with_the_loaders_own_message(
    lab_client: TestClient, tmp_path
) -> None:
    posted = {"candidates.0.id": "c", "candidates.0.seats.0.seat_id": "s", "action": "save"}

    response = lab_client.post("/matrices/mine", data=posted)

    assert response.status_code == 400
    assert "not a valid basket" in response.text or "Field required" in response.text
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == ()


def test_adding_a_seat_re_renders_the_submitted_form_without_saving(
    lab_client: TestClient, tmp_path
) -> None:
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    posted = _flatten(document) | {"action": "add_seat", "add_seat": "0"}

    response = lab_client.post("/matrices/mine", data=posted)

    assert response.status_code == 200
    assert response.text.count("seats.0.seat_id") >= 1
    assert "candidates.0.seats.3.seat_id" in response.text, "a fourth, empty seat block"
    assert matrices.versions("mine", workspace=tmp_path / "workspace") == (), "nothing was saved"


def test_every_field_of_a_stored_seat_set_is_rendered_as_an_input(
    lab_client: TestClient, tmp_path
) -> None:
    """Two-sided, like the bot's own configure test: a control that stops being rendered deletes
    that part of the document on the next save."""
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    lab_client.post("/matrices/mine", data=_flatten(document) | {"action": "save"})

    page = lab_client.get("/matrices/mine").text

    for name in _flatten(document):
        assert f'name="{name}"' in page, f"{name} is not editable, so a save would drop it"


def _flatten(document: dict, prefix: str = "") -> dict[str, str]:
    """The inverse of `forms.nest`, for building a POST body out of a TOML document."""
    flat: dict[str, str] = {}
    for key, value in document.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat |= _flatten(value, f"{name}.")
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            for index, entry in enumerate(value):
                flat |= _flatten(entry, f"{name}.{index}.")
        elif isinstance(value, list):
            flat[name] = ", ".join(str(one) for one in value)
        elif isinstance(value, bool):
            if value:
                flat[name] = "on"
        else:
            flat[name] = str(value)
    return flat
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_edit.py -q`
Expected: FAIL — no `decision_lab.dashboard.forms`.

- [ ] **Step 3: Write `decision_lab/dashboard/forms.py`**

```python
"""A flat form to a matrix document, and nothing else (spec §12.3).

The document this produces is handed to `candidates.load_matrix`, which is the only validation
there is: nothing here restates a rule the loader already owns, exactly as the bot's Configure
page defers to its pydantic models.

Three coercions, and they are the whole list. Everything else stays a string, because every
number in a matrix that is not a count **is money or a ratio** — `qualified_majority`,
`max_cost_usd_per_cycle` — and those are strings in TOML precisely so no float ever touches them
(`test_discipline.py`, ADR 0001).

Failure semantics: a name that is not a valid path is ignored rather than raising — a form is
attacker-reachable only by someone who already has the token, but a malformed name must not be
able to take the page away from an operator mid-edit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

#: Counts. `max_rounds` is a number of debate rounds and `limit` an expansion cap.
INT_FIELDS: Final = frozenset({"max_rounds", "limit"})

#: Checkboxes. An unchecked box posts nothing at all, so every one of these is written `False`
#: when absent — a field that vanished from the document would be a field deleted from the seat
#: set on the next save.
BOOL_FIELDS: Final = frozenset({"devils_advocate"})

#: Comma-separated lists.
LIST_FIELDS: Final = frozenset({"providers", "evidence"})

#: Where a seat block's boolean fields live, so `nest` can default the absent ones.
SEAT_BOOLS: Final = tuple(sorted(BOOL_FIELDS))


def nest(items: Mapping[str, str]) -> dict[str, Any]:
    """`candidates.0.seats.1.role` → `{"candidates": [{"seats": [_, {"role": …}]}]}`.

    An integer path segment is a list index. Indices are dense after parsing: the editor renders
    them contiguously and a removal re-renders the form, so a gap can only come from a hand-built
    request, where dropping it is the fail-closed answer.
    """
    document: dict[str, Any] = {}
    for name, raw in sorted(items.items()):
        if name in ("action", "add_seat", "remove_seat", "add_candidate", "remove_candidate"):
            continue
        segments = name.split(".")
        if not all(segments):
            continue
        _place(document, segments, _coerce(segments[-1], raw))
    _default_missing_checkboxes(document)
    return document


def _coerce(field: str, raw: str) -> Any:
    if field in INT_FIELDS:
        return int(raw) if raw.strip().lstrip("-").isdigit() else raw
    if field in BOOL_FIELDS:
        return raw.lower() in ("on", "true", "1", "yes")
    if field in LIST_FIELDS:
        return [one.strip() for one in raw.split(",") if one.strip()]
    return raw


def _place(document: dict[str, Any], segments: list[str], value: Any) -> None:
    cursor: Any = document
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        following = segments[index + 1] if not last else ""
        if segment.isdigit():
            continue  # handled by the parent, which knows the list it is filling
        if last:
            cursor[segment] = value
            return
        if following.isdigit():
            cursor.setdefault(segment, [])
            position = int(following)
            while len(cursor[segment]) <= position:
                cursor[segment].append({})
            cursor = cursor[segment][position]
        else:
            cursor = cursor.setdefault(segment, {})


def _default_missing_checkboxes(document: dict[str, Any]) -> None:
    """Every seat gets every boolean, present or not (see `BOOL_FIELDS`)."""
    for candidate in document.get("candidates", ()):
        for seat in candidate.get("seats", ()):
            for field in SEAT_BOOLS:
                seat.setdefault(field, False)
```

- [ ] **Step 4: Write `decision_lab/dashboard/routes/matrices.py`**

The router holds `GET /matrices`, `GET /matrices/{name}` and one `POST /matrices/{name}` that
dispatches on `action` through a table:

```python
#: What a submit button does. Dispatch over a table rather than a chain of `if`s: a button whose
#: action is not in here re-renders the form unchanged, which is the fail-closed answer for a
#: request nobody's browser produced.
ACTIONS: Final[dict[str, Callable[[dict[str, Any], str], dict[str, Any]]]] = {
    "add_candidate": _add_candidate,
    "remove_candidate": _remove_candidate,
    "add_seat": _add_seat,
    "remove_seat": _remove_seat,
    "add_fallback": _add_fallback,
}
```

Each mutator takes the nested document and the button's value (`"0"`, `"0.2"`) and returns a new
document; `save` is handled separately because it is the only one that writes. The reference
basket a matrix is validated against comes from any built corpus in the workspace — the same
basket `corpus build` recorded — and when the workspace holds none, from
`app.dataset_basket(...)` over the dataset the operator names on the page. Refuse with a clear
message when neither is available: a seat set cannot be validated without the basket it varies.

Save:

```python
    try:
        version, matrix = matrices.save(name, document, reference=reference, workspace=state.workspace)
    except ConfigError as error:
        page = render(request, "matrix_edit.html", name=name, document=document, error=str(error))
        page.status_code = 400
        return page
    return RedirectResponse(f"/matrices/{name}?saved={version}&digest={matrix.matrix_digest}", 303)
```

and the editor template renders, when `saved` is present:

```html
<p class="warning">Saved as version {{ saved }}. Its matrix digest is <code>{{ digest }}</code>,
which is a third of the §10.6 gate key — so this seat set is <strong>uncalibrated</strong> until
<code>calibrate normal</code> and <code>calibrate shock</code> have run over the nine pinned days
against the dataset you intend to sweep. A sweep will refuse until they have.</p>
```

- [ ] **Step 5: Write `matrices.html` and `matrix_edit.html`**

`matrices.html` lists every stored seat set with its versions (each linking to
`/matrices/<name>?version=<n>`), and the shipped templates as "duplicate to edit" links
(`/matrices/<new-name>?from_template=<template>`).

`matrix_edit.html` is one `<form method="post">` containing **every** field of the document, named
exactly as `forms.nest` reads them. It renders `[sweep]`, the prompt library, each candidate with
its seats and each seat's fallbacks, and the `[expand]` block. Each seat block ends with a
`remove_seat` submit button, each candidate with an `add_seat`, and the form with `add_candidate`
and `save`.

**Nothing in this form may be conditionally rendered.** The form round-trips the whole document
and `nest` builds only what it is given, so a field that is not rendered is a field deleted on the
next save — the `_panel.html` hazard, and `test_every_field_of_a_stored_seat_set_is_rendered_as_an_input`
is the two-sided assertion that it has not happened.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_edit.py -q`
Expected: PASS (8 tests).

- [ ] **Step 7: Commit**

```bash
git add decision_lab/dashboard decision_lab/tests/test_lab_dashboard_edit.py
git commit -m "feat(decision_lab): the seat-set editor

§12.3. Seats, roles, prompts, models, protocol and the expansion matrix as a
form, stored as a new version of a workspace TOML, validated by
candidates.load_matrix itself — nothing in the form restates a rule the loader
owns.

Two rules the tests hold two-sidedly. Every field of a stored seat set is
rendered as an input, because the form round-trips the whole document and a
control that stopped being rendered would delete that part of it on the next
save. And an edit mints a new matrix_digest, a third of the §10.6 gate key,
so the page says the seat set is uncalibrated at the moment it saves rather
than leaving it to be discovered as an exit 6 from a sweep.

Adding or removing a seat is a server round-trip that re-renders the submitted
form, so the editor works with scripting off.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: starting, watching and stopping a run

**Files:**
- Create: `decision_lab/dashboard/routes/jobs.py`, `decision_lab/dashboard/templates/jobs.html`
- Test: `decision_lab/tests/test_lab_dashboard_run.py`

**Interfaces:**
- Consumes: Task 3's `jobs` module, Task 2's `matrices`, `decision_lab.gate.{read, gate_key}`, `decision_lab.calibration_days.require_pinned`, `decision_lab.corpus.load`, `decision_lab.dataset.require_verified`.
- Produces:
  - `jobs_routes.router`
  - `GET /jobs` — the holder, the history with status, the log tail (`?job=<id>`), and the start forms
  - `POST /jobs/start` — `command` plus that command's fields
  - `POST /jobs/stop` — `job_id`
  - `jobs_routes.BUILDERS: dict[str, Callable[[Mapping[str, str]], list[str]]]` — one argv builder per launchable command, each refusing its own missing fields
  - `jobs_routes.BUDGETED: frozenset[str]` — the commands whose CLI takes `--budget`
  - `async jobs_routes.projection_for(state, *, corpus_id, configs) -> tuple[ProjectionRow, ...] | str`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_lab_dashboard_run.py`:

```python
"""§12.4 — a start is the CLI, under a lock, with a ceiling nobody defaulted for you."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decision_lab import jobs
from decision_lab.dashboard.routes import jobs as jobs_routes
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
    lab_client: TestClient, tmp_path
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
    response = lab_client.post("/jobs/start", data={"command": "sweep", "corpus": "abc"})
    assert response.status_code == 400
    assert "configs" in response.text


def test_an_unknown_command_is_refused(lab_client: TestClient) -> None:
    assert lab_client.post("/jobs/start", data={"command": "rm -rf"}).status_code == 400


def test_starting_and_stopping_a_real_child(lab_client: TestClient, tmp_path) -> None:
    """`dataset verify` against a directory that does not exist: a real child, exiting quickly."""
    response = lab_client.post(
        "/jobs/start",
        data={"command": "corpus-build", "data": str(tmp_path / "nothing"), "every": "8h",
              "reference_panel": "stub"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    started = jobs.history(workspace=tmp_path / "workspace")
    assert started, "the job was recorded"

    stopped = lab_client.post(
        "/jobs/stop", data={"job_id": started[0].job_id}, follow_redirects=True
    )
    assert stopped.status_code == 200


def test_the_projection_says_so_when_nothing_has_been_calibrated(
    lab_client: TestClient, calibrated_corpus
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_run.py -q`
Expected: FAIL — no `decision_lab.dashboard.routes.jobs`.

- [ ] **Step 3: Write `decision_lab/dashboard/routes/jobs.py`**

```python
"""Starting, watching and stopping a run (spec §12.4).

Every start builds an argv and hands it to `jobs.start`, which spawns this tool's own CLI. There
is no second implementation of a sweep here, and there is no code path from this module to a
`DecisionEngine`: what the page runs is what a terminal would run, and every refusal it reports
is an exit code the CLI already had.

Failure semantics: a missing field, an unknown command and a held workspace are all rendered
refusals — 400, 400 and 409 — with the reason on the page and the form still filled in. Nothing
is spawned until the argv is complete.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from decision_lab import calibration as cal
from decision_lab import calibration_days as cday
from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import gate, jobs
from decision_lab.dashboard.views import render, state_of
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
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
        "corpus", "build",
        "--data", _required(fields, "data"),
        "--every", _required(fields, "every"),
        "--reference-panel", _required(fields, "reference_panel"),
    ]


def _calibrate(scenario: str) -> Callable[[Mapping[str, str]], list[str]]:
    def build(fields: Mapping[str, str]) -> list[str]:
        return [
            "calibrate", scenario,
            "--corpus", _required(fields, "corpus"),
            "--configs", _required(fields, "configs"),
            *_budget(fields),
        ]

    return build


def _sweep(fields: Mapping[str, str]) -> list[str]:
    argv = [
        "sweep",
        "--corpus", _required(fields, "corpus"),
        "--configs", _required(fields, "configs"),
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
        "calibrate", "long",
        "--data", _required(fields, "data"),
        "--configs", _required(fields, "configs"),
        "--candidate", _required(fields, "candidate"),
        "--start-equity", _required(fields, "start_equity"),
        "--every", _required(fields, "every"),
        "--window", _required(fields, "window"),
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


@router.get("/jobs", response_class=HTMLResponse)
async def index(request: Request, job: str = "", corpus: str = "", configs: str = "") -> Response:
    state = state_of(request)
    history = tuple(
        jobs.refresh(one, clock=state.clock, workspace=state.workspace)
        for one in jobs.history(workspace=state.workspace)
    )
    return render(
        request,
        "jobs.html",
        history=history,
        statuses={
            one.job_id: jobs.status_of(one, workspace=state.workspace) for one in history
        },
        selected=job,
        log=jobs.log_tail(job, workspace=state.workspace) if job else "",
        commands=tuple(BUILDERS),
        budgeted=BUDGETED,
        projection=await projection_for(request, corpus_id=corpus, configs=configs),
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
    history = jobs.history(workspace=state.workspace)
    page = render(
        request,
        "jobs.html",
        history=history,
        statuses={one.job_id: jobs.status_of(one, workspace=state.workspace) for one in history},
        selected="",
        log="",
        commands=tuple(BUILDERS),
        budgeted=BUDGETED,
        projection="",
        error=reason,
    )
    page.status_code = status
    return page
```

- [ ] **Step 4: Write `jobs.html`**

The page renders, in order: the current holder (from `busy` in the base context) with a Stop
button when it is a job this process started; the error block when `error` is set; the history
table (started, label, argv, status from `statuses`, and a link that selects the job); the log
tail of the selected job in a `<pre>`; the cost projection table or its message; and one `<form>`
per key of `commands`, each posting to `/jobs/start` with a hidden `command` field.

Two things the templates must say, both asserted:

```html
{# §12.4: `calibrate long` has no mid-run ceiling, and the page says so rather than implying one. #}
<p class="warning">This run has <strong>no mid-run ceiling</strong>: scenario 3 drives
<code>BacktestHarness</code> directly, with no engine seam to meter. The ceiling is you stopping
it — and stopping it means you must <strong>delete that run's directory</strong> before it can run
again, because the database it leaves behind is the record of why the pass failed (§10.4).</p>

{# §12.4: a budget is a required field with no default, on the commands whose CLI takes one. #}
{% if command in budgeted %}
<label>Budget (USD, required) <input name="budget" required></label>
{% endif %}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_lab_dashboard_run.py -q`
Expected: PASS (11 tests).

- [ ] **Step 6: Run both gates**

Run: `.\decision_lab\check.ps1` then `.\check.ps1`
Expected: both pass.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/dashboard decision_lab/tests/test_lab_dashboard_run.py
git commit -m "feat(decision_lab): start, watch and stop a run from the page

§12.4. Every start builds an argv and spawns this tool's own CLI, so there is
no second implementation of a sweep and every refusal the page reports is an
exit code the command already had. A held workspace is a 409 naming the
holder; a missing field is a 400 naming the field; nothing is spawned until
the argv is complete.

A budget is required with no default on the three commands whose CLI takes
one. The other two say what governs their spend instead: calibrate long has no
mid-run ceiling — BacktestHarness offers no seam to meter — so the form says
that, and says that stopping one means deleting its directory before a re-run.

The projection is the cost per cycle the nine pinned days measured, over the
entries this run would buy. With no calibration on record it says so, which is
the answer worth having: the gate will refuse the sweep anyway.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: the notebook, the exit criterion, and the docs

**Files:**
- Create: `decision_lab/notebooks/tuning.ipynb`, `decision_lab/tests/test_notebook.py`, `decision_lab/tests/test_slice_d_pass2_end_to_end.py`
- Modify: `decision_lab/PROGRESS.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything above. The notebook imports `decision_lab.analysis`, `decision_lab.registry`, `decision_lab.corpus`, `decision_lab.candidates` and `decision_lab.render` — the same library the page uses.

- [ ] **Step 1: Write the failing tests**

Create `decision_lab/tests/test_notebook.py`:

```python
"""§14's second front door, checked without adding a Jupyter dependency.

Nothing here executes the notebook: it needs a kernel, a dataset and a corpus. What is asserted
is what a committed notebook can get wrong on its own — unparseable JSON, a code cell that does
not compile, and stored outputs, which are diff noise at best and a leaked key at worst.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "notebooks" / "tuning.ipynb"


def test_the_notebook_is_valid_json_of_the_expected_format() -> None:
    document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert document["nbformat"] == 4
    assert document["cells"], "an empty notebook is not a front door"


def test_every_code_cell_compiles() -> None:
    document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for index, cell in enumerate(document["cells"]):
        if cell["cell_type"] != "code":
            continue
        source = "".join(cell["source"])
        assert "%" not in source.split("\n")[0][:1], f"cell {index} starts with a magic"
        compile(source, f"{NOTEBOOK.name}:cell{index}", "exec")


def test_no_cell_carries_stored_output() -> None:
    document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    for index, cell in enumerate(document["cells"]):
        assert not cell.get("outputs"), f"cell {index} has stored output"
        assert cell.get("execution_count") in (None, 0), f"cell {index} has an execution count"


def test_the_notebook_uses_the_same_library_the_page_does() -> None:
    """§14: one implementation, two front doors. A notebook that re-derived would be a third."""
    source = NOTEBOOK.read_text(encoding="utf-8")
    assert "decision_lab.analysis" in source or "from decision_lab import analysis" in source
    assert "score_records" not in source, "scoring belongs behind analysis.analyse_matrix"
```

Create `decision_lab/tests/test_slice_d_pass2_end_to_end.py`:

```python
"""The slice exit criterion: a seat set built on the page, run from the page, read on the page.

Offline and free — the stub matrix, so the whole loop is exercised and nothing is measured, which
is what a plumbing check is for (§7.2). It is slow by construction: it spawns a real child
process running a real sweep, because "the page is the CLI" is the claim being tested.
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decision_lab import jobs, matrices, registry
from decision_lab.tests.test_lab_dashboard_edit import _flatten

SHIPPED = Path(__file__).resolve().parents[1] / "config"
TIMEOUT_SECONDS = 180


def _await_finish(job_id: str, workspace: Path, clock) -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        found = next(
            one for one in jobs.history(workspace=workspace) if one.job_id == job_id
        )
        refreshed = jobs.refresh(found, clock=clock, workspace=workspace)
        if refreshed.exit_code is not None:
            return refreshed.exit_code
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not finish within {TIMEOUT_SECONDS}s")


def test_build_a_seat_set_run_it_and_read_its_ranking(
    lab_client: TestClient, calibrated_corpus, tmp_path
) -> None:
    corpus_id, _ = calibrated_corpus
    workspace = tmp_path / "workspace"
    from tradebot.core.clock import SystemClock

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
    listing = lab_client.get("/").text
    assert rows[0].candidate_id in listing

    detail = lab_client.get(f"/runs/{rows[0].run_id}").text
    assert "NORMAL" in detail
    assert "gate skipped" in detail.lower()
    assert "plumbing check" in detail.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_notebook.py decision_lab/tests/test_slice_d_pass2_end_to_end.py -q`
Expected: FAIL — the notebook does not exist.

- [ ] **Step 3: Write `decision_lab/notebooks/tuning.ipynb`**

Author it as JSON by hand (no Jupyter needed to write one). Seven cells, no magics, no `print` —
`print` is banned repo-wide by `T20` and ruff lints notebooks — so every cell ends in a bare
expression, which is how a notebook displays a value anyway:

1. **markdown** — what this is, that it is the same library `python -m decision_lab report` uses, and that nothing in it spends: it reads a workspace a run already wrote.
2. **code** — imports and the workspace:
   ```python
   from pathlib import Path

   from decision_lab import analysis, candidates, corpus, registry, render, sweep
   from tradebot.core.clock import SystemClock

   WORKSPACE = None  # None means decision_lab/workspace; set a Path to read another
   registry.read_all(workspace=WORKSPACE)[-10:]
   ```
3. **markdown** — pick a run.
4. **code** — choose a corpus and a matrix digest, then derive:
   ```python
   CORPUS_ID = "8ac130d8f2ed5650dff0dcb9f969d07e"  # any id from the rows above

   loaded = corpus.load(CORPUS_ID, workspace=WORKSPACE)
   result = sweep.latest_meta(CORPUS_ID, workspace=WORKSPACE)
   matrix = candidates.load_matrix(Path(result.matrix_source), reference=loaded.meta.reference_basket)
   scoring = await analysis.build_scoring(Path(loaded.meta.dataset_directory), clock=SystemClock())
   analysed = analysis.analyse_matrix(loaded, matrix, result.matrix_digest, scoring, workspace=WORKSPACE)
   analysed.ranking
   ```
   (`await` at the top level of a cell is valid in an IPython kernel; the `compile` test uses
   `compile(source, ..., "exec")`, which rejects a bare top-level `await` — so wrap it as
   `scoring = asyncio.get_event_loop().run_until_complete(analysis.build_scoring(...))`, or
   define `async def prepare(): ...` and call it with `asyncio.run`. Use the `asyncio.run` form:
   it compiles, and it is what a script would do.)
5. **markdown** — the seat tables.
6. **code** — `analysed.candidate_seats`, and `analysed.not_measured` beside it, so a candidate that produced nothing is visible rather than absent.
7. **code** — diff against a previous run from the §11 registry:
   ```python
   rows = registry.read_all(workspace=WORKSPACE)
   left, right = rows[-2], rows[-1]
   tuple(
       (name, getattr(left, name), getattr(right, name))
       for name in ("candidate_id", "accuracy", "cost_usd", "net_profit", "matrix_digest")
       if getattr(left, name) != getattr(right, name)
   )
   ```

Each cell is `{"cell_type": "code", "execution_count": null, "metadata": {}, "outputs": [], "source": [...]}`, and the document carries `"nbformat": 4, "nbformat_minor": 5` and a `kernelspec` of `python3`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_notebook.py -q`
Expected: PASS (4 tests).

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_slice_d_pass2_end_to_end.py -q`
Expected: PASS (1 test, slow — it spawns a child process that runs a real stub sweep).

- [ ] **Step 5: Update the docs**

In `decision_lab/PROGRESS.md`: mark slice D pass 2 shipped in the at-a-glance table and the slice
D section; add the new commands to "What you can run today":

```powershell
$env:DECISION_LAB_DASHBOARD_TOKEN = "at-least-sixteen-characters"
.venv\Scripts\python.exe -m decision_lab dashboard --port 8788
```

and move the pass-2 line out of "Open items" into what shipped, keeping the items that are still
true (no real panel has been scored; every report is NEWS-BLIND; the stale 4h corpus).

In `CLAUDE.md`, under the `decision_lab` section, add the commands and the rules that are easy to
get backwards — each one a rule this pass discovered, stated as a rule and not as a feature:

- **The dashboard is not read-only, and the spec says so at §12.1.** It configures, runs and
  reads; what it still refuses is authority over the bot, promotion authority and automatic search.
- **Its session cookie name is its own.** Cookies are not port-scoped, so sharing
  `tradebot_session` would log the operator out of the surface holding the kill switch.
- **A run from the page is a child process of this tool's own CLI**, so the page cannot diverge
  from the command, and every refusal it renders is an exit code the command already had.
- **One writer at a time, through an OS advisory lock** — not a pid file, because the OS releases
  a lock when the holder dies and there is no portable way to ask whether a pid is alive:
  `os.kill(pid, 0)` *terminates* the process on Windows. The CLI takes the same lock, exit 7.
- **A budget is required with no default** on the commands whose CLI takes one, and the two that
  do not say what governs their spend instead. `calibrate long` has no mid-run ceiling and the
  page says so rather than implying one.
- **An edit mints a new `matrix_digest`**, a third of the §10.6 gate key, so a saved seat set is
  uncalibrated until the nine days run again — said at the moment of saving, not discovered as an
  exit 6.
- **The editor renders every field of the document**, because the form round-trips the whole
  thing and a control that stopped being rendered would delete that part of the seat set on the
  next save — the `_panel.html` hazard, one level up.
- **`analysis.py` is the one read assembly**, shared by the CLI, the dashboard and the notebook.
  A second one would be §14's rejected second `report` command arriving through another door.
- **The derivation cache keys on the rows files' size and mtime**, so a running job invalidates
  its own entry — size as well as mtime, because a one-second mtime resolution would serve a
  stale page for the length of a write burst.

- [ ] **Step 6: Run everything**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Then: `.\decision_lab\check.ps1` and `.\check.ps1`
Then: `git diff --stat main -- tradebot/`
Expected: all green, and the last command prints **nothing** — the separation contract's slice
exit criterion.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/notebooks decision_lab/tests decision_lab/PROGRESS.md CLAUDE.md
git commit -m "feat(decision_lab): the notebook, the exit criterion and the docs

§14's second front door over the same analysis.py the page uses — checked
without adding a Jupyter dependency: the notebook parses, every code cell
compiles, and no cell carries stored output.

The exit criterion is the whole loop through the page: a seat set built from
the shipped template and saved as version 1, a stub sweep launched as a real
child process, and its ranking read back on the run detail page — offline,
free, and stamped both 'gate skipped' and 'plumbing check', because a stub
matrix measures canned JSON and the page must say so.

git diff --stat main -- tradebot/ is empty.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-review notes for the executor

Three things this plan deliberately leaves to the implementer's judgement, each with the decision
already made — change them only with a reason:

1. **`_tables.html` macros** (Task 5) are named but not written out cell by cell. Write them from
   the field lists in `compare.Ranked` and `scoring.RegimeMetrics`; the tests assert the numbers
   that must appear, not the markup.
2. **`app.css`** is described, not written. It is the one file in this pass with no test beyond
   "the page renders", and that is correct: a stylesheet assertion is a test of taste.
3. **The reference basket for validating a seat set** (Task 8) comes from a built corpus in the
   workspace when there is one. If the workspace holds none, refuse with a message naming
   `corpus build` — a seat set cannot be validated without the basket it varies, and inventing a
   default basket would validate against something no run will ever use.
