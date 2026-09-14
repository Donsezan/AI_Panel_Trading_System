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


def stub_document() -> dict[str, Any]:
    return tomllib.loads((SHIPPED / "sweep-stub.toml").read_text(encoding="utf-8"))


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
    document = stub_document()

    first, _ = matrices.save("mine", document, reference=basket(), workspace=workspace)
    second, matrix = matrices.save("mine", document, reference=basket(), workspace=workspace)

    assert (first, second) == (1, 2)
    assert matrices.versions("mine", workspace=workspace) == (1, 2)
    assert matrices.read("mine", workspace=workspace)[0] == 2, "no version reads the latest"
    assert matrix.candidates, "save returns the validated matrix, so a caller need not reload it"


def test_every_version_a_reader_can_see_is_complete(tmp_path: Path) -> None:
    """`versions()` and `read()` glob `*.toml` with no readiness check, so a version number must
    never exist before its bytes do: a reader landing in that window would hand a half-written
    file to `tomllib` and get a `TOMLDecodeError` out of a module that promises `ConfigError`.
    Publishing is one rename of an already-validated file, and the draft and the claim marker are
    both `.tmp`, which that glob cannot reach — so the only things in the directory are versions,
    and every one of them parses.
    """
    workspace = tmp_path / "workspace"
    document = stub_document()

    matrices.save("mine", document, reference=basket(), workspace=workspace)
    matrices.save("mine", document, reference=basket(), workspace=workspace)

    assert matrices.versions("mine", workspace=workspace) == (1, 2)
    for version in matrices.versions("mine", workspace=workspace):
        assert matrices.read("mine", version, workspace=workspace)[1] == document
    directory = matrices.root(workspace=workspace) / "mine"
    assert sorted(one.name for one in directory.iterdir()) == ["1.toml", "2.toml"]


def test_a_stale_claim_marker_is_stepped_over_rather_than_reused(tmp_path: Path) -> None:
    """A process that died between claiming a number and publishing it leaves its marker behind,
    and that number is skipped for good. A gap in the numbering is honest — a version is an opaque
    identifier, not a count — where reusing the number would mean two different documents both
    calling themselves version 1.
    """
    workspace = tmp_path / "workspace"
    directory = matrices.root(workspace=workspace) / "mine"
    directory.mkdir(parents=True)
    stranded = matrices._claim(directory, 1)
    stranded.touch()

    version, _ = matrices.save("mine", stub_document(), reference=basket(), workspace=workspace)

    assert version == 2, "the claimed number is skipped, not reused and not an error"
    assert matrices.versions("mine", workspace=workspace) == (2,)
    assert stranded.is_file(), "and the marker is left where it was — it is not this save's to fix"


def test_save_steps_over_a_number_a_racing_save_already_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tabs computing the same next number must produce two versions, not one survivor.

    The window between reading `versions()` and publishing is microseconds wide, so it is frozen
    rather than raced: `versions()` is made to answer what the second saver saw — the store as it
    was before the first saver's file appeared. Against a read-then-`Path.replace` the save below
    returns 1 and the bytes already standing there are gone.
    """
    workspace = tmp_path / "workspace"
    taken = matrices.path_for("mine", 1, workspace=workspace)
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"# the other tab got here first\n")
    monkeypatch.setattr(matrices, "versions", lambda *_, **__: ())

    version, _ = matrices.save("mine", stub_document(), reference=basket(), workspace=workspace)

    assert version == 2, "the taken number is stepped over, never rewritten"
    assert taken.read_bytes() == b"# the other tab got here first\n"


def test_the_matrix_returned_describes_the_file_that_was_stored(tmp_path: Path) -> None:
    """One shared draft would let a save's `load_matrix` read another save's overwrite of it and
    return a `Matrix` describing a document it did not store. The draft is per save, and the file
    the validator read is the file the rename publishes, so the two cannot disagree — including
    `source`, which `load_matrix` sets to the path it read and which would otherwise name a
    temporary file that no longer exists.
    """
    workspace = tmp_path / "workspace"
    document = stub_document()
    renamed = {**document, "candidates": [{**document["candidates"][0], "id": "second-thoughts"}]}

    first, one = matrices.save("mine", document, reference=basket(), workspace=workspace)
    second, two = matrices.save("mine", renamed, reference=basket(), workspace=workspace)

    assert matrices.read("mine", first, workspace=workspace)[1] == document
    assert matrices.read("mine", second, workspace=workspace)[1] == renamed
    # The stub matrix carries an `[expand]` block, so one renamed entry becomes several ids that
    # all share the prefix — the assertion is that each matrix carries its own document's, not
    # that a candidate set is one row long.
    assert all(c.candidate_id.startswith("second-thoughts") for c in two.candidates)
    assert not any(c.candidate_id.startswith("second-thoughts") for c in one.candidates)
    assert two.source == matrices.path_for("mine", second, workspace=workspace)


def test_save_refuses_an_invalid_matrix_and_writes_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    broken = {"candidates": [{"id": "c", "seats": [{"seat_id": "s"}]}]}

    with pytest.raises(ConfigError):
        matrices.save("mine", broken, reference=basket(), workspace=workspace)

    assert matrices.versions("mine", workspace=workspace) == ()
    left = [one for one in matrices.root(workspace=workspace).rglob("*") if one.is_file()]
    assert left == [], "no draft and no claim marker survives — `*.toml` alone would miss both"


def test_a_name_that_could_escape_the_workspace_is_refused(tmp_path: Path) -> None:
    for bad in ("../escape", "with/slash", "", "A" * 80):
        with pytest.raises(ConfigError, match="name"):
            matrices.save(bad, {}, reference=basket(), workspace=tmp_path)


def test_templates_are_the_shipped_files_and_are_never_written(tmp_path: Path) -> None:
    assert set(matrices.templates()) == {"sweep", "sweep-stub"}
    assert matrices.read_template("sweep-stub")["sweep"]["on_fallback"] == "halt"
    assert stub_matrix_file().read_text(encoding="utf-8").startswith("# The plumbing check")
