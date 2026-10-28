"""§12.2 — the assembly the CLI, the dashboard and the notebook all share.

The load-bearing assertion is the last one: what `analyse_matrix` produces is what
`cli.report` already wrote. A second assembly that drifted would be §14's rejected second
`report` command arriving through another door.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import analysis, cli
from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import records as rc
from decision_lab import sweep as sw
from decision_lab.archive.published import ArchiveHeader
from decision_lab.params import CORPUS_META, workspace_root
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


def _header(provider: str) -> ArchiveHeader:
    return ArchiveHeader(
        source="cryptoslate",
        capture="sitemap",
        captured_at=datetime(2026, 10, 5, tzinfo=UTC),
        observed_at_policy="published_at+00:12:00",
        summarizer_provider=provider,
        summarizer_model="stub-summary" if provider == "stub" else "real-model",
        prompt_digest="abc",
        news_window_seconds=172_800,
        cluster_floor=Decimal("0.25"),
        rows=0,
    )


def test_a_news_blind_meta_has_no_provenance(built_corpus_id: str) -> None:
    meta, _ = rc.load(built_corpus_id)

    assert analysis.news_provenance(meta) is None


@pytest.mark.parametrize(("provider", "stub"), [("stub", True), ("openrouter", False)])
def test_the_flag_is_derived_from_the_header_and_never_passed(
    built_corpus_id: str, provider: str, stub: bool
) -> None:
    """Build the provenance from an `ArchiveHeader` and check the flag followed it."""
    meta, _ = rc.load(built_corpus_id)
    fed = meta.model_copy(
        update={
            "archive_digest": "a1",
            "news_blind": False,
            "news_archives": (cp.NewsArchive.of(_header(provider)),),
        }
    )

    provenance = analysis.news_provenance(fed)

    assert provenance is not None
    assert provenance.is_stub is stub
    assert provenance.sources == ("cryptoslate",)
    assert provenance.window_seconds == 172_800
    assert provenance.cluster_floor == Decimal("0.25")


@pytest.mark.parametrize("stub", [True, False])
def test_report_on_a_news_fed_corpus_shows_both_banners(
    built_corpus_id: str, tmp_path: Path, stub: bool
) -> None:
    """The real path: `corpus.json` on disk, `cli.main(["report", ...])`, the page it writes.
    `report` is a separate process that reads only the meta, so the provenance has to be there."""
    meta, _ = rc.load(built_corpus_id)
    fed = meta.model_copy(
        update={
            "archive_digest": "a1",
            "news_blind": False,
            "news_archives": (cp.NewsArchive.of(_header("stub" if stub else "openrouter")),),
        }
    )
    (workspace_root() / built_corpus_id / CORPUS_META).write_text(
        fed.model_dump_json(indent=2), encoding="utf-8"
    )
    out = tmp_path / "page.md"

    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK

    page = out.read_text(encoding="utf-8")
    assert "SUMMARIZED NEWS" in page
    assert "RECONSTRUCTED NEWS" in page
    assert "NEWS-BLIND RUN" not in page
    assert ("PLUMBING CHECK" in page) is stub
