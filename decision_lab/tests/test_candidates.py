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

# Two seats on the same reachable primary; only "news" touches gemini, and only as a fallback.
# Discriminates a per-seat message from a per-provider one: "trend" must never be named.
FALLBACK_MATRIX = """
[[candidates]]
id = "baseline"
providers = ["openrouter", "gemini"]

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "openrouter"
  model = "deepseek/deepseek-chat-v3-0324:free"

  [[candidates.seats]]
  seat_id = "news"
  role = "news analyst"
  provider_id = "openrouter"
  model = "deepseek/deepseek-chat-v3-0324:free"

    [[candidates.seats.fallbacks]]
    provider_id = "gemini"
    model = "gemini-2.0-flash"
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


def test_a_missing_fallback_key_names_the_seat_that_would_substitute(tmp_path: Path) -> None:
    """§7.2: only "news" falls back to gemini — the message must name it, not "trend"."""
    matrix = cd.load_matrix(write(tmp_path, FALLBACK_MATRIX), reference=reference())
    environ = {"OPENROUTER_API_KEY": "sk-test"}

    (finding,) = cd.unreachable(matrix, environ=environ)
    assert "news" in finding
    assert "trend" not in finding
    assert "gemini" in finding
    assert "GEMINI_API_KEY" in finding
    assert "backup" in finding, "a degraded seat substitutes; it does not abstain"

    with pytest.raises(ConfigError, match="news") as excinfo:
        cd.require_reachable(matrix, environ=environ)
    assert "GEMINI_API_KEY" in str(excinfo.value)


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


def test_candidates_differing_only_in_decision_mode_get_different_panel_digests(
    tmp_path: Path,
) -> None:
    """CRITICAL finding: `decision_mode` lives on `Basket`, not `PanelConfig`
    (tradebot/core/config.py:604), but §7.1 lists it as a matrix axis and
    `DecisionEngine.deliberate` reads `basket.decision_mode` to pick `_per_asset` vs `_basket` —
    two paths that can answer differently for the same panel. A digest over `panel` alone would
    collide the two candidates on one §7.4 cache key, so the second is served the first's rows
    verbatim and is never actually evaluated."""
    text = STUB_MATRIX + '\n[expand]\ndecision_mode = ["per_asset", "basket"]\n'
    matrix = cd.load_matrix(write(tmp_path, text), reference=reference())

    per_asset, basket = matrix.candidates
    assert per_asset.basket.decision_mode.value == "per_asset"
    assert basket.basket.decision_mode.value == "basket"
    assert per_asset.panel_digest != basket.panel_digest


def test_the_matrix_digest_does_not_depend_on_declaration_order(tmp_path: Path) -> None:
    """§7.1: the digest is over the fully expanded candidate *set* — a hand-reordered TOML
    declaring the same candidates must mint the same digest, or a report silently loses its own
    sweep to `sweep.latest_meta`'s ambiguity refusal (finding 7)."""
    forward = """
[[candidates]]
id = "a"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-technical"

[[candidates]]
id = "b"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-news"
"""
    reversed_ = """
[[candidates]]
id = "b"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-news"

[[candidates]]
id = "a"

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-technical"
"""
    left = cd.load_matrix(write(tmp_path / "a", forward), reference=reference())
    right = cd.load_matrix(write(tmp_path / "b", reversed_), reference=reference())

    assert {c.candidate_id for c in left.candidates} == {c.candidate_id for c in right.candidates}
    assert left.matrix_digest == right.matrix_digest


def test_a_matrix_mixing_a_stub_control_with_real_candidates_refuses(tmp_path: Path) -> None:
    """finding 5: `is_evaluation` is False when *anything* binds the stub, and it is a whole-run
    label — it waives §7.2's missing-key refusal for every candidate in the matrix and stamps the
    report `PLUMBING_CHECK`. One stub "control" beside real candidates is therefore wrong in both
    directions at once: the real ones spend against seats that may have silently fallen back, and
    the page carrying their ranking says it measured canned JSON. No single label is honest, so
    the matrix is refused before any spend."""
    text = REAL_MATRIX + STUB_MATRIX.replace('id = "baseline"', 'id = "control"')

    with pytest.raises(ConfigError, match="mixes a plumbing check with an evaluation") as raised:
        cd.load_matrix(write(tmp_path, text), reference=reference())

    assert "'control'" in str(raised.value) and "'baseline'" in str(raised.value)


def test_an_all_stub_matrix_and_an_all_real_one_are_both_accepted(tmp_path: Path) -> None:
    """The refusal is on *mixing*, not on the stub. Both pure kinds keep working exactly as
    before, which is what the two shipped matrices rely on."""
    stub = cd.load_matrix(write(tmp_path / "a", STUB_MATRIX), reference=reference())
    real = cd.load_matrix(write(tmp_path / "b", REAL_MATRIX), reference=reference())

    assert stub.is_evaluation is False
    assert real.is_evaluation is True
