"""The candidate matrix: expansion, its cap, and the run policy (spec §7.1, §7.7).

Expansion is a cross product over `[expand]`, applied to every declared candidate. The ids it
produces must be deterministic and readable, because they are what a ranking table is read by and
what §11 keys a row on — an id that moved between two runs would silently make one experiment look
like two.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import candidates as cd
from tradebot.core.errors import ConfigError

MATRIX = """
[sweep]
on_fallback = "exclude"

[prompts.cautious]
text = "Favour standing aside."

[prompts.momentum]
text = "Weight recent trend continuation."

[[candidates]]
id = "baseline"
protocol = "blind_then_debate"
max_rounds = 3

  [[candidates.seats]]
  seat_id = "trend"
  role = "trend analyst"
  provider_id = "stub"
  model = "varied-technical"
  prompt = "momentum"

  [[candidates.seats]]
  seat_id = "risk"
  role = "risk officer"
  provider_id = "stub"
  model = "varied-skeptic"
  prompt = "cautious"

[expand]
max_rounds = [1, 3]
limit = 8
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_expand_takes_the_cross_product_and_names_each_axis(tmp_path: Path) -> None:
    document = cd.read_document(write(tmp_path, MATRIX))
    expanded = cd.expand(document)

    assert [c["id"] for c in expanded] == ["baseline~max_rounds=1", "baseline~max_rounds=3"]
    assert [c["max_rounds"] for c in expanded] == [1, 3]


def test_expand_resolves_a_seats_prompt_from_the_library(tmp_path: Path) -> None:
    document = cd.read_document(write(tmp_path, MATRIX))
    seats = cd.expand(document)[0]["seats"]

    assert seats[0]["instruction"] == "Weight recent trend continuation."
    assert seats[1]["instruction"] == "Favour standing aside."
    assert "prompt" not in seats[0], "the library key is resolved away, not passed through"


def test_a_prompt_axis_varies_one_seat_and_names_it(tmp_path: Path) -> None:
    text = MATRIX.replace("max_rounds = [1, 3]", 'prompts.risk = ["cautious", "momentum"]')
    expanded = cd.expand(cd.read_document(write(tmp_path, text)))

    assert [c["id"] for c in expanded] == ["baseline~risk=cautious", "baseline~risk=momentum"]
    assert expanded[1]["seats"][1]["instruction"] == "Weight recent trend continuation."


def test_a_document_with_no_expand_block_is_its_own_candidates(tmp_path: Path) -> None:
    text = MATRIX.split("[expand]")[0]
    expanded = cd.expand(cd.read_document(write(tmp_path, text)))

    assert [c["id"] for c in expanded] == ["baseline"]


def test_an_unknown_prompt_name_refuses_by_name(tmp_path: Path) -> None:
    text = MATRIX.replace('prompt = "momentum"', 'prompt = "nonexistent"')
    with pytest.raises(ConfigError, match="nonexistent"):
        cd.expand(cd.read_document(write(tmp_path, text)))


def test_an_oversized_matrix_refuses_rather_than_starting(tmp_path: Path) -> None:
    text = MATRIX.replace("max_rounds = [1, 3]", "max_rounds = [1, 2, 3]").replace(
        "limit = 8", "limit = 2"
    )
    document = cd.read_document(write(tmp_path, text))
    with pytest.raises(ConfigError, match="3 candidates"):
        cd.expand(document)


def test_the_policy_defaults_to_halt(tmp_path: Path) -> None:
    text = MATRIX.replace('on_fallback = "exclude"', "")
    assert cd.policy_of(cd.read_document(write(tmp_path, text))) is cd.SweepPolicy.HALT
    assert cd.policy_of(cd.read_document(write(tmp_path, MATRIX))) is cd.SweepPolicy.EXCLUDE


def test_an_unknown_policy_refuses_rather_than_defaulting(tmp_path: Path) -> None:
    text = MATRIX.replace('on_fallback = "exclude"', 'on_fallback = "carry on"')
    with pytest.raises(ConfigError, match="carry on"):
        cd.policy_of(cd.read_document(write(tmp_path, text)))


def test_a_missing_matrix_file_refuses_by_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"sweep\.toml"):
        cd.read_document(tmp_path / "sweep.toml")
