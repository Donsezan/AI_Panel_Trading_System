"""Which Tier-2 edits weaken the limits (DESIGN §6.10's extra confirmation).

Tested field by field, because the confirmation is only a control if it fires on exactly the
edits that permit more. Asking for it on a *tightening* would teach an operator to type the
phrase without reading, which is how a confirmation stops being one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.config import CorrelationCluster, GlobalRiskPolicy
from tradebot.risk.loosening import LOOSER_WHEN_HIGHER, UNCAPPED, describe, looser_limits

BASE = GlobalRiskPolicy()


def test_an_unchanged_policy_loosens_nothing() -> None:
    assert looser_limits(BASE, GlobalRiskPolicy()) == ()


@pytest.mark.parametrize("field", LOOSER_WHEN_HIGHER)
def test_every_ceiling_loosens_upwards(field: str) -> None:
    raised = BASE.model_copy(update={field: _bump(getattr(BASE, field))})
    assert looser_limits(BASE, raised) == (field,)


@pytest.mark.parametrize("field", LOOSER_WHEN_HIGHER)
def test_every_ceiling_tightens_downwards(field: str) -> None:
    lowered = BASE.model_copy(update={field: _drop(getattr(BASE, field))})
    assert looser_limits(BASE, lowered) == ()


def test_several_loosenings_are_all_reported() -> None:
    """An operator must see everything they are about to permit, not the first thing."""
    weaker = BASE.model_copy(
        update={"max_drawdown_pct": Decimal(20), "max_gross_exposure_pct": Decimal(95)}
    )
    assert set(looser_limits(BASE, weaker)) == {"max_drawdown_pct", "max_gross_exposure_pct"}


# ---------------------------------------------------------------- the notional cap


def test_removing_the_per_order_cap_is_the_largest_loosening_there_is() -> None:
    capped = BASE.model_copy(update={"max_order_notional": Decimal(50)})
    assert looser_limits(capped, BASE) == ("max_order_notional",)


def test_adding_a_cap_where_there_was_none_is_a_tightening() -> None:
    capped = BASE.model_copy(update={"max_order_notional": Decimal(50)})
    assert looser_limits(BASE, capped) == ()


def test_raising_an_existing_cap_loosens() -> None:
    low = BASE.model_copy(update={"max_order_notional": Decimal(50)})
    high = BASE.model_copy(update={"max_order_notional": Decimal(500)})
    assert looser_limits(low, high) == ("max_order_notional",)
    assert looser_limits(high, low) == ()


def test_an_uncapped_notional_reads_as_unlimited_not_as_zero() -> None:
    capped = BASE.model_copy(update={"max_order_notional": Decimal(50)})
    assert describe(capped, BASE) == (f"max_order_notional: 50 → {UNCAPPED}",)


# ---------------------------------------------------------------- deliberate exclusions


def test_flatten_on_kill_is_not_ranked() -> None:
    """Neither value permits more trading; it decides what happens after everything stopped."""
    flipped = BASE.model_copy(update={"flatten_on_kill": not BASE.flatten_on_kill})
    assert looser_limits(BASE, flipped) == ()


def test_changing_clusters_is_not_ranked() -> None:
    """Removing a bucket makes `ClusterExposureRule` veto — it tightens, it does not loosen."""
    fewer = BASE.model_copy(
        update={"clusters": (CorrelationCluster(cluster_id="crypto", instrument_keys=()),)}
    )
    assert looser_limits(BASE, fewer) == ()


def test_describe_reads_as_old_to_new() -> None:
    weaker = BASE.model_copy(update={"max_drawdown_pct": Decimal(20)})
    assert describe(BASE, weaker) == ("max_drawdown_pct: 10 → 20",)


def test_every_ranked_field_exists_on_the_policy() -> None:
    """A renamed limit must fail here rather than silently stop needing a confirmation."""
    for field in (*LOOSER_WHEN_HIGHER, "max_order_notional"):
        assert field in GlobalRiskPolicy.model_fields


def _bump(value: Decimal | int) -> Decimal | int:
    return value + 1 if isinstance(value, int) else value + Decimal(1)


def _drop(value: Decimal | int) -> Decimal | int:
    return value - 1 if isinstance(value, int) else value - Decimal(1)
