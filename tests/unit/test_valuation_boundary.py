"""No module may value a position at what it cost.

Asserted structurally rather than by review, in the manner `test_dashboard_chart.py` asserts the
float boundary. The fallback-to-cost in `Ledger.equity` was the entire mechanism of the drawdown
defect — a portfolio that had halved reported 0% drawdown, because the "price" it was marked at
*was* its cost (PHASE_12 Finding 1). A "helpful" fallback re-added later is how that comes back,
and it would come back silently: every test would still pass, because the number is plausible.

The rule is: a missing price is a **freeze**, never a guess. `risk.aggregate` is the only place
allowed to decide what an unmarked position means, and its answer is `frozen_reason`.
"""

from __future__ import annotations

import re
from pathlib import Path

import tradebot

#: `prices.get(key, position.avg_entry)` and every spelling of it — any `.get()` whose *default*
#: mentions a cost basis. One level of nesting is allowed inside the arguments, because the
#: deleted `Ledger.exposure` spelled it `prices.get(key, self.position(key).avg_entry)` and a
#: pattern that stopped at the first `)` would have missed the very defect it was written for.
_ARG = r"(?:[^()]|\([^()]*\))*"
FALLBACK = re.compile(rf"\.get\({_ARG},{_ARG}avg_entry", re.DOTALL)

#: The packages that value things. `dashboard/` is excluded because it renders what these compute
#: and never prices anything itself.
WATCHED = ("ledger", "risk", "control")


def _offenders() -> list[str]:
    root = Path(tradebot.__file__).parent
    return sorted(
        path.relative_to(root).as_posix()
        for package in WATCHED
        for path in (root / package).rglob("*.py")
        if FALLBACK.search(path.read_text(encoding="utf-8"))
    )


def test_no_module_falls_back_to_cost_basis() -> None:
    assert _offenders() == [], (
        f"these modules value a position at its cost when a price is missing: {_offenders()}. "
        "The fallback is a freeze, never cost — see ADR 0027 and PHASE_12 §1.4."
    )


def test_the_guard_can_actually_fail() -> None:
    """A guard that cannot fail is not a guard.

    Proves the pattern matches the defect it was written for, so a future refactor that changes
    how the fallback is spelled cannot leave this test passing vacuously.
    """
    assert FALLBACK.search("value = prices.get(key, position.avg_entry)")
    assert FALLBACK.search("prices.get(\n    instrument_key,\n    self.position(key).avg_entry,\n)")
    assert not FALLBACK.search("price = prices[key]")
    assert not FALLBACK.search("mark = marks.price_of(key, now=now, tolerance=tolerance)")
