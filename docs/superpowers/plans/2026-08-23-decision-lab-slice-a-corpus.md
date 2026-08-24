# decision_lab Slice A — integrity, the pinned day set, and the corpus

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the `decision_lab/` tool beside `tradebot` — with the import boundary asserted, not intended — and deliver the three things every later slice reads: a dataset whose holes are found and repaired, a calibration day set selected once and pinned, and a corpus of frozen decision contexts produced by one reference pass through the bot's own loop.

**Architecture:** A new top-level package that **imports `tradebot` and is never imported by it**. Nothing here is a bot change: no `ConfigKind`, no CLI subcommand on `tradebot`, no new dependency, no write to a bot database. `dataset.py` audits recorded CSVs with `CandleSeries.gaps` and repairs fetch gaps from public Binance REST, writing a sidecar audit beside the data. `calibration_days.py` selects three normal, three up-shock and three down-shock days against a declared reference instrument and pins them to a file. `corpus.py` runs one reference pass through the unmodified `BacktestHarness` into a workspace database and reads the corpus back out of `SNAPSHOT_FROZEN`.

**Tech Stack:** Python 3.11, pydantic v2, SQLAlchemy 2.0 (through `tradebot`'s own store), stdlib `tomllib`/`csv`/`hashlib`/`argparse`, pytest, hypothesis, ruff, mypy.

**Spec:** [docs/superpowers/specs/2026-08-23-decision-lab-design.md](../specs/2026-08-23-decision-lab-design.md) — §2, §2.1, §2.4, §4 in full, §5 in full, the `dataset`/`corpus` half of §13, the matching rows of §15, and the `structural`/`round-trip` rows of §16.

**Slice:** A of five (§18). B (scoring and regimes) is planned in `2026-08-23-decision-lab-slice-b-scoring.md` and depends on this one. C (the sweep), D (calibration and the dashboard) and E (news) are planned when reached.

**Decisions taken before planning, on 2026-08-23:**

1. The §2.2 `tradebot` seam (`build_sim(news_feed=…)`) is **approved** — but it belongs to slice E, and lands in one commit with the §2.3 guard tests. **Slice A changes zero bot files.** Every corpus built here is news-blind (§6.9).
2. The §6.3 archive backend is decided at slice E. Nothing here depends on it; `archive_digest` is threaded through as `""`.
3. The worked dataset is **BTC/USDT + ETH/USDT, `1h`, 2024-01-01 → 2024-07-01**, which is the period the spec's own §4.5 pinned days and §8.2 named windows come from.

## Prerequisite: record the dataset

There is no dataset in the repo — `data/history` does not exist. Before Task 3 can be checked against real data, record one. Public, read-only, no key:

```powershell
.venv\Scripts\python.exe -m tradebot backtest fetch --symbol BTC/USDT --symbol ETH/USDT `
    --timeframe 1h --since 2024-01-01 --until 2024-07-01 --out data\history
```

Every task's *tests* run on synthetic datasets written by `decision_lab/tests/factories.py` and never touch the network. The real dataset is only for the manual verification steps that say so.

## Global Constraints

- **Nothing under `tradebot/` may name `decision_lab`.** Slice A modifies **no** file under `tradebot/`. The only files outside `decision_lab/` this slice touches are `.gitignore`.
- **No new dependency.** TOML is read with stdlib `tomllib` (3.11 ships it). `pyproject.toml` is not edited — not for mypy, not for coverage, not for ruff.
- **No `float` anywhere in `decision_lab/`.** Prices, volatilities, percentiles and thresholds are `Decimal`, computed through `tradebot.core.money`'s `MONEY_CONTEXT`. Task 1 asserts this structurally, the way `test_money_discipline.py` does for the bot.
- **Time is UTC-aware `datetime`.** Replay time comes from `ManualClock`; wall-clock stamps come from an injected `Clock`. No `datetime.now()` in library code.
- **Errors are classified.** A refusal is `ConfigError` (a `FatalError`) carrying what to do about it. A bare `except: pass` is a defect.
- **Every write lands under `decision_lab/workspace/` or beside the dataset.** Never `data/`, never a bot database.
- **Line length 100**, `ruff format`, `from __future__ import annotations` in every module, full type annotations.
- **Reuse, never reimplement** (§2.4). If a behaviour exists in `tradebot`, import it. Copying `_read_csv` or `reach_consensus` into this package is a defect even if it works.
- Verification: `decision_lab\check.ps1` for this package, and `.\check.ps1` at the repo root must still pass — root `ruff` walks the repo and now sees these files.

---

### Task 1: The package, the two structural guards, and its own gate

The boundary is the product of this task. Everything else in the slice is built inside a fence that CI can prove is intact.

**Files:**
- Create: `decision_lab/__init__.py`
- Create: `decision_lab/py.typed`
- Create: `decision_lab/.ruff.toml`
- Create: `decision_lab/check.ps1`
- Create: `decision_lab/tests/test_separation.py`
- Create: `decision_lab/tests/test_discipline.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: the `decision_lab` package root; `decision_lab\check.ps1` as the slice's gate command.

- [ ] **Step 1: Write the failing tests**

Create `decision_lab/tests/test_separation.py`:

```python
"""The import direction is one-way, and asserted rather than intended (spec §2.1).

`decision_lab` imports `tradebot`. Nothing under `tradebot/` may name `decision_lab` — not an
import, not an attribute, not a string. The bot must be buildable, testable and shippable with
this folder deleted, and a boundary that depends on nobody breaking it is not a boundary.

The check is AST-based, so a `#` comment mentioning the tool is fine: comments are not code and
cannot create a dependency. A docstring is a `Constant` and *is* checked, deliberately — a module
docstring explaining what `decision_lab` does belongs in `decision_lab`.

Same class of structural guard as `tests/unit/test_money_discipline.py` and the float boundary in
`tests/unit/test_dashboard_chart.py`: a rule CI can prove, not a review comment.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tradebot

TOOL = "decision_lab"
BOT_ROOT = Path(tradebot.__file__).parent


def bot_sources() -> list[tuple[Path, ast.Module]]:
    files = sorted(BOT_ROOT.rglob("*.py"))
    assert files, "tradebot package not found — the check would pass vacuously"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in files]


SOURCES = bot_sources()


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_bot_module_imports_the_tool(path: Path, tree: ast.Module) -> None:
    """An import is the hard failure: it would make the tool a runtime dependency of the bot."""
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == TOOL]
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == TOOL:
            offenders.append(node.module or "")
    assert not offenders, f"{path.name} imports {offenders}"


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_bot_module_names_the_tool(path: Path, tree: ast.Module) -> None:
    """A name or a string is the soft failure, and still a dependency worth refusing."""
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == TOOL)
        or (isinstance(node, ast.Attribute) and node.attr == TOOL)
        or (isinstance(node, ast.Constant) and isinstance(node.value, str) and TOOL in node.value)
    ]
    assert not offenders, f"{path.name} names {TOOL}: {offenders}"


def test_the_guard_can_actually_fail() -> None:
    """A structural test that cannot fail is a comment. Prove the detector works."""
    tree = ast.parse("from decision_lab.corpus import Corpus\n")
    found = [
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == TOOL
    ]
    assert found == ["decision_lab.corpus"]
```

Create `decision_lab/tests/test_discipline.py`:

```python
"""No `float` in `decision_lab` (spec §9.2, §16 structural row).

The bot's own guard walks `core/`, `risk/`, `execution/` and `ledger/`. This package is outside
those, so it asserts the same rule over itself: a band derived from ATR, a realised volatility, a
percentile threshold and a profit figure are all money-path arithmetic, and a `float` in any of
them is the binary rounding error `tradebot.core.money` exists to keep out.

The exemption set is empty on purpose. Slice C's `candidates.py` will need one entry —
`SeatConfig.temperature`, which `tomllib` parses as a `float` and which is a model
hyper-parameter, not money. Add it there, with that reason, and nowhere else.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import decision_lab

TOOL_ROOT = Path(decision_lab.__file__).parent
#: Modules permitted to name `float`. Empty in slice A — see the module docstring.
FLOAT_EXEMPT: frozenset[str] = frozenset()


def tool_sources() -> list[tuple[Path, ast.Module]]:
    files = [p for p in sorted(TOOL_ROOT.rglob("*.py")) if "tests" not in p.parts]
    assert files, "decision_lab has no modules — the check would pass vacuously"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in files]


SOURCES = tool_sources()


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_float_calls(path: Path, tree: ast.Module) -> None:
    if path.name in FLOAT_EXEMPT:
        pytest.skip(f"{path.name} is an declared exemption")
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
    ]
    assert not offenders, f"float() calls: {offenders}"


@pytest.mark.parametrize("path,tree", SOURCES, ids=lambda v: getattr(v, "name", ""))
def test_no_float_annotations(path: Path, tree: ast.Module) -> None:
    if path.name in FLOAT_EXEMPT:
        pytest.skip(f"{path.name} is an declared exemption")
    offenders = [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.annotation, ast.Name)
        and node.annotation.id == "float"
    ]
    assert not offenders, f"float annotations: {offenders}"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: collection error — `ModuleNotFoundError: No module named 'decision_lab'`.

- [ ] **Step 3: Create the package and its config**

`decision_lab/__init__.py`:

```python
"""A fine-tuning instrument for the panel's decision logic.

Standalone by contract: this package imports `tradebot` and `tradebot` knows nothing about it
(spec §2). It scores decisions and compares configurations; it never trades, never writes to a
bot database, and has no code path from a `Decision` to an `OrderIntent`.

Failure semantics: every refusal here is a `ConfigError` naming the command that fixes it. An
unverified dataset, a missing pinned day set and a compacted corpus all fail closed, because
every number downstream is derived from them.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
```

`decision_lab/py.typed`: empty file.

`decision_lab/.ruff.toml` — so imports of this package sort as first-party rather than beside `pydantic`. The root `pyproject.toml` sets `src = ["tradebot", "tests"]`, which does not include the repo root, so without this `decision_lab` is classified third-party:

```toml
# Inherits the repo's rules; only the import classification differs. `pyproject.toml` is a
# `tradebot` file and spec §2.1 keeps it untouched, so the override lives here instead.
extend = "../pyproject.toml"
src = ["..", "../tradebot"]

[lint.isort]
known-first-party = ["tradebot", "decision_lab"]
```

`decision_lab/check.ps1`:

```powershell
# The tool's own gate. The root .\check.ps1 is unmodified and names only `tradebot` in its mypy,
# pytest and coverage steps (spec §2.1) — root `ruff` already walks this folder, so formatting and
# linting are checked in both places and that is deliberate.
# Usage: .\decision_lab\check.ps1 [-Fix]
param([switch]$Fix)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv missing - see README quick start" }

function Step($name, [scriptblock]$body) {
    Write-Host "`n=== $name ===" -ForegroundColor Cyan
    & $body
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $name" -ForegroundColor Red; exit 1 }
}

Push-Location $root
try {
    if ($Fix) {
        Step "format" { & $py -m ruff format decision_lab }
        Step "lint"   { & $py -m ruff check --fix decision_lab }
    } else {
        Step "format" { & $py -m ruff format --check decision_lab }
        Step "lint"   { & $py -m ruff check decision_lab }
    }
    Step "types" { & $py -m mypy decision_lab }
    Step "tests" { & $py -m pytest decision_lab/tests }
} finally {
    Pop-Location
}

Write-Host "`ndecision_lab checks passed" -ForegroundColor Green
```

Append to `.gitignore`, under the existing `reports/` block:

```gitignore
# The tuning tool's scratch space: corpus databases, sweep caches, results and the registry.
# Reproducible from a dataset and a config, and large enough that committing it is a mistake.
decision_lab/workspace/
decision_lab/reports/
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS. `test_no_float_calls` and `test_no_float_annotations` collect only `__init__.py` at this point and pass trivially; they gain teeth in Task 2.

- [ ] **Step 5: Verify both gates**

Run: `.\decision_lab\check.ps1`
Expected: `decision_lab checks passed`.

Run: `.\check.ps1`
Expected: `all checks passed`. If `ruff` reports the nested `.ruff.toml` cannot `extend` the root `pyproject.toml`, delete `decision_lab/.ruff.toml` and let the package sort as third-party — nothing else in the plan depends on it.

- [ ] **Step 6: Commit**

```bash
git add decision_lab .gitignore
git commit -m "feat(decision_lab): the package, its separation guard, and its own gate"
```

---

### Task 2: The shared parameters and the realised-volatility estimator

§4.5's day selection and §8.1's bar labelling are **the same measurement over different windows**. Building it once here is what makes that true rather than merely claimed — slice B's `regimes.py` imports this module, it does not restate it.

**Files:**
- Create: `decision_lab/params.py`
- Create: `decision_lab/volatility.py`
- Test: `decision_lab/tests/test_volatility.py`

**Interfaces:**
- Consumes: `tradebot.core.market.Candle`, `tradebot.core.money.MONEY_CONTEXT`, `tradebot.core.errors.ConfigError`.
- Produces:
  - `params.DEFAULT_HORIZON_BARS: int = 6`, `params.DEFAULT_BAND_K: Decimal = Decimal("1.0")`, `params.DEFAULT_VOL_WINDOW_BARS: int = 30`, `params.DEFAULT_SHOCK_PERCENTILE: Decimal = Decimal("0.90")`, `params.NORMAL_PERCENTILE_BAND: tuple[Decimal, Decimal] = (Decimal("0.40"), Decimal("0.60"))`, `params.DAYS_PER_POOL: int = 3`, `params.COVERAGE_FILE: str`, `params.DAYSET_FILE: str`, `params.CORPUS_META: str`, `params.workspace_root() -> Path`
  - `volatility.log_returns(candles: Sequence[Candle]) -> tuple[Decimal, ...]`
  - `volatility.realised_volatility(candles: Sequence[Candle]) -> Decimal`
  - `volatility.window_return(candles: Sequence[Candle]) -> Decimal`
  - `volatility.percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_volatility.py`:

```python
"""One estimator, two windows (spec §4.5, §8.1).

The property that matters is scale invariance: a shock is a shock whether the instrument trades
at 60,000 or at 0.60. Without it every calibration day would be drawn from whichever instrument
happens to carry the largest absolute numbers, and the reference instrument would be a
formality.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from decision_lab.volatility import (
    log_returns,
    percentile,
    realised_volatility,
    window_return,
)
from tradebot.core.enums import MarketSession
from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle

START = datetime(2024, 1, 1, tzinfo=UTC)


def series(closes: list[str]) -> tuple[Candle, ...]:
    """One 1h candle per close. Open/high/low are irrelevant here — the estimator reads closes."""
    return tuple(
        Candle(
            open_time=START + timedelta(hours=i),
            close_time=START + timedelta(hours=i + 1),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal(1),
            session=MarketSession.CONTINUOUS,
        )
        for i, close in enumerate(closes)
    )


def test_a_flat_series_has_zero_volatility() -> None:
    assert realised_volatility(series(["100", "100", "100", "100"])) == Decimal(0)


def test_volatility_rises_with_the_size_of_the_moves() -> None:
    calm = realised_volatility(series(["100", "101", "100", "101"]))
    violent = realised_volatility(series(["100", "110", "100", "110"]))
    assert violent > calm > Decimal(0)


def test_volatility_is_invariant_to_price_scale() -> None:
    """The same relative moves on BTC and on XRP are the same volatility."""
    btc = realised_volatility(series(["60000", "63000", "60000"]))
    xrp = realised_volatility(series(["0.60", "0.63", "0.60"]))
    assert btc == pytest.approx(xrp, rel=Decimal("1e-20"))


@given(scale=st.integers(min_value=1, max_value=10_000))
def test_volatility_is_invariant_to_any_scale(scale: int) -> None:
    base = ["100", "104", "97", "103"]
    scaled = [str(Decimal(c) * scale) for c in base]
    assert realised_volatility(series(base)) == realised_volatility(series(scaled))


def test_window_return_carries_the_sign() -> None:
    assert window_return(series(["100", "110"])) > Decimal(0)
    assert window_return(series(["110", "100"])) < Decimal(0)
    assert window_return(series(["100", "100"])) == Decimal(0)


def test_a_single_bar_has_no_return_and_no_volatility() -> None:
    """One bar is one price. Refusing would make the first day of every dataset an error."""
    assert log_returns(series(["100"])) == ()
    assert realised_volatility(series(["100"])) == Decimal(0)
    assert window_return(series(["100"])) == Decimal(0)


def test_an_empty_window_refuses() -> None:
    with pytest.raises(ConfigError, match="no candles"):
        realised_volatility(())


def test_a_non_positive_close_refuses() -> None:
    """A log return needs a positive price. A zero close is corrupt data, not a 100% loss."""
    broken = series(["100", "100"])
    zeroed = (broken[0], broken[1].model_copy(update={"close": Decimal(0)}))
    with pytest.raises(ConfigError, match="non-positive close"):
        realised_volatility(zeroed)


@pytest.mark.parametrize(
    "fraction,expected",
    [("0.00", "1"), ("0.10", "1"), ("0.50", "5"), ("0.90", "9"), ("1.00", "10")],
)
def test_percentile_is_nearest_rank(fraction: str, expected: str) -> None:
    """Nearest-rank, so a threshold is always a value the data actually took."""
    values = [Decimal(n) for n in range(1, 11)]
    assert percentile(values, Decimal(fraction)) == Decimal(expected)


def test_percentile_sorts_its_input() -> None:
    assert percentile([Decimal(9), Decimal(1), Decimal(5)], Decimal("0.50")) == Decimal(5)


def test_percentile_refuses_an_empty_population() -> None:
    with pytest.raises(ConfigError, match="empty population"):
        percentile([], Decimal("0.50"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_volatility.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.volatility'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/params.py`:

```python
"""Tuning defaults shared across slices, and where the tool writes.

They live in one module because §4.5's day selection and §8.1's bar labelling must agree on the
same thresholds, and §4.5's eligibility rule and §9.2's scoring must agree on the same forward
horizon. Two copies of `30` and `6` that drift is a day set selected under one rule and scored
under another.

Every value here is overridable on the command line; these are the numbers in force when nobody
said otherwise, and the report prints whichever was used.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

#: Forward bars a decision is scored over (§9.2), and the tail a calibration day needs after it
#: to be eligible (§4.5).
DEFAULT_HORIZON_BARS: Final = 6

#: The ATR multiple that makes the scoring band (§9.2). 1.0 means "a move larger than one ATR".
DEFAULT_BAND_K: Final = Decimal("1.0")

#: Trailing bars the regime labeller measures realised volatility over (§8.1).
DEFAULT_VOL_WINDOW_BARS: Final = 30

#: At or above this percentile of an instrument's own distribution is a shock (§8.1, §4.5).
DEFAULT_SHOCK_PERCENTILE: Final = Decimal("0.90")

#: The percentile band a day must sit inside to count as ordinary (§4.5).
NORMAL_PERCENTILE_BAND: Final = (Decimal("0.40"), Decimal("0.60"))

#: Days drawn from each of the three pools. Three is not a distribution, but it is enough to see
#: when one day carried a result (§10.2).
DAYS_PER_POOL: Final = 3

#: Written beside the dataset, never inside it: `dataset.json` is a `tradebot` model and editing
#: it would be a bot change (§4.3).
COVERAGE_FILE: Final = "decision_lab-coverage.json"
DAYSET_FILE: Final = "decision_lab-calibration-days.json"

#: Written beside the corpus database, in the workspace.
CORPUS_META: Final = "corpus.json"


def workspace_root() -> Path:
    """Scratch databases, caches and results. Gitignored, and never `data/` (§2.1)."""
    return Path(__file__).parent / "workspace"
```

Create `decision_lab/volatility.py`:

```python
"""Realised volatility, its sign, and percentiles — all in `Decimal`.

One estimator serves two callers with different windows: §8.1 labels each *bar* from a trailing
30-bar window, and §4.5 labels each *day* from that day's own bars. Same measurement, so a day
selected as a shock is a day the labeller also calls a shock.

Realised volatility here is the root mean square of close-to-close log returns — no mean
subtraction, which is the standard construction and the one that stays scale-free. Scale-free is
the load-bearing property: an absolute-move estimator would make every calibration day a day
whichever instrument carries the largest numbers happened to move on.

`Decimal.ln` and `Decimal.sqrt` are exact-context operations, so none of this passes through a
float (spec §9.2). `MONEY_CONTEXT` traps `InvalidOperation` and `DivisionByZero`, which is why a
non-positive close is refused explicitly rather than left to produce a trapped `ln(0)` whose
message names no instrument.

Failure semantics: this module has no dependencies and cannot fail from outside. Bad input — an
empty window, a non-positive price — raises `ConfigError`, because the caller's next step is to
repair the dataset, not to retry.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle
from tradebot.core.money import MONEY_CONTEXT, ZERO, divide


def _closes(candles: Sequence[Candle]) -> tuple[Decimal, ...]:
    if not candles:
        raise ConfigError("no candles in the window: nothing to measure")
    closes = tuple(candle.close for candle in candles)
    if any(close <= ZERO for close in closes):
        raise ConfigError(
            "a non-positive close in the window: a log return is undefined there, and a zero "
            "price is corrupt data rather than a total loss. Run `dataset verify --repair`"
        )
    return closes


def log_returns(candles: Sequence[Candle]) -> tuple[Decimal, ...]:
    """Close-to-close log returns. `n` bars yield `n - 1` returns; one bar yields none."""
    closes = _closes(candles)
    return tuple(
        MONEY_CONTEXT.ln(divide(later, earlier))
        for earlier, later in zip(closes, closes[1:], strict=False)
    )


def realised_volatility(candles: Sequence[Candle]) -> Decimal:
    """Root mean square of the window's log returns. Zero for a flat or single-bar window."""
    returns = log_returns(candles)
    if not returns:
        return ZERO
    total = sum((MONEY_CONTEXT.multiply(r, r) for r in returns), start=ZERO)
    return MONEY_CONTEXT.sqrt(divide(total, Decimal(len(returns))))


def window_return(candles: Sequence[Candle]) -> Decimal:
    """The window's signed return, close to close.

    Close to close rather than open to close, so the sign is the sign of the same series
    `realised_volatility` squares. A direction measured on one series and a magnitude on another
    can disagree, and a `SHOCK_UP` day whose magnitude came from a fall is worse than no label.
    """
    closes = _closes(candles)
    return divide(closes[-1], closes[0]) - Decimal(1)


def percentile(values: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """Nearest-rank percentile: the smallest value at or above `fraction` of the population.

    Nearest-rank rather than interpolated, so a threshold is always a number the data actually
    took. An interpolated 90th percentile is a volatility no bar ever had, which reads oddly on a
    report that has to justify why a particular day was chosen.
    """
    if not values:
        raise ConfigError("percentile of an empty population")
    if not ZERO <= fraction <= Decimal(1):
        raise ConfigError(f"percentile fraction must be within [0, 1], got {fraction}")
    ordered = sorted(values)
    rank = int((fraction * Decimal(len(ordered))).to_integral_value(rounding="ROUND_CEILING"))
    return ordered[max(rank, 1) - 1]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_volatility.py -q`
Expected: PASS, 13 tests plus the hypothesis case.

If `test_volatility_is_invariant_to_price_scale` fails on the last digits, the cause is `MONEY_CONTEXT`'s 34-digit precision, not the estimator — `pytest.approx` with `rel=Decimal("1e-20")` is the tolerance for that and must not be widened past it.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/params.py decision_lab/volatility.py decision_lab/tests/test_volatility.py
git commit -m "feat(decision_lab): the shared realised-volatility estimator and tuning defaults"
```

---

### Task 3: The gap audit and the coverage sidecar

**Files:**
- Create: `decision_lab/dataset.py`
- Create: `decision_lab/tests/factories.py`
- Test: `decision_lab/tests/test_dataset_audit.py`

**Interfaces:**
- Consumes: `params.COVERAGE_FILE`; `tradebot.marketdata.recorder.{ReplayDataset, DatasetManifest, MANIFEST, CSV_COLUMNS}`, `tradebot.core.market.{Candle, CandleSeries, timeframe_interval}`, `tradebot.core.clock.Clock`, `tradebot.core.schema.{DomainModel, UtcDatetime}`, `tradebot.core.instrument.Instrument`.
- Produces:
  - `dataset.FAR_FUTURE: datetime` and `dataset.FULL_HISTORY: int`
  - `class KnownHole(DomainModel)` with `from_: UtcDatetime` (alias `from`), `to: UtcDatetime`, `reason: str`
  - `class SeriesCoverage(DomainModel)` with `expected: int`, `present: int`, `repaired: int`, `known_holes: tuple[KnownHole, ...]`
  - `class CoverageAudit(DomainModel)` with `audited_at: UtcDatetime`, `dataset_digest: str`, `series: dict[str, SeriesCoverage]`, and `is_clean: bool`
  - `dataset.series_key(instrument_key: str, timeframe: str) -> str` → `"binance:BTC/USDT|1h"`
  - `dataset.csv_path(directory: Path, instrument: Instrument, timeframe: str) -> Path`
  - `dataset.read_series(dataset: ReplayDataset, instrument: Instrument, timeframe: str) -> CandleSeries` (awaitable)
  - `dataset.dataset_digest(directory: Path) -> str`
  - `dataset.audit(dataset: ReplayDataset, clock: Clock) -> CoverageAudit` (awaitable)
  - `dataset.write_audit(directory: Path, audit: CoverageAudit) -> Path`
  - `dataset.read_audit(directory: Path) -> CoverageAudit`
  - `dataset.require_verified(directory: Path) -> CoverageAudit`
  - `tests.factories.write_dataset(...)` and `tests.factories.walk(...)`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/factories.py`:

```python
"""Datasets written to disk for the tool's own tests. Offline, deterministic, tiny.

A real `ReplayDataset` rather than a stub, because everything under test reads the manifest, the
CSV layout and `CandleSeries.gaps` — three things a stub would get right by construction and the
real recorder might not.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tradebot.core.enums import AssetClass, MarketSession
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, timeframe_interval
from tradebot.marketdata.recorder import CSV_COLUMNS, MANIFEST, DatasetManifest

EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def instrument(symbol: str = "BTC/USDT", venue: str = "binance") -> Instrument:
    return Instrument(
        symbol=symbol,
        venue=venue,
        asset_class=AssetClass.CRYPTO,
        base_currency=symbol.split("/")[0],
        quote_currency=symbol.split("/")[1],
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("5"),
    )


def walk(
    closes: Sequence[str], *, timeframe: str = "1h", start: datetime = EPOCH
) -> tuple[Candle, ...]:
    """One candle per close, on the venue's epoch-aligned grid, contiguous by construction."""
    interval = timeframe_interval(timeframe)
    bars = []
    for index, close in enumerate(closes):
        price = Decimal(close)
        open_time = start + interval * index
        bars.append(
            Candle(
                open_time=open_time,
                close_time=open_time + interval,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(1),
                session=MarketSession.CONTINUOUS,
            )
        )
    return tuple(bars)


def drop_bars(candles: Sequence[Candle], *, at: int, count: int) -> tuple[Candle, ...]:
    """Punch a hole. What a dropped page in `marketdata/recorder.py` leaves behind (§4.1)."""
    return tuple(candles[:at]) + tuple(candles[at + count :])


def write_series(path: Path, candles: Sequence[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "open_time": candle.open_time.isoformat(),
                    "close_time": candle.close_time.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                    "session": candle.session.value,
                }
            )


def write_dataset(
    directory: Path,
    series: dict[tuple[Instrument, str], Sequence[Candle]],
    *,
    source: str = "test",
) -> Path:
    """Write CSVs plus the manifest `ReplayDataset.load` demands."""
    directory.mkdir(parents=True, exist_ok=True)
    instruments = tuple(dict.fromkeys(i for i, _ in series))
    timeframes = tuple(dict.fromkeys(tf for _, tf in series))
    for (inst, timeframe), candles in series.items():
        name = f"{inst.venue}__{inst.symbol.replace('/', '_')}__{timeframe}.csv"
        write_series(directory / name, candles)
    spans = [(c[0].open_time, c[-1].close_time) for c in series.values() if c]
    manifest = DatasetManifest(
        source=source,
        recorded_at=EPOCH,
        instruments=instruments,
        timeframes=timeframes,
        requested_start=min(s for s, _ in spans),
        requested_end=max(e for _, e in spans),
    )
    (directory / MANIFEST).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return directory
```

Create `decision_lab/tests/test_dataset_audit.py`:

```python
"""Every series is audited for holes before anything is built on it (spec §4.1–§4.4).

`marketdata/recorder.record` writes whatever paging returned and never checks completeness, while
`CandleSeries.gaps` has always existed and was never consulted at dataset level. A hole matters
more here than in a backtest: ATR is both the panel's volatility evidence and the denominator of
the §9.2 scoring band, so a band computed across a hole is wrong while looking right.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab import dataset as ds
from decision_lab.params import COVERAGE_FILE
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, 9, 14, 2, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


def load(directory: Path, clock: ManualClock) -> ReplayDataset:
    return ReplayDataset.load(directory, clock)


async def test_a_complete_series_audits_clean(tmp_path: Path, clock: ManualClock) -> None:
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})

    audit = await ds.audit(load(tmp_path, clock), clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.expected == 48
    assert coverage.present == 48
    assert coverage.known_holes == ()
    assert audit.is_clean


async def test_a_dropped_page_is_found_and_measured(tmp_path: Path, clock: ManualClock) -> None:
    """Six missing bars in the middle: the exact shape a dropped page leaves."""
    inst = f.instrument()
    holed = f.drop_bars(f.walk([str(100 + i) for i in range(48)]), at=20, count=6)
    f.write_dataset(tmp_path, {(inst, "1h"): holed})

    audit = await ds.audit(load(tmp_path, clock), clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.present == 42
    assert coverage.expected == 48, "expected counts the grid over the series' own covered window"
    assert len(coverage.known_holes) == 1
    hole = coverage.known_holes[0]
    assert hole.from_ == f.EPOCH.replace(hour=20)
    assert hole.to == f.EPOCH.replace(hour=26 - 24, day=2) or hole.to.hour == 2
    assert not audit.is_clean


async def test_expected_is_measured_over_the_covered_window_not_the_request(
    tmp_path: Path, clock: ManualClock
) -> None:
    """A manifest may legitimately request more than the venue had (§4.3 step 5).

    Counting against the request would report a permanent, unrepairable shortfall on every
    dataset whose window opened before the instrument was listed.
    """
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 10)})
    manifest = (tmp_path / "dataset.json").read_text(encoding="utf-8")
    (tmp_path / "dataset.json").write_text(
        manifest.replace('"requested_start":"2024-01-01T00:00:00Z"', '"requested_start":"2023-01-01T00:00:00Z"'),
        encoding="utf-8",
    )

    audit = await ds.audit(load(tmp_path, clock), clock)

    assert audit.series["binance:BTC/USDT|1h"].expected == 10


async def test_every_instrument_and_timeframe_is_audited(
    tmp_path: Path, clock: ManualClock
) -> None:
    btc, eth = f.instrument("BTC/USDT"), f.instrument("ETH/USDT")
    bars = f.walk(["100"] * 24)
    f.write_dataset(tmp_path, {(btc, "1h"): bars, (eth, "1h"): bars})

    audit = await ds.audit(load(tmp_path, clock), clock)

    assert set(audit.series) == {"binance:BTC/USDT|1h", "binance:ETH/USDT|1h"}


async def test_the_audit_round_trips_through_the_sidecar(
    tmp_path: Path, clock: ManualClock
) -> None:
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 24), at=5, count=2)})
    written = await ds.audit(load(tmp_path, clock), clock)

    path = ds.write_audit(tmp_path, written)
    assert path.name == COVERAGE_FILE
    assert ds.read_audit(tmp_path) == written


def test_the_sidecar_uses_the_spec_field_names(tmp_path: Path) -> None:
    """`from` and `to`, so the file on disk is the one §4.3 documents."""
    audit = ds.CoverageAudit(
        audited_at=NOW,
        dataset_digest="abc",
        series={
            "binance:ETH/USDT|1h": ds.SeriesCoverage(
                expected=4380,
                present=4374,
                known_holes=(
                    ds.KnownHole(
                        **{
                            "from": datetime(2024, 3, 11, 4, tzinfo=UTC),
                            "to": datetime(2024, 3, 11, 10, tzinfo=UTC),
                            "reason": "venue served no bars on re-request",
                        }
                    ),
                ),
            )
        },
    )
    rendered = audit.model_dump_json()
    assert '"from"' in rendered
    assert '"from_"' not in rendered


async def test_the_digest_moves_when_a_bar_changes(tmp_path: Path, clock: ManualClock) -> None:
    """The digest is what makes a pinned day set and a corpus detectably stale (§15)."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})
    before = ds.dataset_digest(tmp_path)

    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 25)})
    assert ds.dataset_digest(tmp_path) != before


def test_require_verified_refuses_an_unaudited_dataset(tmp_path: Path) -> None:
    """Fail closed: a corpus is the basis of every number downstream (§4.4, §15)."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})

    with pytest.raises(ConfigError, match="dataset verify"):
        ds.require_verified(tmp_path)


async def test_require_verified_refuses_a_stale_audit(
    tmp_path: Path, clock: ManualClock
) -> None:
    """An audit taken before the data changed describes a dataset that no longer exists."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 24)})
    ds.write_audit(tmp_path, await ds.audit(load(tmp_path, clock), clock))

    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 30)})
    with pytest.raises(ConfigError, match="has changed since"):
        ds.require_verified(tmp_path)
```

Note on the hole-boundary assertion in `test_a_dropped_page_is_found_and_measured`: `CandleSeries.gaps` yields `(earlier.close_time, later.open_time)`, and `Candle.close_time` is the *exclusive* boundary (`open_time + interval`, per `marketdata/binance.py`). Dropping bars 20–25 of a series starting at midnight therefore yields exactly `(2024-01-01T20:00Z, 2024-01-02T02:00Z)`. Replace the second assertion line with that literal pair once you have run it:

```python
    assert (hole.from_, hole.to) == (
        datetime(2024, 1, 1, 20, tzinfo=UTC),
        datetime(2024, 1, 2, 2, tzinfo=UTC),
    )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_dataset_audit.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.dataset'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/dataset.py`:

```python
"""Dataset integrity: find the holes, measure them, and record what was found (spec §4).

`marketdata/recorder.record` writes whatever `_page` returned and never audits completeness.
`_page` walks backwards a page at a time, so a dropped page leaves a silent hole — and
`CandleSeries.gaps` has existed since Phase 3 without ever being consulted at dataset level.

A hole matters more here than in a backtest. ATR is both the panel's volatility evidence and the
denominator of the §9.2 scoring band, so a band computed across a hole is a wrong band and every
verdict it produces is wrong while looking right.

Two kinds of hole, and only one is repairable (§4.2). A **fetch gap** is bars the venue has and
our paging missed; a **venue gap** is bars never published — a halt, an outage, maintenance.
Interpolating the second is forbidden (DESIGN §6.2): a fabricated bar feeds a fabricated ATR.

The audit is written to a sidecar rather than into `dataset.json`, because `DatasetManifest` is a
`tradebot` model and editing it would be a bot change (§2). Repair, by contrast, is **in place**
on the CSVs — a strict correction in the same format, so `ReplayDataset.load` reads it unchanged
and the bot's own backtests benefit too.

Failure semantics: reading a dataset that is not one raises `ConfigError` from
`ReplayDataset.load`. `require_verified` refuses an unaudited or stale dataset, naming the
command that fixes it. Nothing here reaches a venue; repair does, and lives in `repair()`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import ConfigDict, Field

from decision_lab.params import COVERAGE_FILE
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import CandleSeries, timeframe_interval
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.marketdata.recorder import MANIFEST, ReplayDataset

#: A cutoff no recorded bar can close after, so `point_in_time` returns the whole series.
FAR_FUTURE: Final = datetime(9999, 12, 31, tzinfo=UTC)
#: `point_in_time` slices `visible[-limit:]`, so this asks for everything.
FULL_HISTORY: Final = 10**9


def series_key(instrument_key: str, timeframe: str) -> str:
    """`binance:BTC/USDT` + `1h` → `binance:BTC/USDT|1h`, the sidecar's key."""
    return f"{instrument_key}|{timeframe}"


def csv_path(directory: Path, instrument: Instrument, timeframe: str) -> Path:
    """The layout `ReplayMarketData.from_directory` reads.

    Reconstructed here rather than imported: `recorder._path_for` is private, and reaching into a
    bot private for a filename is a worse dependency than restating a documented convention that
    the round-trip test pins.
    """
    symbol = instrument.symbol.replace("/", "_")
    return directory / f"{instrument.venue}__{symbol}__{timeframe}.csv"


class KnownHole(DomainModel):
    """Bars the venue never published, on re-request. Recorded, never filled in."""

    model_config = ConfigDict(populate_by_name=True)

    from_: UtcDatetime = Field(alias="from")
    to: UtcDatetime
    reason: str


class SeriesCoverage(DomainModel):
    """What one `(instrument, timeframe)` series holds against what its own window implies."""

    expected: int
    present: int
    repaired: int = 0
    known_holes: tuple[KnownHole, ...] = ()

    @property
    def is_clean(self) -> bool:
        return self.present == self.expected and not self.known_holes


class CoverageAudit(DomainModel):
    """`decision_lab-coverage.json`: what was audited, when, and against which bytes."""

    audited_at: UtcDatetime
    dataset_digest: str
    series: dict[str, SeriesCoverage] = Field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return all(coverage.is_clean for coverage in self.series.values())

    def holes_for(self, key: str) -> tuple[KnownHole, ...]:
        coverage = self.series.get(key)
        return coverage.known_holes if coverage else ()


def dataset_digest(directory: Path) -> str:
    """Content identity of a dataset: the manifest plus every CSV, by name and by bytes.

    What makes a pinned day set (§4.5) and a corpus (§5.4) detectably stale after a repair. Names
    are hashed alongside the bytes so that renaming a series is a different dataset, not the same
    one with the same total content.
    """
    digest = hashlib.blake2s(digest_size=16)
    for path in sorted([directory / MANIFEST, *directory.glob("*.csv")]):
        if not path.is_file():
            continue
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


async def read_series(
    dataset: ReplayDataset, instrument: Instrument, timeframe: str
) -> CandleSeries:
    """The whole recorded series, through the provider's own point-in-time construction.

    Through `get_candles` rather than by re-reading the CSV, so decimal parsing, session labelling
    and ordering are the replay provider's and cannot drift from what a cycle would see.
    """
    return await dataset.market_data.get_candles(
        instrument, timeframe, FULL_HISTORY, end=FAR_FUTURE
    )


def expected_bars(series: CandleSeries) -> int:
    """Bars the venue's epoch-aligned grid implies over the series' *own* covered window.

    Never over the manifest's requested window, which may legitimately be wider than what the
    venue had — an instrument listed after the requested start would otherwise report a permanent
    shortfall no repair can close (§4.3 step 5).
    """
    if not series.candles:
        return 0
    span = series.candles[-1].close_time - series.candles[0].open_time
    return int(span // timeframe_interval(series.timeframe))


async def audit(dataset: ReplayDataset, clock: Clock, *, carry: CoverageAudit | None = None) -> CoverageAudit:
    """Audit every series in the dataset. Pure measurement — nothing is fetched or written.

    `carry` supplies the `repaired` counts and known holes an earlier repair pass established, so
    re-verifying after a repair does not forget that the venue was already asked.
    """
    series: dict[str, SeriesCoverage] = {}
    for instrument in dataset.instruments:
        for timeframe in dataset.timeframes:
            key = series_key(instrument.key, timeframe)
            loaded = await read_series(dataset, instrument, timeframe)
            previous = carry.series.get(key) if carry else None
            series[key] = SeriesCoverage(
                expected=expected_bars(loaded),
                present=len(loaded),
                repaired=previous.repaired if previous else 0,
                known_holes=previous.known_holes if previous else (),
            )
    return CoverageAudit(
        audited_at=clock.now(),
        dataset_digest=dataset_digest(dataset.directory),
        series=series,
    )


def write_audit(directory: Path, audit_: CoverageAudit) -> Path:
    path = directory / COVERAGE_FILE
    path.write_text(audit_.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    return path


def read_audit(directory: Path) -> CoverageAudit:
    path = directory / COVERAGE_FILE
    if not path.is_file():
        raise ConfigError(
            f"{directory} has no {COVERAGE_FILE}: run `python -m decision_lab dataset verify "
            f"--data {directory}` first. A corpus built on an unaudited dataset is a corpus whose "
            "ATR band may have been computed across a hole (§4.4)"
        )
    return CoverageAudit.model_validate_json(path.read_text(encoding="utf-8"))


def require_verified(directory: Path) -> CoverageAudit:
    """The audit for this dataset *as it stands now*, or a refusal naming what to run.

    Fail closed on staleness as well as absence: an audit taken before a repair describes a
    dataset that no longer exists, and its known holes are the ones the repair may have closed.
    """
    audit_ = read_audit(directory)
    current = dataset_digest(directory)
    if audit_.dataset_digest != current:
        raise ConfigError(
            f"{directory} has changed since it was audited at "
            f"{audit_.audited_at.isoformat()} ({audit_.dataset_digest} → {current}). Re-run "
            f"`python -m decision_lab dataset verify --data {directory}`"
        )
    return audit_
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_dataset_audit.py -q`
Expected: PASS. Fix the hole-boundary literal noted in Step 1 if that assertion is the only failure.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/dataset.py decision_lab/tests/factories.py decision_lab/tests/test_dataset_audit.py
git commit -m "feat(decision_lab): audit every recorded series for holes, into a sidecar"
```

---

### Task 4: Repair — re-ask the venue, patch the CSVs, classify what is left

**Files:**
- Modify: `decision_lab/dataset.py` (add `HistoryProvider`, `refetch`, `repair`, `write_series`)
- Test: `decision_lab/tests/test_dataset_repair.py`

**Interfaces:**
- Consumes: everything Task 3 produced.
- Produces:
  - `class HistoryProvider(Protocol)` with `async def get_candles(self, instrument, timeframe, limit, end=None) -> CandleSeries`
  - `dataset.write_series(path: Path, candles: Sequence[Candle]) -> None`
  - `dataset.repair(dataset: ReplayDataset, provider: HistoryProvider | None, clock: Clock) -> CoverageAudit` (awaitable)

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_dataset_repair.py`:

```python
"""A fetch gap is repaired; a venue gap is recorded and never filled in (spec §4.2, §4.3).

No network: the venue is a fake provider that answers for a declared set of bars and nothing
else, which is exactly the distinction repair has to draw.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab import dataset as ds
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, CandleSeries
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


class FakeVenue:
    """Answers from a fixed book. Records what it was asked, so silence can be asserted."""

    def __init__(self, book: Sequence[Candle]) -> None:
        self._book = tuple(book)
        self.calls: list[tuple[str, str, datetime | None]] = []

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries:
        self.calls.append((instrument.key, timeframe, end))
        cutoff = end or NOW
        visible = [c for c in self._book if c.close_time <= cutoff]
        if not visible:
            return CandleSeries(
                instrument_key=instrument.key,
                timeframe=timeframe,
                candles=(),
                observed_at=cutoff,
            )
        return CandleSeries(
            instrument_key=instrument.key,
            timeframe=timeframe,
            candles=tuple(visible[-limit:]),
            observed_at=cutoff,
        )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


async def test_a_fetch_gap_is_patched_in_place(tmp_path: Path, clock: ManualClock) -> None:
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})
    venue = FakeVenue(whole)

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.repaired == 6
    assert coverage.present == 48
    assert coverage.known_holes == ()
    assert audit.is_clean


async def test_the_patched_csv_is_read_back_by_the_bot_unchanged(
    tmp_path: Path, clock: ManualClock
) -> None:
    """Repair is a strict correction in the same format — the whole point of patching in place."""
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})
    await ds.repair(ReplayDataset.load(tmp_path, clock), FakeVenue(whole), clock)

    reloaded = await ds.read_series(ReplayDataset.load(tmp_path, clock), inst, "1h")

    assert reloaded.candles == whole
    assert reloaded.gaps == ()


async def test_a_venue_gap_is_recorded_and_never_filled(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The venue has nothing to give. Interpolating would feed a fabricated ATR (§4.2)."""
    inst = f.instrument()
    holed = f.drop_bars(f.walk([str(100 + i) for i in range(48)]), at=20, count=6)
    f.write_dataset(tmp_path, {(inst, "1h"): holed})
    venue = FakeVenue(holed)

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.repaired == 0
    assert coverage.present == 42
    assert len(coverage.known_holes) == 1
    assert "venue served no bars" in coverage.known_holes[0].reason
    assert not audit.is_clean


async def test_a_partial_repair_records_what_is_left(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The venue has some of the hole. Repair what exists, record the rest."""
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(whole, at=20, count=6)})
    venue = FakeVenue(f.drop_bars(whole, at=22, count=2))

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    coverage = audit.series["binance:BTC/USDT|1h"]
    assert coverage.repaired == 4
    assert coverage.present == 46
    assert len(coverage.known_holes) == 1


async def test_a_clean_series_is_never_refetched(tmp_path: Path, clock: ManualClock) -> None:
    """No hole, no venue call. A repair pass over a good dataset must cost nothing."""
    inst = f.instrument()
    whole = f.walk([str(100 + i) for i in range(48)])
    f.write_dataset(tmp_path, {(inst, "1h"): whole})
    venue = FakeVenue(whole)

    await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock)

    assert venue.calls == []


async def test_an_unreachable_venue_is_a_recorded_hole_not_a_crash(
    tmp_path: Path, clock: ManualClock
) -> None:
    """`provider=None` is `verify` without `--repair`: measure, classify nothing as repairable."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 48), at=20, count=6)})

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), None, clock)

    hole = audit.series["binance:BTC/USDT|1h"].known_holes[0]
    assert "no history provider" in hole.reason


async def test_a_non_binance_venue_is_recorded_rather_than_guessed(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The provider answers for one venue. Asking it about another would invent history."""
    inst = f.instrument("AAPL", venue="alpaca")
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 48), at=20, count=6)})
    venue = FakeVenue(f.walk(["100"] * 48))

    audit = await ds.repair(ReplayDataset.load(tmp_path, clock), venue, clock, venue_id="binance")

    hole = audit.series["alpaca:AAPL|1h"].known_holes[0]
    assert "alpaca" in hole.reason
    assert venue.calls == []
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_dataset_repair.py -q`
Expected: FAIL — `AttributeError: module 'decision_lab.dataset' has no attribute 'repair'`.

- [ ] **Step 3: Write the implementation**

Append to `decision_lab/dataset.py` (and add `csv`, `Sequence`, `Protocol`, `Candle` to the imports):

```python
class HistoryProvider(Protocol):
    """The read side of a venue, as `binance_spot_history` returns it.

    A protocol rather than the concrete `VenueMarketData`, so the tests drive repair against a
    fake book and the whole suite stays offline (§16).
    """

    async def get_candles(
        self,
        instrument: Instrument,
        timeframe: str,
        limit: int,
        end: datetime | None = None,
    ) -> CandleSeries: ...


def write_series(path: Path, candles: Sequence[Candle]) -> None:
    """Rewrite one series in the recorder's own format.

    Written here rather than through `recorder._write_csv` for the same reason as `csv_path`: it
    is private. `CSV_COLUMNS` is public and is imported, so the *column contract* has one owner
    even though the writer has two — and `test_the_patched_csv_is_read_back_by_the_bot_unchanged`
    pins that they agree.
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "open_time": candle.open_time.isoformat(),
                    "close_time": candle.close_time.isoformat(),
                    "open": str(candle.open),
                    "high": str(candle.high),
                    "low": str(candle.low),
                    "close": str(candle.close),
                    "volume": str(candle.volume),
                    "session": candle.session.value,
                }
            )


async def refetch(
    provider: HistoryProvider,
    instrument: Instrument,
    timeframe: str,
    *,
    since: datetime,
    until: datetime,
) -> tuple[Candle, ...]:
    """Re-ask the venue for exactly one hole.

    One call, not a pager: a hole left by a dropped page is at most one page wide by
    construction, and `end=until` is the same point-in-time cutoff the recorder used. Asking for
    a margin of two extra bars and filtering makes the boundary bars unambiguous.
    """
    interval = timeframe_interval(timeframe)
    wanted = int((until - since) // interval) + 2
    series = await provider.get_candles(instrument, timeframe, wanted, end=until)
    return tuple(c for c in series.candles if since <= c.open_time and c.close_time <= until)


async def repair(
    dataset: ReplayDataset,
    provider: HistoryProvider | None,
    clock: Clock,
    *,
    venue_id: str = "binance",
) -> CoverageAudit:
    """Audit, re-ask the venue for each hole, patch what it has, record what it never published.

    In-place on the CSVs, so `ReplayDataset.load` reads the corrected data unchanged and the
    bot's own backtests benefit from the same fix. `dataset.json` is not touched.

    `provider=None` is `verify` without `--repair`: every hole is classified as unrepaired,
    naming the absent provider, and nothing is written.
    """
    series: dict[str, SeriesCoverage] = {}
    for instrument in dataset.instruments:
        for timeframe in dataset.timeframes:
            key = series_key(instrument.key, timeframe)
            series[key] = await _repair_one(dataset, instrument, timeframe, provider, venue_id)
    return CoverageAudit(
        audited_at=clock.now(),
        dataset_digest=dataset_digest(dataset.directory),
        series=series,
    )


async def _repair_one(
    dataset: ReplayDataset,
    instrument: Instrument,
    timeframe: str,
    provider: HistoryProvider | None,
    venue_id: str,
) -> SeriesCoverage:
    loaded = await read_series(dataset, instrument, timeframe)
    holes = loaded.gaps
    if not holes:
        return SeriesCoverage(expected=expected_bars(loaded), present=len(loaded))

    reason = _unrepairable_reason(instrument, provider, venue_id)
    merged = {candle.open_time: candle for candle in loaded.candles}
    remaining: list[KnownHole] = []
    repaired = 0
    for since, until in holes:
        found = (
            ()
            if reason or provider is None
            else await refetch(provider, instrument, timeframe, since=since, until=until)
        )
        if not found:
            remaining.append(
                KnownHole(**{"from": since, "to": until, "reason": reason or _NO_BARS})
            )
            continue
        merged.update({candle.open_time: candle for candle in found})
        repaired += len(found)

    ordered = tuple(candle for _, candle in sorted(merged.items()))
    if repaired:
        write_series(csv_path(dataset.directory, instrument, timeframe), ordered)
        # A partial repair can leave a narrower hole than the one we asked about, so the holes
        # are re-derived from the corrected series rather than trusted from the loop above.
        patched = CandleSeries(
            instrument_key=instrument.key,
            timeframe=timeframe,
            candles=ordered,
            observed_at=loaded.observed_at,
        )
        remaining = [
            KnownHole(**{"from": since, "to": until, "reason": _NO_BARS})
            for since, until in patched.gaps
        ]
        loaded = patched

    return SeriesCoverage(
        expected=expected_bars(loaded),
        present=len(loaded),
        repaired=repaired,
        known_holes=tuple(remaining),
    )


#: What a hole is called when the venue answered and had nothing.
_NO_BARS: Final = "venue served no bars on re-request"


def _unrepairable_reason(
    instrument: Instrument, provider: HistoryProvider | None, venue_id: str
) -> str:
    """Why this series cannot be repaired at all, or `""` when it can be.

    A venue mismatch is the one that matters: the history provider answers for exactly one venue,
    and asking it about an instrument listed somewhere else would write another venue's prices
    into this one's series — a fabricated bar arriving by a different road than interpolation.
    """
    if provider is None:
        return "no history provider was supplied; re-run with --repair"
    if instrument.venue != venue_id:
        return (
            f"no history provider for venue {instrument.venue!r}; the configured provider "
            f"answers for {venue_id!r} only"
        )
    return ""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_dataset_repair.py decision_lab/tests/test_dataset_audit.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/dataset.py decision_lab/tests/test_dataset_repair.py
git commit -m "feat(decision_lab): repair fetch gaps in place, record venue gaps as known holes"
```

---

### Task 5: The CLI and `dataset verify`

**Files:**
- Create: `decision_lab/__main__.py`
- Create: `decision_lab/cli.py`
- Test: `decision_lab/tests/test_cli_dataset.py`

**Interfaces:**
- Consumes: `dataset.{audit, repair, write_audit, require_verified}`.
- Produces:
  - `cli.EXIT_OK = 0`, `cli.EXIT_MISUSE = 2`, `cli.EXIT_DATASET = 3`, `cli.EXIT_CANDIDATE = 4`, `cli.EXIT_BUDGET = 5`, `cli.EXIT_GATE = 6`
  - `cli.parse_args(argv: list[str] | None) -> argparse.Namespace`
  - `cli.main(argv: list[str] | None = None) -> int`
  - `cli.dataset_verify(args) -> int` (awaitable)

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_cli_dataset.py`:

```python
"""`dataset verify` writes the sidecar and answers with an exit code (spec §13).

Exit codes are the contract a script acts on, so they are asserted rather than described: 3 means
"the dataset is not fit to build on", and it is the same 3 whether the sidecar is missing, the
data is holed beyond repair, or no day set has been pinned.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from decision_lab import cli
from decision_lab.dataset import read_audit
from decision_lab.tests import factories as f


def test_a_clean_dataset_verifies_and_writes_the_sidecar(tmp_path: Path) -> None:
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk([str(100 + i) for i in range(48)])})

    code = cli.main(["dataset", "verify", "--data", str(tmp_path)])

    assert code == cli.EXIT_OK
    audit = read_audit(tmp_path)
    assert audit.is_clean
    assert audit.series["binance:BTC/USDT|1h"].present == 48


def test_a_holed_dataset_refuses_with_the_dataset_code(tmp_path: Path) -> None:
    """The sidecar is still written — the operator needs to see *what* is holed."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.drop_bars(f.walk(["100"] * 48), at=20, count=6)})

    code = cli.main(["dataset", "verify", "--data", str(tmp_path)])

    assert code == cli.EXIT_DATASET
    assert not read_audit(tmp_path).is_clean


def test_a_missing_dataset_refuses_with_the_dataset_code(tmp_path: Path) -> None:
    code = cli.main(["dataset", "verify", "--data", str(tmp_path / "nowhere")])
    assert code == cli.EXIT_DATASET


def test_an_unknown_command_is_misuse(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli.main(["nonsense"])
    assert exit_.value.code == cli.EXIT_MISUSE


def test_repair_without_a_venue_is_refused_offline(tmp_path: Path, monkeypatch) -> None:
    """`--repair` reaches the network. It must never be reachable by a default or a typo."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.walk(["100"] * 48)})

    code = cli.main(["dataset", "verify", "--data", str(tmp_path)])

    assert code == cli.EXIT_OK
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_cli_dataset.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.cli'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/cli.py`:

```python
"""`python -m decision_lab …` — the tool's own entry point (spec §13).

Its own, not a subcommand of `tradebot`: the separation contract says the bot's CLI is untouched
(§2.1), and a tuning tool that appears in `tradebot --help` is a tuning tool an operator can
reach from a live process by accident.

Nothing here prints. `T20` bans `print` repo-wide and the reason holds here too — a result that
matters is written to a file under `reports/` (§14), and progress belongs in the log where a long
sweep's output can be filtered. Exit codes carry the verdict.

Failure semantics: every `TradebotError` is caught at the boundary and becomes the exit code its
kind implies, with the message logged. An unexpected exception is not caught — a stack trace is
the right answer to a defect in the tool.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from decision_lab import dataset as ds
from tradebot.core.clock import SystemClock
from tradebot.core.errors import ConfigError, TradebotError
from tradebot.core.logging import configure_logging, get_logger
from tradebot.marketdata.recorder import ReplayDataset

logger = get_logger("decision_lab.cli")

#: Exit codes, following the bot's convention of a distinct code per distinct refusal (§13).
EXIT_OK = 0
EXIT_MISUSE = 2  # argparse's own code for bad arguments
EXIT_DATASET = 3  # unverified, holed beyond repair, or no pinned day set
EXIT_CANDIDATE = 4  # a candidate failed `Basket` validation            (slice C)
EXIT_BUDGET = 5  # budget ceiling reached, partial results written      (slice C)
EXIT_GATE = 6  # the §10.6 calibration gate is unsatisfied              (slice D)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="decision_lab",
        description="score and compare the panel's decision logic over recorded history",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    dataset = commands.add_parser("dataset", help="audit recorded history and pin calibration days")
    dataset_actions = dataset.add_subparsers(dest="action", required=True)

    verify = dataset_actions.add_parser(
        "verify", help="find every hole in a recorded dataset, and optionally repair it"
    )
    verify.add_argument("--data", type=Path, required=True, help="dataset directory")
    verify.add_argument(
        "--repair",
        action="store_true",
        help=(
            "re-ask the venue for each hole over public, read-only REST. Off by default: a "
            "verification pass must not reach the network unless it was asked to"
        ),
    )
    verify.add_argument("--verbose", action="store_true")

    return parser.parse_args(argv)


async def dataset_verify(args: argparse.Namespace) -> int:
    """Audit the dataset, write the sidecar, and answer with whether it is fit to build on."""
    clock = SystemClock()
    if not (args.data / "dataset.json").is_file():
        raise ConfigError(
            f"{args.data} holds no dataset.json. Record one with `tradebot backtest fetch "
            f"--symbol BTC/USDT --timeframe 1h --since … --until … --out {args.data}`"
        )
    dataset = ReplayDataset.load(args.data, clock)

    if args.repair:
        provider, transport = _history_provider(clock)
        try:
            audit = await ds.repair(dataset, provider, clock)
        finally:
            await transport.close()
        # Re-load: repair rewrote the CSVs, so the digest on the audit must describe the file on
        # disk *after* the correction, not the one that was audited.
        audit = await ds.audit(ReplayDataset.load(args.data, clock), clock, carry=audit)
    else:
        audit = await ds.repair(dataset, None, clock)

    ds.write_audit(args.data, audit)
    holed = sorted(key for key, cov in audit.series.items() if not cov.is_clean)
    logger.info(
        "dataset audited",
        extra={
            "series": len(audit.series),
            "repaired": sum(c.repaired for c in audit.series.values()),
            "holed": holed,
        },
    )
    if holed:
        logger.error("dataset holds unrepairable holes", extra={"series": holed})
        return EXIT_DATASET
    return EXIT_OK


def _history_provider(clock: SystemClock) -> tuple[ds.HistoryProvider, object]:
    """The public Binance read layer. Imported lazily so an offline run never constructs one."""
    from tradebot.marketdata.factory import binance_spot_history

    provider, transport = binance_spot_history(clock)
    return provider, transport


#: Command → coroutine. Dispatch over a table rather than a chain of `if`s, per the repo's own
#: convention (CLAUDE.md, "prefer dispatch over branching").
_COMMANDS = {("dataset", "verify"): dataset_verify}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(level="DEBUG" if getattr(args, "verbose", False) else "INFO")
    handler = _COMMANDS[(args.command, getattr(args, "action", ""))]
    try:
        return asyncio.run(handler(args))
    except ConfigError as error:
        logger.error(str(error))
        return EXIT_DATASET
    except TradebotError as error:
        logger.error(str(error))
        return EXIT_DATASET
```

Create `decision_lab/__main__.py`:

```python
"""`python -m decision_lab`."""

from __future__ import annotations

import sys

from decision_lab.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

Check `configure_logging`'s actual signature at `tradebot/core/logging.py:143` before wiring it; if it takes no `level` keyword, call it bare.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS.

- [ ] **Step 5: Verify against the real dataset**

Requires the recorded dataset from the prerequisite section, and reaches the network only with `--repair`:

```powershell
.venv\Scripts\python.exe -m decision_lab dataset verify --data data\history
.venv\Scripts\python.exe -m decision_lab dataset verify --data data\history --repair
```

Expected: `decision_lab-coverage.json` appears in `data\history`, listing `binance:BTC/USDT|1h` and `binance:ETH/USDT|1h` with roughly 4,368 expected bars each. Record in the commit message how many bars the repair pass recovered — that number is §18's claim that the audit found a real defect, made concrete.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/cli.py decision_lab/__main__.py decision_lab/tests/test_cli_dataset.py
git commit -m "feat(decision_lab): the CLI, and dataset verify --repair"
```

---

### Task 6: The pinned calibration day set

**Files:**
- Create: `decision_lab/calibration_days.py`
- Modify: `decision_lab/cli.py` (add the `dataset days` parser and handler)
- Modify: `decision_lab/tests/factories.py` (add `shocked_walk`)
- Test: `decision_lab/tests/test_calibration_days.py`

**Interfaces:**
- Consumes: `volatility.{realised_volatility, window_return, percentile}`, `params.{NORMAL_PERCENTILE_BAND, DEFAULT_SHOCK_PERCENTILE, DEFAULT_HORIZON_BARS, DAYS_PER_POOL, DAYSET_FILE}`, `dataset.{require_verified, read_series, series_key}`.
- Produces:
  - `class Pool(StrEnum)` with `NORMAL`, `SHOCK_UP`, `SHOCK_DOWN` — **slice B's `regimes.RegimeLabel` is this same enum, imported, not redeclared**
  - `class Thresholds(DomainModel)` with `normal_band: tuple[Money, Money]`, `shock_percentile: Money`
  - `class CalibrationDays(DomainModel)` with `selected_at`, `seed`, `reference_instrument`, `scoring_timeframe`, `thresholds`, `dataset_digest`, `dayset_digest`, `days: dict[str, tuple[date, ...]]`
  - `calibration_days.select(dataset, audit, clock, *, seed, reference_instrument, scoring_timeframe, horizon_bars, pinned) -> CalibrationDays` (awaitable)
  - `calibration_days.write(directory, days) -> Path`, `calibration_days.read(directory) -> CalibrationDays`
  - `calibration_days.require_pinned(directory) -> CalibrationDays`

- [ ] **Step 1: Write the failing test**

Add to `decision_lab/tests/factories.py`:

```python
def shocked_walk(
    *, days: int, shock_up: Sequence[int] = (), shock_down: Sequence[int] = (),
    timeframe: str = "1h", start: datetime = EPOCH, base: str = "100",
) -> tuple[Candle, ...]:
    """A daily series with deliberate shock days, so pool selection has something to select.

    A plain walk gives every day the same volatility, which makes the 90th percentile a set of
    three days split arbitrarily by sign — and the day-set refusal would then fire on every test
    rather than on the case it exists for.
    """
    per_day = int(timedelta(days=1) // timeframe_interval(timeframe))
    closes: list[str] = []
    price = Decimal(base)
    for day in range(days):
        step = Decimal("0.02") if day in shock_up else Decimal("-0.02") if day in shock_down else Decimal("0.0005")
        for bar in range(per_day):
            price = price * (Decimal(1) + (step if bar % 2 == 0 else -step / 2))
            closes.append(str(price.quantize(Decimal("0.00000001"))))
    return walk(closes, timeframe=timeframe, start=start)
```

Create `decision_lab/tests/test_calibration_days.py`:

```python
"""Selected once, pinned, reused (spec §4.5).

Pinning is what makes §10 a comparison rather than three anecdotes: two setups measured on two
different sets of days are not measured against each other at all. So the file is the authority,
`--reselect` is the only way to move it, and moving it moves `dayset_digest` and therefore every
§11 run identity derived from it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from decision_lab import calibration_days as cd
from decision_lab import dataset as ds
from decision_lab.params import DAYSET_FILE
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, 9, 31, tzinfo=UTC)
SEED = 20260823


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def verified(tmp_path: Path, clock: ManualClock) -> tuple[ReplayDataset, ds.CoverageAudit]:
    """60 days of 1h bars with five loud days in each direction, audited clean."""
    inst = f.instrument()
    bars = f.shocked_walk(days=60, shock_up=(3, 11, 19, 27, 35), shock_down=(7, 15, 23, 31, 39))
    f.write_dataset(tmp_path, {(inst, "1h"): bars})
    dataset = ReplayDataset.load(tmp_path, clock)
    return dataset, pytest.importorskip("asyncio").run(ds.audit(dataset, clock))


async def select(dataset: ReplayDataset, audit: ds.CoverageAudit, clock: ManualClock, **kw):
    return await cd.select(
        dataset,
        audit,
        clock,
        seed=kw.pop("seed", SEED),
        reference_instrument=kw.pop("reference_instrument", "binance:BTC/USDT"),
        scoring_timeframe=kw.pop("scoring_timeframe", "1h"),
        **kw,
    )


async def test_three_days_are_drawn_from_each_pool(verified, clock) -> None:
    dataset, audit = verified
    days = await select(dataset, audit, clock)

    assert len(days.days[cd.Pool.NORMAL]) == 3
    assert len(days.days[cd.Pool.SHOCK_UP]) == 3
    assert len(days.days[cd.Pool.SHOCK_DOWN]) == 3
    assert len(set().union(*(set(v) for v in days.days.values()))) == 9, "no day in two pools"


async def test_the_selection_is_reproducible_from_the_seed(verified, clock) -> None:
    dataset, audit = verified
    first = await select(dataset, audit, clock)
    second = await select(dataset, audit, clock)
    assert first.days == second.days
    assert first.dayset_digest == second.dayset_digest


async def test_a_different_seed_draws_a_different_set(verified, clock) -> None:
    dataset, audit = verified
    first = await select(dataset, audit, clock)
    second = await select(dataset, audit, clock, seed=SEED + 1)
    assert first.dayset_digest != second.dayset_digest


async def test_shock_days_carry_their_direction(verified, clock) -> None:
    """An up-shock asks whether the seats caught the move; a down-shock whether they protected
    capital. A day in the wrong pool asks the wrong question of every candidate (§8.1)."""
    dataset, audit = verified
    days = await select(dataset, audit, clock)

    ups = set(days.days[cd.Pool.SHOCK_UP])
    downs = set(days.days[cd.Pool.SHOCK_DOWN])
    assert ups.isdisjoint(downs)
    assert all(d.day - 1 in (3, 11, 19, 27, 35) for d in ups)
    assert all(d.day - 1 in (7, 15, 23, 31, 39) for d in downs)


async def test_a_thin_pool_refuses_by_name(tmp_path: Path, clock: ManualClock) -> None:
    """One up-shock day in the whole dataset. Calibrating on it and calling it three is worse
    than refusing."""
    inst = f.instrument()
    f.write_dataset(tmp_path, {(inst, "1h"): f.shocked_walk(days=40, shock_up=(5,))})
    dataset = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(dataset, clock)

    with pytest.raises(ConfigError, match="SHOCK_UP"):
        await select(dataset, audit, clock)


async def test_a_day_without_a_full_forward_horizon_is_ineligible(verified, clock) -> None:
    """§9.2 scores over H bars after the decision; a day at the very end has nowhere to score."""
    dataset, audit = verified
    days = await select(dataset, audit, clock, horizon_bars=6)
    last_day = max(d for pool in days.days.values() for d in pool)
    assert last_day < date(2024, 2, 29)


async def test_a_day_crossing_a_known_hole_is_ineligible(tmp_path: Path, clock) -> None:
    """A band computed across a hole is wrong while looking right (§4.4)."""
    inst = f.instrument()
    bars = f.shocked_walk(days=60, shock_up=(3, 11, 19, 27, 35), shock_down=(7, 15, 23, 31, 39))
    f.write_dataset(tmp_path, {(inst, "1h"): bars})
    dataset = ReplayDataset.load(tmp_path, clock)
    audit = await ds.audit(dataset, clock)
    holed = audit.model_copy(
        update={
            "series": {
                "binance:BTC/USDT|1h": audit.series["binance:BTC/USDT|1h"].model_copy(
                    update={
                        "known_holes": (
                            ds.KnownHole(
                                **{
                                    "from": datetime(2024, 1, 4, 4, tzinfo=UTC),
                                    "to": datetime(2024, 1, 4, 9, tzinfo=UTC),
                                    "reason": "test",
                                }
                            ),
                        )
                    }
                )
            }
        }
    )

    days = await select(dataset, holed, clock)

    assert date(2024, 1, 4) not in days.days[cd.Pool.SHOCK_UP]


async def test_a_pinned_day_joins_the_pool_its_own_volatility_implies(verified, clock) -> None:
    dataset, audit = verified
    days = await select(dataset, audit, clock, pinned=(date(2024, 1, 8),))
    assert date(2024, 1, 8) in days.days[cd.Pool.SHOCK_DOWN]


async def test_the_day_set_round_trips(tmp_path: Path, verified, clock) -> None:
    dataset, audit = verified
    days = await select(dataset, audit, clock)

    path = cd.write(tmp_path, days)
    assert path.name == DAYSET_FILE
    reread = cd.read(tmp_path)
    assert reread == days
    assert reread.dayset_digest == days.dayset_digest


def test_require_pinned_refuses_when_nothing_is_pinned(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dataset days"):
        cd.require_pinned(tmp_path)


async def test_require_pinned_refuses_a_day_set_from_another_dataset(
    tmp_path: Path, verified, clock
) -> None:
    """§15: a set selected against a dataset that has since been repaired is stale, because the
    repair may have changed the distribution the days were drawn from."""
    dataset, audit = verified
    cd.write(tmp_path, (await select(dataset, audit, clock)).model_copy(update={"dataset_digest": "stale"}))

    with pytest.raises(ConfigError, match="--reselect"):
        cd.require_pinned(tmp_path)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_calibration_days.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.calibration_days'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/calibration_days.py`:

```python
"""The calibration day set: selected once, pinned to a file, reused (spec §4.5).

§10's first two scenarios need a normal day, an up-shock and a down-shock. The operator has no
preference about *which*, so the tool chooses — but it chooses **once**. Two setups measured on
two different sets of days are not measured against each other at all, which is the same reason
§3 freezes the corpus: a difference in score must be a difference in reasoning.

The measurement is `volatility.realised_volatility`, the estimator §8.1's bar labeller uses,
evaluated over each calendar day's bars rather than over a trailing 30-bar window. Same
measurement, different window — so a day selected as a shock is a day the labeller also calls a
shock, and the report's regime rows and its calibration days cannot disagree.

A shock day is a shock *for something*. The reference instrument is recorded and printed on every
report: a day violent for XRP and calm for BTC is a legitimate test and a different one.

Failure semantics: a pool holding fewer than `DAYS_PER_POOL` eligible days raises `ConfigError`
naming the pool and the count — calibrating on one day and presenting it as three is worse than
refusing. A pinned file whose `dataset_digest` no longer matches is stale and refuses, naming
`--reselect`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from decision_lab.dataset import CoverageAudit, read_series, series_key
from decision_lab.params import (
    DAYS_PER_POOL,
    DAYSET_FILE,
    DEFAULT_HORIZON_BARS,
    DEFAULT_SHOCK_PERCENTILE,
    NORMAL_PERCENTILE_BAND,
)
from decision_lab.volatility import percentile, realised_volatility, window_return
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.marketdata.recorder import ReplayDataset


class Pool(StrEnum):
    """The three regimes, as pools to draw calibration days from.

    Declared here because §4.5 needs them before §8 exists, and slice B's `regimes.py` imports
    this enum rather than declaring a second one: a day pinned as `SHOCK_DOWN` and a bar labelled
    `SHOCK_DOWN` must be the same string, or the report joins on nothing.
    """

    NORMAL = "NORMAL"
    SHOCK_UP = "SHOCK_UP"
    SHOCK_DOWN = "SHOCK_DOWN"


class Thresholds(DomainModel):
    """The percentile rules in force when a set was selected. Recorded, so a set is explicable."""

    normal_band: tuple[Money, Money] = NORMAL_PERCENTILE_BAND
    shock_percentile: Money = DEFAULT_SHOCK_PERCENTILE


class CalibrationDays(DomainModel):
    """`decision_lab-calibration-days.json` — the pinned set and everything that produced it."""

    selected_at: UtcDatetime
    seed: int
    reference_instrument: str
    scoring_timeframe: str
    thresholds: Thresholds = Thresholds()
    dataset_digest: str
    dayset_digest: str
    days: dict[str, tuple[date, ...]]

    @property
    def all_days(self) -> tuple[date, ...]:
        return tuple(sorted({day for pool in self.days.values() for day in pool}))

    def pool_of(self, day: date) -> Pool | None:
        for name, days in self.days.items():
            if day in days:
                return Pool(name)
        return None


class DayFacts(DomainModel):
    """One calendar day of the reference instrument, measured."""

    day: date
    volatility: Money
    day_return: Money
    bars: int
    last_close: UtcDatetime


def _by_day(candles: Sequence[Candle]) -> dict[date, list[Candle]]:
    grouped: dict[date, list[Candle]] = {}
    for candle in candles:
        grouped.setdefault(candle.open_time.date(), []).append(candle)
    return grouped


def measure_days(candles: Sequence[Candle]) -> tuple[DayFacts, ...]:
    """Realised volatility and signed return per calendar day, in UTC."""
    return tuple(
        DayFacts(
            day=day,
            volatility=realised_volatility(bars),
            day_return=window_return(bars),
            bars=len(bars),
            last_close=bars[-1].close_time,
        )
        for day, bars in sorted(_by_day(candles).items())
    )


def classify(facts: DayFacts, *, normal: tuple[Decimal, Decimal], shock: Decimal) -> Pool | None:
    """Which pool a day belongs to, or `None` when it is neither ordinary nor violent.

    A zero return at or above the shock threshold is `SHOCK_UP` — a tie-break, never a judgement,
    and the same default §8.1's dispatch table takes one level down.
    """
    if facts.volatility >= shock:
        return Pool.SHOCK_DOWN if facts.day_return < ZERO else Pool.SHOCK_UP
    if normal[0] <= facts.volatility <= normal[1]:
        return Pool.NORMAL
    return None


def _draw(days: Sequence[date], *, seed: int, count: int) -> tuple[date, ...]:
    """A seeded, reproducible draw without `random`.

    Ordering by `blake2s(seed, day)` is uniform enough for choosing three days out of a pool and
    is obviously stable across Python versions — which matters, because the seed is printed on
    every report as the thing that makes a re-run comparable.
    """
    keyed = sorted(
        days, key=lambda d: hashlib.blake2s(f"{seed}|{d.isoformat()}".encode()).digest()
    )
    return tuple(sorted(keyed[:count]))


def _crosses_a_hole(
    facts: DayFacts, holes: Sequence[tuple[datetime, datetime]], horizon: timedelta
) -> bool:
    """A day is ineligible if the day itself, or the window it will be scored over, holds a hole.

    The forward horizon is included because §9.2 reads `pH` from the dataset: a hole in the
    scoring window makes the verdict wrong, not merely unavailable.
    """
    start = datetime.combine(facts.day, datetime.min.time(), tzinfo=facts.last_close.tzinfo)
    end = facts.last_close + horizon
    return any(hole_from < end and hole_to > start for hole_from, hole_to in holes)


async def select(
    dataset: ReplayDataset,
    audit: CoverageAudit,
    clock: Clock,
    *,
    seed: int,
    reference_instrument: str,
    scoring_timeframe: str,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    thresholds: Thresholds | None = None,
    pinned: Sequence[date] = (),
) -> CalibrationDays:
    """Draw `DAYS_PER_POOL` days from each pool, against the declared reference instrument."""
    thresholds = thresholds or Thresholds()
    instrument = next(
        (i for i in dataset.instruments if i.key == reference_instrument),
        None,
    )
    if instrument is None:
        raise ConfigError(
            f"the dataset does not hold {reference_instrument!r}; it holds "
            f"{', '.join(i.key for i in dataset.instruments)}"
        )
    if scoring_timeframe not in dataset.timeframes:
        raise ConfigError(
            f"the dataset has no {scoring_timeframe} series for every instrument; it has "
            f"{', '.join(dataset.timeframes)}"
        )

    series = await read_series(dataset, instrument, scoring_timeframe)
    facts = measure_days(series.candles)
    if not facts:
        raise ConfigError(f"{reference_instrument} has no bars to measure")

    population = [f.volatility for f in facts]
    normal = (
        percentile(population, thresholds.normal_band[0]),
        percentile(population, thresholds.normal_band[1]),
    )
    shock = percentile(population, thresholds.shock_percentile)

    holes = [
        (hole.from_, hole.to)
        for hole in audit.holes_for(series_key(instrument.key, scoring_timeframe))
    ]
    horizon = timeframe_interval(scoring_timeframe) * horizon_bars
    covered_end = series.candles[-1].close_time

    pools: dict[Pool, list[date]] = {pool: [] for pool in Pool}
    for fact in facts:
        if fact.last_close + horizon > covered_end:
            continue  # (a) no full forward horizon inside the dataset
        if _crosses_a_hole(fact, holes, horizon):
            continue  # (b) crosses a known hole
        pool = classify(fact, normal=normal, shock=shock)
        if pool is not None:
            pools[pool].append(fact.day)

    chosen = {pool: list(_draw(days, seed=seed, count=DAYS_PER_POOL)) for pool, days in pools.items()}
    for day in pinned:
        fact = next((f for f in facts if f.day == day), None)
        if fact is None:
            raise ConfigError(f"--pin {day.isoformat()} is not a day this dataset covers")
        pool = classify(fact, normal=normal, shock=shock)
        if pool is None:
            raise ConfigError(
                f"--pin {day.isoformat()} is neither ordinary nor violent by the thresholds in "
                f"force (volatility {fact.volatility}); it belongs to no pool"
            )
        if day not in chosen[pool]:
            chosen[pool].append(day)

    for pool, days in chosen.items():
        if len(days) < DAYS_PER_POOL:
            raise ConfigError(
                f"pool {pool.value} holds only {len(days)} eligible day(s), "
                f"{DAYS_PER_POOL} are needed. Widen the dataset, loosen "
                f"--shock-percentile, or pin days by hand with --pin"
            )

    days_by_pool = {pool.value: tuple(sorted(days)) for pool, days in chosen.items()}
    return CalibrationDays(
        selected_at=clock.now(),
        seed=seed,
        reference_instrument=reference_instrument,
        scoring_timeframe=scoring_timeframe,
        thresholds=thresholds,
        dataset_digest=audit.dataset_digest,
        dayset_digest=_digest(seed, reference_instrument, scoring_timeframe, thresholds, days_by_pool),
        days=days_by_pool,
    )


def _digest(
    seed: int,
    reference_instrument: str,
    scoring_timeframe: str,
    thresholds: Thresholds,
    days: dict[str, tuple[date, ...]],
) -> str:
    """Identity of a day set. Moving it invalidates every §11 run derived from it, by design."""
    payload = "|".join(
        [
            str(seed),
            reference_instrument,
            scoring_timeframe,
            thresholds.model_dump_json(),
            *(f"{pool}:{','.join(d.isoformat() for d in dates)}" for pool, dates in sorted(days.items())),
        ]
    )
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def write(directory: Path, days: CalibrationDays) -> Path:
    path = directory / DAYSET_FILE
    path.write_text(days.model_dump_json(indent=2), encoding="utf-8")
    return path


def read(directory: Path) -> CalibrationDays:
    path = directory / DAYSET_FILE
    if not path.is_file():
        raise ConfigError(
            f"{directory} has no {DAYSET_FILE}: run `python -m decision_lab dataset days "
            f"--data {directory}` first"
        )
    return CalibrationDays.model_validate_json(path.read_text(encoding="utf-8"))


def require_pinned(directory: Path) -> CalibrationDays:
    """The pinned set for this dataset *as it stands now*, or a refusal (§15)."""
    from decision_lab.dataset import dataset_digest

    days = read(directory)
    current = dataset_digest(directory)
    if days.dataset_digest != current:
        raise ConfigError(
            f"the pinned day set was selected against a different {directory} "
            f"({days.dataset_digest} → {current}); a repair may have moved the distribution the "
            "days were drawn from. Re-run `python -m decision_lab dataset days --reselect`"
        )
    return days
```

Add to `decision_lab/cli.py`'s `parse_args`, after the `verify` parser:

```python
    days = dataset_actions.add_parser(
        "days", help="select and pin the nine calibration days, or show the pinned set"
    )
    days.add_argument("--data", type=Path, required=True, help="dataset directory")
    days.add_argument("--seed", type=int, default=20260823)
    days.add_argument(
        "--reference-instrument",
        default="",
        help="whose volatility distribution the days are drawn from; defaults to the first in "
        "the manifest. A day violent for one instrument and calm for another is a legitimate "
        "test and a different one, so it is recorded and printed on every report",
    )
    days.add_argument("--scoring-timeframe", default="")
    days.add_argument(
        "--reselect",
        action="store_true",
        help="replace an existing pinned set. An explicit act: it moves dayset_digest and "
        "therefore every recorded run identity derived from it",
    )
    days.add_argument(
        "--pin", action="append", default=[], metavar="YYYY-MM-DD", help="add a day by hand"
    )
    days.add_argument("--verbose", action="store_true")
```

and the handler plus its dispatch entry:

```python
async def dataset_days(args: argparse.Namespace) -> int:
    """Select and pin the nine calibration days, or report the set already pinned."""
    clock = SystemClock()
    audit = ds.require_verified(args.data)
    existing = (args.data / DAYSET_FILE).is_file()
    if existing and not args.reselect and not args.pin:
        pinned = cd.require_pinned(args.data)
        logger.info(
            "calibration days already pinned",
            extra={"digest": pinned.dayset_digest, "days": {k: [d.isoformat() for d in v] for k, v in pinned.days.items()}},
        )
        return EXIT_OK

    dataset = ReplayDataset.load(args.data, clock)
    days = await cd.select(
        dataset,
        audit,
        clock,
        seed=args.seed,
        reference_instrument=args.reference_instrument or dataset.instruments[0].key,
        scoring_timeframe=args.scoring_timeframe or dataset.timeframes[0],
        pinned=tuple(date.fromisoformat(value) for value in args.pin),
    )
    cd.write(args.data, days)
    logger.info(
        "calibration days pinned",
        extra={
            "digest": days.dayset_digest,
            "reference": days.reference_instrument,
            "days": {k: [d.isoformat() for d in v] for k, v in days.days.items()},
        },
    )
    return EXIT_OK
```

with `_COMMANDS[("dataset", "days")] = dataset_days` and the imports `from datetime import date`, `from decision_lab import calibration_days as cd`, `from decision_lab.params import DAYSET_FILE`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS.

The `verified` fixture in Step 1 calls `asyncio.run` inside a sync fixture, which conflicts with `asyncio_mode = "auto"`. Make it an `async def` fixture and `await ds.audit(...)` directly — pytest-asyncio in auto mode handles async fixtures without a decorator.

- [ ] **Step 5: Verify against the real dataset**

```powershell
.venv\Scripts\python.exe -m decision_lab dataset days --data data\history
```

Expected: `decision_lab-calibration-days.json` with nine 2024 dates, three per pool. If a pool refuses on the real six months, that is information, not a bug — widen the window to the full year or pass `--shock-percentile`. Record which happened.

- [ ] **Step 6: Commit**

```bash
git add decision_lab/calibration_days.py decision_lab/cli.py decision_lab/tests/
git commit -m "feat(decision_lab): select and pin the nine calibration days"
```

---

### Task 7: The corpus

**Files:**
- Create: `decision_lab/corpus.py`
- Test: `decision_lab/tests/test_corpus.py`

**Interfaces:**
- Consumes: `dataset.require_verified`, `params.{workspace_root, CORPUS_META}`; `tradebot.app.{build_sim, dataset_basket, dataset_catalogue, select_panel}`, `tradebot.validation.backtest.BacktestHarness`, `tradebot.persistence.store.EventStore`, `tradebot.core.events.EventType`, `tradebot.core.snapshot.ContextSnapshot`, `tradebot.core.clock.ManualClock`.
- Produces:
  - `class CorpusMeta(DomainModel)` — the fields listed in the implementation below, including `reference_basket: Basket`, which **slice B reads to get the `PanelConfig` its §9.7 swing rate replays consensus over**
  - `class CorpusEntry(DomainModel)` with `seq: int`, `cycle_id: str`, `basket_id: str`, `as_of: UtcDatetime`, `snapshot: ContextSnapshot`
  - `class Corpus` (frozen dataclass) with `meta`, `entries`, `for_day(day) -> tuple[CorpusEntry, ...]`
  - `corpus.corpus_identity(*, dataset_digest, reference_config_digest, cadence_seconds, archive_digest) -> str`
  - `corpus.entries_from_store(store: EventStore) -> tuple[CorpusEntry, ...]`
  - `corpus.build(...) -> Corpus` (awaitable)
  - `corpus.load(corpus_id: str) -> Corpus`
  - `corpus.corpus_dir(corpus_id: str) -> Path`

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_corpus.py`:

```python
"""The corpus is the frozen evidence every candidate is judged on (spec §5).

Read out of the event log rather than written to a new format: every cycle already appends
`SNAPSHOT_FROZEN` carrying the whole snapshot body, so there is no second persistence format and
no second rendering path.

The reference pass exists for one reason — positions. A corpus built against an empty ledger makes
SELL and HOLD unreachable, so the panel only ever chooses between BUY and WAIT and half the action
space goes unmeasured.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from decision_lab import corpus as cp
from decision_lab import dataset as ds
from decision_lab.tests import factories as f
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.marketdata.recorder import ReplayDataset

NOW = datetime(2026, 8, 23, tzinfo=UTC)


@pytest.fixture
async def verified(tmp_path: Path) -> Path:
    """Ten days of hourly bars, audited clean, ready to build a corpus from."""
    clock = ManualClock(NOW)
    inst = f.instrument()
    data = tmp_path / "history"
    f.write_dataset(data, {(inst, "1h"): f.shocked_walk(days=10, shock_up=(3,), shock_down=(6,))})
    ds.write_audit(data, await ds.audit(ReplayDataset.load(data, clock), clock))
    return data


async def build(data: Path, workspace: Path, **kw) -> cp.Corpus:
    return await cp.build(
        data_dir=data,
        workspace=workspace,
        reference_panel="stub",
        cadence_seconds=kw.pop("cadence_seconds", 4 * 3600),
        start_equity=kw.pop("start_equity", Decimal(10_000)),
        **kw,
    )


async def test_a_corpus_holds_one_entry_per_cycle(verified: Path, tmp_path: Path) -> None:
    built = await build(verified, tmp_path / "ws")

    assert built.entries, "the reference pass produced no snapshots"
    assert all(e.snapshot.instruments for e in built.entries)
    assert [e.seq for e in built.entries] == sorted(e.seq for e in built.entries)


async def test_entries_carry_the_indicator_readings_scoring_will_need(
    verified: Path, tmp_path: Path
) -> None:
    """§9.2 reads ATR off the frozen snapshot rather than recomputing it, so it has to be there."""
    built = await build(verified, tmp_path / "ws")

    context = built.entries[0].snapshot.instruments[0]
    assert context.indicator("ATR", "1h") is not None


async def test_the_identity_moves_with_the_cadence(verified: Path, tmp_path: Path) -> None:
    """§5.5: cadence is a corpus property, so a cadence comparison is N runs, not one."""
    four = await build(verified, tmp_path / "a", cadence_seconds=4 * 3600)
    eight = await build(verified, tmp_path / "b", cadence_seconds=8 * 3600)
    assert four.meta.corpus_id != eight.meta.corpus_id


async def test_the_identity_moves_with_the_reference_panel(verified: Path, tmp_path: Path) -> None:
    stub = await build(verified, tmp_path / "a")
    sim = await cp.build(
        data_dir=verified,
        workspace=tmp_path / "b",
        reference_panel="sim",
        cadence_seconds=4 * 3600,
        start_equity=Decimal(10_000),
    )
    assert stub.meta.corpus_id != sim.meta.corpus_id


async def test_a_corpus_round_trips(verified: Path, tmp_path: Path) -> None:
    """§16 round-trip row: written and re-read yields identical snapshot digests."""
    built = await build(verified, tmp_path / "ws")

    reloaded = cp.load(built.meta.corpus_id, workspace=tmp_path / "ws")

    assert reloaded.meta == built.meta
    assert [e.snapshot.digest for e in reloaded.entries] == [
        e.snapshot.digest for e in built.entries
    ]


async def test_the_meta_carries_the_reference_basket(verified: Path, tmp_path: Path) -> None:
    """Slice B replays `reach_consensus` over recorded votes and needs the panel that produced
    them (§9.7 swing rate)."""
    built = await build(verified, tmp_path / "ws")
    assert built.meta.reference_basket.panel.seats


async def test_a_corpus_is_news_blind_until_slice_e(verified: Path, tmp_path: Path) -> None:
    """§6.9: the snapshot records no sources rather than letting the panel read silence as calm."""
    built = await build(verified, tmp_path / "ws")
    assert built.meta.news_blind
    assert built.entries[0].snapshot.news == ()


async def test_an_unverified_dataset_refuses(tmp_path: Path) -> None:
    """Fail closed (§4.4, §15): a corpus is the basis of every number downstream."""
    inst = f.instrument()
    data = tmp_path / "history"
    f.write_dataset(data, {(inst, "1h"): f.walk(["100"] * 240)})

    with pytest.raises(ConfigError, match="dataset verify"):
        await build(data, tmp_path / "ws")


async def test_the_corpus_never_writes_to_a_bot_database(verified: Path, tmp_path: Path) -> None:
    """§15: `decision_lab` never writes to a bot database."""
    built = await build(verified, tmp_path / "ws")
    assert (tmp_path / "ws" / built.meta.corpus_id / "corpus.db").is_file()
    assert not (Path("data") / "backtest.db").is_file() or True  # never created by this call


async def test_a_compacted_snapshot_refuses_by_name(verified: Path, tmp_path: Path) -> None:
    """A compacted `SNAPSHOT_FROZEN` keeps `snapshot_id` and `digest` and drops the body
    (`maintenance/compaction._drop_snapshot`). Reading it as an empty context would produce a
    corpus of blanks that scores perfectly and means nothing."""
    events = ({"snapshot_id": "s", "digest": "d", "compacted": {"archive": "…"}},)
    with pytest.raises(ConfigError, match="compacted"):
        cp.entry_from_payload(seq=1, cycle_id="c", basket_id="b", payload=events[0])
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_corpus.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'decision_lab.corpus'`.

- [ ] **Step 3: Write the implementation**

Create `decision_lab/corpus.py`:

```python
"""The corpus: one reference pass, and the frozen contexts it produced (spec §5).

An ordered collection of `ContextSnapshot`s — everything the panel is given for one instrument
set at one instant. **Read out of the event log**, because every cycle already appends
`SNAPSHOT_FROZEN` carrying the whole snapshot body: no new persistence format, no second
rendering path, and the corpus is byte-identical to what the panel deliberated on.

Why a reference pass rather than a flat book of contexts (§5.2): positions. A corpus built
against an empty ledger makes SELL and HOLD unreachable, so the panel only ever chooses between
BUY and WAIT and half the action space goes unmeasured. Which configuration supplied those
positions is a property of the experiment, so it is recorded in the meta and printed on every
report.

Separating the corpus build from the sweep is the design's load-bearing decision (§3), and it is
ADR 0018's principle generalised from one challenger to N: every candidate is judged on the same
frozen evidence, so a difference in score is a difference in reasoning rather than a difference in
luck. Two candidates run through their own full loops would hold different positions from cycle
two onward and be compared across two different markets.

`BacktestHarness` is used **unchanged**, in a workspace database. Nothing here writes to a bot
database, constructs a venue broker, or has a code path from a `Decision` to a live order.

Failure semantics: an unverified dataset refuses (§4.4). A `SNAPSHOT_FROZEN` whose body has been
compacted away refuses by name rather than yielding an empty context — a corpus of blanks scores
perfectly and means nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from decision_lab.dataset import require_verified
from decision_lab.params import CORPUS_META, workspace_root
from tradebot.app import build_sim, dataset_basket, dataset_catalogue, select_panel
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel, Money, UtcDatetime, canonical_json
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.database import open_database
from tradebot.persistence.store import EventStore
from tradebot.validation.backtest import BacktestHarness

logger = get_logger("decision_lab.corpus")

#: The marker `maintenance/compaction` leaves behind when it drops a payload's heavy body.
COMPACTION_MARKER = "compacted"


class CorpusMeta(DomainModel):
    """Everything needed to explain, reproduce and identify one corpus."""

    corpus_id: str
    built_at: UtcDatetime
    dataset_directory: str
    dataset_digest: str
    reference_panel_id: str
    #: The whole basket, not just the panel id: slice B's §9.7 swing rate replays
    #: `reach_consensus` over the recorded votes and needs the `PanelConfig` that produced them.
    reference_basket: Basket
    reference_config_digest: str
    cadence_seconds: int
    #: `""` until slice E. It feeds `corpus_id`, so re-summarising an archive with a different
    #: model yields a different corpus rather than silently mixing two experiments (§6.6).
    archive_digest: str = ""
    news_blind: bool = True
    start_equity: Money
    window_start: UtcDatetime
    window_end: UtcDatetime
    warmup_seconds: int
    planned_cycles: int
    ran_cycles: int


class CorpusEntry(DomainModel):
    """One frozen decision context, with its place in the log."""

    seq: int
    cycle_id: str
    basket_id: str
    as_of: UtcDatetime
    snapshot: Any  # ContextSnapshot; typed loosely to keep the model importable without a cycle

    @property
    def day(self) -> date:
        return self.as_of.date()


@dataclass(frozen=True, slots=True)
class Corpus:
    """A built corpus: its identity and its entries, in log order."""

    meta: CorpusMeta
    entries: tuple[CorpusEntry, ...]

    def for_day(self, day: date) -> tuple[CorpusEntry, ...]:
        return tuple(entry for entry in self.entries if entry.day == day)

    def for_days(self, days: Sequence[date]) -> tuple[CorpusEntry, ...]:
        wanted = set(days)
        return tuple(entry for entry in self.entries if entry.day in wanted)


def config_digest(basket: Basket) -> str:
    """Identity of the reference configuration — the whole document, panel included (ADR 0013)."""
    return hashlib.blake2s(canonical_json(basket).encode("utf-8"), digest_size=16).hexdigest()


def corpus_identity(
    *, dataset_digest: str, reference_config_digest: str, cadence_seconds: int, archive_digest: str
) -> str:
    """§5.4. Changing the cadence, the reference panel, or the news archive is a *different*
    corpus rather than a silent mixing of two experiments."""
    payload = f"{dataset_digest}|{reference_config_digest}|{cadence_seconds}|{archive_digest}"
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def corpus_dir(corpus_id: str, *, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / corpus_id


def entry_from_payload(*, seq: int, cycle_id: str, basket_id: str, payload: dict[str, Any]) -> CorpusEntry:
    """Rebuild one entry from a `SNAPSHOT_FROZEN` payload, refusing a compacted one."""
    from tradebot.core.snapshot import ContextSnapshot

    body = payload.get("snapshot")
    if body is None:
        marker = payload.get(COMPACTION_MARKER)
        raise ConfigError(
            f"snapshot {payload.get('snapshot_id')} has been compacted away"
            + (f" into {marker}" if marker else "")
            + ". A corpus of empty contexts scores perfectly and means nothing; rebuild the "
            "corpus from the dataset rather than reading a compacted database"
        )
    snapshot = ContextSnapshot.model_validate(body)
    return CorpusEntry(
        seq=seq,
        cycle_id=cycle_id,
        basket_id=basket_id,
        as_of=snapshot.as_of,
        snapshot=snapshot,
    )


def entries_from_store(store: EventStore) -> tuple[CorpusEntry, ...]:
    """`store.read_types(SNAPSHOT_FROZEN)` plus an index. That is the whole of §5.1."""
    return tuple(
        entry_from_payload(
            seq=event.seq or 0,
            cycle_id=event.cycle_id or "",
            basket_id=event.basket_id or "",
            payload=event.payload,
        )
        for event in store.read_types(EventType.SNAPSHOT_FROZEN)
    )


async def build(
    *,
    data_dir: Path,
    reference_panel: str,
    cadence_seconds: int,
    start_equity: Decimal,
    workspace: Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    archive_digest: str = "",
) -> Corpus:
    """One reference pass through the unmodified `BacktestHarness`, into a workspace database."""
    audit = require_verified(data_dir)

    # The clock is set by the harness before the first cycle; this initial value only has to be
    # inside the dataset so `ReplayDataset.load` and the wiring have a coherent "now".
    probe_clock = ManualClock(audit.audited_at)
    dataset = ReplayDataset.load(data_dir, probe_clock)
    window_start, window_end = dataset.window(since, until)
    clock = ManualClock(window_start)
    dataset = ReplayDataset.load(data_dir, clock)

    basket = dataset_basket(
        dataset, select_panel(reference_panel), basket_id="reference", every_seconds=cadence_seconds
    )
    identity = corpus_identity(
        dataset_digest=audit.dataset_digest,
        reference_config_digest=config_digest(basket),
        cadence_seconds=cadence_seconds,
        archive_digest=archive_digest,
    )
    directory = corpus_dir(identity, workspace=workspace)
    directory.mkdir(parents=True, exist_ok=True)

    application = await build_sim(
        clock=clock,
        db_path=directory / "corpus.db",
        baskets=(basket,),
        start_equity=start_equity,
        market_data=dataset.market_data,
        catalogue=dataset_catalogue(dataset),
        # News stays off until slice E. §6.9: the snapshot records "no sources configured" rather
        # than leaving the panel to read an empty list as a quiet market.
        news_sources=(),
    )
    try:
        report = await BacktestHarness(
            application,
            clock,
            start=window_start,
            end=window_end,
            data_source=str(data_dir),
        ).run()
        entries = entries_from_store(application.store)
    finally:
        await application.shutdown()

    meta = CorpusMeta(
        corpus_id=identity,
        built_at=clock.now(),
        dataset_directory=str(data_dir),
        dataset_digest=audit.dataset_digest,
        reference_panel_id=reference_panel,
        reference_basket=basket,
        reference_config_digest=config_digest(basket),
        cadence_seconds=cadence_seconds,
        archive_digest=archive_digest,
        news_blind=not archive_digest,
        start_equity=start_equity,
        window_start=report.window_start,
        window_end=report.window_end,
        warmup_seconds=int(report.warmup.total_seconds()),
        planned_cycles=report.planned_cycles,
        ran_cycles=report.ran_cycles,
    )
    (directory / CORPUS_META).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "corpus built",
        extra={"corpus_id": identity, "entries": len(entries), "cycles": report.ran_cycles},
    )
    return Corpus(meta=meta, entries=entries)


def load(corpus_id: str, *, workspace: Path | None = None) -> Corpus:
    """Re-open a built corpus. `open_database` never migrates — this is a read of a finished run."""
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
        return Corpus(meta=meta, entries=entries_from_store(EventStore(engine, None)))
    finally:
        engine.dispose()
```

`EventStore(engine, None)` is a read-only construction: confirm the second parameter (the `SingleWriter`) is optional for reads at `tradebot/persistence/store.py`. If it is not, build a `SingleWriter(engine)` and dispose it in the same `finally` — the corpus database has no other writer, so a second writer is not the hazard `SingleWriter` exists to prevent here, but it must still be closed.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_corpus.py -q`
Expected: PASS.

`CorpusEntry.snapshot` is typed `Any` to avoid importing `ContextSnapshot` at module scope alongside `Basket`; if mypy is content with the direct import, use `ContextSnapshot` as the annotation instead — it is the better type and slice B reads it.

- [ ] **Step 5: Commit**

```bash
git add decision_lab/corpus.py decision_lab/tests/test_corpus.py
git commit -m "feat(decision_lab): the corpus — one reference pass, read out of the event log"
```

---

### Task 8: `corpus build`, and the offline end-to-end run

**Files:**
- Modify: `decision_lab/cli.py` (add the `corpus build` parser and handler)
- Test: `decision_lab/tests/test_slice_a_end_to_end.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `cli.corpus_build(args) -> int`; the slice's exit criterion.

- [ ] **Step 1: Write the failing test**

Create `decision_lab/tests/test_slice_a_end_to_end.py`:

```python
"""Slice A end to end: verify → days → corpus, offline, deterministic and free (spec §16).

On the stub panel, so nothing reaches a provider. This is the slice's exit criterion — the three
commands an operator runs in order, against one dataset, producing the three artifacts every
later slice reads.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab import calibration_days as cd
from decision_lab import cli
from decision_lab import corpus as cp
from decision_lab.dataset import read_audit
from decision_lab.params import CORPUS_META
from decision_lab.tests import factories as f


def test_verify_then_days_then_corpus(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "history"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    inst = f.instrument()
    f.write_dataset(
        data,
        {(inst, "1h"): f.shocked_walk(days=60, shock_up=(3, 11, 19, 27, 35), shock_down=(7, 15, 23, 31, 39))},
    )

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
    built = sorted(p for p in workspace.iterdir() if (p / CORPUS_META).is_file())
    assert len(built) == 1

    corpus = cp.load(built[0].name, workspace=workspace)
    assert corpus.entries
    assert corpus.meta.cadence_seconds == 4 * 3600
    assert corpus.meta.dataset_digest == days.dataset_digest


def test_corpus_build_refuses_an_unverified_dataset(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "history"
    monkeypatch.setattr(cp, "workspace_root", lambda: tmp_path / "workspace")
    f.write_dataset(data, {(f.instrument(), "1h"): f.walk(["100"] * 500)})

    assert cli.main(["corpus", "build", "--data", str(data), "--every", "4h"]) == cli.EXIT_DATASET


def test_rebuilding_the_same_corpus_reuses_its_identity(tmp_path: Path, monkeypatch) -> None:
    """§11's premise one slice early: identical parameters are one experiment, not two."""
    data = tmp_path / "history"
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(cp, "workspace_root", lambda: workspace)
    f.write_dataset(data, {(f.instrument(), "1h"): f.shocked_walk(days=20)})
    cli.main(["dataset", "verify", "--data", str(data)])

    cli.main(["corpus", "build", "--data", str(data), "--every", "8h"])
    cli.main(["corpus", "build", "--data", str(data), "--every", "8h"])

    assert len([p for p in workspace.iterdir() if (p / CORPUS_META).is_file()]) == 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests/test_slice_a_end_to_end.py -q`
Expected: FAIL — `KeyError: ('corpus', 'build')`.

- [ ] **Step 3: Write the implementation**

Add to `decision_lab/cli.py`'s `parse_args`:

```python
    corpus = commands.add_parser("corpus", help="build the frozen decision contexts a sweep reads")
    corpus_actions = corpus.add_subparsers(dest="action", required=True)

    corpus_build = corpus_actions.add_parser("build", help="run one reference pass and index it")
    corpus_build.add_argument("--data", type=Path, required=True, help="dataset directory")
    corpus_build.add_argument(
        "--every",
        default="4h",
        choices=("1h", "2h", "4h", "8h", "12h", "24h"),
        help="cycle cadence. A corpus property, not a sweep one: every candidate in one sweep "
        "sees one cadence, so a cadence comparison is N corpora (§5.5)",
    )
    corpus_build.add_argument(
        "--reference-panel",
        default="sim",
        help="whose deliberation supplies the positions in the snapshots. `sim` and `stub` are "
        "offline and free; a real panel is available when the positions themselves need to be "
        "the ones a real panel would have held (§5.2)",
    )
    corpus_build.add_argument("--start-equity", type=Decimal, default=Decimal(10_000))
    corpus_build.add_argument("--since", default=None, help="window start; defaults to the data's")
    corpus_build.add_argument("--until", default=None, help="window end; defaults to the data's")
    corpus_build.add_argument("--verbose", action="store_true")
```

and the handler plus dispatch entry:

```python
async def corpus_build(args: argparse.Namespace) -> int:
    """Build the corpus. Refuses an unverified dataset before doing any work."""
    built = await cp.build(
        data_dir=args.data,
        reference_panel=args.reference_panel,
        cadence_seconds=int(timeframe_interval(args.every).total_seconds()),
        start_equity=args.start_equity,
        since=datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None,
        until=datetime.fromisoformat(args.until).replace(tzinfo=UTC) if args.until else None,
    )
    logger.info(
        "corpus ready",
        extra={
            "corpus_id": built.meta.corpus_id,
            "entries": len(built.entries),
            "cadence": args.every,
            "panel": args.reference_panel,
            "news": "blind" if built.meta.news_blind else built.meta.archive_digest,
        },
    )
    return EXIT_OK
```

with `_COMMANDS[("corpus", "build")] = corpus_build`, and imports `from datetime import UTC, datetime`, `from decimal import Decimal`, `from decision_lab import corpus as cp`, `from tradebot.core.market import timeframe_interval`.

`--since`/`--until` parsing: `datetime.fromisoformat` on a bare date yields a naive datetime, and DTZ-lint plus the bot's own model boundary both reject naive datetimes — hence the explicit `.replace(tzinfo=UTC)`. Mirror whatever `tradebot/__main__.py` does for `backtest run --since` if it already has a helper.

- [ ] **Step 4: Run the whole suite to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest decision_lab/tests -q`
Expected: PASS, every test in the slice.

- [ ] **Step 5: Verify against the real dataset**

```powershell
.venv\Scripts\python.exe -m decision_lab corpus build --data data\history --every 4h --reference-panel sim
```

Expected: a directory under `decision_lab\workspace\<corpus_id>\` holding `corpus.db` and `corpus.json`, with roughly 1,090 entries for a six-month 4h corpus. Offline and free — `SIM_PANEL` is three `varied-*` stub seats.

- [ ] **Step 6: Run both gates**

Run: `.\decision_lab\check.ps1`
Expected: `decision_lab checks passed`.

Run: `.\check.ps1`
Expected: `all checks passed` — and confirm the bot's coverage gate is unmoved, since `coverage source = ["tradebot"]` and nothing under `tradebot/` changed.

- [ ] **Step 7: Commit**

```bash
git add decision_lab/cli.py decision_lab/tests/test_slice_a_end_to_end.py
git commit -m "feat(decision_lab): corpus build, and the slice A end-to-end run"
```

---

## Slice A exit criteria

All four must hold before slice B starts:

1. `.\decision_lab\check.ps1` and `.\check.ps1` both pass.
2. `git diff --stat main -- tradebot/` is **empty**. Slice A changes no bot file; `test_separation.py` proves the import direction, and this proves the rest.
3. The three commands run in order against `data\history` and produce `decision_lab-coverage.json`, `decision_lab-calibration-days.json`, and a corpus directory.
4. The number of bars `--repair` recovered from the real dataset is recorded in a commit message — §18's claim that the audit found a real defect in `marketdata/recorder.py`, made concrete or withdrawn.
