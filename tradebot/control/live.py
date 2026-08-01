"""Live's training wheels: the Tier-2 ceiling every limit is clamped to (DESIGN §9 rung 6).

Rung 6 is "live with training wheels — hard-capped budget, tightest Tier-2 policy, widened only
manually and gradually". The published policy is what the operator edited in the dashboard for a
soak; live does not trust it to have been re-tightened first, because the failure that costs money
is the one nobody remembered to prevent.

So the ceiling **only ever tightens**. Every clamped field takes `min(published, ceiling)`, which
has three consequences worth stating:

* An operator who has already published a tighter policy keeps it. The ceiling is a maximum, not
  a setting, and it never loosens a limit somebody chose deliberately.
* Widening past the ceiling is not something a config edit can do. Raising it is a source change,
  reviewed and released — which is what "widened only manually and gradually" has to mean if it
  is to mean anything.
* The clamp is *recorded*, as a `RISK_EVENT` at wiring time and in the log line beside it, so the
  policy live actually ran under is in the audit trail rather than inferable from two documents.

The per-order notional cap is folded in from the arming row rather than fixed here: it is the one
live limit a human sets per account, and `capped` already knows how to apply it (ADR 0012).

Failure semantics: nothing here does I/O and nothing raises. A mode with no ceiling — sim, paper —
gets its published policy back untouched, so this module cannot change a non-live run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from tradebot.control.arming import capped
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Mode

#: The tightest Tier-2 policy live may run under. Every value is a *ceiling*: a published policy
#: below one of these keeps its own number. Deliberately far below the seed defaults — first live
#: money buys evidence, not returns, and the arming row's notional cap bounds it further still.
LIVE_CEILING: Final = GlobalRiskPolicy(
    max_gross_exposure_pct=Decimal(20),
    max_instrument_exposure_pct=Decimal(5),
    max_cluster_exposure_pct=Decimal(10),
    price_collar_pct=Decimal(2),
    max_orders_per_hour=5,
    max_daily_loss_pct=Decimal(1),
    max_drawdown_pct=Decimal(5),
    stablecoin_peg_tolerance_pct=Decimal(1),
)

#: Fields on which a smaller number is a tighter limit, and which are therefore clamped. The rest
#: of `GlobalRiskPolicy` is not a magnitude: `clusters` is structure and `flatten_on_kill` is a
#: choice about a broken market that only the operator can make (DESIGN §6.6).
CEILED_FIELDS: Final[tuple[str, ...]] = (
    "max_gross_exposure_pct",
    "max_instrument_exposure_pct",
    "max_cluster_exposure_pct",
    "price_collar_pct",
    "max_orders_per_hour",
    "max_daily_loss_pct",
    "max_drawdown_pct",
    "stablecoin_peg_tolerance_pct",
)

#: Which ceiling each mode runs under. A table rather than a test on the mode, for the same reason
#: `MODE_SANDBOX` is one: no boolean flip anywhere can promote a paper run into live's rules or,
#: worse, demote live out of them (PLAN §2.4).
MODE_CEILING: Final[Mapping[Mode, GlobalRiskPolicy | None]] = {
    Mode.SIM: None,
    Mode.PAPER: None,
    Mode.LIVE: LIVE_CEILING,
}


@dataclass(frozen=True, slots=True)
class Clamp:
    """One limit the ceiling tightened, kept so the audit trail can name it."""

    field: str
    #: `None` is the published value only for `max_order_notional`, where it means *uncapped* —
    #: which is looser than any number, and must not print as a zero.
    published: Decimal | int | None
    enforced: Decimal | int

    def __str__(self) -> str:
        published = "uncapped" if self.published is None else self.published
        return f"{self.field} {published}→{self.enforced}"


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The Tier-2 policy a mode actually enforces, and what it had to tighten to get there."""

    policy: GlobalRiskPolicy
    clamps: tuple[Clamp, ...] = ()

    @property
    def detail(self) -> str:
        return ", ".join(str(clamp) for clamp in self.clamps) or "published policy already within"


def effective_policy(
    published: GlobalRiskPolicy, *, mode: Mode, max_order_notional: Decimal
) -> EffectivePolicy:
    """The policy this mode runs under: the published one, capped, and — in live — clamped.

    The single answer to "which limits are in force", so the wiring, the runner rebuild at each
    cycle boundary, and the report all read the same number.
    """
    ceiling = MODE_CEILING[mode]
    if ceiling is None:
        return EffectivePolicy(capped(published, max_order_notional))
    return _tightened(published, capped(ceiling, max_order_notional))


def _tightened(published: GlobalRiskPolicy, ceiling: GlobalRiskPolicy) -> EffectivePolicy:
    """`min(published, ceiling)` per clamped field, plus the notional cap the ceiling carries.

    An unset `max_order_notional` on the published policy means *uncapped*, which is looser than
    any number — so it clamps to the ceiling's cap rather than surviving as `None`. Live cannot be
    armed without one, so the cap is always present here (ADR 0012).
    """
    clamps = [
        Clamp(field, getattr(published, field), getattr(ceiling, field))
        for field in CEILED_FIELDS
        if getattr(published, field) > getattr(ceiling, field)
    ]
    cap, published_cap = ceiling.max_order_notional, published.max_order_notional
    if cap is not None and (published_cap is None or published_cap > cap):
        clamps.append(Clamp("max_order_notional", published_cap, cap))
    if not clamps:
        return EffectivePolicy(published)
    return EffectivePolicy(
        published.model_copy(update={clamp.field: clamp.enforced for clamp in clamps}),
        tuple(clamps),
    )
