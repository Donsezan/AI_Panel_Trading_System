"""A named window overrides the automatic label and is reported twice (spec §8.2).

Both: inside its `SHOCK_UP` or `SHOCK_DOWN` aggregate, and on its own row, so an episode can be
read by name. Reporting it only by name would drop it out of the aggregate; only in the aggregate
would make "how did the panel handle the ETF approval" unanswerable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import regimes as rg
from decision_lab.calibration_days import Pool
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)

TOML = """
[[window]]
name = "spot ETF approval"
from = "2024-01-01T10:00:00Z"
to   = "2024-01-01T20:00:00Z"

[[window]]
name = "August carry unwind"
from = "2024-01-02T00:00:00Z"
to   = "2024-01-02T12:00:00Z"
"""


def test_windows_load_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "regimes.toml"
    path.write_text(TOML, encoding="utf-8")

    windows = rg.load_windows(path)

    assert [w.name for w in windows] == ["spot ETF approval", "August carry unwind"]
    assert windows[0].from_ == datetime(2024, 1, 1, 10, tzinfo=UTC)


def test_the_shipped_config_parses() -> None:
    """The file the repo ships is part of the contract, not an example."""
    windows = rg.load_windows(rg.DEFAULT_REGIMES_TOML)
    assert windows
    assert all(w.from_ < w.to for w in windows)


def test_a_missing_file_is_no_windows_not_a_refusal(tmp_path: Path) -> None:
    """Named windows are optional. The automatic labeller answers on its own."""
    assert rg.load_windows(tmp_path / "absent.toml") == ()


def test_an_inverted_window_refuses(tmp_path: Path) -> None:
    path = tmp_path / "regimes.toml"
    path.write_text(
        '[[window]]\nname = "x"\nfrom = "2024-02-01T00:00:00Z"\nto = "2024-01-01T00:00:00Z"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="ends before it begins"):
        rg.load_windows(path)


async def test_a_named_window_overrides_the_automatic_label(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 80)})
    index = await rg.index_dataset(
        ReplayDataset.load(tmp_path, clock), "1h", window_bars=10, shock_percentile=Decimal("0.90")
    )
    inside = f.EPOCH + timedelta(hours=12)
    assert index.label_at(inst.key, inside) is Pool.NORMAL

    path = tmp_path / "regimes.toml"
    path.write_text(TOML, encoding="utf-8")
    named = index.with_windows(rg.load_windows(path))

    window = named.window_at(inside)
    assert window is not None
    assert window.name == "spot ETF approval"
    assert named.label_at(inst.key, inside) is not Pool.NORMAL


async def test_an_instant_outside_every_window_has_none(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 80)})
    path = tmp_path / "regimes.toml"
    path.write_text(TOML, encoding="utf-8")
    index = (
        await rg.index_dataset(ReplayDataset.load(tmp_path, clock), "1h", window_bars=10)
    ).with_windows(rg.load_windows(path))

    assert index.window_at(f.EPOCH + timedelta(hours=60)) is None


async def test_a_windows_direction_comes_from_its_own_return(tmp_path: Path) -> None:
    """`SHOCK_UP` or `SHOCK_DOWN` for a named episode is measured, not declared in the file —
    a window named after a crash but holding a rally is a mislabelled file, and the data wins."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    rising = [str(100 + i) for i in range(80)]
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(rising)})
    path = tmp_path / "regimes.toml"
    path.write_text(TOML, encoding="utf-8")
    index = (
        await rg.index_dataset(ReplayDataset.load(tmp_path, clock), "1h", window_bars=10)
    ).with_windows(rg.load_windows(path))

    assert index.label_at(inst.key, f.EPOCH + timedelta(hours=12)) is Pool.SHOCK_UP
