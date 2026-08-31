"""Slice C end to end: corpus → sweep → report → registry, offline and free (spec §16).

The slice's exit criterion, driven through `cli.main` exactly as an operator would — the same
shape slices A and B take, and for the same reason: a handler called directly proves the handler,
while the operator's failure is usually in the wiring between them.

On the stub matrix, so the whole run is a plumbing check and says so — which is itself one of the
things asserted.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab import candidates as cd
from decision_lab import corpus as cp
from decision_lab import registry
from decision_lab import sweep as sw


def test_a_sweep_ranks_its_candidates_and_files_the_result(
    tmp_path: Path, built_corpus_id: str
) -> None:
    assert cli_sweep(built_corpus_id) == 0, "the stub matrix needs no key and reaches no network"

    out = tmp_path / "slice-c.md"
    from decision_lab import cli

    assert cli.main(["report", "--corpus", built_corpus_id, "--out", str(out)]) == cli.EXIT_OK
    text = out.read_text(encoding="utf-8")

    assert "PLUMBING CHECK — NOT AN EVALUATION" in text
    assert "## Candidates, by regime" in text
    assert "### Agreement" in text
    assert "| SHOCK_DOWN |" in text, "every regime is always rendered (§8.3)"
    assert "### Seats — varied-three~max_rounds=1" in text
    assert "### Seats — varied-three~max_rounds=3" in text


def test_a_second_sweep_buys_nothing_and_the_registry_holds_one_row_per_identity(
    tmp_path: Path, built_corpus_id: str
) -> None:
    assert cli_sweep(built_corpus_id) == 0
    digest = _digest(built_corpus_id)
    first = sw.read_meta(built_corpus_id, digest)

    assert cli_sweep(built_corpus_id) == 0
    second = sw.read_meta(built_corpus_id, digest)

    assert first is not None and second is not None
    assert first.evaluated > 0
    assert second.evaluated == 0, "§7.6: an already-complete sweep re-runs nothing"

    rows = registry.read_all(workspace=tmp_path / "workspace")
    assert len({row.run_id for row in rows}) == len(rows), "identical parameters update (§11)"


def cli_sweep(corpus_id: str) -> int:
    from decision_lab import cli

    return cli.main(
        ["sweep", "--corpus", corpus_id, "--configs", str(cd.STUB_MATRIX), "--budget", "1"]
    )


def _digest(corpus_id: str) -> str:
    matrix = cd.load_matrix(cd.STUB_MATRIX, reference=cp.load(corpus_id).meta.reference_basket)
    return matrix.matrix_digest
