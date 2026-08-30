"""N candidates over one corpus (spec §7).

The design's load-bearing decision is that a candidate never runs its own loop (§3): it
deliberates on the corpus's *already-frozen* snapshots, so every candidate is judged on the same
evidence and the same positions, and a difference in score is a difference in reasoning rather
than a difference in luck. That is ADR 0018's principle generalised from one challenger to N.

A result folds back into slice B's `CycleRecord`, which is why this package gains no scoring code:
`score_records` and `score_seats` read a sweep exactly as they read the reference pass.

Two storage locations, deliberately different in scope:

* `workspace/<corpus_id>/cache/` — content-addressed by (snapshot, panel) and **shared across
  matrices**, because that key already names everything that determines the answer. Scoping it by
  matrix would defeat §7.4: adding one candidate would re-pay for every candidate already answered.
* `workspace/<corpus_id>/sweep-<matrix_digest>/<candidate_id>.jsonl` — the experiment's record,
  appended as results are produced so an interrupted sweep resumes (§7.6) and so §12 can tail a
  running one.

Failure semantics: every refusal happens before spend (`candidates.py`); a budget breach halts and
keeps what it bought (§7.5); a substitute model answering is contamination and never scores (§7.7).
Nothing here writes to a bot database or constructs a venue broker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from decision_lab.candidates import Candidate, Matrix, SweepPolicy
from decision_lab.corpus import Corpus, CorpusEntry, corpus_dir
from decision_lab.records import CycleRecord
from decision_lab.sampling import Sample
from tradebot.core.clock import Clock
from tradebot.core.config import Basket, PanelConfig
from tradebot.core.decision import Decision, PanelOutcome, SeatResponse
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.providers.registry import build_providers
from tradebot.decision.seat import SeatRunner

logger = get_logger("decision_lab.sweep")

CACHE_DIR: Final = "cache"
SWEEP_META: Final = "sweep.json"


class SweepRow(DomainModel):
    """One candidate's answer for one corpus entry."""

    cycle_id: str
    as_of: UtcDatetime
    decisions: tuple[Decision, ...] = ()
    responses: tuple[SeatResponse, ...] = ()
    cost_usd: Money = ZERO
    #: Seats whose answer came from a binding other than their primary (§7.7). Non-empty means the
    #: whole cycle is contaminated — the peers read this seat's arguments and its vote reached
    #: `reach_consensus`, so no part of the decision measures the configured panel.
    substitutes: tuple[str, ...] = ()
    #: A deliberation that raised. Recorded and counted, exactly as `ShadowEvaluator` does: a
    #: candidate that silently stopped being evaluated would leave a comparison built on fewer
    #: cycles than it claims.
    error: str = ""

    @property
    def contaminated(self) -> bool:
        return bool(self.substitutes)


def sweep_dir(corpus_id: str, matrix_digest: str, *, workspace: Path | None = None) -> Path:
    return corpus_dir(corpus_id, workspace=workspace) / f"sweep-{matrix_digest}"


def cache_dir(corpus_id: str, *, workspace: Path | None = None) -> Path:
    return corpus_dir(corpus_id, workspace=workspace) / CACHE_DIR


def rows_path(
    corpus_id: str, matrix_digest: str, candidate_id: str, *, workspace: Path | None = None
) -> Path:
    safe = candidate_id.replace("/", "_").replace("\\", "_")
    return sweep_dir(corpus_id, matrix_digest, workspace=workspace) / f"{safe}.jsonl"


def read_rows(path: Path) -> dict[str, SweepRow]:
    """Every row already bought, keyed by cycle. An absent file is an empty sweep, not an error."""
    if not path.is_file():
        return {}
    rows: dict[str, SweepRow] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = SweepRow.model_validate_json(line)
            rows[row.cycle_id] = row
    return rows


def append_row(path: Path, row: SweepRow) -> None:
    """Append one result. Written as it is produced, so a killed process keeps what it paid for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(row.model_dump_json() + "\n")


def cache_key(snapshot_digest: str, panel_digest: str) -> str:
    """§7.4. The evidence and the panel — the two things that determine the answer, and nothing
    else, which is what makes the cache shareable across matrices."""
    return hashlib.blake2s(f"{snapshot_digest}|{panel_digest}".encode(), digest_size=16).hexdigest()


def cache_read(corpus_id: str, key: str, *, workspace: Path | None = None) -> SweepRow | None:
    path = cache_dir(corpus_id, workspace=workspace) / f"{key}.json"
    if not path.is_file():
        return None
    return SweepRow.model_validate_json(path.read_text(encoding="utf-8"))


def cache_write(corpus_id: str, key: str, row: SweepRow, *, workspace: Path | None = None) -> None:
    directory = cache_dir(corpus_id, workspace=workspace)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}.json").write_text(row.model_dump_json(), encoding="utf-8")


def substitutes_in(responses: Sequence[SeatResponse], panel: PanelConfig) -> tuple[str, ...]:
    """Seats that answered on something other than their primary binding (§7.7).

    Reads `SeatResponse.fingerprint`, which is the binding that actually answered after any
    fallback — the same field §9.7's fallback rate is computed from.
    """
    primary = {seat.seat_id: seat.primary.fingerprint for seat in panel.seats}
    found = set()
    for r in responses:
        if r.seat_id not in primary:
            # §7.7: a response naming a seat the configured panel does not declare cannot be
            # attributed to that panel at all — "I cannot match this to a seat" must resolve to
            # contaminated, not to clean, the same fail-closed rule the bot applies to every
            # other uncertainty. Worded distinctly from the ordinary fallback case below so an
            # operator can tell "wrong model" apart from "no such seat" at a glance.
            found.add(f"{r.seat_id}: not a seat this panel declares")
        elif r.fingerprint != primary[r.seat_id]:
            found.add(f"{r.seat_id}: {primary[r.seat_id]} -> {r.fingerprint}")
    return tuple(sorted(found))


def records_from_rows(corpus: Corpus, rows: Mapping[str, SweepRow]) -> tuple[CycleRecord, ...]:
    """Fold a candidate's rows onto the corpus's frozen snapshots (§3).

    This is the whole reason slice C adds no scoring code: what comes out is exactly the
    `CycleRecord` slice B's `score_records` and `score_seats` already read.

    A contaminated or failed row yields no record at all — never a record with an empty decision
    list, which would score as a cycle the panel answered by saying nothing (§7.7).
    """
    by_cycle = {entry.cycle_id: entry for entry in corpus.entries}
    return tuple(
        CycleRecord(
            cycle_id=row.cycle_id,
            basket_id=by_cycle[row.cycle_id].basket_id,
            as_of=row.as_of,
            snapshot=by_cycle[row.cycle_id].snapshot,
            decisions=row.decisions,
            responses=row.responses,
            cost_usd=row.cost_usd,
        )
        for row in (rows[e.cycle_id] for e in corpus.entries if e.cycle_id in rows)
        if not row.contaminated and not row.error
    )


class SweepStatus(StrEnum):
    """How a sweep ended. Recorded on the §11 row, because a run that produced no number is
    still a fact about the experiment."""

    OK = "ok"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    HALTED_FALLBACK = "halted_fallback"
    HALTED_BUDGET = "halted_budget"


class SweepResult(DomainModel):
    """What one sweep did, and what stopped it."""

    corpus_id: str
    matrix_digest: str
    status: SweepStatus = SweepStatus.OK
    #: False when any candidate binds the stub (§7.2). Carried into every banner and every row.
    evaluation: bool = True
    on_fallback: str = ""
    spent_usd: Money = ZERO
    budget_usd: Money = ZERO
    evaluated: int = 0
    cached: int = 0
    contaminated: int = 0
    failed: int = 0
    #: What the run stopped on — candidate, entry, seat and both bindings, or the ceiling.
    halted_on: str = ""
    sample: Sample = Sample()
    candidate_ids: tuple[str, ...] = ()
    matrix_source: str = ""


class DeliberatingEngine(Protocol):
    """What the loop needs of an engine. A Protocol, so a test drives the real loop offline."""

    async def deliberate(self, snapshot: ContextSnapshot, basket: Basket) -> PanelOutcome: ...


#: How the loop obtains an engine for one candidate.
EngineFor = Callable[[Candidate], DeliberatingEngine]


def engine_from_pool(clock: Clock) -> EngineFor:
    """The real engine: one provider pool per candidate, since panels declare their own."""

    def build(candidate: Candidate) -> DeliberatingEngine:
        pool = build_providers(candidate.panel.providers, clock)
        return DecisionEngine(SeatRunner(pool.providers, clock))

    return build


async def run(
    corpus: Corpus,
    matrix: Matrix,
    *,
    sample: Sample,
    clock: Clock,
    budget_usd: Decimal,
    workspace: Path | None = None,
    engine_for: EngineFor | None = None,
) -> SweepResult:
    """Every candidate over every sampled entry, cached, budgeted and resumable (§7.4–§7.7)."""
    build = engine_for or engine_from_pool(clock)
    wanted = [entry for entry in corpus.entries if entry.cycle_id in set(sample.cycle_ids)]
    result = SweepResult(
        corpus_id=corpus.meta.corpus_id,
        matrix_digest=matrix.matrix_digest,
        evaluation=matrix.is_evaluation,
        on_fallback=matrix.on_fallback.value,
        budget_usd=budget_usd,
        sample=sample,
        candidate_ids=tuple(c.candidate_id for c in matrix.candidates),
        matrix_source=str(matrix.source),
    )

    for candidate in matrix.candidates:
        engine = build(candidate)
        path = rows_path(
            corpus.meta.corpus_id,
            matrix.matrix_digest,
            candidate.candidate_id,
            workspace=workspace,
        )
        done = read_rows(path)
        for entry in wanted:
            if entry.cycle_id in done:
                continue

            key = cache_key(entry.snapshot.digest, candidate.panel_digest)
            hit = cache_read(corpus.meta.corpus_id, key, workspace=workspace)
            if hit is not None:
                append_row(path, hit.model_copy(update={"cycle_id": entry.cycle_id}))
                result = result.model_copy(update={"cached": result.cached + 1})
                continue

            if result.spent_usd >= budget_usd:
                return _halt(
                    result,
                    SweepStatus.HALTED_BUDGET,
                    f"the ${budget_usd} ceiling was reached at {candidate.candidate_id} "
                    f"/ {entry.as_of.isoformat()}",
                )

            row = await _evaluate(engine, candidate, entry)
            append_row(path, row)
            cache_write(corpus.meta.corpus_id, key, row, workspace=workspace)
            result = result.model_copy(
                update={
                    "evaluated": result.evaluated + 1,
                    "spent_usd": result.spent_usd + row.cost_usd,
                    "failed": result.failed + int(bool(row.error)),
                    "contaminated": result.contaminated + int(row.contaminated),
                }
            )
            if row.contaminated and matrix.on_fallback is SweepPolicy.HALT:
                return _halt(
                    result,
                    SweepStatus.HALTED_FALLBACK,
                    f"{candidate.candidate_id} at {entry.as_of.isoformat()}: "
                    + "; ".join(row.substitutes),
                )
    return result


def _halt(result: SweepResult, status: SweepStatus, reason: str) -> SweepResult:
    """Stop, keeping everything already appended. §7.5: never overspend, never discard."""
    logger.warning("sweep halted", extra={"status": status.value, "reason": reason})
    return result.model_copy(update={"status": status, "halted_on": reason})


async def _evaluate(
    engine: DeliberatingEngine, candidate: Candidate, entry: CorpusEntry
) -> SweepRow:
    """One candidate on one frozen snapshot.

    Never raises, exactly as `ShadowEvaluator` never does: a candidate that silently stopped being
    evaluated would leave a comparison built on fewer cycles than it claims (§15).
    """
    try:
        outcome = await engine.deliberate(entry.snapshot, candidate.basket)
    # Deliberately broad, and for the reason above: every failure is written down and counted.
    # No lint suppression needed here: this repo's ruff config does not select the blind-except
    # rule set (see `tradebot/decision/shadow.py`'s `ShadowEvaluator`, this pattern's origin).
    except Exception as exc:
        return SweepRow(
            cycle_id=entry.cycle_id, as_of=entry.as_of, error=f"{type(exc).__name__}: {exc}"
        )
    return SweepRow(
        cycle_id=entry.cycle_id,
        as_of=entry.as_of,
        decisions=outcome.decisions,
        responses=outcome.responses,
        cost_usd=outcome.cost_usd,
        substitutes=substitutes_in(outcome.responses, candidate.panel),
    )


def write_meta(result: SweepResult, *, workspace: Path | None = None) -> Path:
    directory = sweep_dir(result.corpus_id, result.matrix_digest, workspace=workspace)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SWEEP_META
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_meta(
    corpus_id: str, matrix_digest: str, *, workspace: Path | None = None
) -> SweepResult | None:
    path = sweep_dir(corpus_id, matrix_digest, workspace=workspace) / SWEEP_META
    if not path.is_file():
        return None
    return SweepResult.model_validate_json(path.read_text(encoding="utf-8"))


def latest_meta(corpus_id: str, *, workspace: Path | None = None) -> SweepResult | None:
    """The one sweep under this corpus, or `None`. Refuses to guess between two (§14).

    `report --matrix` is how a reader picks when more than one has run; choosing for them would
    silently rank one experiment's candidates on another's page.
    """
    directory = corpus_dir(corpus_id, workspace=workspace)
    found = sorted(directory.glob(f"sweep-*/{SWEEP_META}")) if directory.is_dir() else []
    if len(found) != 1:
        return None
    return SweepResult.model_validate_json(found[0].read_text(encoding="utf-8"))
