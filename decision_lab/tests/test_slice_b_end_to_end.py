"""Slice B end to end: corpus → regimes → scoring → report, offline and free (spec §16).

The slice's exit criterion, driven through `cli.main` exactly as an operator would — the same
shape slice A's end-to-end test takes, and for the same reason: a handler called directly proves
the handler, while the operator's failure is usually in the wiring between them.

On `SIM_PANEL` — three `varied-*` stub seats over the fifteen entries in `stub_responses.json` —
so the panel reaches BUY, SELL, HOLD *and* `no qualified majority`, and the scoring tables are
exercised rather than being three rows of the same verdict.
"""

from __future__ import annotations

import functools
import random
from pathlib import Path

import pytest

from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab.params import CORPUS_META
from decision_lab.tests import factories as f
from tradebot.decision.providers import registry
from tradebot.decision.providers.stub import StubLLMProvider

#: `SIM_PANEL`'s `varied-*` seats draw a vote per instrument from `stub_responses.json`. Unseeded
#: — which is right for a simulated run and wrong for a test. `StubLLMProvider` takes an `rng` for
#: exactly this, and its own docstring says so: "tests pass a seeded one, because a flaky suite
#: proves nothing".
#:
#: The seed is not arbitrary, and it is no longer about KNOWN_GAPS §5. That closed on 2026-10-29
#: (ADR 0030: the snapshot polls between its market and position reads); measured unpinned over
#: seeds 0-6 and 2024, none reached `exceeds holding`, an unbookable fill or a poisoned group.
#:
#: What the pin buys now is a reference pass that reaches the calibration days. An unseeded
#: `varied-*` pass legitimately auto-pauses on `max_consecutive_losses` before them — a replay has
#: no human to clear it — and then no rows exist: seeds 0, 2, 3 and 6 of those eight failed six
#: calibrate/sweep tests with exit 6. 2024 completes both fixtures at 228/228 cycles. Delete the pin
#: when `corpus build` gains a `--seed`, or when the fixture stops depending on the pass reaching
#: those days — not before.
STUB_SEED = 2024


def seeded_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        registry,
        "StubLLMProvider",
        functools.partial(StubLLMProvider, rng=random.Random(STUB_SEED)),
    )


def verified_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, days: int = 40, **shocks: object
) -> Path:
    """Write and verify a shocked dataset under `tmp_path`, with no reference pass over it.

    The cheap half of `built_corpus`, for tests that need the history and never read a corpus: the
    reference pass is most of the cost of every fixture built on it.
    """
    data = tmp_path / "history"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    # `corpus build` and `report`, both called through `cli.main` by this helper's callers, are
    # §12.4-locked: `main` takes `jobs.WorkspaceLock()` around them, which reads
    # `params.workspace_root()` directly — untouched by the rebinding above, since that only
    # redirects `corpus.py`'s own copy of the name. Left unset, every caller would take that lock
    # in the operator's real `decision_lab/workspace/`.
    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(workspace))
    seeded_stub(monkeypatch)
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=days, **shocks)})  # type: ignore[arg-type]

    assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    return data


def built_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, days: int = 40, **shocks: object
) -> str:
    """Verify a dataset and build one reference pass over it. Returns the corpus id.

    `days` is a parameter because the length decides how many shock days a pool can hold at all:
    nearest-rank admits exactly `days - ceil(0.9 * days) + 1` of them, which is five over the
    forty a scoring test needs and seven over the sixty `dataset days` needs to fill three pools
    (`test_calibration_days.DAYS`). A caller wanting a pinned day set must ask for the longer one.
    """
    data = verified_dataset(tmp_path, monkeypatch, days=days, **shocks)
    workspace = tmp_path / "workspace"
    assert (
        cli.main(
            ["corpus", "build", "--data", str(data), "--every", "4h", "--reference-panel", "sim"]
        )
        == cli.EXIT_OK
    )
    corpora = sorted(p for p in workspace.iterdir() if (p / CORPUS_META).is_file())
    assert len(corpora) == 1
    return corpora[0].name


def test_verify_build_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus_id = built_corpus(tmp_path, monkeypatch, shock_up=(5, 12, 19), shock_down=(8, 15, 22))
    out = tmp_path / "reports" / "slice-b.md"

    assert cli.main(["report", "--corpus", corpus_id, "--out", str(out)]) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    assert "NORMAL" in text and "SHOCK_UP" in text and "SHOCK_DOWN" in text
    assert "NEWS-BLIND RUN" in text
    assert corpus_id in text


def test_the_report_names_the_seats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The core question this slice exists to answer: which seat carried the result."""
    corpus_id = built_corpus(tmp_path, monkeypatch, shock_up=(5,), shock_down=(8,))
    out = tmp_path / "r.md"

    assert cli.main(["report", "--corpus", corpus_id, "--out", str(out)]) == cli.EXIT_OK

    text = out.read_text(encoding="utf-8")
    meta = cp.load(corpus_id, workspace=tmp_path / "workspace").meta
    for seat in meta.reference_basket.panel.seats:
        assert seat.seat_id in text
    assert "round 0" in text
