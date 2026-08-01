"""Quarantine: an operator's exclusion from automated trading (ADR 0022).

Three things are asserted here, and the third is what makes the first two safe:

* the **rule** refuses every automated order in a quarantined scope, whole-basket or single
  instrument, and does so as an ordinary recorded Tier-1 verdict rather than a special case;
* the **exemption** for a human's exit is the existing ADR 0015 predicate, reused unchanged, so a
  quarantined position is never orphaned — and it stands aside *only* when quarantine would
  otherwise have bitten, so no unquarantined close silently gains a fourth stood-aside rule;
* the **configuration** cannot name a scope that does not exist, because a quarantine matching
  nothing is a limit an operator believes is in force while the panel keeps trading through it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, PanelConfig, RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, Mode, RiskDecision, SizeHint
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.portfolio import Position
from tradebot.interfaces.risk import RiskProposal
from tradebot.risk.rules import (
    DEFAULT_TIER1_RULES,
    STOOD_ASIDE,
    LongOnlyRule,
    QuarantineRule,
)
from tradebot.risk.tier1 import Tier1RiskEngine

HELD = Decimal("0.5")
OTHER_KEY = "sim:ETH/USDT"


def policy(*, whole_basket: bool = False, keys: tuple[str, ...] = ()) -> RiskPolicy:
    return RiskPolicy(quarantined=whole_basket, quarantined_instruments=keys)


def proposal(
    instrument: Instrument,
    *,
    action: Action = Action.BUY,
    held: Decimal = ZERO,
    operator: bool = False,
    risk_policy: RiskPolicy | None = None,
) -> RiskProposal:
    return RiskProposal(
        decision=Decision(
            instrument_key=instrument.key,
            action=action,
            conviction=Decimal(1),
            size_hint=SizeHint.FULL,
        ),
        instrument=instrument,
        policy=risk_policy or RiskPolicy(),
        position=Position(instrument_key=instrument.key, qty=held, avg_entry=Decimal(50_000)),
        price=Decimal(50_000),
        last_price=Decimal(50_000),
        atr=Decimal(500),
        equity=Decimal(10_000),
        basket_budget=Decimal(1_000),
        basket_exposure=ZERO,
        operator_initiated=operator,
    )


# ---------------------------------------------------------------- the rule


def test_nothing_quarantined_permits_the_order(instrument: Instrument) -> None:
    result = QuarantineRule().evaluate(proposal(instrument), HELD)

    assert result.decision is RiskDecision.PASS
    assert result.max_qty == HELD


def test_a_quarantined_instrument_is_vetoed(instrument: Instrument) -> None:
    excluded = proposal(instrument, risk_policy=policy(keys=(instrument.key,)))

    result = QuarantineRule().evaluate(excluded, HELD)

    assert result.decision is RiskDecision.VETO
    assert instrument.key in result.detail


def test_a_quarantined_basket_vetoes_every_instrument_in_it(instrument: Instrument) -> None:
    """The whole-basket flag is not a shorthand for listing the keys — it covers them all."""
    excluded = proposal(instrument, risk_policy=policy(whole_basket=True))

    assert QuarantineRule().evaluate(excluded, HELD).decision is RiskDecision.VETO


def test_a_sibling_instrument_is_untouched(instrument: Instrument) -> None:
    """Quarantine is per scope: excluding one instrument must not stop the rest of the basket."""
    unaffected = proposal(instrument, risk_policy=policy(keys=(OTHER_KEY,)))

    assert QuarantineRule().evaluate(unaffected, HELD).decision is RiskDecision.PASS


def test_a_quarantined_sell_from_the_panel_is_vetoed_too(instrument: Instrument) -> None:
    """ "Hands off" means both directions: the bot does not exit a quarantined position either."""
    excluded = proposal(
        instrument, action=Action.SELL, held=HELD, risk_policy=policy(keys=(instrument.key,))
    )

    assert QuarantineRule().evaluate(excluded, HELD).decision is RiskDecision.VETO


# ---------------------------------------------------------------- the operator's exit


def test_an_operator_exit_is_let_through_and_says_so(instrument: Instrument) -> None:
    """A position that cannot be closed is not made safer by being excluded from new trades."""
    exit_request = proposal(
        instrument,
        action=Action.SELL,
        held=HELD,
        operator=True,
        risk_policy=policy(keys=(instrument.key,)),
    )

    result = QuarantineRule().evaluate(exit_request, HELD)

    assert result.decision is RiskDecision.PASS
    assert result.max_qty == HELD
    assert result.detail == STOOD_ASIDE


def test_the_whole_basket_flag_does_not_trap_an_operator_either(instrument: Instrument) -> None:
    exit_request = proposal(
        instrument,
        action=Action.SELL,
        held=HELD,
        operator=True,
        risk_policy=policy(whole_basket=True),
    )

    assert QuarantineRule().evaluate(exit_request, HELD).detail == STOOD_ASIDE


def test_standing_aside_is_recorded_only_when_quarantine_would_have_bitten(
    instrument: Instrument,
) -> None:
    """Otherwise every ordinary manual close would gain a stood-aside rule that never applied.

    The provenance record exists to say which limits declined to act on a decision they *would*
    otherwise have made; a rule that was never engaged has nothing to stand aside from.
    """
    ordinary = proposal(instrument, action=Action.SELL, held=HELD, operator=True)

    result = QuarantineRule().evaluate(ordinary, HELD)

    assert result.decision is RiskDecision.PASS
    assert result.detail != STOOD_ASIDE


def test_an_operator_may_not_open_a_position_in_a_quarantined_scope(
    instrument: Instrument,
) -> None:
    """The exemption is an *exit*: `is_operator_exit` requires a SELL against a holding."""
    buying = proposal(instrument, operator=True, risk_policy=policy(keys=(instrument.key,)))

    assert QuarantineRule().evaluate(buying, HELD).decision is RiskDecision.VETO


# ---------------------------------------------------------------- through the engine


def test_the_engine_refuses_a_quarantined_entry(instrument: Instrument, clock: ManualClock) -> None:
    outcome = Tier1RiskEngine(clock).approve(
        proposal(instrument, risk_policy=policy(keys=(instrument.key,))),
        mode=Mode.SIM,
        basket_id="b1",
        cycle_id="c1",
    )

    assert outcome.intent is None
    assert outcome.veto_reason.startswith("quarantine:")


def test_the_engine_still_approves_a_quarantined_operator_exit(
    instrument: Instrument, clock: ManualClock
) -> None:
    outcome = Tier1RiskEngine(clock).approve(
        proposal(
            instrument,
            action=Action.SELL,
            held=HELD,
            operator=True,
            risk_policy=policy(keys=(instrument.key,)),
        ),
        mode=Mode.SIM,
        basket_id="b1",
        cycle_id="c1",
    )

    assert outcome.intent is not None
    assert outcome.intent.qty == HELD


def test_it_is_wired_into_the_default_rules_after_long_only() -> None:
    """Order fixes which reason is reported first; correctness rules outrank an exclusion."""
    ids = [rule.rule_id for rule in DEFAULT_TIER1_RULES]

    assert ids.index(QuarantineRule.rule_id) == ids.index(LongOnlyRule.rule_id) + 1


# ---------------------------------------------------------------- the configuration


@pytest.mark.parametrize(
    ("configured", "key", "excluded"),
    [
        (policy(), "sim:BTC/USDT", False),
        (policy(keys=("sim:BTC/USDT",)), "sim:BTC/USDT", True),
        (policy(keys=("sim:BTC/USDT",)), OTHER_KEY, False),
        (policy(whole_basket=True), OTHER_KEY, True),
    ],
)
def test_excludes_is_the_one_predicate(configured: RiskPolicy, key: str, excluded: bool) -> None:
    assert configured.excludes(key) is excluded


def test_quarantine_defaults_to_off() -> None:
    """A basket published before this existed must keep trading exactly as it did."""
    assert RiskPolicy().quarantine == ""
    assert not RiskPolicy().excludes("sim:BTC/USDT")


def test_adding_and_removing_an_instrument_round_trips() -> None:
    added = RiskPolicy().with_quarantine("sim:BTC/USDT", excluded=True)
    released = added.with_quarantine("sim:BTC/USDT", excluded=False)

    assert added.quarantined_instruments == ("sim:BTC/USDT",)
    assert released.quarantined_instruments == ()


def test_setting_the_same_state_twice_changes_nothing() -> None:
    """A double-clicked toggle must not publish a version that says what the last one said."""
    once = RiskPolicy().with_quarantine("sim:BTC/USDT", excluded=True)

    assert once.with_quarantine("sim:BTC/USDT", excluded=True) == once


def test_keys_are_kept_sorted_so_a_version_diff_shows_only_real_changes() -> None:
    built = (
        RiskPolicy()
        .with_quarantine(OTHER_KEY, excluded=True)
        .with_quarantine("sim:BTC/USDT", excluded=True)
    )

    assert built.quarantined_instruments == ("sim:BTC/USDT", OTHER_KEY)


def test_an_empty_key_means_the_whole_basket() -> None:
    whole = RiskPolicy().with_quarantine(excluded=True)

    assert whole.quarantined
    assert whole.quarantined_instruments == ()
    assert whole.quarantine == "whole basket"


def test_a_basket_refuses_to_quarantine_an_instrument_it_does_not_hold(
    instrument: Instrument, panel: PanelConfig
) -> None:
    """A quarantine matching nothing excludes nothing, and would be believed to be in force."""
    with pytest.raises(ValidationError, match="does not hold"):
        Basket(
            basket_id="b1",
            name="test basket",
            instruments=(instrument,),
            panel=panel,
            risk_policy=policy(keys=("sim:DOGE/USDT",)),
        )


def test_a_basket_accepts_a_quarantine_on_an_instrument_it_holds(
    instrument: Instrument, panel: PanelConfig
) -> None:
    basket = Basket(
        basket_id="b1",
        name="test basket",
        instruments=(instrument,),
        panel=panel,
        risk_policy=policy(keys=(instrument.key,)),
    )

    assert basket.risk_policy.excludes(instrument.key)
    assert basket.risk_policy.quarantine == instrument.key
