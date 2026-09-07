"""§12.3 — seat sets as versioned TOML in the workspace.

The round-trip test is the one that matters: the writer is hand-rolled (stdlib `tomllib` reads
and does not write, and §2.1's "no new dependency" is a property worth keeping), so it is proved
against the two matrices this repo ships rather than against a document written to suit it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from decision_lab import matrices
from decision_lab.tests.factories import _reference_basket as basket
from decision_lab.tests.factories import stub_matrix_file
from tradebot.core.errors import ConfigError

SHIPPED = Path(__file__).resolve().parents[1] / "config"


@pytest.mark.parametrize("shipped", ["sweep.toml", "sweep-stub.toml"])
def test_writer_round_trips_every_shipped_matrix(shipped: str) -> None:
    original = tomllib.loads((SHIPPED / shipped).read_text(encoding="utf-8"))

    written = matrices.dumps(original)

    assert tomllib.loads(written) == original


def test_the_writer_escapes_every_character_toml_demands_be_escaped() -> None:
    """Neither shipped matrix holds a quote, a backslash or a control character, so the round-trip
    above exercises none of the escaping. A seat's `instruction` is browser-textarea text and
    arrives CRLF, and TOML demands every control character in U+0000-U+001F except tab, plus
    U+007F: a carriage return written through raw produces output `tomllib` itself refuses, which
    is a writer whose own reader cannot read it back.
    """
    document = {"instruction": 'a "quote", a \\ backslash,\r\na tab\there, \x01 and \x7f'}

    written = matrices.dumps(document)

    assert tomllib.loads(written) == document


@pytest.mark.parametrize(
    "document,offender",
    [
        ({"sweep": {"budget_usd": 0.5}}, "sweep.budget_usd"),
        ({"candidates": [{"id": "c", "sampling": 0.3}]}, "candidates.sampling"),
        (
            {"candidates": [{"id": "c", "seats": [{"seat_id": "s", "temperature": 0.3}]}]},
            "candidates.seats.temperature",
        ),
    ],
)
def test_writer_refuses_a_value_the_schema_has_no_place_for(
    document: dict[str, Any], offender: str
) -> None:
    """The refusal names the offending key path, not merely the type it found.

    Asserting on `"temperature"` alone would pass whatever key was at fault — that word is in the
    advisory sentence every one of these refusals carries — so the assertion is on the path, which
    is the part an operator staring at a dozen seats actually needs.
    """
    with pytest.raises(ConfigError, match=re.escape(offender)):
        matrices.dumps(document)


def test_save_mints_a_new_version_and_never_overwrites(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))

    first, _ = matrices.save("mine", document, reference=basket(), workspace=workspace)
    second, matrix = matrices.save("mine", document, reference=basket(), workspace=workspace)

    assert (first, second) == (1, 2)
    assert matrices.versions("mine", workspace=workspace) == (1, 2)
    assert matrices.read("mine", workspace=workspace)[0] == 2, "no version reads the latest"
    assert matrix.candidates, "save returns the validated matrix, so a caller need not reload it"


def test_save_steps_over_a_number_a_racing_save_already_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tabs computing the same next number must produce two versions, not one survivor.

    The window between reading `versions()` and creating the file is microseconds wide, so it is
    frozen rather than raced: `versions()` is made to answer what the second saver saw — the store
    as it was before the first saver's file appeared. Against a read-then-`Path.replace` the save
    below returns 1 and the bytes already standing there are gone.
    """
    workspace = tmp_path / "workspace"
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))
    taken = matrices.path_for("mine", 1, workspace=workspace)
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"# the other tab got here first\n")
    monkeypatch.setattr(matrices, "versions", lambda *_, **__: ())

    version, _ = matrices.save("mine", document, reference=basket(), workspace=workspace)

    assert version == 2, "the taken number is stepped over, never rewritten"
    assert taken.read_bytes() == b"# the other tab got here first\n"


def test_save_refuses_an_invalid_matrix_and_writes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    broken = {"candidates": [{"id": "c", "seats": [{"seat_id": "s"}]}]}

    with pytest.raises(ConfigError):
        matrices.save("mine", broken, reference=basket(), workspace=workspace)

    assert matrices.versions("mine", workspace=workspace) == ()
    assert list(matrices.root(workspace=workspace).rglob("*.toml")) == [], "no temp file survives"


def test_a_name_that_could_escape_the_workspace_is_refused(tmp_path: Path) -> None:
    for bad in ("../escape", "with/slash", "", "A" * 80):
        with pytest.raises(ConfigError, match="name"):
            matrices.save(bad, {}, reference=basket(), workspace=tmp_path)


def test_templates_are_the_shipped_files_and_are_never_written(tmp_path: Path) -> None:
    assert set(matrices.templates()) == {"sweep", "sweep-stub"}
    assert matrices.read_template("sweep-stub")["sweep"]["on_fallback"] == "halt"
    assert stub_matrix_file().read_text(encoding="utf-8").startswith("# The plumbing check")
