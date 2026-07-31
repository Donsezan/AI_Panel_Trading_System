"""The operator-exit exemption: what stands aside, what never does, and who may ask (ADR 0015).

This is a deliberate narrowing of when the *metering* rules apply, so it is tested from three
directions: the predicate's truth table, each exempted rule's behaviour on both sides of it, and
the rules that must keep vetoing a human's close no matter what. The last group is the important
one — the exemption is only safe because correctness and venue legality are outside it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy, RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.enums import Action, Mode, RiskDecision, SizeHint
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.portfolio import Position
from tradebot.interfaces.risk import RiskProposal, TradingHistory
from tradebot.risk.rules import (
    STOOD_ASIDE,
    ConsecutiveLossRule,
    CooldownRule,
    DailyTradeCapRule,
    LongOnlyRule,
    MinConvictionRule,
)
from tradebot.risk.tier1 import Tier1RiskEngine
from tradebot.risk.tier2 import OrderRateRule, PriceCollarRule

HELD = Decimal("0.5")

#: Every rule the exemption touches, Tier-1 and Tier-2 alike.
METERING_RULES = [CooldownRule(), DailyTradeCapRule(), ConsecutiveLossRule()]

#: History that trips all three Tier-1 metering rules at once, and the hourly rate cap.
MAXED_OUT = TradingHistory(
    cycles_since_trade=0, trades_today=99, consecutive_losses=99, orders_last_hour=99
)


def proposal(
    instrument: Instrument,
    *,
    action: Action = Action.SELL,
    held: Decimal = HELD,
    operator: bool = False,
    history: TradingHistory = MAXED_OUT,
    price: Decimal = Decimal(50_000),
) -> RiskProposal:
    return RiskProposal(
        decision=Decision(
            instrument_key=instrument.key,
            action=action,
            conviction=Decimal(1),
            size_hint=SizeHint.FULL,
        ),
        instrument=instrument,
        policy=RiskPolicy(),
        position=Position(instrument_key=instrument.key, qty=held, avg_entry=Decimal(50_000)),
        price=price,
        last_price=Decimal(50_000),
        atr=Decimal(500),
        equity=Decimal(10_000),
        basket_budget=Decimal(1_000),
        basket_exposure=ZERO,
        history=history,
        operator_initiated=operator,
    )


# ---------------------------------------------------------------- the predicate


def test_an_operator_selling_a_held_position_is_an_exit(instrument: Instrument) -> None:
    assert proposal(instrument, operator=True).is_operator_exit


def test_the_panel_never_qualifies(instrument: Instrument) -> None:
    """The SELL test alone would exempt the panel's own churn — which cooldown exists to meter."""
    assert not proposal(instrument, operator=False).is_operator_exit


def test_an_operator_buying_does_not_qualify(instrument: Instrument) -> None:
    """`operator_initiated` alone would let a human open a position past the daily cap."""
    assert not proposal(instrument, action=Action.BUY, operator=True).is_operator_exit


def test_selling_while_flat_does_not_qualify(instrument: Instrument) -> None:
    """There is nothing to reduce, so nothing is risk-reducing about it."""
    assert not proposal(instrument, held=ZERO, operator=True).is_operator_exit


def test_the_flag_defaults_off(instrument: Instrument) -> None:
    """Anything that forgets to set it gets the metered behaviour, which is the safe default."""
    assert RiskProposal.model_fields["operator_initiated"].default is False


# ---------------------------------------------------------------- what stands aside


@pytest.mark.parametrize("rule", METERING_RULES, ids=lambda rule: str(rule.rule_id))
def test_a_metering_rule_vetoes_the_panel(rule: object, instrument: Instrument) -> None:
    result = rule.evaluate(proposal(instrument), HELD)  # type: ignore[attr-defined]
    assert result.decision is RiskDecision.VETO


@pytest.mark.parametrize("rule", METERING_RULES, ids=lambda rule: str(rule.rule_id))
def test_a_metering_rule_stands_aside_for_an_operator_exit(
    rule: object, instrument: Instrument
) -> None:
    result = rule.evaluate(proposal(instrument, operator=True), HELD)  # type: ignore[attr-defined]

    assert result.decision is RiskDecision.PASS
    assert result.max_qty == HELD


@pytest.mark.parametrize("rule", METERING_RULES, ids=lambda rule: str(rule.rule_id))
def test_standing_aside_is_recorded_not_silent(rule: object, instrument: Instrument) -> None:
    """The provenance is what makes this a decision by the risk layer rather than a bypass."""
    result = rule.evaluate(proposal(instrument, operator=True), HELD)  # type: ignore[attr-defined]
    assert result.detail == STOOD_ASIDE


def test_the_hourly_rate_cap_stands_aside_too(instrument: Instrument) -> None:
    rule = OrderRateRule(GlobalRiskPolicy())

    assert rule.evaluate(proposal(instrument), HELD).decision is RiskDecision.VETO

    stood_aside = rule.evaluate(proposal(instrument, operator=True), HELD)
    assert stood_aside.decision is RiskDecision.PASS
    assert stood_aside.detail == STOOD_ASIDE


# ---------------------------------------------------------------- what never does


def test_long_only_still_refuses_an_operator_selling_while_flat(instrument: Instrument) -> None:
    """The exemption may never open, enlarge or invert a position (R13)."""
    result = LongOnlyRule().evaluate(proposal(instrument, held=ZERO, operator=True), HELD)
    assert result.decision is RiskDecision.VETO


def test_long_only_still_clamps_an_operator_to_what_is_held(instrument: Instrument) -> None:
    result = LongOnlyRule().evaluate(proposal(instrument, operator=True), HELD * 10)
    assert result.max_qty == HELD


def test_the_price_collar_still_refuses_an_operators_fat_finger(instrument: Instrument) -> None:
    """Metering is exempt; a close priced far from the market is *wrong*, not merely metered."""
    rule = PriceCollarRule(GlobalRiskPolicy())
    result = rule.evaluate(proposal(instrument, operator=True, price=Decimal(90_000)), HELD)
    assert result.decision is RiskDecision.VETO


def test_venue_minimums_still_veto_an_operator_exit(
    instrument: Instrument, clock: ManualClock
) -> None:
    """A dust position cannot be closed, because the venue will not accept the order."""
    dust = proposal(instrument, held=Decimal("0.00001"), operator=True)

    outcome = Tier1RiskEngine(clock).approve(dust, mode=Mode.SIM, basket_id="b1", cycle_id="c1")

    assert outcome.intent is None
    assert "min_notional" in outcome.veto_reason


def test_the_conviction_floor_is_untouched_because_an_operator_is_certain(
    instrument: Instrument,
) -> None:
    """Not exempted, just never binding: the floor gates the panel, and a human rates 1.0."""
    result = MinConvictionRule().evaluate(proposal(instrument, operator=True), HELD)
    assert result.decision is RiskDecision.PASS


# ---------------------------------------------------------------- end to end


def test_a_maxed_out_operator_exit_is_approved(instrument: Instrument, clock: ManualClock) -> None:
    """Every metering limit breached at once, and the close still goes through."""
    outcome = Tier1RiskEngine(clock).approve(
        proposal(instrument, operator=True), mode=Mode.SIM, basket_id="b1", cycle_id="c1"
    )

    assert outcome.intent is not None
    assert outcome.intent.qty == HELD
    stood_aside = {c.rule for c in outcome.checks if c.detail == STOOD_ASIDE}
    assert stood_aside == {"cooldown", "max_trades_per_day", "max_consecutive_losses"}


def test_the_same_proposal_from_the_panel_is_vetoed(
    instrument: Instrument, clock: ManualClock
) -> None:
    """The control: identical state, no operator flag, and it is refused."""
    outcome = Tier1RiskEngine(clock).approve(
        proposal(instrument), mode=Mode.SIM, basket_id="b1", cycle_id="c1"
    )

    assert outcome.intent is None
    assert outcome.veto_reason
