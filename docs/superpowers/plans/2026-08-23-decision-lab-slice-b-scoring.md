# decision_lab Slice B — regimes, scoring, and the report

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the question the bot has never been able to answer about itself — *given this evidence, was BUY the right call?* — for the panel that is actually configured, over six months of recorded history, split into ordinary volatility, rallies and crashes, and broken down to which seat carried the result.

**Architecture:** Four modules over slice A's corpus. `records.py` re-reads the corpus database and folds each cycle's `SNAPSHOT_FROZEN`, `DECISION_MADE`, `SEAT_RESPONDED` and `CYCLE_COMPLETED` into one `CycleRecord`. `regimes.py` labels every bar `NORMAL`, `SHOCK_UP` or `SHOCK_DOWN` from slice A's own volatility estimator, with named event windows overriding from a TOML file. `scoring.py` scores each decision against what the market did over the next H bars, using an ATR band read **off the frozen snapshot**, and then scores every seat's own vote against the same truth label. `render.py` writes it all to Markdown under `decision_lab/reports/`.

**Tech Stack:** Python 3.11, pydantic v2, stdlib `tomllib`, pytest, hypothesis, ruff, mypy. No new dependency.

**Spec:** [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../specs/2026-08-23-decision-lab-design.md) — §8 in full, §9 except §9.6, §14, the `report` row of §13, the matching rows of §15, and §16.1.

**Depends on:** Slice A (`2026-08-23-decision-lab-slice-a-corpus.md`). **Do not start until slice A is merged** — every number here is derived from a corpus, and a corpus built on an unaudited dataset may have its ATR band computed across a hole.

**Deliberately out of scope, and why:**

- **§9.6 cross-candidate tables** (agreement matrix, tradable divergence) belong to slice C. There is one configuration here — the reference panel — so there is nothing to compare it against yet.
- **§10 calibration scenarios** and the §10.6 gate belong to slice D.
- **News.** Every corpus is news-blind until slice E, so every report this slice writes carries `NEWS-BLIND RUN` (§6.9).

**The one thing that must not be deferred:** §18 is explicit — *"B carries the direction split, and it must not be deferred into D. Adding `SHOCK_UP` and `SHOCK_DOWN` after §9's tables exist means rewriting every one of them; landing it with the labeller costs nothing."* Task 1 is therefore the direction split, and every table from Task 4 onward is keyed on the three-way label from the start.

## Global Constraints

- **Nothing under `tradebot/` may name `decision_lab`,** and this slice modifies **no** file under `tradebot/`. `test_separation.py` from slice A enforces the first; `git diff --stat main -- tradebot/` must stay empty.
- **No `float` anywhere in `decision_lab/`.** Slice A's `test_discipline.py` enforces it. Every price, band, volatility, ratio and percentage here is a `Decimal` computed through `tradebot.core.money`.
- **No metric is ever shown without its regime split** (§8.3). A blended accuracy over a period containing one violent week describes neither week. Every table carries `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN`, and one row per named window.
- **`SHOCK_UP` and `SHOCK_DOWN` are never pooled** (§10.3). They ask opposite questions of a long-only system, and a blended figure hides both.
- **Unscored decisions are counted with their reason, never dropped** (§9.4). There are exactly three reasons — `gap`, `horizon`, `no ATR` — and adding a fourth is a spec change, not an implementation detail.
- **ATR is read off the frozen snapshot, never recomputed** (§9.2, §2.4). The band is then derived from exactly the evidence the panel had.
- **The report is written to a file, never printed** (§14), exactly as `report promotion` and `report shadow` are.
- **Reuse, never reimplement.** `reach_consensus`, `Action.is_tradable`, `total_cost`, `CandleSeries` and the volatility estimator from slice A are imported. A second consensus rule in this package would make the swing rate a measurement of the copy.
- **Line length 100**, `ruff format`, `from __future__ import annotations`, full annotations.
- Verification: `.\decision_lab\check.ps1`, and `.\check.ps1` at the repo root must still pass.

---

### Task 1: Regime labelling, with the direction split

**Files:**
- Create: `decision_lab/regimes.py`
- Test: `decision_lab/tests/test_regimes.py`

**Interfaces:**
- Consumes: `decision_lab.calibration_days.Pool`, `decision_lab.volatility.{realised_volatility, window_return, percentile}`, `decision_lab.params.{DEFAULT_VOL_WINDOW_BARS, DEFAULT_SHOCK_PERCENTILE}`, `tradebot.core.market.Candle`.
- Produces:
  - `RegimeLabel = Pool` — **the same enum slice A pinned days with**, aliased, never redeclared
  - `class BarLabel(DomainModel)` with `close_time: UtcDatetime`, `label: RegimeLabel`, `volatility: Money`, `window_return_: Money`
  - `regimes.label_bars(candles, *, window_bars, shock_percentile) -> tuple[BarLabel, ...]`
  - `class RegimeIndex` (frozen dataclass) with `labels: dict[str, tuple[BarLabel, ...]]`, `threshold: dict[str, Money]`, and `label_at(instrument_key: str, as_of: datetime) -> RegimeLabel`
  - `regimes.index_dataset(dataset, timeframe, *, window_bars, shock_percentile) -> RegimeIndex` (awaitable)

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_regimes.py`:

```python
"""A shock carries its sign, and that is the whole point (spec §8.1).

Realised volatility is a magnitude, so it is direction-blind. The system is long-only: an
up-shock asks *did the seats catch the move*, a down-shock asks *did the seats protect capital*.
Those are opposite competences, and a blended `SHOCK` figure averages them and hides both.
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
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


def rising_then_calm(*, calm: int = 60, shock: int = 10) -> tuple:
    """A long calm stretch, then a violent rally. The rally must label `SHOCK_UP`."""
    closes = ["100"] * calm + [str(100 + 5 * i) for i in range(1, shock + 1)]
    return f.walk(closes)


def falling_then_calm(*, calm: int = 60, shock: int = 10) -> tuple:
    closes = ["100"] * calm + [str(100 - 5 * i) for i in range(1, shock + 1)]
    return f.walk(closes)


def test_a_calm_stretch_is_normal() -> None:
    labels = rg.label_bars(rising_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert labels[30].label is Pool.NORMAL


def test_a_rally_is_shock_up() -> None:
    labels = rg.label_bars(rising_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert labels[-1].label is Pool.SHOCK_UP


def test_a_crash_is_shock_down() -> None:
    labels = rg.label_bars(falling_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert labels[-1].label is Pool.SHOCK_DOWN


def test_the_same_magnitude_in_two_directions_gets_two_labels() -> None:
    """The failure this split exists to prevent: one `SHOCK` bucket averaging both."""
    up = rg.label_bars(rising_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    down = rg.label_bars(falling_then_calm(), window_bars=10, shock_percentile=Decimal("0.90"))
    assert up[-1].volatility == down[-1].volatility
    assert up[-1].label is not down[-1].label


def test_a_flat_window_at_or_above_the_threshold_breaks_up() -> None:
    """A tie-break, never a judgement, and the same default §4.5's `classify` takes."""
    assert rg.direction_of(Decimal(0)) is Pool.SHOCK_UP
    assert rg.direction_of(Decimal("-0.0001")) is Pool.SHOCK_DOWN


def test_every_bar_gets_exactly_one_label() -> None:
    candles = rising_then_calm()
    labels = rg.label_bars(candles, window_bars=10, shock_percentile=Decimal("0.90"))
    assert len(labels) == len(candles)
    assert [b.close_time for b in labels] == [c.close_time for c in candles]


def test_the_threshold_is_the_instruments_own_distribution() -> None:
    """A 90th percentile taken across two instruments would make the quieter one all-normal and
    the louder one all-shock, which measures the instruments rather than the panel."""
    quiet = rg.label_bars(f.walk([str(100 + i % 2) for i in range(80)]), window_bars=10, shock_percentile=Decimal("0.90"))
    assert any(b.label is not Pool.NORMAL for b in quiet), "a quiet series still has a top decile"


async def test_the_index_answers_for_an_instant(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): rising_then_calm()})
    dataset = ReplayDataset.load(tmp_path, clock)

    index = await rg.index_dataset(dataset, "1h", window_bars=10, shock_percentile=Decimal("0.90"))

    last_close = f.EPOCH + timedelta(hours=70)
    assert index.label_at(inst.key, last_close) is Pool.SHOCK_UP


async def test_the_index_answers_between_bars_with_the_last_closed_one(tmp_path: Path) -> None:
    """A decision is taken at an instant, not on a bar boundary. The label is the most recent
    *closed* bar's, the same point-in-time rule `CandleSeries.point_in_time` applies."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): rising_then_calm()})
    index = await rg.index_dataset(
        ReplayDataset.load(tmp_path, clock), "1h", window_bars=10, shock_percentile=Decimal("0.90")
    )

    mid_bar = f.EPOCH + timedelta(hours=69, minutes=30)
    assert index.label_at(inst.key, mid_bar) is index.label_at(inst.key, f.EPOCH + timedelta(hours=69))


async def test_an_instant_before_the_series_refuses(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): rising_then_calm()})
    index = await rg.index_dataset(
        ReplayDataset.load(tmp_path, clock), "1h", window_bars=10, shock_percentile=Decimal("0.90")
    )

    with pytest.raises(KeyError):
        index.label_at(inst.key, f.EPOCH - timedelta(hours=1))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_regimes.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.regimes'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/regimes.py`:

```python
"""Which market each decision was taken in (spec §8).

Every bar of the scoring timeframe gets a label from its own realised volatility relative to that
instrument's own distribution across the dataset. Per instrument, because a 90th percentile taken
across two instruments measures which of them is louder rather than which bars were violent.

**A shock carries its sign.** Realised volatility is a magnitude and therefore direction-blind,
and the direction is the whole question: the system is long-only, so an up-shock asks whether the
seats caught the move and a down-shock asks whether they protected capital. Those are opposite
competences. A blended `SHOCK` figure averages them and hides both — the same sin §8.3 forbids one
level up.

The estimator is `decision_lab.volatility`, the one §4.5 draws calibration days with. Same
measurement, different window: §4.5 measures a calendar day, this measures a trailing 30 bars. A
day pinned as a shock is therefore a day whose bars this labeller also calls a shock, and the
report's regime rows cannot disagree with its day set.

Bars near the start of a series are measured over the bars available rather than skipped. The
corpus's own indicator warm-up (`BacktestHarness.warmup_for`) puts every real decision hundreds of
bars past that region, so it is a definition that keeps the function total rather than one that
affects a number anybody reads.

Failure semantics: `label_at` raises `KeyError` for an instrument the index does not hold or an
instant before its first bar. Both are defects in the caller — a decision the dataset has no
prices for cannot be scored either — and a silent default would put a real decision in the wrong
regime.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from decision_lab.calibration_days import Pool
from decision_lab.dataset import read_series
from decision_lab.params import DEFAULT_SHOCK_PERCENTILE, DEFAULT_VOL_WINDOW_BARS
from decision_lab.volatility import percentile, realised_volatility, window_return
from tradebot.core.market import Candle
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.marketdata.recorder import ReplayDataset

#: The regime vocabulary is `Pool`, aliased rather than redeclared: a day pinned `SHOCK_DOWN` and
#: a bar labelled `SHOCK_DOWN` must be the same string, or the report joins on nothing.
RegimeLabel = Pool


def direction_of(window_return_: Decimal) -> RegimeLabel:
    """Which way a shock went. Zero breaks up — a tie-break, never a judgement (§8.1)."""
    return RegimeLabel.SHOCK_DOWN if window_return_ < ZERO else RegimeLabel.SHOCK_UP


class BarLabel(DomainModel):
    """One bar's regime, and the two numbers that produced it."""

    close_time: UtcDatetime
    label: RegimeLabel
    volatility: Money
    window_return_: Money


def label_bars(
    candles: Sequence[Candle],
    *,
    window_bars: int = DEFAULT_VOL_WINDOW_BARS,
    shock_percentile: Decimal = DEFAULT_SHOCK_PERCENTILE,
) -> tuple[BarLabel, ...]:
    """Label every bar. Two passes: measure the whole series, then threshold it against itself."""
    measured = [
        (
            candle.close_time,
            realised_volatility(window := candles[max(0, index - window_bars + 1) : index + 1]),
            window_return(window),
        )
        for index, candle in enumerate(candles)
    ]
    threshold = percentile([vol for _, vol, _ in measured], shock_percentile)
    return tuple(
        BarLabel(
            close_time=close_time,
            label=direction_of(ret) if vol >= threshold else RegimeLabel.NORMAL,
            volatility=vol,
            window_return_=ret,
        )
        for close_time, vol, ret in measured
    )


@dataclass(frozen=True, slots=True)
class RegimeIndex:
    """Every instrument's labelled bars, answerable at an arbitrary instant."""

    timeframe: str
    window_bars: int
    shock_percentile: Money
    labels: dict[str, tuple[BarLabel, ...]]
    threshold: dict[str, Money]

    def label_at(self, instrument_key: str, as_of: datetime) -> RegimeLabel:
        """The label of the most recent bar to have *closed* at or before `as_of`.

        The same point-in-time rule `CandleSeries.point_in_time` applies to prices: a bar still
        forming at `as_of` is not a fact yet, and a decision taken mid-bar was taken knowing only
        the bars behind it.
        """
        bars = self.labels[instrument_key]
        index = bisect.bisect_right([bar.close_time for bar in bars], as_of) - 1
        if index < 0:
            raise KeyError(
                f"{instrument_key} has no bar closed at or before {as_of.isoformat()}; "
                f"its series starts at {bars[0].close_time.isoformat()}"
            )
        return bars[index].label


async def index_dataset(
    dataset: ReplayDataset,
    timeframe: str,
    *,
    window_bars: int = DEFAULT_VOL_WINDOW_BARS,
    shock_percentile: Decimal = DEFAULT_SHOCK_PERCENTILE,
) -> RegimeIndex:
    """Label every instrument's scoring-timeframe series, each against its own distribution."""
    labels: dict[str, tuple[BarLabel, ...]] = {}
    threshold: dict[str, Money] = {}
    for instrument in dataset.instruments:
        series = await read_series(dataset, instrument, timeframe)
        bars = label_bars(
            series.candles, window_bars=window_bars, shock_percentile=shock_percentile
        )
        labels[instrument.key] = bars
        threshold[instrument.key] = percentile(
            [bar.volatility for bar in bars], shock_percentile
        )
    return RegimeIndex(
        timeframe=timeframe,
        window_bars=window_bars,
        shock_percentile=shock_percentile,
        labels=labels,
        threshold=threshold,
    )
```

The walrus inside the list comprehension in `label_bars` is compact but reads poorly; if ruff or review objects, hoist it to an explicit loop. The behaviour is what matters: the window is `candles[max(0, i - window + 1) : i + 1]`, inclusive of the bar being labelled.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_regimes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/regimes.py decision_lab/tests/test_regimes.py
git commit -m "feat(decision_lab): label every bar NORMAL, SHOCK_UP or SHOCK_DOWN"
```

---

### Task 2: Named event windows

**Files:**
- Modify: `decision_lab/regimes.py` (add `EventWindow`, `load_windows`, and window override on `RegimeIndex`)
- Create: `decision_lab/config/regimes.toml`
- Test: `decision_lab/tests/test_regime_windows.py`

**Interfaces:**
- Consumes: Task 1's `RegimeIndex`.
- Produces:
  - `class EventWindow(DomainModel)` with `name: str`, `from_: UtcDatetime` (alias `from`), `to: UtcDatetime`
  - `regimes.load_windows(path: Path) -> tuple[EventWindow, ...]`
  - `RegimeIndex.windows: tuple[EventWindow, ...]` and `RegimeIndex.window_at(as_of) -> EventWindow | None`
  - `RegimeIndex.with_windows(windows) -> RegimeIndex`
  - `regimes.DEFAULT_REGIMES_TOML: Path`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_regime_windows.py`:

```python
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
    path.write_text('[[window]]\nname = "x"\nfrom = "2024-02-01T00:00:00Z"\nto = "2024-01-01T00:00:00Z"\n', encoding="utf-8")
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

    assert named.window_at(inside) is not None
    assert named.window_at(inside).name == "spot ETF approval"
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_regime_windows.py -q`
Expected: FAIL — `AttributeError: module 'decision_lab.regimes' has no attribute 'load_windows'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/config/regimes.toml`:

```toml
# Named event windows (spec §8.2). A window overrides the automatic label for the bars inside it,
# keeps its own measured direction, and is reported **both** inside its SHOCK_UP or SHOCK_DOWN
# aggregate and on its own row — so an episode can be read by name without vanishing from the
# totals.
#
# The direction is measured from the window's own bars, never declared here: a window named after
# a crash that in fact holds a rally is a mislabelled file, and the data wins.
#
# These two are the episodes the 2024 dataset the tool is first pointed at contains.

[[window]]
name = "spot ETF approval"
from = "2024-01-10T00:00:00Z"
to   = "2024-01-16T00:00:00Z"

[[window]]
name = "August carry unwind"
from = "2024-08-02T00:00:00Z"
to   = "2024-08-09T00:00:00Z"
```

Append to `decision_lab/regimes.py` (adding `tomllib`, `Path`, `ConfigError`, `Field`, `ConfigDict` to the imports):

```python
#: The windows the repo ships. Overridable with `--regimes`.
DEFAULT_REGIMES_TOML: Final = Path(__file__).parent / "config" / "regimes.toml"


class EventWindow(DomainModel):
    """One named episode. Its direction is measured from its bars, never declared (§8.2)."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime

    def covers(self, moment: datetime) -> bool:
        return self.from_ <= moment < self.to


def load_windows(path: Path) -> tuple[EventWindow, ...]:
    """Read `regimes.toml`, or nothing at all when there is no such file.

    Absent is not a refusal: named windows are an annotation on top of the automatic labeller,
    which answers on its own. A *malformed* file is a refusal, because a window silently dropped
    would move numbers on a report that still claims to cover the episode.
    """
    if not path.is_file():
        return ()
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    windows = tuple(EventWindow.model_validate(row) for row in document.get("window", ()))
    for window in windows:
        if window.from_ >= window.to:
            raise ConfigError(
                f"named window {window.name!r} ends before it begins "
                f"({window.from_.isoformat()} → {window.to.isoformat()})"
            )
    return windows
```

and give `RegimeIndex` its windows — add the field and two methods:

```python
    #: Named episodes that override the automatic label for the bars they cover (§8.2).
    windows: tuple[EventWindow, ...] = ()

    def with_windows(self, windows: Sequence[EventWindow]) -> RegimeIndex:
        return replace(self, windows=tuple(windows))

    def window_at(self, as_of: datetime) -> EventWindow | None:
        """The named episode covering this instant, if any. First match wins."""
        return next((window for window in self.windows if window.covers(as_of)), None)
```

and change `label_at` so a named window overrides:

```python
    def label_at(self, instrument_key: str, as_of: datetime) -> RegimeLabel:
        """The bar's label, unless a named window covers the instant and says otherwise.

        A named window is an operator's assertion that this period is an episode, and the label
        it carries is measured over the *window's* own bars rather than the trailing 30. That is
        the point of naming it: an episode is a shape a fixed-width window can straddle.
        """
        window = self.window_at(as_of)
        if window is not None:
            return self._window_label(instrument_key, window)
        bars = self.labels[instrument_key]
        index = bisect.bisect_right([bar.close_time for bar in bars], as_of) - 1
        if index < 0:
            raise KeyError(
                f"{instrument_key} has no bar closed at or before {as_of.isoformat()}; "
                f"its series starts at {bars[0].close_time.isoformat()}"
            )
        return bars[index].label

    def _window_label(self, instrument_key: str, window: EventWindow) -> RegimeLabel:
        covered = [
            bar for bar in self.labels[instrument_key] if window.covers(bar.close_time)
        ]
        if not covered:
            raise KeyError(f"{instrument_key} has no bars inside window {window.name!r}")
        # Measured the same way §4.5 measures a day: the window's own realised volatility against
        # the instrument's own threshold, with the sign from its own return.
        returns = [bar.window_return_ for bar in covered]
        volatility = max(bar.volatility for bar in covered)
        if volatility < self.threshold[instrument_key]:
            return RegimeLabel.NORMAL
        return direction_of(sum(returns, start=ZERO))
```

`RegimeIndex` is a frozen slotted dataclass, so `with_windows` uses `dataclasses.replace` — add `from dataclasses import dataclass, replace` and `from typing import Final` to the imports. `_window_label` recomputes per call; if profiling on a six-month corpus shows it matters, memoise it in a `dict[(instrument_key, window.name)]` built in `with_windows` — not before.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_regime_windows.py decision_lab/tests/test_regimes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/regimes.py decision_lab/config/regimes.toml decision_lab/tests/test_regime_windows.py
git commit -m "feat(decision_lab): named event windows override the automatic regime label"
```

---

### Task 3: Reading the reference pass back out of the corpus

**Files:**
- Create: `decision_lab/records.py`
- Test: `decision_lab/tests/test_records.py`

**Interfaces:**
- Consumes: `decision_lab.corpus.{corpus_dir, entry_from_payload, CorpusMeta}`, `tradebot.core.decision.{Decision, SeatResponse}`, `tradebot.core.events.EventType`, `tradebot.persistence.{database.open_database, store.EventStore}`.
- Produces:
  - `class CycleRecord(DomainModel)` with `cycle_id: str`, `basket_id: str`, `as_of: UtcDatetime`, `snapshot: ContextSnapshot`, `decisions: tuple[Decision, ...]`, `responses: tuple[SeatResponse, ...]`, `outcome: str`, `cost_usd: Money`, and the properties `decision_for(key) -> Decision | None`, `final_round_for(key) -> tuple[SeatResponse, ...]`, `round_zero_for(key) -> tuple[SeatResponse, ...]`, `degraded: bool`
  - `records.records_from_store(store: EventStore) -> tuple[CycleRecord, ...]`
  - `records.load(corpus_id: str, *, workspace: Path | None = None) -> tuple[CorpusMeta, tuple[CycleRecord, ...]]`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_records.py`:

```python
"""One cycle, folded from the four event types it wrote (spec §5.1, §9.7).

Read from the log rather than from a projection, for the same reason `validation/evidence.py`
does: the facts a score turns on — every seat's vote, including the abstentions, and the round it
was cast in — have no projector at all, and the log is the audit artifact.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab import records as rc
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.enums import Action
from tradebot.marketdata.recorder import ReplayDataset


@pytest.fixture
async def built(tmp_path: Path) -> tuple[Path, str]:
    """A real corpus over the `sim` panel: three `varied-*` stub seats, offline and free."""
    clock = ManualClock(f.EPOCH)
    data = tmp_path / "history"
    workspace = tmp_path / "ws"
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=20, shock_up=(4,), shock_down=(9,))})
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    corpus = await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel="sim",
        cadence_seconds=4 * 3600,
        start_equity=Decimal(10_000),
    )
    return workspace, corpus.meta.corpus_id


async def test_every_cycle_carries_its_snapshot_and_its_decisions(built) -> None:
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    assert cycles
    assert all(cycle.snapshot.instruments for cycle in cycles)
    assert any(cycle.decisions for cycle in cycles)


async def test_every_seat_response_is_kept_including_abstentions(built) -> None:
    """An abstention is a fact about a seat, and §9.7's abstention rate is made of them."""
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    responded = [r for cycle in cycles for r in cycle.responses]
    assert responded
    assert all(r.seat_id for r in responded)


async def test_round_zero_and_the_final_round_are_separable(built) -> None:
    """§9.7: under `blind_then_debate` a later vote is contaminated by peers *by design*, so
    'which seat reasons well' and 'which seat is easily talked round' are different questions."""
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)
    cycle = next(c for c in cycles if c.responses)
    key = cycle.snapshot.instruments[0].instrument.key

    assert all(r.round_index == 0 for r in cycle.round_zero_for(key))
    finals = cycle.final_round_for(key)
    assert finals
    assert len({r.round_index for r in finals}) == 1


async def test_the_outcome_and_cost_come_off_cycle_completed(built) -> None:
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    assert all(cycle.outcome for cycle in cycles)
    assert all(cycle.cost_usd >= Decimal(0) for cycle in cycles)


async def test_a_degraded_cycle_is_identifiable(built) -> None:
    """§9.5: a candidate that scores well on the cycles it answered while failing a third of them
    is not a better panel, so the degradation rate has to be countable."""
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)

    assert all(isinstance(cycle.degraded, bool) for cycle in cycles)


async def test_decisions_are_addressable_by_instrument(built) -> None:
    workspace, corpus_id = built
    _, cycles = rc.load(corpus_id, workspace=workspace)
    cycle = next(c for c in cycles if c.decisions)
    key = cycle.decisions[0].instrument_key

    found = cycle.decision_for(key)
    assert found is not None
    assert found.action in set(Action)
    assert cycle.decision_for("binance:NOPE/USDT") is None


async def test_the_meta_travels_with_the_records(built) -> None:
    """Scoring needs the reference `PanelConfig` for §9.7's swing rate, and the dataset directory
    for the forward prices. Both are on the meta slice A wrote."""
    workspace, corpus_id = built
    meta, _ = rc.load(corpus_id, workspace=workspace)

    assert meta.reference_basket.panel.seats
    assert meta.dataset_directory
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_records.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.records'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/records.py`:

```python
"""One cycle of the reference pass, folded from the log (spec §5.1).

Slice A's `Corpus` holds the frozen contexts; scoring needs three more things per cycle: what the
panel decided, what every seat voted in every round, and how the cycle ended. All four come from
the same workspace database, grouped by `cycle_id`.

Read from the **log**, not from a projection, for the reason ADR 0016 gives: the facts a score
turns on — an abstention, a losing argument, the round a vote was cast in — have no projector at
all. `EventStore.read_types` narrows to exactly the four types, which is what keeps loading a
six-month corpus affordable.

Failure semantics: a compacted `SEAT_RESPONDED` has lost its `raw_text` and nothing else, so it
still scores; a compacted `SNAPSHOT_FROZEN` has lost the whole context and refuses, in
`corpus.entry_from_payload`. Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from decision_lab.corpus import CorpusMeta, corpus_dir, entry_from_payload
from decision_lab.params import CORPUS_META
from tradebot.core.decision import Decision, SeatResponse, total_cost
from tradebot.core.errors import ConfigError
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import ContextSnapshot
from tradebot.persistence.database import open_database
from tradebot.persistence.store import EventStore
# The flag `reach_consensus` sets on a WAIT from a panel that could not answer. Imported rather
# than restated: a second copy of the string would silently stop matching if the bot renamed it,
# and the degradation rate would quietly read zero.

from tradebot.decision.consensus import PANEL_DEGRADED

_TYPES = (
    EventType.SNAPSHOT_FROZEN,
    EventType.DECISION_MADE,
    EventType.SEAT_RESPONDED,
    EventType.CYCLE_COMPLETED,
)


class CycleRecord(DomainModel):
    """Everything one cycle of the reference pass produced."""

    cycle_id: str
    basket_id: str
    as_of: UtcDatetime
    snapshot: ContextSnapshot
    decisions: tuple[Decision, ...] = ()
    responses: tuple[SeatResponse, ...] = ()
    outcome: str = ""
    cost_usd: Money = ZERO

    def decision_for(self, instrument_key: str) -> Decision | None:
        return next((d for d in self.decisions if d.instrument_key == instrument_key), None)

    def responses_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        return tuple(r for r in self.responses if r.instrument_key == instrument_key)

    def round_zero_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        """The seat's own independent opinion, before any peer argued with it (§9.7)."""
        return tuple(r for r in self.responses_for(instrument_key) if r.round_index == 0)

    def final_round_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        """The votes the consensus rule actually read. Mirrors `Deliberation.final_round_for`."""
        about = self.responses_for(instrument_key)
        if not about:
            return ()
        last = max(r.round_index for r in about)
        return tuple(r for r in about if r.round_index == last)

    @property
    def rounds(self) -> int:
        return max((r.round_index for r in self.responses), default=0) + 1

    @property
    def degraded(self) -> bool:
        """Did the panel fail to answer? `WAIT (PANEL_DEGRADED)` on any instrument (§9.5)."""
        return any(PANEL_DEGRADED in d.flags for d in self.decisions)


def records_from_store(store: EventStore) -> tuple[CycleRecord, ...]:
    """Fold the log's four relevant types into one record per cycle, in cycle order."""
    grouped: dict[str, list[Event]] = {}
    for event in store.read_types(*_TYPES):
        grouped.setdefault(event.cycle_id or "", []).append(event)

    records = []
    for cycle_id, events in grouped.items():
        frozen = next((e for e in events if e.type is EventType.SNAPSHOT_FROZEN), None)
        if frozen is None:
            # A cycle that failed before freezing its snapshot. There is nothing to score it on,
            # and counting it as a decision would flatter or damn a panel that never ran.
            continue
        entry = entry_from_payload(
            seq=frozen.seq or 0,
            cycle_id=cycle_id,
            basket_id=frozen.basket_id or "",
            payload=frozen.payload,
        )
        completed = next((e for e in events if e.type is EventType.CYCLE_COMPLETED), None)
        responses = tuple(
            SeatResponse.model_validate(e.payload["response"])
            for e in events
            if e.type is EventType.SEAT_RESPONDED
        )
        records.append(
            CycleRecord(
                cycle_id=cycle_id,
                basket_id=entry.basket_id,
                as_of=entry.as_of,
                snapshot=entry.snapshot,
                decisions=tuple(
                    Decision.model_validate(e.payload["decision"])
                    for e in events
                    if e.type is EventType.DECISION_MADE
                ),
                responses=responses,
                outcome=str(completed.payload.get("outcome", "")) if completed else "",
                # From the responses rather than from the event's own field, so `basket` mode —
                # one provider call answering for N instruments — is not counted N times. That
                # de-duplication is `total_cost`'s job and only its job (DESIGN §6.5).
                cost_usd=total_cost(responses),
            )
        )
    return tuple(sorted(records, key=lambda record: record.as_of))


def load(
    corpus_id: str, *, workspace: Path | None = None
) -> tuple[CorpusMeta, tuple[CycleRecord, ...]]:
    """Re-open a built corpus and read its cycles. `open_database` never migrates."""
    directory = corpus_dir(corpus_id, workspace=workspace)
    meta_path = directory / CORPUS_META
    if not meta_path.is_file():
        raise ConfigError(
            f"no corpus {corpus_id!r} in {directory.parent}. Build one with "
            "`python -m decision_lab corpus build --data … --every …`"
        )
    meta = CorpusMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    engine = open_database(directory / "corpus.db")
    try:
        return meta, records_from_store(EventStore(engine, None))
    finally:
        engine.dispose()
```

If `EventStore` requires a non-`None` writer, mirror whatever slice A's `corpus.load` settled on — the two must construct it identically.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_records.py -q`
Expected: PASS.

`test_round_zero_and_the_final_round_are_separable` needs a panel that debates. `SIM_PANEL`'s protocol must be `blind_then_debate` with `max_rounds > 1` for round 0 and the final round to differ; check `tradebot/decision/presets.py:183` and, if it is `single_round`, assert that the two are *identical* instead — that is the §9.7 behaviour for `single_round` and is equally worth pinning.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/records.py decision_lab/tests/test_records.py
git commit -m "feat(decision_lab): fold each reference cycle out of the corpus log"
```

---

### Task 4: The truth label, the band, and the verdict

This is the heart of the slice. Get the long-only asymmetry wrong and the tool systematically punishes exactly the conservative behaviour the bot is built for.

**Files:**
- Create: `decision_lab/scoring.py`
- Test: `decision_lab/tests/test_scoring_truth.py`

**Interfaces:**
- Consumes: `decision_lab.params.{DEFAULT_BAND_K, DEFAULT_HORIZON_BARS}`, `decision_lab.dataset.read_series`, `tradebot.core.enums.Action`, `tradebot.core.snapshot.InstrumentContext`, `tradebot.indicators.library.REGISTRY`.
- Produces:
  - `class Truth(StrEnum)`: `BUY`, `STAND_ASIDE`, `ADD`, `EXIT`, `HOLD`
  - `class Verdict(StrEnum)`: `CORRECT`, `WRONG`, `UNSCORED_GAP`, `UNSCORED_HORIZON`, `UNSCORED_NO_ATR`
  - `scoring.CORRECT_ACTIONS: dict[Truth, frozenset[Action]]`
  - `scoring.truth_for(*, holding: bool, move: Decimal, band: Decimal) -> Truth`
  - `class ScoringParams(DomainModel)` with `timeframe: str`, `band_k: Money`, `horizon_bars: int`, `atr_lookback_bars: int`
  - `class Forward(DomainModel)` with `p0`, `p_h`, `move`, `mfe`, `mae`
  - `class PriceIndex` with `forward(instrument_key, as_of, horizon) -> Forward | None` and `crosses_hole(instrument_key, as_of, params) -> bool`
  - `scoring.build_price_index(dataset, audit, params) -> PriceIndex` (awaitable)
  - `class ScoredDecision(DomainModel)` — the per-(cycle, instrument) result
  - `scoring.score_decision(*, context, decision, forward, band, regime, window_name) -> ScoredDecision`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_scoring_truth.py`:

```python
"""The truth label is long-only aware (spec §9.3).

The system is long-only — Tier-1 refuses otherwise — so standing aside from a fall while flat is
**correct**, not a missed short. Getting this backwards would systematically punish exactly the
conservative behaviour the bot is built for, and it is what makes `SHOCK_DOWN` a test the bot can
pass rather than a period it is doomed to score badly in.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from decision_lab import scoring as sc
from tradebot.core.enums import Action

BAND = Decimal("10")


@pytest.mark.parametrize(
    "holding,move,expected",
    [
        (False, Decimal("15"), sc.Truth.BUY),
        (False, Decimal("10"), sc.Truth.STAND_ASIDE),  # exactly the band is not "> band"
        (False, Decimal("5"), sc.Truth.STAND_ASIDE),
        (False, Decimal("-50"), sc.Truth.STAND_ASIDE),  # a fall while flat: nothing was missed
        (True, Decimal("15"), sc.Truth.ADD),
        (True, Decimal("-15"), sc.Truth.EXIT),
        (True, Decimal("5"), sc.Truth.HOLD),
        (True, Decimal("-10"), sc.Truth.HOLD),  # exactly the band is inside it
    ],
)
def test_the_truth_table(holding: bool, move: Decimal, expected: sc.Truth) -> None:
    assert sc.truth_for(holding=holding, move=move, band=BAND) is expected


@pytest.mark.parametrize(
    "truth,correct",
    [
        (sc.Truth.BUY, {Action.BUY}),
        (sc.Truth.STAND_ASIDE, {Action.WAIT, Action.HOLD}),
        (sc.Truth.ADD, {Action.BUY, Action.HOLD}),
        (sc.Truth.EXIT, {Action.SELL}),
        (sc.Truth.HOLD, {Action.HOLD, Action.WAIT}),
    ],
)
def test_the_correct_actions_are_exactly_the_spec_table(truth: sc.Truth, correct: set) -> None:
    assert sc.CORRECT_ACTIONS[truth] == frozenset(correct)


def test_every_truth_has_correct_actions() -> None:
    """A truth with no correct action would score every decision WRONG and nobody would notice."""
    assert set(sc.CORRECT_ACTIONS) == set(sc.Truth)
    assert all(actions for actions in sc.CORRECT_ACTIONS.values())


def test_standing_aside_from_a_crash_while_flat_is_correct() -> None:
    """The single most important row. A long-only system cannot short a fall."""
    truth = sc.truth_for(holding=False, move=Decimal("-500"), band=BAND)
    assert Action.WAIT in sc.CORRECT_ACTIONS[truth]


def test_holding_through_a_crash_is_wrong() -> None:
    """And the mirror: while holding, the same fall demanded an exit."""
    truth = sc.truth_for(holding=True, move=Decimal("-500"), band=BAND)
    assert Action.HOLD not in sc.CORRECT_ACTIONS[truth]
    assert sc.CORRECT_ACTIONS[truth] == frozenset({Action.SELL})


def test_the_verdict_is_scale_invariant() -> None:
    """§16 property row: the same ATR-relative move scores identically for BTC and XRP."""
    for scale in (Decimal(1), Decimal("0.00001"), Decimal(100_000)):
        assert (
            sc.truth_for(holding=False, move=Decimal("15") * scale, band=BAND * scale)
            is sc.Truth.BUY
        )
        assert (
            sc.truth_for(holding=True, move=Decimal("-15") * scale, band=BAND * scale)
            is sc.Truth.EXIT
        )


def test_a_zero_band_refuses() -> None:
    """A zero ATR makes every move a breakout. That is a broken snapshot, not a verdict."""
    with pytest.raises(ValueError, match="positive band"):
        sc.truth_for(holding=False, move=Decimal("1"), band=Decimal(0))
```

Create `decision_lab/tests/test_scoring_verdicts.py`:

```python
"""Every decision gets a verdict, and an unscorable one is counted with its reason (spec §9.4).

A run that quietly dropped them would report accuracy over a subset it chose after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import dataset as ds
from decision_lab import scoring as sc
from decision_lab.calibration_days import Pool
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, SizeHint
from tradebot.core.market import Quote
from tradebot.core.snapshot import IndicatorReading, InstrumentContext, PositionView
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


def context(*, atr: str = "5", price: str = "100", holding: bool = False) -> InstrumentContext:
    inst = f.instrument()
    return InstrumentContext(
        instrument=inst,
        quote=Quote(
            instrument_key=inst.key,
            bid=Decimal(price),
            ask=Decimal(price),
            last=Decimal(price),
            observed_at=f.EPOCH,
        ),
        indicators=(IndicatorReading(name="ATR", timeframe="1h", value=Decimal(atr), text=""),),
        position=PositionView(qty=Decimal(1), unrealized_pnl_pct=Decimal(0), held_cycles=1)
        if holding
        else None,
    )


def decision(action: Action, *, conviction: str = "0.8") -> Decision:
    return Decision(
        instrument_key=f.instrument().key,
        action=action,
        conviction=Decimal(conviction),
        size_hint=SizeHint.HALF if action.is_tradable else SizeHint.NONE,
        votes_for=2,
        votes_total=3,
    )


def forward(move: str) -> sc.Forward:
    p0 = Decimal("100")
    return sc.Forward(p0=p0, p_h=p0 + Decimal(move), move=Decimal(move), mfe=Decimal(move), mae=Decimal(0))


def score(ctx, dec, fwd, regime=Pool.NORMAL) -> sc.ScoredDecision:
    return sc.score_decision(
        cycle_id="c",
        as_of=f.EPOCH,
        context=ctx,
        decision=dec,
        forward=fwd,
        band=Decimal("5"),
        regime=regime,
        window_name="",
    )


def test_a_buy_before_a_rally_is_correct() -> None:
    result = score(context(), decision(Action.BUY), forward("12"))
    assert result.verdict is sc.Verdict.CORRECT
    assert result.truth is sc.Truth.BUY


def test_a_buy_before_nothing_is_wrong() -> None:
    assert score(context(), decision(Action.BUY), forward("1")).verdict is sc.Verdict.WRONG


def test_waiting_through_a_crash_while_flat_is_correct() -> None:
    result = score(context(), decision(Action.WAIT), forward("-40"), regime=Pool.SHOCK_DOWN)
    assert result.verdict is sc.Verdict.CORRECT
    assert result.regime is Pool.SHOCK_DOWN


def test_holding_through_a_crash_is_wrong() -> None:
    result = score(context(holding=True), decision(Action.HOLD), forward("-40"))
    assert result.verdict is sc.Verdict.WRONG
    assert result.truth is sc.Truth.EXIT


def test_a_missing_atr_is_unscored_by_name() -> None:
    inst = f.instrument()
    bare = context().model_copy(update={"indicators": ()})
    result = sc.score_decision(
        cycle_id="c",
        as_of=f.EPOCH,
        context=bare,
        decision=decision(Action.BUY),
        forward=forward("12"),
        band=None,
        regime=Pool.NORMAL,
        window_name="",
    )
    assert result.verdict is sc.Verdict.UNSCORED_NO_ATR
    assert inst.key


def test_a_missing_forward_window_is_unscored_by_name() -> None:
    result = sc.score_decision(
        cycle_id="c",
        as_of=f.EPOCH,
        context=context(),
        decision=decision(Action.BUY),
        forward=None,
        band=Decimal("5"),
        regime=Pool.NORMAL,
        window_name="",
    )
    assert result.verdict is sc.Verdict.UNSCORED_HORIZON


def test_the_action_rate_flag_is_the_decisions_own() -> None:
    """§9.5's precision-on-action needs to know which decisions asked for an order."""
    assert score(context(), decision(Action.BUY), forward("12")).asked_for_an_order
    assert not score(context(), decision(Action.WAIT), forward("1")).asked_for_an_order


async def test_the_price_index_finds_the_bar_h_ahead(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})
    data = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(data, clock)
    params = sc.ScoringParams(timeframe="1h", horizon_bars=6)

    index = await sc.build_price_index(data, audit, params)
    found = index.forward(inst.key, f.EPOCH + timedelta(hours=10), horizon=6)

    assert found is not None
    assert found.move == Decimal(6)


async def test_a_decision_near_the_end_has_no_forward_window(tmp_path: Path) -> None:
    """§5.6: never silently dropped, which would flatter a run by discarding its most recent
    behaviour."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})
    data = ReplayDataset.load(tmp_path, clock)
    index = await sc.build_price_index(data, await ds.audit(data, clock), sc.ScoringParams(timeframe="1h"))

    assert index.forward(inst.key, f.EPOCH + timedelta(hours=46), horizon=6) is None


async def test_a_decision_whose_window_crosses_a_hole_is_flagged(tmp_path: Path) -> None:
    """§4.4: scoring across a hole is a wrong answer wearing a right one's clothes."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})
    data = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(data, clock)
    holed = audit.model_copy(
        update={
            "series": {
                "binance:BTC/USDT|1h": audit.series["binance:BTC/USDT|1h"].model_copy(
                    update={
                        "known_holes": (
                            ds.KnownHole(
                                **{
                                    "from": f.EPOCH + timedelta(hours=12),
                                    "to": f.EPOCH + timedelta(hours=14),
                                    "reason": "test",
                                }
                            ),
                        )
                    }
                )
            }
        }
    )
    params = sc.ScoringParams(timeframe="1h", horizon_bars=6)
    index = await sc.build_price_index(data, holed, params)

    assert index.crosses_hole(inst.key, f.EPOCH + timedelta(hours=10), params)
    assert not index.crosses_hole(inst.key, f.EPOCH + timedelta(hours=40), params)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_scoring_truth.py decision_lab/tests/test_scoring_verdicts.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.scoring'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/scoring.py`:

```python
"""Was the decision right? (spec §9.)

Per (candidate, snapshot, instrument): take `p0` — the quote in the snapshot, what the panel
actually saw — and `atr`, read **off the frozen snapshot** rather than recomputed. The band is
`k × atr`, so it is derived from exactly the evidence the panel had rather than from a better
view of the same market. Then compare against `pH`, the close `H` bars later.

**The truth label is long-only aware, and this is the rule easiest to get backwards.** Tier-1
refuses a short, so standing aside from a fall while flat is *correct*, not a missed opportunity.
Scoring it as a miss would systematically punish the conservative behaviour the bot is built for,
and would make `SHOCK_DOWN` a period the bot is doomed to score badly in rather than a test it
can pass.

Every decision gets a verdict, and there are exactly three ways to be unscorable: the ATR lookback
or the forward window crosses a known hole, the forward window runs off the end of the dataset, or
the snapshot carries no ATR for the scoring timeframe. Each is counted with its reason on every
table — a run that quietly dropped them would report accuracy over a subset it chose after the
fact.

Everything here is `Decimal`. `decision_lab/tests/test_discipline.py` asserts that structurally.

Failure semantics: this module computes and never fetches. Bad input raises `ValueError`; a
missing input is a verdict, not an exception.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from decision_lab.calibration_days import Pool
from decision_lab.dataset import CoverageAudit, read_series, series_key
from decision_lab.params import DEFAULT_BAND_K, DEFAULT_HORIZON_BARS
from tradebot.core.decision import Decision
from tradebot.core.enums import Action
from tradebot.core.market import Candle, timeframe_interval
from pydantic import Field

from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import InstrumentContext
from tradebot.indicators.library import REGISTRY
from tradebot.marketdata.recorder import ReplayDataset


class Truth(StrEnum):
    """What the market went on to do, expressed as what the right call would have been."""

    BUY = "BUY"
    STAND_ASIDE = "STAND_ASIDE"
    ADD = "ADD"
    EXIT = "EXIT"
    HOLD = "HOLD"


class Verdict(StrEnum):
    """§9.4. Three unscored reasons, and adding a fourth is a spec change."""

    CORRECT = "CORRECT"
    WRONG = "WRONG"
    UNSCORED_GAP = "UNSCORED (gap)"
    UNSCORED_HORIZON = "UNSCORED (horizon)"
    UNSCORED_NO_ATR = "UNSCORED (no ATR)"

    @property
    def is_scored(self) -> bool:
        return self in (Verdict.CORRECT, Verdict.WRONG)


#: §9.3's fourth column, as data. `HOLD` is correct for `ADD` because a position already on is
#: already exposed to the move — the panel is not required to press a winner to be right about it.
CORRECT_ACTIONS: dict[Truth, frozenset[Action]] = {
    Truth.BUY: frozenset({Action.BUY}),
    Truth.STAND_ASIDE: frozenset({Action.WAIT, Action.HOLD}),
    Truth.ADD: frozenset({Action.BUY, Action.HOLD}),
    Truth.EXIT: frozenset({Action.SELL}),
    Truth.HOLD: frozenset({Action.HOLD, Action.WAIT}),
}


def truth_for(*, holding: bool, move: Decimal, band: Decimal) -> Truth:
    """§9.3's truth table. Long-only: a fall while flat cost nothing and demanded nothing."""
    if band <= ZERO:
        raise ValueError(f"scoring needs a positive band, got {band}")
    if not holding:
        return Truth.BUY if move > band else Truth.STAND_ASIDE
    if move > band:
        return Truth.ADD
    if move < -band:
        return Truth.EXIT
    return Truth.HOLD


class ScoringParams(DomainModel):
    """The four numbers a verdict depends on, printed on every report (§14)."""

    #: Defaults to the dataset's shortest timeframe; `ReplayDataset.timeframes` is shortest-first.
    timeframe: str
    band_k: Money = DEFAULT_BAND_K
    horizon_bars: int = DEFAULT_HORIZON_BARS
    #: Bars the ATR reading in the snapshot was averaged over, plus one for the true range's
    #: previous close. Read from the registry so a change to the indicator moves this with it.
    atr_lookback_bars: int = REGISTRY["ATR"].period + 1


class Forward(DomainModel):
    """What the market did over the horizon, from the price the panel saw."""

    p0: Money
    p_h: Money
    move: Money
    #: Maximum favourable and adverse excursion over the same window — recorded alongside because
    #: `move` alone cannot distinguish a straight climb from a round trip through a drawdown.
    mfe: Money
    mae: Money


@dataclass(frozen=True, slots=True)
class PriceIndex:
    """The scoring timeframe's bars per instrument, plus the holes not to score across."""

    timeframe: str
    candles: dict[str, tuple[Candle, ...]]
    holes: dict[str, tuple[tuple[datetime, datetime], ...]]

    def _bar_index(self, instrument_key: str, as_of: datetime) -> int:
        closes = [candle.close_time for candle in self.candles[instrument_key]]
        return bisect.bisect_right(closes, as_of) - 1

    def forward(self, instrument_key: str, as_of: datetime, *, horizon: int) -> Forward | None:
        """`p0`, `pH`, the move, and the excursions — or `None` when the window runs off the end."""
        bars = self.candles[instrument_key]
        start = self._bar_index(instrument_key, as_of)
        if start < 0 or start + horizon >= len(bars):
            return None
        window = bars[start + 1 : start + horizon + 1]
        p0 = bars[start].close
        p_h = window[-1].close
        return Forward(
            p0=p0,
            p_h=p_h,
            move=p_h - p0,
            mfe=max(bar.high for bar in window) - p0,
            mae=min(bar.low for bar in window) - p0,
        )

    def crosses_hole(self, instrument_key: str, as_of: datetime, params: ScoringParams) -> bool:
        """Does the ATR lookback or the forward window touch a known hole? (§4.4)"""
        interval = timeframe_interval(self.timeframe)
        since = as_of - interval * params.atr_lookback_bars
        until = as_of + interval * params.horizon_bars
        return any(
            hole_from < until and hole_to > since
            for hole_from, hole_to in self.holes.get(instrument_key, ())
        )


async def build_price_index(
    dataset: ReplayDataset, audit: CoverageAudit, params: ScoringParams
) -> PriceIndex:
    """Load the scoring timeframe for every instrument, with its known holes attached."""
    candles: dict[str, tuple[Candle, ...]] = {}
    holes: dict[str, tuple[tuple[datetime, datetime], ...]] = {}
    for instrument in dataset.instruments:
        series = await read_series(dataset, instrument, params.timeframe)
        candles[instrument.key] = series.candles
        holes[instrument.key] = tuple(
            (hole.from_, hole.to)
            for hole in audit.holes_for(series_key(instrument.key, params.timeframe))
        )
    return PriceIndex(timeframe=params.timeframe, candles=candles, holes=holes)


def band_for(context: InstrumentContext, params: ScoringParams) -> Decimal | None:
    """`k × ATR`, read off the frozen snapshot. `None` when the snapshot has no ATR (§9.1)."""
    reading = context.indicator("ATR", params.timeframe)
    if reading is None or reading.value <= ZERO:
        return None
    return multiply(params.band_k, reading.value)


class ScoredDecision(DomainModel):
    """One (cycle, instrument) verdict, with everything the report needs to explain it."""

    cycle_id: str
    as_of: UtcDatetime
    instrument_key: str
    regime: Pool
    #: The named episode covering this instant, or `""`. Reported on its own row *and* inside the
    #: regime aggregate, never instead of it (§8.2).
    window_name: str = ""
    action: Action
    conviction: Money
    asked_for_an_order: bool
    holding: bool
    degraded: bool = False
    truth: Truth | None = None
    verdict: Verdict
    band: Money | None = None
    move: Money | None = None
    mfe: Money | None = None
    mae: Money | None = None
    #: `oracle − panel`, in band units so instruments are comparable. A ranking aid, explicitly
    #: unreachable: an oracle exits at the high of every window and no risk-managed system can.
    regret: Money | None = None
    cost_usd: Money = ZERO


def score_decision(
    *,
    cycle_id: str,
    as_of: datetime,
    context: InstrumentContext,
    decision: Decision,
    forward: Forward | None,
    band: Decimal | None,
    regime: Pool,
    window_name: str,
    degraded: bool = False,
    cost_usd: Decimal = ZERO,
    crossed_hole: bool = False,
) -> ScoredDecision:
    """One verdict. Unscorable is a verdict, never an exception and never a drop."""
    holding = context.position is not None
    common = {
        "cycle_id": cycle_id,
        "as_of": as_of,
        "instrument_key": context.instrument.key,
        "regime": regime,
        "window_name": window_name,
        "action": decision.action,
        "conviction": decision.conviction,
        "asked_for_an_order": decision.action.is_tradable,
        "holding": holding,
        "degraded": degraded,
        "cost_usd": cost_usd,
    }
    if crossed_hole:
        return ScoredDecision(**common, verdict=Verdict.UNSCORED_GAP)
    if band is None:
        return ScoredDecision(**common, verdict=Verdict.UNSCORED_NO_ATR)
    if forward is None:
        return ScoredDecision(**common, verdict=Verdict.UNSCORED_HORIZON, band=band)

    truth = truth_for(holding=holding, move=forward.move, band=band)
    return ScoredDecision(
        **common,
        truth=truth,
        verdict=Verdict.CORRECT if decision.action in CORRECT_ACTIONS[truth] else Verdict.WRONG,
        band=band,
        move=forward.move,
        mfe=forward.mfe,
        mae=forward.mae,
        regret=_regret(decision.action, forward, band, holding=holding),
    )


def _regret(action: Action, forward: Forward, band: Decimal, *, holding: bool) -> Decimal:
    """Oracle capture minus the panel's, in band units (§9.5).

    The oracle is long-only too and exits at the window's high, so its capture is `max(mfe, 0)`
    whether or not a position was already on. The panel captures the move only if its decision
    left it exposed: BUY, or HOLD while already holding. Standing aside captures nothing, which is
    exactly right — and is why regret is a *ranking aid* rather than a score: a system that never
    trades has maximal regret and may still be the correct system for the period.
    """
    exposed = action is Action.BUY or (holding and action is Action.HOLD)
    return divide(max(forward.mfe, ZERO) - (forward.move if exposed else ZERO), band)
```

`REGISTRY["ATR"].period` assumes the `Indicator` base exposes `period`; confirm at `tradebot/indicators/library.py` and fall back to the literal `15` with a comment naming `ATR(period=14)` if it does not.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_scoring_truth.py decision_lab/tests/test_scoring_verdicts.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/scoring.py decision_lab/tests/test_scoring_truth.py decision_lab/tests/test_scoring_verdicts.py
git commit -m "feat(decision_lab): the long-only truth table, the ATR band, and the five verdicts"
```

---

### Task 5: Per-regime metrics for the panel

**Files:**
- Modify: `decision_lab/scoring.py` (add `RegimeMetrics`, `summarise`, `score_records`)
- Test: `decision_lab/tests/test_scoring_metrics.py`

**Interfaces:**
- Consumes: Task 4's `ScoredDecision`, Task 1's `RegimeIndex`, Task 3's `CycleRecord`.
- Produces:
  - `class RegimeMetrics(DomainModel)` with `regime: str`, `scored: int`, `correct: int`, `accuracy: Money`, `action_rate: Money`, `precision_on_action: Money`, `mean_conviction_gap: Money`, `regret_total: Money`, `regret_per_decision: Money`, `degradation_rate: Money`, `cost_usd: Money`, `cost_per_scored: Money`, `unscored: dict[str, int]`
  - `scoring.summarise(decisions: Sequence[ScoredDecision], *, regime: str) -> RegimeMetrics`
  - `scoring.by_regime(decisions) -> tuple[RegimeMetrics, ...]` — `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN`, then one row per named window, **never a pooled `SHOCK`**
  - `scoring.score_records(records, *, index, regimes, params) -> tuple[ScoredDecision, ...]`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_scoring_metrics.py`:

```python
"""§9.5's metrics, and the reporting rule that no metric is shown without its split (§8.3).

`precision on action` is the figure that matters most: a WAIT-heavy panel scores well on accuracy
while never trading, and accuracy alone would recommend it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from decision_lab import scoring as sc
from decision_lab.calibration_days import Pool
from tradebot.core.enums import Action

AT = datetime(2024, 1, 1, tzinfo=UTC)


def scored(
    verdict: sc.Verdict,
    action: Action = Action.BUY,
    *,
    regime: Pool = Pool.NORMAL,
    conviction: str = "0.8",
    window: str = "",
    regret: str = "0",
    cost: str = "0.01",
    degraded: bool = False,
) -> sc.ScoredDecision:
    return sc.ScoredDecision(
        cycle_id="c",
        as_of=AT,
        instrument_key="binance:BTC/USDT",
        regime=regime,
        window_name=window,
        action=action,
        conviction=Decimal(conviction),
        asked_for_an_order=action.is_tradable,
        holding=False,
        degraded=degraded,
        verdict=verdict,
        regret=Decimal(regret),
        cost_usd=Decimal(cost),
    )


def test_accuracy_counts_only_scored_decisions() -> None:
    metrics = sc.summarise(
        [
            scored(sc.Verdict.CORRECT),
            scored(sc.Verdict.WRONG),
            scored(sc.Verdict.UNSCORED_GAP),
            scored(sc.Verdict.UNSCORED_HORIZON),
        ],
        regime="NORMAL",
    )
    assert metrics.scored == 2
    assert metrics.accuracy == Decimal("0.5")


def test_unscored_counts_carry_their_reasons() -> None:
    """A run that dropped them would report accuracy over a subset it chose after the fact."""
    metrics = sc.summarise(
        [scored(sc.Verdict.UNSCORED_GAP), scored(sc.Verdict.UNSCORED_NO_ATR)], regime="NORMAL"
    )
    assert metrics.unscored == {"UNSCORED (gap)": 1, "UNSCORED (no ATR)": 1}


def test_precision_on_action_ignores_the_wait_heavy_panel() -> None:
    """Two correct WAITs and one wrong BUY: accuracy 2/3, precision on action 0/1."""
    metrics = sc.summarise(
        [
            scored(sc.Verdict.CORRECT, Action.WAIT),
            scored(sc.Verdict.CORRECT, Action.WAIT),
            scored(sc.Verdict.WRONG, Action.BUY),
        ],
        regime="NORMAL",
    )
    assert metrics.accuracy > Decimal("0.6")
    assert metrics.precision_on_action == Decimal(0)
    assert metrics.action_rate < Decimal("0.4")


def test_the_conviction_gap_is_correct_minus_wrong() -> None:
    """A panel whose conviction carries information is worth more than one right as often by
    accident, because conviction feeds the Tier-1 floor and sizing."""
    metrics = sc.summarise(
        [
            scored(sc.Verdict.CORRECT, conviction="0.9"),
            scored(sc.Verdict.WRONG, conviction="0.4"),
        ],
        regime="NORMAL",
    )
    assert metrics.mean_conviction_gap == Decimal("0.5")


def test_a_panel_with_no_wrong_calls_has_no_conviction_gap() -> None:
    """No denominator. Reporting the correct-side mean as the gap would flatter it."""
    metrics = sc.summarise([scored(sc.Verdict.CORRECT)], regime="NORMAL")
    assert metrics.mean_conviction_gap == Decimal(0)


def test_the_degradation_rate_is_over_every_decision_not_the_scored_ones() -> None:
    """A candidate that scores well on the cycles it answered while failing a third of them is
    not a better panel (§9.5)."""
    metrics = sc.summarise(
        [scored(sc.Verdict.CORRECT), scored(sc.Verdict.UNSCORED_GAP, degraded=True)],
        regime="NORMAL",
    )
    assert metrics.degradation_rate == Decimal("0.5")


def test_cost_per_scored_decision_divides_by_the_scored_ones() -> None:
    metrics = sc.summarise(
        [scored(sc.Verdict.CORRECT, cost="0.10"), scored(sc.Verdict.UNSCORED_GAP, cost="0.10")],
        regime="NORMAL",
    )
    assert metrics.cost_usd == Decimal("0.20")
    assert metrics.cost_per_scored == Decimal("0.20")


def test_an_empty_regime_reports_zeroes_rather_than_dividing_by_zero() -> None:
    metrics = sc.summarise([], regime="SHOCK_UP")
    assert metrics.scored == 0
    assert metrics.accuracy == Decimal(0)


def test_the_split_always_carries_all_three_regimes() -> None:
    """§8.3: no metric is ever shown without its regime split, including an empty one — a missing
    `SHOCK_DOWN` row reads as 'not measured', which is the opposite of 'never happened'."""
    rows = sc.by_regime([scored(sc.Verdict.CORRECT, regime=Pool.NORMAL)])
    assert [row.regime for row in rows[:3]] == ["NORMAL", "SHOCK_UP", "SHOCK_DOWN"]


def test_shock_up_and_shock_down_are_never_pooled() -> None:
    """§10.3. They ask opposite questions of a long-only system."""
    rows = sc.by_regime(
        [
            scored(sc.Verdict.CORRECT, regime=Pool.SHOCK_UP),
            scored(sc.Verdict.WRONG, regime=Pool.SHOCK_DOWN),
        ]
    )
    assert {row.regime for row in rows} >= {"SHOCK_UP", "SHOCK_DOWN"}
    assert "SHOCK" not in {row.regime for row in rows}
    up = next(r for r in rows if r.regime == "SHOCK_UP")
    down = next(r for r in rows if r.regime == "SHOCK_DOWN")
    assert up.accuracy == Decimal(1)
    assert down.accuracy == Decimal(0)


def test_a_named_window_appears_on_its_own_row_and_in_its_aggregate() -> None:
    """§8.2: both, so an episode can be read by name without vanishing from the totals."""
    rows = sc.by_regime(
        [scored(sc.Verdict.CORRECT, regime=Pool.SHOCK_UP, window="spot ETF approval")]
    )
    by_name = {row.regime: row for row in rows}
    assert by_name["SHOCK_UP"].scored == 1
    assert by_name["spot ETF approval"].scored == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_scoring_metrics.py -q`
Expected: FAIL — `AttributeError: module 'decision_lab.scoring' has no attribute 'summarise'`.

- [ ] **Step 3: Write the implementation**

Append to `decision_lab/scoring.py`:

```python
class RegimeMetrics(DomainModel):
    """§9.5, for one regime or one named window. Every field is `Decimal` or a count."""

    regime: str
    decisions: int = 0
    scored: int = 0
    correct: int = 0
    accuracy: Money = ZERO
    action_rate: Money = ZERO
    precision_on_action: Money = ZERO
    mean_conviction_gap: Money = ZERO
    regret_total: Money = ZERO
    regret_per_decision: Money = ZERO
    degradation_rate: Money = ZERO
    cost_usd: Money = ZERO
    cost_per_scored: Money = ZERO
    unscored: dict[str, int] = Field(default_factory=dict)


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    """Zero rather than a refusal on an empty denominator: an empty regime is a row of zeroes,
    and `§8.3` requires the row to exist so 'never happened' does not read as 'not measured'."""
    return divide(Decimal(numerator), Decimal(denominator)) if denominator else ZERO


def _mean(values: Sequence[Decimal]) -> Decimal:
    return divide(sum(values, start=ZERO), Decimal(len(values))) if values else ZERO


def summarise(decisions: Sequence[ScoredDecision], *, regime: str) -> RegimeMetrics:
    """Fold one regime's decisions into §9.5's metrics."""
    scored = [d for d in decisions if d.verdict.is_scored]
    correct = [d for d in scored if d.verdict is Verdict.CORRECT]
    wrong = [d for d in scored if d.verdict is Verdict.WRONG]
    acted = [d for d in scored if d.asked_for_an_order]
    acted_correct = [d for d in acted if d.verdict is Verdict.CORRECT]
    regrets = [d.regret for d in scored if d.regret is not None]
    cost = sum((d.cost_usd for d in decisions), start=ZERO)
    unscored: dict[str, int] = {}
    for decision in decisions:
        if not decision.verdict.is_scored:
            unscored[decision.verdict.value] = unscored.get(decision.verdict.value, 0) + 1

    return RegimeMetrics(
        regime=regime,
        decisions=len(decisions),
        scored=len(scored),
        correct=len(correct),
        accuracy=_ratio(len(correct), len(scored)),
        action_rate=_ratio(len(acted), len(scored)),
        precision_on_action=_ratio(len(acted_correct), len(acted)),
        # Zero when either side is empty: a panel with no wrong calls has no *gap*, and reporting
        # its correct-side mean as one would flatter it.
        mean_conviction_gap=(
            _mean([d.conviction for d in correct]) - _mean([d.conviction for d in wrong])
            if correct and wrong
            else ZERO
        ),
        regret_total=sum(regrets, start=ZERO),
        regret_per_decision=_mean(regrets),
        # Over *every* decision, not the scored ones: degradation is the reason a decision is
        # missing, so measuring it against what survived would hide it.
        degradation_rate=_ratio(sum(1 for d in decisions if d.degraded), len(decisions)),
        cost_usd=cost,
        cost_per_scored=_ratio(cost, len(scored)),
        unscored=unscored,
    )


def by_regime(decisions: Sequence[ScoredDecision]) -> tuple[RegimeMetrics, ...]:
    """The three regimes, always all three, then one row per named window (§8.3, §8.2).

    `SHOCK_UP` and `SHOCK_DOWN` are never pooled: they ask opposite questions of a long-only
    system, and a blended figure averages "did the seats catch the move" with "did the seats
    protect capital" and hides both.
    """
    rows = [
        summarise([d for d in decisions if d.regime is pool], regime=pool.value) for pool in Pool
    ]
    windows = sorted({d.window_name for d in decisions if d.window_name})
    rows += [
        summarise([d for d in decisions if d.window_name == name], regime=name)
        for name in windows
    ]
    return tuple(rows)


def score_records(
    records: Sequence["CycleRecord"],
    *,
    index: PriceIndex,
    regimes: "RegimeIndex",
    params: ScoringParams,
) -> tuple[ScoredDecision, ...]:
    """Score every (cycle, instrument) of the reference pass."""
    results: list[ScoredDecision] = []
    for record in records:
        # `basket` mode answers for N instruments in one provider call, so the cycle's cost is
        # already de-duplicated by `total_cost` and is split evenly across the instruments it
        # answered for rather than counted once per instrument.
        per_instrument = _ratio(record.cost_usd, len(record.snapshot.instruments))
        for context in record.snapshot.instruments:
            decision = record.decision_for(context.instrument.key)
            if decision is None:
                continue
            window = regimes.window_at(record.as_of)
            results.append(
                score_decision(
                    cycle_id=record.cycle_id,
                    as_of=record.as_of,
                    context=context,
                    decision=decision,
                    forward=index.forward(
                        context.instrument.key, record.as_of, horizon=params.horizon_bars
                    ),
                    band=band_for(context, params),
                    regime=regimes.label_at(context.instrument.key, record.as_of),
                    window_name=window.name if window else "",
                    degraded=PANEL_DEGRADED in decision.flags,
                    cost_usd=per_instrument,
                    crossed_hole=index.crosses_hole(context.instrument.key, record.as_of, params),
                )
            )
    return tuple(results)
```

Add `from decision_lab.records import PANEL_DEGRADED, CycleRecord` and `from decision_lab.regimes import RegimeIndex` — plain imports, not `TYPE_CHECKING` strings, unless mypy reports a cycle (`records` imports `corpus`, `regimes` imports `calibration_days` and `dataset`; neither imports `scoring`, so there is none). Drop the quotes on the annotations once the imports are in.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/scoring.py decision_lab/tests/test_scoring_metrics.py
git commit -m "feat(decision_lab): per-regime metrics, with SHOCK_UP and SHOCK_DOWN kept apart"
```

---

### Task 6: Per-seat scoring, and the round-0 split

**Files:**
- Create: `decision_lab/seats.py`
- Test: `decision_lab/tests/test_seat_scoring.py`

**Interfaces:**
- Consumes: `scoring.{Truth, Verdict, CORRECT_ACTIONS, ScoredDecision, _ratio, _mean}` (promote `_ratio` and `_mean` to `ratio` and `mean` in `scoring.py` as part of this task — two modules need them), `records.CycleRecord`, `tradebot.core.decision.{SeatResponse, total_cost}`.
- Produces:
  - `class SeatMetrics(DomainModel)` with `seat_id`, `regime`, `round_label` (`"round 0"` | `"final"`), `votes`, `scored`, `accuracy`, `action_rate`, `precision_on_action`, `mean_conviction_gap`, `abstention_rate`, `fallback_rate`, `cost_per_vote`, `latency_ms_per_vote`
  - `seats.score_seats(records, scored, *, panel) -> tuple[SeatMetrics, ...]`
  - `seats.SEAT_CONVICTION_SCALE: Decimal` = `Decimal(5)` — seats rate 1–5, decisions are 0–1

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_seat_scoring.py`:

```python
"""A seat is not a panel (spec §9.7).

Every `SeatResponse` already recorded carries the seat, the vote, the round, the latency, the
tokens, the cost and the `fingerprint` — the binding that actually answered after any fallback.
So this costs no new data and no new provider calls, and it answers the question an operator
tuning seats actually has: which of them is carrying the result.

**Round 0 is reported separately from the final vote, and the split is not cosmetic.** Under
`blind_then_debate` a seat's later votes are contaminated by its peers *by design* — that is what
the debate is for. "Which seat reasons well" and "which seat is easily talked round" are different
questions, and one column cannot answer both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab.calibration_days import Pool
from tradebot.core.decision import SeatResponse, SeatVote
from tradebot.core.enums import Action, SizeHint

AT = datetime(2024, 1, 1, tzinfo=UTC)
KEY = "binance:BTC/USDT"


def response(
    seat_id: str,
    action: Action | None,
    *,
    round_index: int = 0,
    conviction: int = 4,
    model: str = "primary",
    latency_ms: int = 100,
    cost: str = "0.01",
    call_id: str = "",
) -> SeatResponse:
    vote = (
        None
        if action is None
        else SeatVote(
            action=action,
            conviction=conviction,
            size_hint=SizeHint.HALF if action.is_tradable else SizeHint.NONE,
            thesis="because",
        )
    )
    return SeatResponse(
        seat_id=seat_id,
        role="analyst",
        provider_id="openrouter",
        model=model,
        round_index=round_index,
        instrument_key=KEY,
        vote=vote,
        abstain_reason=None if vote else "provider unreachable",
        responded_at=AT,
        latency_ms=latency_ms,
        cost_usd=Decimal(cost),
        **({"call_id": call_id} if call_id else {}),
    )


def scored(verdict: sc.Verdict, truth: sc.Truth) -> sc.ScoredDecision:
    return sc.ScoredDecision(
        cycle_id="c",
        as_of=AT,
        instrument_key=KEY,
        regime=Pool.NORMAL,
        action=Action.BUY,
        conviction=Decimal("0.8"),
        asked_for_an_order=True,
        holding=False,
        truth=truth,
        verdict=verdict,
    )


def test_a_seat_is_scored_against_the_same_truth_label() -> None:
    metrics = st.score_seat_votes(
        [response("trend", Action.BUY)], truth=sc.Truth.BUY, regime=Pool.NORMAL, round_label="round 0"
    )
    assert metrics["trend"].accuracy == Decimal(1)


def test_a_seat_that_stood_aside_from_a_rally_is_wrong() -> None:
    metrics = st.score_seat_votes(
        [response("risk", Action.WAIT)], truth=sc.Truth.BUY, regime=Pool.NORMAL, round_label="round 0"
    )
    assert metrics["risk"].accuracy == Decimal(0)


def test_the_abstention_rate_is_over_every_turn() -> None:
    metrics = st.score_seat_votes(
        [response("flaky", None), response("flaky", Action.BUY)],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["flaky"].abstention_rate == Decimal("0.5")
    assert metrics["flaky"].scored == 1, "an abstention has no vote to score"


def test_the_fallback_rate_reads_the_fingerprint() -> None:
    """A seat that answered on its backup all sweep is a seat that was never tested, and today
    nothing would say so (§9.7)."""
    metrics = st.score_seat_votes(
        [response("trend", Action.BUY, model="primary"), response("trend", Action.BUY, model="backup")],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
        primary={"trend": "openrouter:primary"},
    )
    assert metrics["trend"].fallback_rate == Decimal("0.5")


def test_cost_and_latency_are_per_answered_vote() -> None:
    """A seat marginally better and four times slower is a different trade-off at 1h cadence
    than at 24h."""
    metrics = st.score_seat_votes(
        [
            response("trend", Action.BUY, latency_ms=100, cost="0.02"),
            response("trend", None, latency_ms=900, cost="0.00"),
        ],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["trend"].latency_ms_per_vote == 100
    assert metrics["trend"].cost_per_vote == Decimal("0.02")


def test_cost_is_deduplicated_by_call_id() -> None:
    """In `basket` mode one call answers for every instrument; `total_cost` is the only sanctioned
    way to total money, and this must go through it."""
    shared = "one-call"
    metrics = st.score_seat_votes(
        [
            response("trend", Action.BUY, cost="0.05", call_id=shared),
            response("trend", Action.BUY, cost="0.05", call_id=shared),
        ],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        round_label="round 0",
    )
    assert metrics["trend"].cost_per_vote == Decimal("0.025")


def test_seat_conviction_is_normalised_to_the_decision_scale() -> None:
    """Seats rate 1–5, `Decision.conviction` is 0–1. A gap on two scales is not a gap."""
    high = st.score_seat_votes(
        [response("a", Action.BUY, conviction=5)], truth=sc.Truth.BUY, regime=Pool.NORMAL, round_label="round 0"
    )
    assert high["a"].mean_conviction_correct == Decimal(1)
```

Create `decision_lab/tests/test_seat_influence.py`:

```python
"""Swing rate and marginal contribution, on handmade vote sets (spec §16.1).

These are the two metrics a reader will trust without checking, so they are asserted against
constructed panels rather than only end to end: a three-seat panel where removing seat A flips the
decision and removing seat B does not, and a seat that dissents correctly against a wrong panel
beside one that dissents wrongly against a right one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from decision_lab import scoring as sc
from decision_lab import seats as st
from decision_lab.calibration_days import Pool
from decision_lab.tests.test_seat_scoring import KEY, response
from tradebot.core.config import PanelConfig, ProviderSettings, SeatConfig
from tradebot.core.enums import Action

AT = datetime(2024, 1, 1, tzinfo=UTC)


def panel(*seat_ids: str, majority: str = "0.5") -> PanelConfig:
    return PanelConfig(
        panel_id="test",
        providers=(ProviderSettings(provider_id="openrouter", kind="openai_compat", base_url="https://x", secret_ref="X"),),
        seats=tuple(
            SeatConfig(seat_id=s, role="analyst", provider_id="openrouter", model="primary")
            for s in seat_ids
        ),
        qualified_majority=Decimal(majority),
    )


def test_a_seat_that_flips_the_decision_has_a_swing() -> None:
    """Two BUY, one WAIT, majority 0.5 of three → BUY. Remove a BUY and no majority remains."""
    votes = [
        response("a", Action.BUY),
        response("b", Action.BUY),
        response("c", Action.WAIT),
    ]
    swings = st.swings(votes, panel=panel("a", "b", "c"), instrument_key=KEY)

    assert swings["a"] is True
    assert swings["b"] is True
    assert swings["c"] is False


def test_a_padding_seat_has_no_swing() -> None:
    """Three BUY out of three: removing any one still leaves a majority."""
    votes = [response(s, Action.BUY) for s in ("a", "b", "c", "d")]
    swings = st.swings(votes, panel=panel("a", "b", "c", "d"), instrument_key=KEY)
    assert not any(swings.values())


def test_a_one_seat_panel_has_no_swing_rate() -> None:
    """Removing the only seat leaves no panel to reach consensus with. Report nothing, not zero."""
    assert st.swings([response("solo", Action.BUY)], panel=panel("solo"), instrument_key=KEY) == {}


def test_a_seat_right_against_a_wrong_panel_earns_its_slot() -> None:
    contribution = st.marginal_contribution(
        seat_action=Action.WAIT,
        panel_action=Action.BUY,
        truth=sc.Truth.STAND_ASIDE,
    )
    assert contribution == 1


def test_a_seat_wrong_against_a_right_panel_costs_it() -> None:
    contribution = st.marginal_contribution(
        seat_action=Action.WAIT,
        panel_action=Action.BUY,
        truth=sc.Truth.BUY,
    )
    assert contribution == -1


def test_agreeing_with_the_panel_contributes_nothing_either_way() -> None:
    """The question is what the seat added, and a seat that agreed added no information."""
    assert st.marginal_contribution(seat_action=Action.BUY, panel_action=Action.BUY, truth=sc.Truth.BUY) == 0
    assert st.marginal_contribution(seat_action=Action.BUY, panel_action=Action.BUY, truth=sc.Truth.STAND_ASIDE) == 0


def test_dissenting_and_both_being_wrong_contributes_nothing() -> None:
    """Neither earned nor cost the slot: the panel would have been wrong either way."""
    assert st.marginal_contribution(seat_action=Action.SELL, panel_action=Action.WAIT, truth=sc.Truth.BUY) == 0


def test_round_zero_and_final_are_reported_separately() -> None:
    votes = [
        response("a", Action.WAIT, round_index=0),
        response("a", Action.BUY, round_index=1),
    ]
    rows = st.score_seats_for_instrument(
        votes, truth=sc.Truth.BUY, regime=Pool.NORMAL, panel=panel("a", "b"), instrument_key=KEY
    )
    labels = {row.round_label: row for row in rows}
    assert set(labels) == {"round 0", "final"}
    assert labels["round 0"].accuracy == Decimal(0)
    assert labels["final"].accuracy == Decimal(1)


def test_single_round_reports_the_two_as_identical() -> None:
    """§9.7: 'Under `single_round` the two are identical and the report says so rather than
    printing the same numbers twice.'"""
    votes = [response("a", Action.BUY, round_index=0)]
    rows = st.score_seats_for_instrument(
        votes, truth=sc.Truth.BUY, regime=Pool.NORMAL, panel=panel("a", "b"), instrument_key=KEY
    )
    assert st.rounds_are_identical(rows) is True
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_seat_scoring.py decision_lab/tests/test_seat_influence.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.seats'`.

- [ ] **Step 3: Write the implementation**

First promote the two helpers in `scoring.py` — rename `_ratio` → `ratio` and `_mean` → `mean` and update their call sites, because `seats.py` needs both and a second copy would drift.

Create `decision_lab/seats.py`:

```python
"""Per-seat scoring: which seat is carrying the result (spec §9.7).

§9.5 scores what the *panel* decided. A seat is not a panel, and an operator tuning seats needs
the level below. Everything this needs is already recorded on `SeatResponse` — `seat_id`, `vote`,
`abstain_reason`, `round_index`, `latency_ms`, the tokens, `cost_usd`, and `fingerprint`, the
binding that actually answered after any fallback — so it costs no new data and no new provider
calls.

Two metrics carry most of the weight and are the ones a reader will trust without checking:

* **Swing rate** — how often replaying `decision.consensus.reach_consensus` over the recorded
  votes *minus this seat* changes the panel's decision. Deterministic, free, and the number that
  separates a seat carrying weight from one padding a majority. The counterfactual removes the
  seat from the `PanelConfig` too, not only from the votes: `required_votes` and the abstention
  fraction are both computed from the seat count, and leaving it at four while three seats voted
  would ask "what if this seat had abstained", which is a different question.
* **Marginal contribution** — dissents that were right against a wrong panel, minus dissents that
  were wrong against a right one. "Does this seat earn its slot", in one signed figure.

`reach_consensus` is **imported**, never reimplemented: a second consensus rule here would make
the swing rate a measurement of the copy.

Failure semantics: nothing here fetches or writes. A one-seat panel has no swing rate and reports
none rather than zero — removing the only seat leaves no panel to reach consensus with, and zero
would read as "this seat does not matter".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from decision_lab.calibration_days import Pool
from decision_lab.records import CycleRecord
from decision_lab.scoring import CORRECT_ACTIONS, ScoredDecision, Truth, mean, ratio
from tradebot.core.config import PanelConfig
from tradebot.core.decision import SeatResponse, total_cost
from tradebot.core.enums import Action
from tradebot.core.money import ZERO, divide
from tradebot.core.schema import DomainModel, Money
from tradebot.decision.consensus import reach_consensus

#: Seats rate 1–5; `Decision.conviction` is 0–1 (DESIGN §6.5). A gap computed on two scales is
#: not a gap, so seat convictions are normalised before they are compared to anything.
SEAT_CONVICTION_SCALE = Decimal(5)

ROUND_ZERO = "round 0"
FINAL = "final"


class SeatMetrics(DomainModel):
    """One seat, one regime, one round label."""

    seat_id: str
    regime: str
    round_label: str
    turns: int = 0
    scored: int = 0
    correct: int = 0
    accuracy: Money = ZERO
    action_rate: Money = ZERO
    precision_on_action: Money = ZERO
    mean_conviction_correct: Money = ZERO
    mean_conviction_wrong: Money = ZERO
    mean_conviction_gap: Money = ZERO
    abstention_rate: Money = ZERO
    fallback_rate: Money = ZERO
    cost_per_vote: Money = ZERO
    latency_ms_per_vote: int = 0
    swings: int = 0
    swing_rate: Money = ZERO
    marginal_contribution: int = 0


def _conviction(response: SeatResponse) -> Decimal:
    return divide(Decimal(response.vote.conviction), SEAT_CONVICTION_SCALE) if response.vote else ZERO


def score_seat_votes(
    responses: Sequence[SeatResponse],
    *,
    truth: Truth | None,
    regime: Pool,
    round_label: str,
    primary: Mapping[str, str] | None = None,
) -> dict[str, SeatMetrics]:
    """Score each seat's turns in one round, against the panel's own §9.3 truth label."""
    by_seat: dict[str, list[SeatResponse]] = {}
    for response in responses:
        by_seat.setdefault(response.seat_id, []).append(response)

    metrics: dict[str, SeatMetrics] = {}
    for seat_id, turns in by_seat.items():
        voted = [t for t in turns if t.vote is not None]
        correct = (
            [t for t in voted if t.vote and t.vote.action in CORRECT_ACTIONS[truth]]
            if truth is not None
            else []
        )
        wrong = [t for t in voted if t not in correct] if truth is not None else []
        acted = [t for t in voted if t.vote and t.vote.action.is_tradable]
        expected = (primary or {}).get(seat_id)
        metrics[seat_id] = SeatMetrics(
            seat_id=seat_id,
            regime=regime.value,
            round_label=round_label,
            turns=len(turns),
            scored=len(voted) if truth is not None else 0,
            correct=len(correct),
            accuracy=ratio(len(correct), len(voted)) if truth is not None else ZERO,
            action_rate=ratio(len(acted), len(voted)),
            precision_on_action=ratio(
                sum(1 for t in acted if t in correct), len(acted)
            ),
            mean_conviction_correct=mean([_conviction(t) for t in correct]),
            mean_conviction_wrong=mean([_conviction(t) for t in wrong]),
            mean_conviction_gap=(
                mean([_conviction(t) for t in correct]) - mean([_conviction(t) for t in wrong])
                if correct and wrong
                else ZERO
            ),
            abstention_rate=ratio(len(turns) - len(voted), len(turns)),
            # A seat that answered on its backup all sweep is a seat that was never tested, and
            # today nothing anywhere would say so.
            fallback_rate=(
                ratio(sum(1 for t in turns if t.fingerprint != expected), len(turns))
                if expected
                else ZERO
            ),
            # Through `total_cost`, so `basket` mode — one call answering for N instruments — is
            # not counted N times (DESIGN §6.5).
            cost_per_vote=ratio(total_cost(turns), len(voted)),
            latency_ms_per_vote=int(ratio(sum(t.latency_ms for t in voted), len(voted))),
        )
    return metrics


def swings(
    final_round: Sequence[SeatResponse], *, panel: PanelConfig, instrument_key: str
) -> dict[str, bool]:
    """Which seats' removal would have changed the panel's decision.

    Empty for a one-seat panel: removing the only seat leaves no panel, and reporting `False`
    would read as "this seat does not matter" — the opposite of the truth.
    """
    if len(panel.seats) < 2:
        return {}
    actual = reach_consensus(tuple(final_round), panel, instrument_key).action
    result: dict[str, bool] = {}
    for seat in panel.seats:
        without = panel.model_copy(
            update={"seats": tuple(s for s in panel.seats if s.seat_id != seat.seat_id)}
        )
        remaining = tuple(r for r in final_round if r.seat_id != seat.seat_id)
        counterfactual = reach_consensus(remaining, without, instrument_key).action
        result[seat.seat_id] = counterfactual is not actual
    return result


def marginal_contribution(
    *, seat_action: Action, panel_action: Action, truth: Truth | None
) -> int:
    """+1 for a right dissent against a wrong panel, −1 for a wrong dissent against a right one.

    Zero when the seat agreed — it added no information — and zero when both were wrong, because
    the panel would have been wrong either way and the seat neither earned nor cost its slot.
    """
    if truth is None or seat_action is panel_action:
        return 0
    correct = CORRECT_ACTIONS[truth]
    seat_right = seat_action in correct
    panel_right = panel_action in correct
    if seat_right and not panel_right:
        return 1
    if panel_right and not seat_right:
        return -1
    return 0


def score_seats_for_instrument(
    responses: Sequence[SeatResponse],
    *,
    truth: Truth | None,
    regime: Pool,
    panel: PanelConfig,
    instrument_key: str,
) -> tuple[SeatMetrics, ...]:
    """Both rounds' tables for one instrument in one cycle."""
    primary = {seat.seat_id: f"{seat.provider_id}:{seat.model}" for seat in panel.seats}
    about = [r for r in responses if r.instrument_key == instrument_key]
    if not about:
        return ()
    last = max(r.round_index for r in about)
    rounds = {
        ROUND_ZERO: [r for r in about if r.round_index == 0],
        FINAL: [r for r in about if r.round_index == last],
    }
    return tuple(
        metrics
        for label, turns in rounds.items()
        for metrics in score_seat_votes(
            turns, truth=truth, regime=regime, round_label=label, primary=primary
        ).values()
    )


def rounds_are_identical(rows: Sequence[SeatMetrics]) -> bool:
    """§9.7: under `single_round` the two are the same, and the report says so rather than
    printing the same numbers twice."""
    zero = {r.seat_id: r for r in rows if r.round_label == ROUND_ZERO}
    final = {r.seat_id: r for r in rows if r.round_label == FINAL}
    return all(
        zero[seat_id].model_copy(update={"round_label": FINAL}) == final.get(seat_id)
        for seat_id in zero
    )


def score_seats(
    records: Sequence[CycleRecord],
    scored: Sequence[ScoredDecision],
    *,
    panel: PanelConfig,
) -> tuple[SeatMetrics, ...]:
    """Fold every cycle's seat tables into one row per (seat, regime, round label)."""
    truth_by: dict[tuple[str, str], Truth | None] = {
        (d.cycle_id, d.instrument_key): d.truth for d in scored
    }
    regime_by: dict[tuple[str, str], Pool] = {
        (d.cycle_id, d.instrument_key): d.regime for d in scored
    }
    panel_action: dict[tuple[str, str], Action] = {
        (d.cycle_id, d.instrument_key): d.action for d in scored
    }

    collected: list[SeatMetrics] = []
    influence: dict[tuple[str, str], list[int]] = {}
    swung: dict[tuple[str, str], int] = {}
    seen: dict[tuple[str, str], int] = {}
    for record in records:
        for context in record.snapshot.instruments:
            key = (record.cycle_id, context.instrument.key)
            if key not in truth_by:
                continue
            collected += score_seats_for_instrument(
                record.responses,
                truth=truth_by[key],
                regime=regime_by[key],
                panel=panel,
                instrument_key=context.instrument.key,
            )
            final = record.final_round_for(context.instrument.key)
            for seat_id, swung_here in swings(
                final, panel=panel, instrument_key=context.instrument.key
            ).items():
                index = (seat_id, regime_by[key].value)
                seen[index] = seen.get(index, 0) + 1
                swung[index] = swung.get(index, 0) + int(swung_here)
            for response in final:
                if response.vote is None:
                    continue
                index = (response.seat_id, regime_by[key].value)
                influence.setdefault(index, []).append(
                    marginal_contribution(
                        seat_action=response.vote.action,
                        panel_action=panel_action[key],
                        truth=truth_by[key],
                    )
                )

    return _fold(collected, swung=swung, seen=seen, influence=influence)


def _fold(
    rows: Sequence[SeatMetrics],
    *,
    swung: Mapping[tuple[str, str], int],
    seen: Mapping[tuple[str, str], int],
    influence: Mapping[tuple[str, str], Sequence[int]],
) -> tuple[SeatMetrics, ...]:
    """Sum per-cycle rows into one row per (seat, regime, round label), weighted by turns."""
    grouped: dict[tuple[str, str, str], list[SeatMetrics]] = {}
    for row in rows:
        grouped.setdefault((row.seat_id, row.regime, row.round_label), []).append(row)

    folded = []
    for (seat_id, regime, label), members in sorted(grouped.items()):
        turns = sum(m.turns for m in members)
        scored_ = sum(m.scored for m in members)
        correct = sum(m.correct for m in members)
        index = (seat_id, regime)
        folded.append(
            SeatMetrics(
                seat_id=seat_id,
                regime=regime,
                round_label=label,
                turns=turns,
                scored=scored_,
                correct=correct,
                accuracy=ratio(correct, scored_),
                action_rate=mean([m.action_rate for m in members]),
                precision_on_action=mean([m.precision_on_action for m in members]),
                mean_conviction_correct=mean([m.mean_conviction_correct for m in members]),
                mean_conviction_wrong=mean([m.mean_conviction_wrong for m in members]),
                mean_conviction_gap=mean([m.mean_conviction_gap for m in members]),
                abstention_rate=ratio(turns - scored_, turns),
                fallback_rate=mean([m.fallback_rate for m in members]),
                cost_per_vote=mean([m.cost_per_vote for m in members]),
                latency_ms_per_vote=int(mean([Decimal(m.latency_ms_per_vote) for m in members])),
                swings=swung.get(index, 0) if label == FINAL else 0,
                swing_rate=ratio(swung.get(index, 0), seen.get(index, 0)) if label == FINAL else ZERO,
                marginal_contribution=sum(influence.get(index, ())) if label == FINAL else 0,
            )
        )
    return tuple(folded)
```

Swing rate and marginal contribution are attached only to the `final` row, because both are properties of the vote the consensus rule actually read. Round 0 has no panel decision to swing.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS.

`test_a_seat_that_flips_the_decision_has_a_swing` depends on `reach_consensus`'s exact majority arithmetic — `required_votes(panel)` over three seats at `qualified_majority = 0.5`. Run it and read the actual behaviour before adjusting the expectation: if two-of-three does not flip on removal, adjust the *fixture* (four seats, three BUY) rather than the assertion, because the assertion is what §16.1 asks for.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/seats.py decision_lab/scoring.py decision_lab/tests/test_seat_scoring.py decision_lab/tests/test_seat_influence.py
git commit -m "feat(decision_lab): per-seat scoring, swing rate and marginal contribution"
```

---

### Task 7: The report, and the `report` command

**Files:**
- Create: `decision_lab/render.py`
- Modify: `decision_lab/cli.py` (add the `report` parser and handler)
- Test: `decision_lab/tests/test_render.py`
- Test: `decision_lab/tests/test_slice_b_end_to_end.py`

**Interfaces:**
- Consumes: everything above; `tradebot.validation.backtest.BANNER`, `tradebot.validation.cutoffs`.
- Produces:
  - `class LabReport(DomainModel)` — the whole thing, so the notebook and the renderer read one object
  - `render.report_markdown(report: LabReport) -> str`
  - `render.write_report(report: LabReport, path: Path) -> Path`
  - `cli.report(args) -> int`
  - `params.REPORTS_DIR`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_render.py`:

```python
"""Every report opens with its banners and its identity (spec §14).

A tuning result is filed beside the decision it justified, exactly as `report promotion` and
`report shadow` are — so it is written to a file, never printed, and a result whose provenance is
not on the page is not reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import render as rd
from decision_lab import scoring as sc
from decision_lab.calibration_days import Pool
from tradebot.validation.backtest import BANNER

AT = datetime(2026, 8, 23, tzinfo=UTC)


def report(**overrides) -> rd.LabReport:
    base = dict(
        generated_at=AT,
        corpus_id="abc123",
        dataset_directory="data/history",
        dataset_digest="d0",
        dayset_digest="d1",
        reference_instrument="binance:BTC/USDT",
        reference_panel_id="sim",
        reference_config_digest="c0",
        cadence_seconds=14_400,
        scoring=sc.ScoringParams(timeframe="1h"),
        vol_window_bars=30,
        shock_percentile=Decimal("0.90"),
        named_windows=("spot ETF approval",),
        start_equity=Decimal(10_000),
        news_blind=True,
        panel_models=("varied-a", "varied-b"),
        cycles=120,
        regimes=(sc.RegimeMetrics(regime="NORMAL", decisions=10, scored=8, correct=6),),
        seats=(),
    )
    return rd.LabReport(**{**base, **overrides})


def test_the_contamination_banner_is_unconditional() -> None:
    """§1.1: every model in `validation/cutoffs.py` was trained on this period."""
    assert BANNER in rd.report_markdown(report())


def test_the_tools_own_disclaimer_is_on_every_report() -> None:
    text = rd.report_markdown(report())
    assert "comparison instrument" in text
    assert "not evidence of alpha" in text


def test_a_news_blind_run_says_so() -> None:
    assert "NEWS-BLIND RUN" in rd.report_markdown(report(news_blind=True))


def test_the_identity_block_carries_every_parameter() -> None:
    """A result whose provenance is not on the page is not reproducible (§14)."""
    text = rd.report_markdown(report())
    for expected in ("abc123", "data/history", "binance:BTC/USDT", "sim", "1h", "0.90", "30"):
        assert expected in text


def test_every_regime_gets_a_row_even_when_empty() -> None:
    """§8.3: a missing SHOCK_DOWN row reads as 'not measured', which is the opposite of
    'never happened'."""
    text = rd.report_markdown(
        report(regimes=sc.by_regime([]))
    )
    for regime in ("NORMAL", "SHOCK_UP", "SHOCK_DOWN"):
        assert regime in text


def test_no_pooled_shock_row_is_ever_rendered() -> None:
    text = rd.report_markdown(report(regimes=sc.by_regime([])))
    assert "| SHOCK |" not in text


def test_unscored_counts_appear_with_their_reasons() -> None:
    metrics = sc.RegimeMetrics(regime="NORMAL", decisions=3, unscored={"UNSCORED (gap)": 2})
    text = rd.report_markdown(report(regimes=(metrics,)))
    assert "UNSCORED (gap)" in text
    assert "2" in text


def test_the_regret_column_is_labelled_unreachable() -> None:
    """§9.5: reported as a ranking aid, explicitly labelled unreachable."""
    assert "unreachable" in rd.report_markdown(report()).lower()


def test_the_report_is_written_to_a_file(tmp_path: Path) -> None:
    path = rd.write_report(report(), tmp_path / "r.md")
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("#")


def test_identical_input_renders_identically() -> None:
    """Deterministic, so two reports diff cleanly — which is how a tuning result is compared."""
    assert rd.report_markdown(report()) == rd.report_markdown(report())
```

Create `decision_lab/tests/test_slice_b_end_to_end.py`:

```python
"""Slice B end to end: corpus → regimes → scoring → report, offline and free (spec §16).

The slice's exit criterion. On `SIM_PANEL` — three `varied-*` stub seats over the fifteen entries
in `stub_responses.json` — so the panel reaches BUY, SELL, HOLD *and* `no qualified majority`, and
the scoring tables are exercised rather than being three rows of the same verdict.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.marketdata.recorder import ReplayDataset


async def test_verify_build_and_report(tmp_path: Path, monkeypatch) -> None:
    clock = ManualClock(f.EPOCH)
    data = tmp_path / "history"
    workspace = tmp_path / "ws"
    reports = tmp_path / "reports"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    f.write_dataset(
        data,
        {(f.instrument(), "1h"): f.shocked_walk(days=40, shock_up=(5, 12, 19), shock_down=(8, 15, 22))},
    )
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    built = await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel="sim",
        cadence_seconds=4 * 3600,
        start_equity=Decimal(10_000),
    )

    code = cli.main(
        [
            "report",
            "--corpus",
            built.meta.corpus_id,
            "--out",
            str(reports / "slice-b.md"),
        ]
    )

    assert code == cli.EXIT_OK
    text = (reports / "slice-b.md").read_text(encoding="utf-8")
    assert "NORMAL" in text and "SHOCK_UP" in text and "SHOCK_DOWN" in text
    assert "NEWS-BLIND RUN" in text
    assert built.meta.corpus_id in text


async def test_the_report_names_the_seats(tmp_path: Path, monkeypatch) -> None:
    """The core question this slice exists to answer: which seat carried the result."""
    clock = ManualClock(f.EPOCH)
    data = tmp_path / "history"
    workspace = tmp_path / "ws"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=40, shock_up=(5,), shock_down=(8,))})
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    built = await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel="sim",
        cadence_seconds=4 * 3600,
        start_equity=Decimal(10_000),
    )

    cli.main(["report", "--corpus", built.meta.corpus_id, "--out", str(tmp_path / "r.md")])
    text = (tmp_path / "r.md").read_text(encoding="utf-8")

    for seat in built.meta.reference_basket.panel.seats:
        assert seat.seat_id in text
    assert "round 0" in text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_render.py decision_lab/tests/test_slice_b_end_to_end.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.render'`.

- [ ] **Step 3: Write the implementation**

Add to `decision_lab/params.py`:

```python
def reports_dir() -> Path:
    """Where a tuning result is filed. Never printed — a result outlives the process (§14)."""
    return Path(__file__).parent / "reports"
```

Create `decision_lab/render.py`:

```python
"""The report, as Markdown (spec §14).

Markdown rather than a dashboard page or JSON, for the reason `validation/render.py` gives: a
result that justified a decision gets attached to that decision, read six months later, and
diffed against the next one. Plain text does that; a rendered view does not. Never printed.

Every report opens with its banners. The `BacktestHarness` contamination banner is verbatim and
unconditional — every model in `validation/cutoffs.py` was trained on this period, and a tool that
only warned when it thought it mattered would be a tool nobody could quote. Then `NEWS-BLIND RUN`
where it applies, then the tool's own line stating it is a comparison instrument and not evidence
of alpha.

Then the experiment's identity, in full, because a result whose provenance is not on the page is
not reproducible. Then, per regime — `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN` and one row per named
window — the tables. Never a pooled `SHOCK`: it averages "did the seats catch the move" with "did
the seats protect capital" and hides both.

Numbers are formatted from `Decimal` directly; no value passes through a float on its way to being
read, which `test_discipline.py` asserts structurally.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from decision_lab.scoring import RegimeMetrics, ScoringParams
from decision_lab.seats import FINAL, ROUND_ZERO, SeatMetrics, rounds_are_identical
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.validation.backtest import BANNER

#: The tool's own standing disclaimer. §1.1: it compares configurations an operator wrote; it
#: does not search, does not optimise, and has no authority over anything.
DISCLAIMER = (
    "**This is a comparison instrument, not evidence of alpha.** It ranks configurations against "
    "one another on recorded history. It is not a promotion gate — `validation/promotion.py` "
    "remains the only thing that answers whether anything may be promoted, and it reads the "
    "production log."
)

NEWS_BLIND = (
    "**NEWS-BLIND RUN** — no news archive was wired, so every snapshot records "
    '"no sources configured". A shock block therefore measures the panel\'s reaction to a violent '
    "price move rather than to the reporting of an event."
)


class LabReport(DomainModel):
    """Everything one report says. One object, so the notebook and the renderer agree."""

    generated_at: UtcDatetime
    corpus_id: str
    dataset_directory: str
    dataset_digest: str
    dayset_digest: str = ""
    reference_instrument: str
    reference_panel_id: str
    reference_config_digest: str
    cadence_seconds: int
    scoring: ScoringParams
    vol_window_bars: int
    shock_percentile: Money
    named_windows: tuple[str, ...] = ()
    start_equity: Money
    news_blind: bool = True
    panel_models: tuple[str, ...] = ()
    cycles: int = 0
    regimes: tuple[RegimeMetrics, ...] = ()
    seats: tuple[SeatMetrics, ...] = ()


def report_markdown(report: LabReport) -> str:
    sections = [
        "# decision_lab — decision quality over recorded history",
        "",
        BANNER,
        "",
        DISCLAIMER,
    ]
    if report.news_blind:
        sections += ["", NEWS_BLIND]
    sections += [
        "",
        _identity(report),
        "",
        "## Panel, by regime",
        "",
        _regime_table(report.regimes),
        "",
        _unscored(report.regimes),
        "",
        "## Seats, by regime",
        "",
        _seat_tables(report.seats),
    ]
    return "\n".join(sections).rstrip() + "\n"


def _identity(report: LabReport) -> str:
    rows = [
        ("generated", _stamp(report.generated_at)),
        ("corpus", report.corpus_id),
        ("dataset", f"{report.dataset_directory} (`{report.dataset_digest}`)"),
        ("day set", report.dayset_digest or "not pinned"),
        ("reference instrument", report.reference_instrument),
        ("reference panel", f"{report.reference_panel_id} (`{report.reference_config_digest}`)"),
        ("panel models", ", ".join(report.panel_models) or "none recorded"),
        ("cadence", f"{report.cadence_seconds}s"),
        ("cycles", str(report.cycles)),
        ("scoring timeframe", report.scoring.timeframe),
        ("band", f"{report.scoring.band_k} × ATR"),
        ("forward horizon", f"{report.scoring.horizon_bars} bars"),
        ("ATR lookback", f"{report.scoring.atr_lookback_bars} bars"),
        ("volatility window", f"{report.vol_window_bars} bars"),
        ("shock percentile", str(report.shock_percentile)),
        ("named windows", ", ".join(report.named_windows) or "none"),
        ("starting equity", str(report.start_equity)),
    ]
    return "## Experiment\n\n" + _table(("", ""), [[label, value] for label, value in rows])


def _regime_table(regimes: Sequence[RegimeMetrics]) -> str:
    headers = (
        "regime",
        "scored",
        "accuracy",
        "action rate",
        "precision on action",
        "conviction gap",
        "regret/decision",
        "degraded",
        "$/scored",
    )
    rows = [
        [
            metrics.regime,
            str(metrics.scored),
            _pct(metrics.accuracy),
            _pct(metrics.action_rate),
            _pct(metrics.precision_on_action),
            _num(metrics.mean_conviction_gap),
            _num(metrics.regret_per_decision),
            _pct(metrics.degradation_rate),
            _num(metrics.cost_per_scored),
        ]
        for metrics in regimes
    ]
    note = (
        "\n\n`regret/decision` is the oracle's capture minus the panel's, in band units. It is a "
        "**ranking aid and is unreachable by construction**: the oracle exits at the high of every "
        "window and no risk-managed system can match it.\n\n"
        "`SHOCK_UP` and `SHOCK_DOWN` are never pooled. An up-shock asks whether the seats caught "
        "the move; a down-shock asks whether they protected capital. **Read `SHOCK_DOWN` first** — "
        "a long-only system's worst outcome is not a missed rally."
    )
    return _table(headers, rows) + note


def _unscored(regimes: Sequence[RegimeMetrics]) -> str:
    rows = [
        [metrics.regime, reason, str(count)]
        for metrics in regimes
        for reason, count in sorted(metrics.unscored.items())
    ]
    if not rows:
        return "Every decision was scored."
    return (
        "### Unscored\n\nCounted with its reason, never dropped — a run that dropped them would "
        "report accuracy over a subset it chose after the fact.\n\n"
        + _table(("regime", "reason", "count"), rows)
    )


def _seat_tables(seats: Sequence[SeatMetrics]) -> str:
    if not seats:
        return "No seat responses were recorded."
    identical = rounds_are_identical(seats)
    shown = [s for s in seats if s.round_label == FINAL] if identical else list(seats)
    headers = (
        "seat",
        "regime",
        "round",
        "votes",
        "accuracy",
        "precision on action",
        "abstained",
        "fell back",
        "swing rate",
        "marginal",
        "$/vote",
        "ms/vote",
    )
    rows = [
        [
            metrics.seat_id,
            metrics.regime,
            metrics.round_label,
            str(metrics.scored),
            _pct(metrics.accuracy),
            _pct(metrics.precision_on_action),
            _pct(metrics.abstention_rate),
            _pct(metrics.fallback_rate),
            _pct(metrics.swing_rate) if metrics.round_label == FINAL else "—",
            str(metrics.marginal_contribution) if metrics.round_label == FINAL else "—",
            _num(metrics.cost_per_vote),
            str(metrics.latency_ms_per_vote),
        ]
        for metrics in shown
    ]
    note = (
        "\n\nUnder `blind_then_debate` a seat's later votes are contaminated by its peers **by "
        f"design** — that is what the debate is for. `{ROUND_ZERO}` is the seat's own independent "
        f"opinion; `{FINAL}` is the seat after persuasion. *Which seat reasons well* and *which "
        "seat is easily talked round* are different questions.\n\n"
        "`swing rate` is how often removing this seat would have changed the panel's decision — "
        "what separates a seat carrying weight from one padding a majority. `marginal` is right "
        "dissents against a wrong panel minus wrong dissents against a right one."
    )
    if identical:
        note = (
            "\n\nThis panel ran `single_round`, so round 0 **is** the final vote; one table is "
            "shown rather than the same numbers twice." + note
        )
    return _table(headers, rows) + note


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _pct(value: Decimal) -> str:
    return f"{(value * Decimal(100)).quantize(Decimal('0.1'))}%"


def _num(value: Decimal | None) -> str:
    return "—" if value is None else str(value.quantize(Decimal("0.0001")))


def _stamp(moment: datetime) -> str:
    return moment.isoformat()


def write_report(report: LabReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_markdown(report), encoding="utf-8")
    return path
```

Add to `decision_lab/cli.py`'s `parse_args`:

```python
    report_ = commands.add_parser(
        "report", help="score a built corpus and file the result under decision_lab/reports/"
    )
    report_.add_argument("--corpus", required=True, help="corpus id from `corpus build`")
    report_.add_argument("--data", type=Path, default=None, help="override the recorded dataset path")
    report_.add_argument("--regimes", type=Path, default=None, help="named event windows TOML")
    report_.add_argument("--scoring-timeframe", default="", help="defaults to the shortest")
    report_.add_argument("--band-k", type=Decimal, default=None, help="the ATR multiple, default 1.0")
    report_.add_argument("--horizon", type=int, default=None, help="forward bars, default 6")
    report_.add_argument("--out", type=Path, default=None, help="report path (.md)")
    report_.add_argument("--verbose", action="store_true")
```

and the handler:

```python
async def report(args: argparse.Namespace) -> int:
    """Score the reference pass in a built corpus and write the Markdown report."""
    meta, cycles = rc.load(args.corpus)
    data_dir = args.data or Path(meta.dataset_directory)
    audit = ds.require_verified(data_dir)
    dataset = ReplayDataset.load(data_dir, SystemClock())

    params = sc.ScoringParams(
        timeframe=args.scoring_timeframe or dataset.timeframes[0],
        **({"band_k": args.band_k} if args.band_k is not None else {}),
        **({"horizon_bars": args.horizon} if args.horizon is not None else {}),
    )
    index = await sc.build_price_index(dataset, audit, params)
    regime_index = (
        await rg.index_dataset(dataset, params.timeframe)
    ).with_windows(rg.load_windows(args.regimes or rg.DEFAULT_REGIMES_TOML))

    scored = sc.score_records(cycles, index=index, regimes=regime_index, params=params)
    panel = meta.reference_basket.panel
    built = rd.LabReport(
        generated_at=SystemClock().now(),
        corpus_id=meta.corpus_id,
        dataset_directory=str(data_dir),
        dataset_digest=meta.dataset_digest,
        dayset_digest=_dayset_digest(data_dir),
        reference_instrument=dataset.instruments[0].key,
        reference_panel_id=meta.reference_panel_id,
        reference_config_digest=meta.reference_config_digest,
        cadence_seconds=meta.cadence_seconds,
        scoring=params,
        vol_window_bars=regime_index.window_bars,
        shock_percentile=regime_index.shock_percentile,
        named_windows=tuple(w.name for w in regime_index.windows),
        start_equity=meta.start_equity,
        news_blind=meta.news_blind,
        panel_models=tuple(dict.fromkeys(f"{s.provider_id}:{s.model}" for s in panel.seats)),
        cycles=len(cycles),
        regimes=sc.by_regime(scored),
        seats=st.score_seats(cycles, scored, panel=panel),
    )
    out = args.out or reports_dir() / f"decision-lab-{meta.corpus_id}.md"
    rd.write_report(built, out)
    logger.info("report written", extra={"path": str(out), "decisions": len(scored)})
    return EXIT_OK


def _dayset_digest(data_dir: Path) -> str:
    """The pinned day set is not required to score a corpus — it is required to *calibrate* one
    (slice D). Recorded when present so a report can be tied to the set in force, absent
    otherwise rather than refusing a scoring run for want of a §10 artifact."""
    try:
        return cd.require_pinned(data_dir).dayset_digest
    except ConfigError:
        return ""
```

with `_COMMANDS[("report", "")] = report` — note `report` has no sub-action, so `getattr(args, "action", "")` yields `""` — and the imports `from decision_lab import records as rc, regimes as rg, render as rd, scoring as sc, seats as st` plus `from decision_lab.params import reports_dir`.

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS, every test in slices A and B.

- [ ] **Step 5: Verify against the real corpus**

```powershell
.venv\Scripts\python.exe -m decision_lab report --corpus <id from slice A>
```

Expected: a Markdown file under `decision_lab\reports\` with the contamination banner, the `NEWS-BLIND RUN` banner, the identity block, three regime rows plus the two named 2024 windows if the dataset covers them, and one seat table per seat and regime. Read it: **that report is the whole point of the slice**, and anything on it that is not legible to an operator tuning seats is a defect worth fixing before slice C builds on it.

- [ ] **Step 6: Run both gates**

Run: `.\decision_lab\check.ps1`
Expected: `decision_lab checks passed`.

Run: `.\check.ps1`
Expected: `all checks passed`.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/render.py decision_lab/params.py decision_lab/cli.py decision_lab/tests/
git commit -m "feat(decision_lab): the regime and per-seat report, filed as Markdown"
```

---

## Slice B exit criteria

1. `.\decision_lab\check.ps1` and `.\check.ps1` both pass.
2. `git diff --stat main -- tradebot/` is **empty**.
3. `report --corpus <id>` against the real six-month 2024 corpus produces a Markdown file whose `NORMAL`, `SHOCK_UP` and `SHOCK_DOWN` rows all carry a non-zero `scored` count — if `SHOCK_DOWN` is empty on six months of 2024 crypto, the labeller's threshold is wrong, not the market.
4. The report names every seat, with round 0 beside final, a swing rate and a marginal contribution — §18's promise that slice B "scores the existing panel over six months in all three regimes, and says which seat carried it".
5. Nothing in `scoring.py`, `seats.py` or `render.py` calls `float`, asserted by slice A's `test_discipline.py`.

## Notes for whoever plans slice C

- `by_regime` returns one row per regime *per candidate* once there is more than one candidate; the signature will need a `candidate_id`. Plan for it rather than retrofitting `RegimeMetrics`.
- §9.6's agreement matrix and tradable divergence reuse `validation/comparison.py`'s definition of a divergence — import it, do not restate it.
- `test_discipline.py`'s `FLOAT_EXEMPT` will need `candidates.py`: `tomllib` parses `temperature = 0.3` as a `float`, and `SeatConfig.temperature` is a model hyper-parameter rather than money. That is the only exemption the spec anticipates.
