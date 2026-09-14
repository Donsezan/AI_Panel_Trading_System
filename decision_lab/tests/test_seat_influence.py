"""Swing rate and marginal contribution, on handmade vote sets (spec §16.1).

These are the two metrics a reader will trust without checking, so they are asserted against
constructed panels rather than only end to end: a three-seat panel where removing seat A flips the
decision and removing seat B does not, and a seat that dissents correctly against a wrong panel
beside one that dissents wrongly against a right one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
        providers=(
            ProviderSettings(
                provider_id="openrouter", kind="openai_compat", base_url="https://x", secret_ref="X"
            ),
        ),
        seats=tuple(
            SeatConfig(seat_id=s, role="analyst", provider_id="openrouter", model="primary")
            for s in seat_ids
        ),
        qualified_majority=Decimal(majority),
    )


def test_a_seat_that_flips_the_decision_has_a_swing() -> None:
    """Two BUY, one WAIT, majority 0.6 of three → 2 votes needed, so BUY.

    The counterfactual removes the seat from the `PanelConfig` as well as from the votes, so a
    two-seat panel needs `ceil(0.6 × 2) = 2` and the lone remaining BUY no longer carries it.
    At 0.5 the threshold would fall with the seat count — `ceil(0.5 × 2) = 1` — and every
    removal would leave the decision standing, which measures the arithmetic rather than the seat.
    """
    votes = [
        response("a", Action.BUY),
        response("b", Action.BUY),
        response("c", Action.WAIT),
    ]
    swings = st.swings(votes, panel=panel("a", "b", "c", majority="0.6"), instrument_key=KEY)

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
    assert (
        st.marginal_contribution(
            seat_action=Action.BUY, panel_action=Action.BUY, truth=sc.Truth.BUY
        )
        == 0
    )
    assert (
        st.marginal_contribution(
            seat_action=Action.BUY, panel_action=Action.BUY, truth=sc.Truth.STAND_ASIDE
        )
        == 0
    )


def test_dissenting_and_both_being_wrong_contributes_nothing() -> None:
    """Neither earned nor cost the slot: the panel would have been wrong either way."""
    assert (
        st.marginal_contribution(
            seat_action=Action.SELL, panel_action=Action.WAIT, truth=sc.Truth.BUY
        )
        == 0
    )


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


def test_one_regime_agreeing_does_not_speak_for_the_others() -> None:
    """Controller ruling, item 6: the round-0-vs-final check is per regime, not per seat.

    `rounds_are_identical` keyed `zero` and `final` by `seat_id` alone across every regime at
    once, so one seat's rows overwrote each other and only the last regime placed survived the
    comparison. A thin `SHOCK_DOWN` in which debate happened to change nothing therefore made the
    whole candidate read as `single_round` — and both readers of this answer (`render._seat_tables`
    and the dashboard's `seats.html`) then *suppress every round-0 row*, so a genuinely debated
    `NORMAL` block is shown as one column, which is the one thing §9.7 exists to prevent.

    NORMAL here is debated (WAIT in round 0, BUY in the final round); SHOCK_DOWN carries a single
    round, so its two labels are identical by construction. Asserted in both concatenation orders
    because the answer is a property of the rows, never of the order they arrive in: `_fold` emits
    them sorted by regime, so the buggy keying survived on the alphabetically-last regime alone.
    """
    debated = st.score_seats_for_instrument(
        [
            response("a", Action.WAIT, round_index=0),
            response("a", Action.BUY, round_index=1),
        ],
        truth=sc.Truth.BUY,
        regime=Pool.NORMAL,
        panel=panel("a", "b"),
        instrument_key=KEY,
    )
    single = st.score_seats_for_instrument(
        [response("a", Action.BUY, round_index=0)],
        truth=sc.Truth.BUY,
        regime=Pool.SHOCK_DOWN,
        panel=panel("a", "b"),
        instrument_key=KEY,
    )

    assert st.rounds_are_identical(single) is True, "the fixture's quiet regime really is single"
    assert st.rounds_are_identical(debated + single) is False
    assert st.rounds_are_identical(single + debated) is False
