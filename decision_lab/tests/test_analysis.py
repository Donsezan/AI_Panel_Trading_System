"""§12.2 — the assembly the CLI, the dashboard and the notebook all share.

The load-bearing assertion is the last one: what `analyse_matrix` produces is what
`cli.report` already wrote. A second assembly that drifted would be §14's rejected second
`report` command arriving through another door.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import analysis
from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import records as rc
from decision_lab import sweep as sw
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
    assert all("the sweep halted before reaching it" in row.reason for row in result.not_measured)


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
