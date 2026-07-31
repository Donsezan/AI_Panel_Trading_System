"""Which Tier-2 edits *weaken* the limits, and therefore need a second human act.

DESIGN §6.10 requires an extra typed confirmation to loosen a Tier-2 limit. That is only a
control if something can tell loosening from tightening, and the answer is per field: a higher
`max_gross_exposure_pct` permits more, a higher `max_drawdown_pct` tolerates more loss before
the kill switch, and removing a per-order notional cap permits any size at all.

The direction table is the whole module. It lives beside the rules rather than in the dashboard
because it states what a limit *means*, and a form is only one of the things that may ask.

Two fields are deliberately absent:

* **`flatten_on_kill`** is not a limit. Neither value permits more trading — it chooses what
  happens to open positions after everything has already stopped, and DESIGN §6.6 is explicit
  that the call belongs to the operator.
* **`clusters`** cannot be ranked. Removing a bucket does not loosen `ClusterExposureRule`; it
  makes the rule *veto*, because an instrument in no bucket has unbounded concentration. Asking
  for a loosening confirmation on a change that tightens would teach an operator to type the
  phrase without reading it, which is how a confirmation stops being one.

Failure semantics: unknown or unrankable fields are reported as *not* loosened, and the caller
still publishes a new version that the log records either way. This decides whether to ask for a
confirmation, never whether a limit is enforced.
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.money import ZERO

#: Field → whether a *larger* value is the weaker one. Every Tier-2 limit here is a ceiling, so
#: all of them loosen upwards; the mapping is explicit anyway, because a limit added later that
#: tightens upwards would otherwise inherit the wrong direction silently.
LOOSER_WHEN_HIGHER: tuple[str, ...] = (
    "max_gross_exposure_pct",
    "max_instrument_exposure_pct",
    "max_cluster_exposure_pct",
    "price_collar_pct",
    "max_orders_per_hour",
    "max_daily_loss_pct",
    "max_drawdown_pct",
    "stablecoin_peg_tolerance_pct",
)

#: What an unset per-order notional cap means when it is compared against a number. "No cap" is
#: the loosest possible setting, so it must not compare as zero.
UNCAPPED = "unlimited"


def looser_limits(current: GlobalRiskPolicy, proposed: GlobalRiskPolicy) -> tuple[str, ...]:
    """Field names `proposed` weakens relative to `current`, in declaration order.

    Empty means nothing was loosened, which is the only case that publishes without a second
    typed confirmation (DESIGN §6.10).
    """
    loosened = [
        name
        for name in LOOSER_WHEN_HIGHER
        if _number(getattr(proposed, name)) > _number(getattr(current, name))
    ]
    if _notional_loosened(current.max_order_notional, proposed.max_order_notional):
        loosened.append("max_order_notional")
    return tuple(loosened)


def describe(current: GlobalRiskPolicy, proposed: GlobalRiskPolicy) -> tuple[str, ...]:
    """Human-readable `field: old → new` lines for each loosened limit, for the confirmation."""
    return tuple(
        f"{name}: {_shown(getattr(current, name))} → {_shown(getattr(proposed, name))}"
        for name in looser_limits(current, proposed)
    )


def _notional_loosened(current: Decimal | None, proposed: Decimal | None) -> bool:
    """Removing the cap is the largest loosening there is, so `None` is not a small number."""
    if proposed is None:
        return current is not None
    return current is not None and proposed > current


def _number(value: Decimal | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(value)


def _shown(value: Decimal | int | None) -> str:
    if value is None:
        return UNCAPPED
    return str(value) if value != ZERO else "0"
