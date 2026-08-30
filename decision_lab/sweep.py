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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from decision_lab.corpus import Corpus, corpus_dir
from decision_lab.records import CycleRecord
from tradebot.core.config import PanelConfig
from tradebot.core.decision import Decision, SeatResponse
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime

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
