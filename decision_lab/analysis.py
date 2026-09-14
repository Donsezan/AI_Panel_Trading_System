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


def not_measured_reason(rows: Mapping[str, sw.SweepRow], records: Sequence[CycleRecord]) -> str:
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
                seats=(st.score_seats(records, scored, panel=candidate.panel) if scored else ()),
            )
        )
    return MatrixAnalysis(candidates=tuple(built))
