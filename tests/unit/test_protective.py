"""Protective legs: sized to what filled, and never two unlinked exits on one holding.

Both rules exist because the failure is silent. A leg sized to the *order* after a half fill
tries to sell what is not held; two unlinked exits both fill and the second one sells a position
that is already gone — a short, in a long-only system.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.enums import OrderRole, OrderState, OrderType, Side
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.orders import Fill, Order, OrderIntent, ProtectivePlan
from tradebot.execution.protective import plan_legs
from tradebot.interfaces.broker import BrokerCapabilities

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
PLAN = ProtectivePlan(
    stop_price=Decimal("48000"),
    take_profit_price=Decimal("54000"),
    limit_offset_pct=Decimal("0.5"),
)


def capabilities(*, protective: bool = True, oco: bool = True) -> BrokerCapabilities:
    return BrokerCapabilities(
        venue_id="sim",
        order_types=tuple(OrderType),
        protective_orders=protective,
        oco_groups=oco,
    )


def entry(
    instrument: Instrument,
    *,
    qty: str = "0.5",
    filled: str | None = "0.5",
    plan: ProtectivePlan | None = PLAN,
) -> Order:
    order = Order.from_intent(
        OrderIntent(
            client_order_id="sim-ENTRY",
            basket_id="b1",
            cycle_id="c1",
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=Decimal(qty),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("50000"),
            protective=plan,
            created_at=NOW,
        )
    ).transition_to(OrderState.SUBMITTED, at=NOW)
    if filled is None:
        return order
    return order.with_fill(
        Fill(
            fill_id="f1",
            client_order_id="sim-ENTRY",
            instrument_key=instrument.key,
            side=Side.BUY,
            qty=Decimal(filled),
            price=Decimal("50000"),
            filled_at=NOW,
        )
    )


class TestSizing:
    def test_legs_guard_the_quantity_they_are_given_not_the_entry_fill(
        self, instrument: Instrument
    ) -> None:
        """Design §2: the caller is the only thing that can see the position.

        Sizing from `entry.filled_qty` here is KNOWN_GAPS §4 one level down — the decision made in
        the one place with no view of what is actually held.
        """
        plan = plan_legs(
            entry(instrument, qty="0.5", filled="0.5"),
            instrument,
            capabilities(),
            at=NOW,
            qty=Decimal("0.2"),
        )

        assert plan.protected
        assert {leg.qty for leg in plan.intents} == {Decimal("0.2")}

    def test_legs_guard_what_filled_not_what_was_ordered(self, instrument: Instrument) -> None:
        """A leg for the full order after a half fill tries to sell what is not held."""
        order = entry(instrument, qty="0.5", filled="0.2")
        plan = plan_legs(order, instrument, capabilities(), at=NOW, qty=order.filled_qty)

        assert plan.protected
        assert {leg.qty for leg in plan.intents} == {Decimal("0.2")}

    def test_a_zero_quantity_has_nothing_to_protect(self, instrument: Instrument) -> None:
        plan = plan_legs(
            entry(instrument, filled=None), instrument, capabilities(), at=NOW, qty=ZERO
        )

        assert not plan.protected
        assert "no quantity to protect" in plan.unprotected_reason

    def test_legs_below_a_venue_minimum_are_reported_not_silently_skipped(
        self, instrument: Instrument
    ) -> None:
        """The operator must be able to see that the guard is missing."""
        tiny = instrument.model_copy(update={"min_notional": Decimal("1000000")})

        plan = plan_legs(
            entry(tiny, filled="0.5"), tiny, capabilities(), at=NOW, qty=Decimal("0.5")
        )

        assert not plan.protected
        assert "below venue minimums" in plan.unprotected_reason


class TestPlacement:
    def test_both_legs_are_placed_where_the_venue_links_them(self, instrument: Instrument) -> None:
        plan = plan_legs(entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"))

        assert {leg.role for leg in plan.intents} == {OrderRole.STOP_LOSS, OrderRole.TAKE_PROFIT}
        assert {leg.side for leg in plan.intents} == {Side.SELL}
        assert {leg.group_id for leg in plan.intents} == {"sim-ENTRY"}

    def test_only_the_stop_is_placed_without_venue_side_oco(self, instrument: Instrument) -> None:
        """Two unlinked exits can both fill; the second sells a position that is already gone."""
        plan = plan_legs(
            entry(instrument), instrument, capabilities(oco=False), at=NOW, qty=Decimal("0.5")
        )

        assert [leg.role for leg in plan.intents] == [OrderRole.STOP_LOSS]

    def test_a_venue_that_holds_no_stops_leaves_the_position_flagged(
        self, instrument: Instrument
    ) -> None:
        plan = plan_legs(
            entry(instrument),
            instrument,
            capabilities(protective=False),
            at=NOW,
            qty=Decimal("0.5"),
        )

        assert not plan.protected
        assert "holds no protective orders" in plan.unprotected_reason

    def test_an_entry_without_a_plan_is_not_guessed_at(self, instrument: Instrument) -> None:
        plan = plan_legs(
            entry(instrument, plan=None), instrument, capabilities(), at=NOW, qty=Decimal("0.5")
        )

        assert not plan.protected


class TestPrices:
    def test_the_trigger_sits_on_a_venue_tick(self, instrument: Instrument) -> None:
        """An off-tick trigger is rejected by the venue, leaving the position unguarded."""
        odd = ProtectivePlan(stop_price=Decimal("48000.123456789"), take_profit_price=None)

        plan = plan_legs(
            entry(instrument, plan=odd),
            instrument,
            capabilities(oco=False),
            at=NOW,
            qty=Decimal("0.5"),
        )

        stop = plan.intents[0]
        assert stop.stop_price is not None
        assert stop.stop_price % instrument.tick_size == 0

    def test_the_stop_limit_sits_through_its_trigger_so_it_can_fill(
        self, instrument: Instrument
    ) -> None:
        """A sell limit resting *above* a falling market is a stop that never executes."""
        plan = plan_legs(entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"))

        stop = next(leg for leg in plan.intents if leg.role is OrderRole.STOP_LOSS)
        assert stop.limit_price is not None and stop.stop_price is not None
        assert stop.limit_price < stop.stop_price

    def test_the_take_profit_limit_also_sits_through_its_trigger(
        self, instrument: Instrument
    ) -> None:
        """Both exits cross on trigger. A take-profit that triggers and never fills is not a
        conservative take-profit, it is a missing one — and it leaves the OCO group unresolved."""
        plan = plan_legs(entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"))

        target = next(leg for leg in plan.intents if leg.role is OrderRole.TAKE_PROFIT)
        assert target.limit_price is not None and target.stop_price is not None
        assert target.limit_price < target.stop_price


class TestIdentity:
    def test_leg_ids_are_derived_from_the_entry_and_stable(self, instrument: Instrument) -> None:
        """Recovery must be able to find a leg at the venue without having stored its id."""
        first = plan_legs(entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"))
        again = plan_legs(entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"))

        assert [leg.client_order_id for leg in first.intents] == [
            leg.client_order_id for leg in again.intents
        ]

    def test_a_replacement_revision_mints_different_ids(self, instrument: Instrument) -> None:
        """No venue lets a resting order's quantity be edited, so a resize is a new order."""
        first = plan_legs(
            entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"), revision=0
        )
        second = plan_legs(
            entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"), revision=1
        )

        assert not {leg.client_order_id for leg in first.intents} & {
            leg.client_order_id for leg in second.intents
        }

    def test_leg_ids_keep_the_entrys_mode_prefix(self, instrument: Instrument) -> None:
        """A leg is provably ours — and provably this mode's — by the same test as its entry."""
        plan = plan_legs(entry(instrument), instrument, capabilities(), at=NOW, qty=Decimal("0.5"))

        assert all(leg.client_order_id.startswith("sim-") for leg in plan.intents)


@pytest.mark.parametrize("role", list(OrderRole))
def test_every_role_maps_to_an_order_type(role: OrderRole) -> None:
    """A role with no order type would fall through to an entry, sending an unguarded order."""
    assert isinstance(role.order_type, OrderType)
