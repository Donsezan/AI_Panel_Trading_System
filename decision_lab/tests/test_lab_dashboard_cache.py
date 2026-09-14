"""Covers `cache.py`'s `_rows_stamp` (review fix round 1, item 2 — a TOCTOU on the rows file).

`AnalysisCache.matrix_analysis` is the public entry point, but exercising it end to end needs a
built corpus, a real sweep and an async event loop for what is a two-line synchronous stat — the
same reasoning `test_matrices.py` gives for importing `_reference_basket` directly. `_rows_stamp`
is imported the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab.dashboard.cache import _rows_stamp
from decision_lab.tests.factories import corpus_with_entries, stub_matrix_file

AT = datetime(2026, 1, 1, tzinfo=UTC)


def test_a_rows_file_that_never_existed_reads_as_absent(tmp_path: Path) -> None:
    """The plain case: nothing has ever run this candidate under this workspace."""
    corpus = corpus_with_entries(count=1, as_of=AT)
    matrix = cd.load_matrix(stub_matrix_file(), reference=corpus.meta.reference_basket)

    stamp = _rows_stamp(corpus, matrix, matrix.matrix_digest, workspace=tmp_path)

    assert stamp == "|".join(f"{c.candidate_id}:-" for c in matrix.candidates)


def test_a_rows_file_deleted_between_the_check_and_the_stat_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual TOCTOU the old `if path.is_file(): stat = path.stat()` had: `is_file()` says
    yes, and `stat()` then finds nothing — a sweep's rows directory is exactly what an operator
    deletes to retry, and a later task puts this behind an HTTP route.

    `Path.is_file` is forced to answer `True` for every path, reproducing "it existed a moment
    ago" without racing two real threads; the path genuinely does not exist on disk, so the fixed
    code's own `path.stat()` still raises `FileNotFoundError` for real. Against the old
    check-then-act code this reproduces the crash (`is_file()` reads the forced `True`, then the
    unguarded `stat()` raises); against the fix, `stat()` alone is the only call made and its
    failure is caught.
    """
    corpus = corpus_with_entries(count=1, as_of=AT)
    matrix = cd.load_matrix(stub_matrix_file(), reference=corpus.meta.reference_basket)
    monkeypatch.setattr(Path, "is_file", lambda _self: True)

    stamp = _rows_stamp(corpus, matrix, matrix.matrix_digest, workspace=tmp_path)

    assert stamp == "|".join(f"{c.candidate_id}:-" for c in matrix.candidates)
