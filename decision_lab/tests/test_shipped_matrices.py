"""The two matrices the repo ships, and what each one is (spec §7.2).

`sweep.toml` is an evaluation: real endpoints, real model ids, and it refuses without keys.
`sweep-stub.toml` is the plumbing check: stubs, so it runs anywhere and is stamped as not being a
measurement. Both are asserted here because a matrix that stopped parsing would be found by an
operator mid-run rather than by CI — and the free model ids rot (R11).
"""

from __future__ import annotations

import pytest

from decision_lab import candidates as cd
from decision_lab.tests.test_candidates import reference
from tradebot.core.errors import ConfigError
from tradebot.decision import presets


def test_the_shipped_evaluation_matrix_parses_and_is_an_evaluation() -> None:
    matrix = cd.load_matrix(cd.DEFAULT_MATRIX, reference=reference())

    assert matrix.is_evaluation is True
    assert matrix.on_fallback is cd.SweepPolicy.HALT, "strict by default (§7.7)"
    assert len(matrix.candidates) >= 2, "a sweep of one candidate compares nothing"


def test_the_shipped_evaluation_matrix_refuses_without_keys() -> None:
    matrix = cd.load_matrix(cd.DEFAULT_MATRIX, reference=reference())

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        cd.require_reachable(matrix, environ={})


def test_the_shipped_matrix_uses_the_model_ids_presets_declares() -> None:
    """One place to fix when a free slot disappears. R11 is why they carry fallbacks at all."""
    matrix = cd.load_matrix(cd.DEFAULT_MATRIX, reference=reference())
    bound = {
        binding.model
        for candidate in matrix.candidates
        for seat in candidate.panel.seats
        for binding in seat.bindings
    }
    known = {
        presets.TECHNICAL_MODEL,
        presets.NEWS_MODEL,
        presets.SKEPTIC_MODEL,
        presets.LOCAL_TECHNICAL_MODEL,
        presets.LOCAL_SKEPTIC_MODEL,
        presets.GEMINI_MODEL,
    }
    assert bound <= known, f"model ids not declared in decision/presets.py: {sorted(bound - known)}"


def test_the_stub_matrix_is_a_plumbing_check_and_needs_nothing() -> None:
    matrix = cd.load_matrix(cd.STUB_MATRIX, reference=reference())

    assert matrix.is_evaluation is False
    assert len(matrix.candidates) >= 2
    cd.require_reachable(matrix, environ={})
