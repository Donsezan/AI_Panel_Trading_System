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
    yield built_corpus(tmp_path, monkeypatch, shock_up=(5,), shock_down=(9,))
