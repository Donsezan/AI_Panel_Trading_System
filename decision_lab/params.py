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

#: Cadences `corpus build --every` accepts, in seconds. Deliberately **not**
#: `market.timeframe_interval`: a cadence is a schedule interval (`Schedule.every_seconds`), not a
#: bar duration, and the two sets differ — 8h is a legitimate cycle cadence and not a Binance
#: kline timeframe, so reading it through the timeframe table would refuse it as unsupported.
CADENCE_SECONDS: Final = {
    "1h": 3_600,
    "2h": 7_200,
    "4h": 14_400,
    "8h": 28_800,
    "12h": 43_200,
    "24h": 86_400,
}

#: The seed a day set is drawn with when nobody said otherwise. Printed on every report as the
#: thing that makes a re-selection comparable.
DEFAULT_SEED: Final = 20260823

#: Written beside the dataset, never inside it: `dataset.json` is a `tradebot` model and editing
#: it would be a bot change (§4.3).
COVERAGE_FILE: Final = "decision_lab-coverage.json"
DAYSET_FILE: Final = "decision_lab-calibration-days.json"

#: Written beside the corpus database, in the workspace.
CORPUS_META: Final = "corpus.json"


def workspace_root() -> Path:
    """Scratch databases, caches and results. Gitignored, and never `data/` (§2.1)."""
    return Path(__file__).parent / "workspace"
