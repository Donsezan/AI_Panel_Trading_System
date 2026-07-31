"""The read-only query layer, and the two places it could quietly corrupt a number.

`test_cost_totals_never_pass_through_a_float` is the load-bearing one: SQLite's `SUM` over the
TEXT columns money is stored in converts through an IEEE-754 double, which is exactly what
`DecimalText` exists to prevent. Totalling in Python is not a style choice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.app import Application
from tradebot.core.config import ConfigRef
from tradebot.core.enums import ConfigKind, CycleOutcome
from tradebot.core.events import EventType
from tradebot.dashboard.queries import Queries, parse_pins


@pytest.fixture
def queries(sim_application: Application) -> Queries:
    return Queries(sim_application.store)


@pytest.fixture
async def cycled(sim_application: Application) -> Application:
    await sim_application.recover()
    results = await sim_application.supervisor.run_once()
    assert results and results[0].outcome is CycleOutcome.ORDERS_PLACED
    return sim_application


# ---------------------------------------------------------------- pins


def test_pins_parse_into_refs() -> None:
    refs = parse_pins('{"basket:demo": 4, "global_risk:global": 2}')
    assert refs == (
        ConfigRef(kind=ConfigKind.BASKET, config_id="demo", version=4),
        ConfigRef(kind=ConfigKind.GLOBAL_RISK, config_id="global", version=2),
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "{}",
        "not json at all",
        '{"basket:demo": "four"}',  # a version that is not a number
        '{"nosuchkind:demo": 1}',  # a kind this build does not know
        '{"basket": 1}',  # no config id
    ],
)
def test_unreadable_pins_yield_nothing_rather_than_raising(raw: str | None) -> None:
    """One cycle's configuration becomes unresolvable; the rest of its audit trail still shows."""
    assert parse_pins(raw) == ()


# ---------------------------------------------------------------- money


async def test_cost_totals_never_pass_through_a_float(cycled: Application) -> None:
    """Totalled in Python because SQL `SUM` over a TEXT money column rounds through a double."""
    rows = Queries(cycled.store).cost_by_basket()
    assert [row.basket_id for row in rows] == ["demo"]
    assert isinstance(rows[0].total_cost, Decimal)
    assert isinstance(rows[0].per_cycle, Decimal)
    assert rows[0].cycle_count == 1


def test_cost_of_a_system_that_never_cycled_is_empty(queries: Queries) -> None:
    assert queries.cost_by_basket() == ()


async def test_equity_curve_accumulates_from_the_opening_figure(cycled: Application) -> None:
    curve = Queries(cycled.store).equity_curve(opening_equity=Decimal(10_000))
    running = Decimal(10_000)
    for point in curve:
        running += point.realized_pnl
        assert point.cumulative == running
        assert isinstance(point.cumulative, Decimal)


def test_equity_curve_is_empty_before_any_round_trip_closes(queries: Queries) -> None:
    assert queries.equity_curve() == ()


# ---------------------------------------------------------------- cycle detail


async def test_cycle_detail_carries_projections_and_the_audit_log(cycled: Application) -> None:
    queries = Queries(cycled.store)
    cycle_id = queries.cycles()[0].cycle_id
    detail = queries.cycle(cycle_id)

    assert detail is not None
    assert detail.decisions
    assert detail.orders
    assert detail.snapshot is not None
    assert detail.pins
    assert detail.events_of(EventType.SEAT_RESPONDED)
    # StrEnum, so a template may ask by name without importing the enum.
    assert detail.events_of("SEAT_RESPONDED") == detail.events_of(EventType.SEAT_RESPONDED)


def test_unknown_cycle_is_none(queries: Queries) -> None:
    assert queries.cycle("not-a-cycle") is None


async def test_reading_one_cycle_does_not_read_the_whole_log(cycled: Application) -> None:
    """A soak accumulates months of snapshots; a drill-down must cost one cycle, not one DB."""
    cycle_id = Queries(cycled.store).cycles()[0].cycle_id
    scoped = cycled.store.read_cycle(cycle_id)

    assert scoped
    assert all(event.cycle_id == cycle_id for event in scoped)
    assert len(scoped) < len(cycled.store.read_all())


async def test_open_orders_exclude_terminal_ones(cycled: Application) -> None:
    queries = Queries(cycled.store)
    open_states = {row.state for row in queries.open_orders()}
    assert open_states <= {"submitted", "open", "partially_filled"}
    assert queries.orders()


async def test_cycles_can_be_filtered_by_basket(cycled: Application) -> None:
    queries = Queries(cycled.store)
    assert queries.cycles(basket_id="demo")
    assert queries.cycles(basket_id="no-such-basket") == ()
