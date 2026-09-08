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
from typing import Final, TypeVar

from decision_lab import analysis as an
from decision_lab import corpus as cp
from decision_lab import sweep as sw
from decision_lab.candidates import Matrix
from tradebot.core.clock import Clock

#: Entries kept. Small deliberately: each holds a price index over the whole dataset.
MAX_ENTRIES: Final = 4

#: `_store` is shared by two caches of different value types; `OrderedDict` is invariant in its
#: value parameter, so one non-generic signature could not serve both without a cast at each call.
_V = TypeVar("_V")


class AnalysisCache:
    """One process's memory of what it has already derived."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._scoring: OrderedDict[str, an.Scoring] = OrderedDict()
        self._analysis: OrderedDict[str, an.MatrixAnalysis] = OrderedDict()
        self._lock = asyncio.Lock()

    async def scoring_for(self, data_dir: Path) -> an.Scoring:
        """The dataset, its audit, the price index and the regime labels."""
        key = str(data_dir.resolve())  # noqa: ASYNC240 — local path normalisation, not venue I/O
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
    def _store(store: OrderedDict[str, _V], key: str, value: _V) -> None:
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
        # `stat()` alone, not `is_file()` then `stat()`: a sweep's rows are exactly what an
        # operator deletes to retry, and a later route (Task 6) reaches this with the file gone
        # between the two calls — the miss is the same "-" marker the absent branch always meant.
        try:
            stat = path.stat()
        except FileNotFoundError:
            parts.append(f"{candidate.candidate_id}:-")
        else:
            parts.append(f"{candidate.candidate_id}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)
