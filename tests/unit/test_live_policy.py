"""The Tier-2 ceiling live runs under (DESIGN §9 rung 6, ADR 0020).

The property that carries the money safety is one-directional: the ceiling may only ever tighten.
A bug that let it *loosen* a published limit would take a policy an operator deliberately narrowed
and widen it at the exact moment the money became real, which is the opposite of what rung 6 asks
for. So the tests are written against that direction, not against the specific numbers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.control.live import (
    CEILED_FIELDS,
    LIVE_CEILING,
    MODE_CEILING,
    effective_policy,
)
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Mode
from tradebot.core.money import ZERO

CAP = Decimal(50)

#: A policy looser than the ceiling on every clamped field — the seed defaults are exactly that,
#: which is the case an operator reaches by doing nothing.
LOOSE = GlobalRiskPolicy()


def test_the_seed_defaults_are_looser_than_the_ceiling_on_every_clamped_field() -> None:
    """Otherwise the ceiling is decorative and the tests below prove nothing."""
    for field in CEILED_FIELDS:
        assert getattr(LOOSE, field) > getattr(LIVE_CEILING, field), field


class TestLive:
    def test_every_clamped_field_is_tightened_to_the_ceiling(self) -> None:
        effective = effective_policy(LOOSE, mode=Mode.LIVE, max_order_notional=CAP)
        for field in CEILED_FIELDS:
            assert getattr(effective.policy, field) == getattr(LIVE_CEILING, field), field

    def test_the_arming_cap_becomes_the_order_notional_limit(self) -> None:
        """Uncapped is looser than any number, so it clamps rather than surviving as `None`."""
        assert LOOSE.max_order_notional is None
        effective = effective_policy(LOOSE, mode=Mode.LIVE, max_order_notional=CAP)
        assert effective.policy.max_order_notional == CAP

    def test_a_tighter_published_limit_is_kept(self) -> None:
        """The ceiling is a maximum, not a setting: it never loosens a deliberate choice."""
        published = LOOSE.model_copy(
            update={
                "max_gross_exposure_pct": Decimal(5),
                "max_orders_per_hour": 1,
                "max_order_notional": Decimal(10),
            }
        )
        effective = effective_policy(published, mode=Mode.LIVE, max_order_notional=CAP)
        assert effective.policy.max_gross_exposure_pct == Decimal(5)
        assert effective.policy.max_orders_per_hour == 1
        assert effective.policy.max_order_notional == Decimal(10)

    def test_a_policy_already_within_the_ceiling_is_returned_untouched(self) -> None:
        tight = LIVE_CEILING.model_copy(update={"max_order_notional": CAP})
        effective = effective_policy(tight, mode=Mode.LIVE, max_order_notional=CAP)
        assert effective.policy == tight
        assert effective.clamps == ()
        assert "already within" in effective.detail

    def test_every_clamp_is_named_for_the_audit_trail(self) -> None:
        """`_record_effective_policy` writes this string into a RISK_EVENT (PLAN §3.3)."""
        effective = effective_policy(LOOSE, mode=Mode.LIVE, max_order_notional=CAP)
        named = {clamp.field for clamp in effective.clamps}
        assert named == {*CEILED_FIELDS, "max_order_notional"}
        assert "max_drawdown_pct" in effective.detail

    def test_an_uncapped_published_notional_reads_as_uncapped_not_as_zero(self) -> None:
        effective = effective_policy(LOOSE, mode=Mode.LIVE, max_order_notional=CAP)
        clamp = next(c for c in effective.clamps if c.field == "max_order_notional")
        assert str(clamp) == f"max_order_notional uncapped→{CAP}"

    def test_the_ceiling_never_widens_a_limit(self) -> None:
        """The one-directional property, stated over the whole field set."""
        for published in (
            LOOSE,
            LIVE_CEILING,
            LIVE_CEILING.model_copy(update={"max_orders_per_hour": 1}),
        ):
            effective = effective_policy(published, mode=Mode.LIVE, max_order_notional=CAP)
            for field in CEILED_FIELDS:
                assert getattr(effective.policy, field) <= getattr(published, field), field


class TestOtherModes:
    @pytest.mark.parametrize("mode", [Mode.SIM, Mode.PAPER])
    def test_no_ceiling_applies(self, mode: Mode) -> None:
        """Sim and paper run the published policy. A ceiling there would make the soak evidence
        about limits live would not actually use."""
        assert MODE_CEILING[mode] is None
        effective = effective_policy(LOOSE, mode=mode, max_order_notional=ZERO)
        assert effective.policy == LOOSE
        assert effective.clamps == ()

    def test_a_cap_still_applies_where_one_is_given(self) -> None:
        """The arming cap is not live-only machinery — it is an ordinary Tier-2 limit (ADR 0012)."""
        effective = effective_policy(LOOSE, mode=Mode.PAPER, max_order_notional=CAP)
        assert effective.policy.max_order_notional == CAP
