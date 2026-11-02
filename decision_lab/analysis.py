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
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from decision_lab import compare as cmp
from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import period as pd
from decision_lab import regimes as rg
from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab import sweep as sw
from decision_lab.calibration_days import Pool
from decision_lab.candidates import Matrix
from decision_lab.newsfeed import ArchiveNewsFeed
from decision_lab.records import CycleRecord, records_from_store
from decision_lab.render import CandidateSeats, NewsProvenance, NotMeasured
from decision_lab.spikes import LEAD_IN, EventNews, SpikeEvent, find_events, news_for
from tradebot.core.clock import Clock
from tradebot.core.config import PanelConfig
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore


def news_provenance(meta: cp.CorpusMeta) -> NewsProvenance | None:
    """§6.7's provenance for `LabReport.news_archive`, from the meta alone - `None` if blind.

    The one place the report path learns about the archive, called from every `LabReport`
    construction in the CLI so a news-fed corpus cannot render without its banners. Several
    archives are read through one window and floor (`load_feed` refuses otherwise), so those are
    taken from the first; models, prompts and policies are listed, de-duplicated. A stub archive
    anywhere makes the run a plumbing check.
    """
    if meta.news_blind or not meta.news_archives:
        return None
    archives = meta.news_archives

    def unique(values: Sequence[str]) -> str:
        return ", ".join(dict.fromkeys(values))

    return NewsProvenance(
        sources=tuple(a.source for a in archives),
        summarizer_model=unique([a.summarizer_model for a in archives]),
        prompt_digest=unique([a.prompt_digest for a in archives]),
        observed_at_policy=unique([a.observed_at_policy for a in archives]),
        window_seconds=archives[0].news_window_seconds,
        cluster_floor=archives[0].cluster_floor,
        is_stub=any(a.is_stub for a in archives),
    )


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


@dataclass(frozen=True, slots=True)
class PeriodAnalysis:
    """One period run, folded and scored: the single answer the page, the report and the notebook
    all read (period spec §5.6, §7; `render.write_period` is the CLI's door onto it).

    `records` holds every cycle the log froze, including the quarantined stretch of a paused month
    (no decision, no spend); `scored`, `by_month`, `seats` and `seat_trend` are derived from the
    clean ones only. `regimes` is always the three pools, never pooled, with a zero row for a pool
    that never occurred (§7.2). `event_news` runs parallel to `events`.
    """

    meta: pd.PeriodMeta
    records: tuple[CycleRecord, ...]
    scored: tuple[sc.ScoredDecision, ...]
    seats: tuple[st.SeatMetrics, ...]
    regimes: tuple[sc.RegimeMetrics, ...]
    events: tuple[SpikeEvent, ...]
    event_news: tuple[EventNews, ...]
    by_month: Mapping[str, tuple[sc.ScoredDecision, ...]]
    #: seat -> bucket label -> final-vote accuracy, `None` where the seat had no scored vote there.
    #: Zero would read as "always wrong" for a bucket in which it was never measured.
    seat_trend: Mapping[str, Mapping[str, Decimal | None]]
    #: Cycles a substituted model answered in. Never scored; their trades still happened (§5.6).
    contaminated: int

    @property
    def running(self) -> bool:
        """`RUNNING` on disk means a process is working, or died; the page cannot tell which."""
        return self.meta.ending is pd.RunEnding.RUNNING

    @property
    def not_run(self) -> tuple[str, ...]:
        """Months the run never reached, derived and never stored as a zero row (§5.3)."""
        return pd.not_run_months(self.meta)


def _buckets(period: pd.Period) -> tuple[tuple[str, datetime, datetime], ...]:
    """Months for a period of up to a quarter, quarters for a year (spec §4, results item 4)."""
    months = period.months
    if len(months) <= 3:
        return tuple((m.label, m.start, m.end) for m in months)
    quarters = []
    for first in range(0, len(months), 3):
        chunk = months[first : first + 3]
        label = f"{chunk[0].start.year:04d}-Q{(chunk[0].start.month - 1) // 3 + 1}"
        quarters.append((label, chunk[0].start, chunk[-1].end))
    return tuple(quarters)


def _seat_trend(
    records: Sequence[CycleRecord],
    scored: Sequence[sc.ScoredDecision],
    *,
    panel: PanelConfig,
    buckets: Sequence[tuple[str, datetime, datetime]],
) -> dict[str, dict[str, Decimal | None]]:
    trend: dict[str, dict[str, Decimal | None]] = {seat.seat_id: {} for seat in panel.seats}
    for label, start, end in buckets:
        rows = st.score_seats(
            [r for r in records if start <= r.as_of < end],
            [d for d in scored if start <= d.as_of < end],
            panel=panel,
        )
        for seat_id, by_bucket in trend.items():
            final = [r for r in rows if r.seat_id == seat_id and r.round_label == st.FINAL]
            graded = sum(r.scored for r in final)
            by_bucket[label] = sc.ratio(sum(r.correct for r in final), graded) if graded else None
    return trend


def fold_period(
    meta: pd.PeriodMeta,
    records: Sequence[CycleRecord],
    scoring: Scoring,
    *,
    panel: PanelConfig,
    archive: ArchiveNewsFeed | None,
) -> PeriodAnalysis:
    """Score a run's cycles. Split from the database read so a test can hand it doctored records."""
    # Contamination is read off `SeatResponse.fingerprint` exactly as the sweep reads it (§7.7).
    # The cycle is dropped before scoring: the peers read the substitute's arguments.
    clean = tuple(r for r in records if not sw.substitutes_in(r.responses, panel))
    scored = sc.score_records(
        clean, index=scoring.index, regimes=scoring.regimes, params=scoring.params
    )

    reached = {m.label for m in meta.months}
    by_month = {
        m.label: tuple(d for d in scored if m.start <= d.as_of < m.end)
        for m in meta.period.months
        if m.label in reached or any(m.start <= d.as_of < m.end for d in scored)
    }

    key = next(
        (one.key for one in scoring.dataset.instruments if one.symbol == pd.PERIOD_SYMBOL), None
    )
    events: tuple[SpikeEvent, ...] = ()
    if key is not None:
        found = find_events(
            scoring.regimes.labels[key],
            scoring.index.candles[key],
            windows=scoring.regimes.windows,
        )
        inside = (e for e in found if e.end >= meta.period.start and e.start < meta.period.end)
        # Absolute: a merged event can retrace, so the sign of `move_pct` is not its direction.
        events = tuple(sorted(inside, key=lambda e: (-abs(e.move_pct), e.start)))
    # §6.8.3: covered means one source's span holds the whole lead-in and the event, as the feed
    # itself decides it. A cycle that ran no panel read no news, so it neither adds to what the
    # panel could see nor moves the last decision; a contaminated one did read it (its panel ran).
    deciding = tuple(r for r in records if r.decisions)
    items = archive.items if archive is not None else ()
    event_news = tuple(
        news_for(
            e,
            deciding,
            items,
            coverage=archive is not None and archive.covers_span(e.start - LEAD_IN, e.end),
        )
        for e in events
    )

    pools = {pool.value for pool in Pool}
    return PeriodAnalysis(
        meta=meta,
        records=tuple(records),
        scored=scored,
        seats=st.score_seats(clean, scored, panel=panel) if scored else (),
        regimes=tuple(row for row in sc.by_regime(scored) if row.regime in pools),
        events=events,
        event_news=event_news,
        by_month=by_month,
        seat_trend=_seat_trend(clean, scored, panel=panel, buckets=_buckets(meta.period)),
        contaminated=len(records) - len(clean),
    )


def analyse_period(
    run_id: str,
    scoring: Scoring,
    *,
    panel: PanelConfig,
    archive: ArchiveNewsFeed | None,
    workspace: Path | None = None,
) -> PeriodAnalysis:
    """Read a run's meta and log, and fold them. Writes nothing.

    `open_database` never migrates, so this is safe against a run another process has open (WAL).
    A run with no meta or no database refuses, naming the directory.
    """
    directory = pd.period_dir(run_id, workspace=workspace)
    meta_path = directory / pd.PERIOD_META
    database = directory / pd.PERIOD_DB
    # Named apart: a database lost beside its meta is not "no such run", and the operator needs
    # to know which file to look for and where.
    if not meta_path.is_file():
        raise ConfigError(f"no period run {run_id!r}: {pd.PERIOD_META} is missing from {directory}")
    if not database.is_file():
        raise ConfigError(
            f"period run {run_id!r} has its {pd.PERIOD_META} but {pd.PERIOD_DB} is missing "
            f"from {directory}; its cycles cannot be scored without the log"
        )
    meta = pd.PeriodMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    engine = open_database(database)
    writer = SingleWriter(engine)
    try:
        records = records_from_store(EventStore(engine, writer))
    finally:
        writer.close()
        engine.dispose()
    return fold_period(meta, records, scoring, panel=panel, archive=archive)
