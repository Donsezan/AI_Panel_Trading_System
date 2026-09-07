"""§12.3 — seat sets as versioned TOML in the workspace.

The round-trip test is the one that matters: the writer is hand-rolled (stdlib `tomllib` reads
and does not write, and §2.1's "no new dependency" is a property worth keeping), so it is proved
against the two matrices this repo ships rather than against a document written to suit it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

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


def test_writer_refuses_a_value_the_schema_has_no_place_for() -> None:
    with pytest.raises(ConfigError, match="temperature"):
        matrices.dumps({"candidates": [{"id": "c", "temperature": 0.3}]})


def test_save_mints_a_new_version_and_never_overwrites(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    document = tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))

    first, _ = matrices.save("mine", document, reference=basket(), workspace=workspace)
    second, matrix = matrices.save("mine", document, reference=basket(), workspace=workspace)

    assert (first, second) == (1, 2)
    assert matrices.versions("mine", workspace=workspace) == (1, 2)
    assert matrices.read("mine", workspace=workspace)[0] == 2, "no version reads the latest"
    assert matrix.candidates, "save returns the validated matrix, so a caller need not reload it"


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
