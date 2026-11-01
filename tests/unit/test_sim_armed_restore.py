"""Stating which stops had triggered, across a restart of the simulated venue.

Arming is memory. A stop whose trigger was crossed by a bar wholly below its limit is armed and
unfilled, and it fills on the next bar reaching its limit whether or not that bar crosses the
trigger again. Nothing in the log says so, so `restore` cannot derive it, and a replay that must
match an unbroken path has to state it.

Three things are held here: the set is honoured; a stale id does nothing; and a restart that states
nothing behaves exactly as one always has. The refusals are `news_feed`'s two closures again (see
`test_news_feed_seam.py`), for the same reason: a soak's restart must stay an ordinary restart.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot import app as app_module
from tradebot.app import (
    BrokerChoice,
    PriceFeed,
    StackRequest,
    _assemble,
    _sim_stack,
    build,
    build_live,
    build_paper,
    build_sim,
)
from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode, OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import ConfigError
from tradebot.core.orders import Order, OrderIntent
from tradebot.core.portfolio import AccountState, Balance, Position
from tradebot.execution.brokers.sim import SimBroker, Tick

NOW = datetime(2024, 3, 1, tzinfo=UTC)
KEY = "binance:BTC/USDT"
STOP = "sim-STOP"


def stop_intent(
    *, role: OrderRole = OrderRole.STOP_LOSS, client_order_id: str = STOP
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        basket_id="b1",
        cycle_id="c1",
        instrument_key=KEY,
        side=Side.SELL,
        qty=Decimal("0.1"),
        order_type=OrderType.STOP_LOSS_LIMIT,
        limit_price=Decimal(47_000),
        stop_price=Decimal(48_000),
        role=role,
        group_id="g1",
        created_at=NOW,
    )


def bar(price: str, *, at: datetime) -> Tick:
    value = Decimal(price)
    return Tick(
        instrument_key=KEY,
        bid=value,
        ask=value,
        last=value,
        high=value,
        low=value,
        covers_since=at,
        observed_at=at,
    )


#: Wholly below the limit: crosses the trigger, cannot trade through the limit.
THROUGH_THE_LIMIT = bar("46000", at=NOW + timedelta(minutes=1))
#: Wholly above the trigger: fills an armed stop at its limit, and does nothing to an un-armed one.
BACK_ABOVE = bar("49000", at=NOW + timedelta(minutes=2))


def restored(order: Order, **options: Any) -> SimBroker:
    broker = SimBroker(ManualClock(NOW), **options)
    broker.restore(
        AccountState(
            venue="sim",
            positions=(Position(instrument_key=KEY, qty=order.qty, avg_entry=Decimal(50_000)),),
            balances=(Balance(currency="BTC", free=order.qty, locked=Decimal(0)),),
            observed_at=NOW,
        ),
        (order,),
    )
    return broker


def open_stop(intent: OrderIntent | None = None) -> Order:
    order = Order.from_intent(intent or stop_intent())
    return order.model_copy(update={"state": OrderState.OPEN})


async def test_armed_orders_names_a_triggered_unfilled_stop_and_then_forgets_it() -> None:
    broker = SimBroker(ManualClock(NOW), balances={"BTC": Decimal("0.1")})
    await broker.submit(stop_intent())
    assert broker.armed_orders() == frozenset()

    broker.observe(THROUGH_THE_LIMIT)
    assert broker.armed_orders() == frozenset({STOP})

    assert broker.observe(BACK_ABOVE)
    assert broker.armed_orders() == frozenset(), "a filled order is no longer armed"


def test_restore_honours_a_stated_set_so_the_stop_fills_without_a_second_crossing() -> None:
    broker = restored(open_stop(), armed_on_restore=frozenset({STOP}))

    assert broker.armed_orders() == frozenset({STOP})
    fills = broker.observe(BACK_ABOVE)
    assert [fill.price for fill in fills] == [Decimal(47_000)]


def test_a_restart_stating_nothing_behaves_as_it_always_has() -> None:
    """Un-armed, so the same bar does nothing, and only a second crossing can arm it."""
    broker = restored(open_stop())

    assert broker.armed_orders() == frozenset()
    assert broker.observe(BACK_ABOVE) == ()
    broker.observe(bar("46000", at=NOW + timedelta(minutes=3)))
    assert broker.armed_orders() == frozenset({STOP})


def test_a_stale_id_naming_no_open_order_has_no_effect() -> None:
    broker = restored(open_stop(), armed_on_restore=frozenset({"sim-GONE"}))

    assert broker.armed_orders() == frozenset()
    assert broker.observe(BACK_ABOVE) == ()


def test_a_stated_id_cannot_arm_what_is_not_protective() -> None:
    entry = stop_intent(role=OrderRole.ENTRY, client_order_id="sim-ENTRY")
    broker = restored(open_stop(entry), armed_on_restore=frozenset({"sim-ENTRY"}))

    assert broker.armed_orders() == frozenset()


def test_the_sim_stack_passes_the_set_to_its_broker_and_reads_it_back() -> None:
    clock = ManualClock(NOW)
    stack = _sim_stack(
        StackRequest(
            universe=tuple,
            clock=clock,
            mode=Mode.SIM,
            feed=PriceFeed(),
            start_equity=Decimal(10_000),
            quote_currency="USDT",
            armed_orders=frozenset({STOP}),
        )
    )
    assert stack.venue_restore is not None
    stack.venue_restore.restore(
        AccountState(venue="sim", positions=(), balances=(), observed_at=NOW), (open_stop(),)
    )

    assert stack.armed_orders() == frozenset({STOP})


async def test_a_wired_application_reads_empty_with_nothing_armed() -> None:
    application = await build_sim(clock=ManualClock(NOW))
    try:
        assert application.armed_orders() == frozenset()
    finally:
        await application.shutdown()


def test_build_paper_and_build_live_carry_no_armed_orders_parameter() -> None:
    for builder in (build_paper, build_live):
        assert "armed_orders" not in inspect.signature(builder).parameters, builder.__name__


def test_build_sim_does_carry_it() -> None:
    """The guard above must not pass by the parameter simply never having been added."""
    assert "armed_orders" in inspect.signature(build_sim).parameters


def _explode(*_: object, **__: object) -> Any:
    raise AssertionError("the refusal must come before anything is built")


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_assemble_refuses_a_stated_set_outside_sim(
    mode: Mode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "create_database", _explode)

    with pytest.raises(ConfigError, match=mode.value):
        await _assemble(
            ManualClock(NOW),
            mode=mode,
            db_path=None,
            broker_choice=BrokerChoice.SIM,
            start_equity=Decimal(10_000),
            baskets=None,
            global_policy=None,
            feed=PriceFeed(),
            news_sources=(),
            panel_id="stub",
            armed_orders=frozenset({STOP}),
        )


@pytest.mark.parametrize("mode", [Mode.PAPER, Mode.LIVE])
async def test_the_dispatch_door_is_shut_too(mode: Mode) -> None:
    with pytest.raises(TypeError):
        await build(mode, armed_orders=frozenset({STOP}))
