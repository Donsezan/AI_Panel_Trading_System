"""Slice A end to end: verify → days → corpus, offline, deterministic and free (spec §16).

On the stub panel, so nothing reaches a provider. This is the slice's exit criterion — the three
commands an operator runs in order, against one dataset, producing the three artifacts every
later slice reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import calibration_days as cd
from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab.dataset import read_audit
from decision_lab.params import CORPUS_META
from decision_lab.tests import factories as f

#: The shock counts `test_calibration_days` explains: seven days above a nearest-rank p90 of 60.
SHOCK_UP = (3, 11, 19, 27)
SHOCK_DOWN = (7, 15, 23)


def corpora(workspace: Path) -> list[Path]:
    return sorted(p for p in workspace.iterdir() if (p / CORPUS_META).is_file())


def test_verify_then_days_then_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "history"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    bars = f.shocked_walk(days=60, shock_up=SHOCK_UP, shock_down=SHOCK_DOWN)
    f.write_dataset(data, {(f.instrument(), "1h"): bars})

    assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK
    assert read_audit(data).is_clean

    assert cli.main(["dataset", "days", "--data", str(data)]) == cli.EXIT_OK
    days = cd.read(data)
    assert len(days.all_days) == 9

    assert (
        cli.main(
            [
                "corpus",
                "build",
                "--data",
                str(data),
                "--every",
                "4h",
                "--reference-panel",
                "stub",
            ]
        )
        == cli.EXIT_OK
    )
    built = corpora(workspace)
    assert len(built) == 1

    corpus = cp.load(built[0].name, workspace=workspace)
    assert corpus.entries
    assert corpus.meta.cadence_seconds == 4 * 3600
    assert corpus.meta.dataset_digest == days.dataset_digest


def test_corpus_build_refuses_an_unverified_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "history"
    monkeypatch.setattr(cp, "workspace_root", lambda: tmp_path / "workspace")
    f.write_dataset(data, {(f.instrument(), "1h"): f.walk(["100"] * 500)})

    assert cli.main(["corpus", "build", "--data", str(data), "--every", "4h"]) == cli.EXIT_DATASET


def test_rebuilding_the_same_corpus_reuses_its_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11's premise one slice early: identical parameters are one experiment, not two."""
    data = tmp_path / "history"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=20)})
    assert cli.main(["dataset", "verify", "--data", str(data)]) == cli.EXIT_OK

    build = ["corpus", "build", "--data", str(data), "--every", "8h", "--reference-panel", "stub"]
    assert cli.main(build) == cli.EXIT_OK
    first = cp.load(corpora(workspace)[0].name, workspace=workspace)
    assert cli.main(build) == cli.EXIT_OK

    assert len(corpora(workspace)) == 1
    again = cp.load(corpora(workspace)[0].name, workspace=workspace)
    assert len(again.entries) == len(first.entries), "a second pass would have doubled the log"


def test_a_cadence_the_bot_has_no_timeframe_for_is_still_a_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`8h` is a legitimate `Schedule.every_seconds` and not a Binance kline timeframe.

    Reading `--every` through `market.timeframe_interval` would refuse four of the six cadences
    §5.5 lists as unsupported, which is why `params.CADENCE_SECONDS` exists.
    """
    data = tmp_path / "history"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=20)})
    cli.main(["dataset", "verify", "--data", str(data)])

    for cadence in ("2h", "12h"):
        assert (
            cli.main(
                [
                    "corpus",
                    "build",
                    "--data",
                    str(data),
                    "--every",
                    cadence,
                    "--reference-panel",
                    "stub",
                ]
            )
            == cli.EXIT_OK
        )

    assert len(corpora(workspace)) == 2, "each cadence is its own corpus (§5.5)"
