"""The consensus decision table.

DESIGN §6.5 specifies this behaviour precisely because it is cheap to specify and expensive to
get wrong: it is the rule that decides whether a panel's disagreement becomes a trade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.config import PanelConfig, SeatConfig
from tradebot.core.decision import SeatResponse, SeatVote
from tradebot.core.enums import Action, SizeHint
from tradebot.decision.consensus import (
    PANEL_DEGRADED,
    PANEL_HOMOGENEOUS,
    reach_consensus,
    required_votes,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)
KEY = "sim:BTC/USDT"


def panel_of(count: int, *, models: list[str] | None = None) -> PanelConfig:
    names = models or [f"model-{i}" for i in range(count)]
    return PanelConfig(
        panel_id="p",
        seats=tuple(
            SeatConfig(seat_id=f"s{i}", role=f"role-{i}", provider_id="stub", model=names[i])
            for i in range(count)
        ),
    )


def response(
    index: int,
    action: Action | None,
    conviction: int = 4,
    size: SizeHint = SizeHint.HALF,
    model: str | None = None,
) -> SeatResponse:
    """A seat response; `action=None` means the seat abstained."""
    vote = (
        None
        if action is None
        else SeatVote(
            action=action,
            conviction=conviction,
            size_hint=size if action.is_tradable else SizeHint.NONE,
            thesis=f"thesis from seat {index}",
        )
    )
    return SeatResponse(
        seat_id=f"s{index}",
        role=f"role-{index}",
        provider_id="stub",
        model=model or f"model-{index}",
        round_index=0,
        instrument_key=KEY,
        vote=vote,
        abstain_reason=None if vote else "scripted failure",
        responded_at=NOW,
    )


class TestQualifiedMajority:
    @pytest.mark.parametrize(("seats", "needed"), [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3)])
    def test_threshold_is_over_the_original_seat_count(self, seats: int, needed: int) -> None:
        assert required_votes(panel_of(seats)) == needed

    def test_two_of_three_buy_trades(self) -> None:
        decision = reach_consensus(
            (response(0, Action.BUY), response(1, Action.BUY), response(2, Action.HOLD)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.BUY
        assert decision.votes_for == 2

    def test_one_buy_plus_one_abstention_is_not_a_mandate(self) -> None:
        """The failure this rule exists to prevent: a minority made decisive by silence."""
        decision = reach_consensus(
            (response(0, Action.BUY), response(1, None), response(2, Action.HOLD)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.WAIT
        assert "no qualified majority" in decision.reasoning_summary

    def test_a_split_panel_waits_rather_than_being_pushed_to_converge(self) -> None:
        decision = reach_consensus(
            (response(0, Action.BUY), response(1, Action.SELL), response(2, Action.HOLD)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.WAIT
        assert len(decision.dissent) == 3


class TestHoldVersusWait:
    def test_hold_majority_yields_hold_not_wait(self) -> None:
        """HOLD is an affirmative vote against acting; WAIT is the absence of a signal."""
        decision = reach_consensus(
            (response(0, Action.HOLD), response(1, Action.HOLD), response(2, Action.BUY)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.HOLD
        assert not decision.is_actionable
        assert decision.size_hint is SizeHint.NONE

    def test_wait_majority_yields_wait(self) -> None:
        decision = reach_consensus(
            (response(0, Action.WAIT), response(1, Action.WAIT), response(2, Action.BUY)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.WAIT


class TestAbstentions:
    def test_one_abstention_of_three_is_not_yet_degraded(self) -> None:
        """Exactly ⅓ is not *more* than ⅓; the panel proceeds with the remaining seats."""
        decision = reach_consensus(
            (response(0, Action.BUY), response(1, Action.BUY), response(2, None)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.BUY
        assert decision.abstentions == 1
        assert PANEL_DEGRADED not in decision.flags

    def test_two_abstentions_of_three_degrades_the_panel(self) -> None:
        decision = reach_consensus(
            (response(0, Action.BUY), response(1, None), response(2, None)),
            panel_of(3),
            KEY,
        )
        assert decision.action is Action.WAIT
        assert PANEL_DEGRADED in decision.flags

    def test_every_seat_abstaining_still_produces_a_decision(self) -> None:
        """The consensus rule must never raise — a cycle with no decision is unrecordable."""
        decision = reach_consensus(
            (response(0, None), response(1, None), response(2, None)), panel_of(3), KEY
        )
        assert decision.action is Action.WAIT
        assert PANEL_DEGRADED in decision.flags

    def test_no_responses_at_all(self) -> None:
        assert reach_consensus((), panel_of(3), KEY).action is Action.WAIT


class TestConviction:
    def test_unanimous_top_rating_reaches_one(self) -> None:
        decision = reach_consensus(
            tuple(response(i, Action.BUY, conviction=5) for i in range(3)), panel_of(3), KEY
        )
        assert decision.conviction == Decimal(1)

    def test_normalization_scales_by_agreement_fraction(self) -> None:
        """((mean − 1) / 4) × agreement — 2 of 3 seats at rating 4 → 0.75 × ⅔ = 0.5."""
        decision = reach_consensus(
            (
                response(0, Action.BUY, conviction=4),
                response(1, Action.BUY, conviction=4),
                response(2, Action.HOLD),
            ),
            panel_of(3),
            KEY,
        )
        assert decision.conviction == Decimal("0.5")

    def test_lowest_rating_is_zero_conviction(self) -> None:
        decision = reach_consensus(
            tuple(response(i, Action.BUY, conviction=1) for i in range(3)), panel_of(3), KEY
        )
        assert decision.conviction == Decimal(0)


class TestSizeHint:
    def test_panel_size_is_the_most_conservative_agreeing_voice(self) -> None:
        """One enthusiastic seat must not drag the size up."""
        decision = reach_consensus(
            (
                response(0, Action.BUY, size=SizeHint.FULL),
                response(1, Action.BUY, size=SizeHint.QUARTER),
                response(2, Action.BUY, size=SizeHint.HALF),
            ),
            panel_of(3),
            KEY,
        )
        assert decision.size_hint is SizeHint.QUARTER


class TestHeterogeneity:
    def test_fallbacks_collapsing_onto_one_model_is_flagged(self) -> None:
        """Heterogeneity is a design control; its silent loss must be visible (R11)."""
        decision = reach_consensus(
            (
                response(0, Action.BUY, model="same-model"),
                response(1, Action.BUY, model="same-model"),
                response(2, Action.HOLD, model="other"),
            ),
            panel_of(3),
            KEY,
        )
        assert PANEL_HOMOGENEOUS in decision.flags

    def test_a_heterogeneous_panel_is_not_flagged(self) -> None:
        decision = reach_consensus(
            tuple(response(i, Action.BUY) for i in range(3)), panel_of(3), KEY
        )
        assert PANEL_HOMOGENEOUS not in decision.flags

    def test_a_single_seat_panel_is_not_homogeneous_by_definition(self) -> None:
        decision = reach_consensus((response(0, Action.BUY),), panel_of(1), KEY)
        assert PANEL_HOMOGENEOUS not in decision.flags


class TestDissent:
    def test_minority_views_are_preserved_verbatim(self) -> None:
        decision = reach_consensus(
            (response(0, Action.BUY), response(1, Action.BUY), response(2, Action.SELL)),
            panel_of(3),
            KEY,
        )
        assert decision.dissent == ("role-2: thesis from seat 2",)
