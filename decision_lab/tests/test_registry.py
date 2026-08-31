"""Every run leaves a row, including the ones that produced no number (spec §11).

Identity is the whole design. Identical parameters *update* the row, so a re-run never duplicates;
any changed parameter creates a new one, so a changed prompt never silently overwrites the result
it should be compared against. Two rows on screen are always two genuinely different experiments.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import registry

AT = datetime(2026, 8, 30, tzinfo=UTC)


def row(**overrides: object) -> registry.RunRow:
    base: dict[str, object] = {
        "recorded_at": AT,
        "scenario": "sweep",
        "status": "ok",
        "evaluation": True,
        "on_fallback": "halt",
        "dataset_digest": "d1",
        "corpus_id": "c1",
        "matrix_digest": "m1",
        "dayset_digest": "p1",
        "candidate_id": "baseline",
        "cadence_seconds": 28800,
        "sample_seed": 20260823,
    }
    base.update(overrides)
    return registry.RunRow(**base)


def test_identical_parameters_update_rather_than_duplicate(tmp_path: Path) -> None:
    registry.record(row(scored=100), workspace=tmp_path)
    registry.record(row(scored=140, accuracy=Decimal("0.42")), workspace=tmp_path)

    rows = registry.read_all(workspace=tmp_path)

    assert len(rows) == 1
    assert rows[0].scored == 140
    assert rows[0].accuracy == Decimal("0.42")


def test_a_changed_parameter_is_a_new_row(tmp_path: Path) -> None:
    registry.record(row(), workspace=tmp_path)
    registry.record(row(matrix_digest="m2"), workspace=tmp_path)

    assert len(registry.read_all(workspace=tmp_path)) == 2


def test_the_policy_and_the_run_kind_do_not_change_identity(tmp_path: Path) -> None:
    """§7.7: `on_fallback` changes when a run stops, never what it produces."""
    registry.record(row(), workspace=tmp_path)
    registry.record(row(on_fallback="exclude", evaluation=False), workspace=tmp_path)

    rows = registry.read_all(workspace=tmp_path)
    assert len(rows) == 1
    assert rows[0].on_fallback == "exclude"


def test_slice_d_fields_are_in_the_identity_from_the_start(tmp_path: Path) -> None:
    """Empty for a sweep, so slice D lands without renumbering rows already written."""
    registry.record(row(), workspace=tmp_path)
    registry.record(
        row(scenario="long", start_equity=Decimal(1000), window="6m"), workspace=tmp_path
    )

    assert len(registry.read_all(workspace=tmp_path)) == 2


def test_a_refused_run_is_recorded_with_its_reason(tmp_path: Path) -> None:
    registry.record(
        row(status="provider_unavailable", note="baseline: OPENROUTER_API_KEY is not set"),
        workspace=tmp_path,
    )

    stored = registry.read_all(workspace=tmp_path)[0]
    assert stored.status == "provider_unavailable"
    assert stored.scored == 0
    assert "OPENROUTER_API_KEY" in stored.note


def test_reading_an_absent_registry_is_empty(tmp_path: Path) -> None:
    assert registry.read_all(workspace=tmp_path) == ()


def test_rows_keep_the_order_they_were_first_written_in(tmp_path: Path) -> None:
    registry.record(row(candidate_id="a"), workspace=tmp_path)
    registry.record(row(candidate_id="b"), workspace=tmp_path)
    registry.record(row(candidate_id="a", scored=9), workspace=tmp_path)

    assert [r.candidate_id for r in registry.read_all(workspace=tmp_path)] == ["a", "b"]


def test_a_failed_write_never_corrupts_the_registry_already_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """finding 8: `record` used to rewrite `registry.jsonl` in place with `write_text` — a process
    dying mid-write left a truncated final line that `read_all` would then raise on forever.
    Writing to a temp file and swapping it in with `Path.replace` (which is `os.replace`) means a
    failure before the swap must leave the file `read_all` already trusts completely untouched,
    and must not leave a stray temp file behind either."""
    registry.record(row(candidate_id="a"), workspace=tmp_path)

    def boom(self: Path, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError):
        registry.record(row(candidate_id="b"), workspace=tmp_path)

    rows = registry.read_all(workspace=tmp_path)
    assert [r.candidate_id for r in rows] == ["a"], "the original file must survive a failed swap"
    assert list(tmp_path.glob("*.tmp")) == [], "a failed write must not leave a temp file behind"
