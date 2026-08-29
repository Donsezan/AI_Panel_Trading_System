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
def test_the_correct_actions_are_exactly_the_spec_table(
    truth: sc.Truth, correct: set[Action]
) -> None:
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
