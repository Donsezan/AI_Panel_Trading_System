"""Candidates are validated, and a run's *kind* is decided by what it binds (spec §7.2).

Two refusals, both before a provider call, and one classification:

* a candidate the bot's own `Basket` would reject never reaches a model — the sweep cannot test a
  panel the bot itself would refuse;
* an evaluation whose declared endpoint has no key refuses naming the variable, deliberately
  stricter than ADR 0023, which is right for a trading system and wrong for a measuring one;
* a matrix binding the offline stub anywhere is a *plumbing check*, not an evaluation, and the
  binding decides that — never a flag.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from decision_lab.tests import factories as f
from tradebot.core.config import Basket, PanelConfig, SeatConfig
from tradebot.core.errors import ConfigError

STUB_MATRIX = """
[[candidates]]
id = "baseline"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-technical"
"""

REAL_MATRIX = """
[[candidates]]
id = "baseline"
providers = ["openrouter"]

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "openrouter"
  model = "deepseek/deepseek-chat-v3-0324:free"
"""


def reference() -> Basket:
    return Basket(
        basket_id="reference",
        name="reference",
        instruments=(f.instrument(),),
        panel=PanelConfig(
            panel_id="reference",
            seats=(SeatConfig(seat_id="a", role="a", provider_id="stub", model="stub-technical"),),
        ),
    )


def write(tmp_path: Path, text: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_candidate_is_the_reference_basket_with_its_panel_swapped(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, STUB_MATRIX), reference=reference())
    candidate = matrix.candidates[0]

    assert candidate.candidate_id == "baseline"
    assert candidate.basket.instruments == reference().instruments
    assert candidate.basket.basket_id == reference().basket_id
    assert [s.seat_id for s in candidate.panel.seats] == ["trend"]


def test_an_invalid_candidate_refuses_the_whole_matrix(tmp_path: Path) -> None:
    text = STUB_MATRIX.replace('id = "baseline"', 'id = "baseline"\nqualified_majority = "1.5"')
    with pytest.raises(ConfigError, match="baseline"):
        cd.load_matrix(write(tmp_path, text), reference=reference())


def test_a_stub_binding_makes_the_run_a_plumbing_check(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, STUB_MATRIX), reference=reference())

    assert matrix.is_evaluation is False
    assert matrix.stub_bindings == ("baseline: trend->stub:varied-technical",)


def test_a_real_binding_makes_the_run_an_evaluation(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, REAL_MATRIX), reference=reference())

    assert matrix.is_evaluation is True
    assert matrix.stub_bindings == ()


def test_a_stub_fallback_is_as_disqualifying_as_a_primary_one(tmp_path: Path) -> None:
    text = (
        REAL_MATRIX.replace('["openrouter"]', '["openrouter", "stub"]')
        + """
    [[candidates.seats.fallbacks]]
    provider_id = "stub"
    model = "varied-news"
"""
    )
    matrix = cd.load_matrix(write(tmp_path, text), reference=reference())

    assert matrix.is_evaluation is False


def test_a_missing_key_refuses_an_evaluation_and_names_the_variable(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, REAL_MATRIX), reference=reference())

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        cd.require_reachable(matrix, environ={})


def test_a_present_key_passes(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, REAL_MATRIX), reference=reference())

    cd.require_reachable(matrix, environ={"OPENROUTER_API_KEY": "sk-test"})


def test_a_plumbing_matrix_needs_no_key_at_all(tmp_path: Path) -> None:
    matrix = cd.load_matrix(write(tmp_path, STUB_MATRIX), reference=reference())

    cd.require_reachable(matrix, environ={})


def test_the_policy_is_not_part_of_the_matrix_identity(tmp_path: Path) -> None:
    """§7.7: it changes when a run stops, never what a run produces."""
    base = cd.load_matrix(write(tmp_path / "a", STUB_MATRIX), reference=reference())
    other = cd.load_matrix(
        write(tmp_path / "b", '[sweep]\non_fallback = "exclude"\n' + STUB_MATRIX),
        reference=reference(),
    )

    assert other.matrix_digest == base.matrix_digest
    assert other.on_fallback is cd.SweepPolicy.EXCLUDE


def test_a_reworded_prompt_is_a_different_matrix(tmp_path: Path) -> None:
    """§7.1: changing one prompt is a new matrix and needs a new calibration."""
    base = cd.load_matrix(write(tmp_path / "a", STUB_MATRIX), reference=reference())
    reworded = cd.load_matrix(
        write(
            tmp_path / "b",
            '[prompts.p]\ntext = "Stand aside."\n'
            + STUB_MATRIX.replace(
                'model = "varied-technical"', 'model = "varied-technical"\n  prompt = "p"'
            ),
        ),
        reference=reference(),
    )

    assert reworded.matrix_digest != base.matrix_digest
