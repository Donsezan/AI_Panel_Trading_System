"""Fixtures shared across the CLI tests.

`built_corpus_id` reuses slice B's own end-to-end builder rather than writing a second one: a
corpus assembled by a different code path would be a corpus these tests agree with and the tool
does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from decision_lab import registry
from decision_lab.tests.test_slice_b_end_to_end import built_corpus


@pytest.fixture
def built_corpus_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A verified dataset, a pinned day set and one reference pass, all under `tmp_path`."""
    monkeypatch.setattr(registry, "workspace_root", lambda: tmp_path / "workspace")
    # `jobs.WorkspaceLock` (taken by `cli.main` for every §12.4-locked command: `corpus build`,
    # `sweep`, `report`, `calibrate *`) reads `params.workspace_root()` directly, which none of the
    # per-module rebindings above reach — a monkeypatched attribute is invisible to a module that
    # was never told to look at it. The env var is the one redirection `params.workspace_root()`
    # itself understands, so it is the only kind that also reaches a child process, and it is what
    # keeps every test here from taking the lock in the operator's real `decision_lab/workspace/`.
    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(tmp_path / "workspace"))
    yield built_corpus(tmp_path, monkeypatch, shock_up=(5,), shock_down=(9,))


@pytest.fixture
def calibrated_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Path]]:
    """A corpus whose dataset also carries a pinned day set — what §10 requires and a sweep does
    not.

    Sixty days with four up-shocks and three down, which is `test_calibration_days`' own
    configuration and its arithmetic: nearest-rank p90 over sixty days admits exactly seven, so
    three per pool fits with one up-day to spare. `built_corpus_id`'s forty would admit five —
    `dataset days` would then refuse a pool for want of a day, and every test here would fail on
    the fixture rather than on the command it exercises.
    """
    from decision_lab import cli
    from decision_lab import gate as gate_module
    from decision_lab import longrun as longrun_module

    # Each of these imported `workspace_root` into its own namespace, so each needs its own
    # rebinding — `corpus`'s is done inside `built_corpus`. Missing one is not a failing test but
    # a test that writes into the operator's real `decision_lab/workspace/`: `longrun`'s absence
    # here put a `long-*` directory, database and all, beside their actual corpora.
    for module in (registry, gate_module, longrun_module):
        monkeypatch.setattr(module, "workspace_root", lambda: tmp_path / "workspace")
    # `jobs.WorkspaceLock`, taken by `cli.main` for every locked command this fixture's tests run
    # (`sweep`, `report`, `calibrate *`), reads `params.workspace_root()` directly — none of the
    # three rebindings above reach it, the same gap `built_corpus_id` closes the same way.
    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(tmp_path / "workspace"))
    corpus_id = built_corpus(
        tmp_path, monkeypatch, days=60, shock_up=(3, 11, 19, 27), shock_down=(7, 15, 23)
    )
    data = tmp_path / "history"
    assert cli.main(["dataset", "days", "--data", str(data)]) == cli.EXIT_OK
    yield corpus_id, data
