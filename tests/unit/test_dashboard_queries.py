"""The read-only query layer, and the two places it could quietly corrupt a number.

`test_cost_totals_never_pass_through_a_float` is the load-bearing one: SQLite's `SUM` over the
TEXT columns money is stored in converts through an IEEE-754 double, which is exactly what
`DecimalText` exists to prevent. Totalling in Python is not a style choice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.app import Application
from tradebot.core.clock import ManualClock
from tradebot.core.config import ConfigRef
from tradebot.core.enums import ConfigKind, CycleOutcome
from tradebot.core.events import EventType
from tradebot.dashboard.queries import Queries, parse_pins
from tradebot.dashboard.scope import Scope


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


# ---------------------------------------------------------------- the workspace's reads


def instrument_of(cycled: Application) -> str:
    return Queries(cycled.store).positions()[0].instrument_key


async def test_latest_decisions_returns_one_row_per_instrument(cycled: Application) -> None:
    """The blotter draws one line per instrument, so this must not fan out over history."""
    rows = Queries(cycled.store).latest_decisions()
    keys = [(row.basket_id, row.instrument_key) for row in rows]
    assert keys
    assert len(keys) == len(set(keys))


async def test_latest_decisions_keeps_only_the_newest(
    cycled: Application, clock: ManualClock
) -> None:
    """A second cycle must move the blotter row, not add one beside it."""
    before = Queries(cycled.store).latest_decisions()
    clock.advance(3600)
    await cycled.supervisor.run_once()
    after = Queries(cycled.store).latest_decisions()

    assert len(after) == len(before)
    assert [row.decided_at for row in after] > [row.decided_at for row in before]
    assert {row.cycle_id for row in after}.isdisjoint({row.cycle_id for row in before})


async def test_latest_decisions_are_stable_when_the_clock_did_not_move(
    cycled: Application,
) -> None:
    """A replay or a frozen clock can tie `decided_at`; a blotter row must not flicker."""
    queries = Queries(cycled.store)
    await cycled.supervisor.run_once()
    assert queries.latest_decisions() == queries.latest_decisions()


async def test_latest_decisions_carries_the_basket_that_decided(cycled: Application) -> None:
    """Two baskets may hold one instrument; a row must say whose panel it reports."""
    assert {row.basket_id for row in Queries(cycled.store).latest_decisions()} == {"demo"}


def test_latest_decisions_of_a_system_that_never_cycled_is_empty(queries: Queries) -> None:
    assert queries.latest_decisions() == ()


async def test_activity_pairs_a_cycle_with_its_decision(cycled: Application) -> None:
    rows = Queries(cycled.store).activity()
    assert rows
    assert all(row.cycle_id for row in rows)
    assert any(row.action for row in rows)


async def test_activity_narrows_to_a_basket(cycled: Application) -> None:
    queries = Queries(cycled.store)
    assert queries.activity(Scope("demo"))
    assert queries.activity(Scope("no-such-basket")) == ()


async def test_activity_narrows_to_an_instrument(cycled: Application) -> None:
    rows = Queries(cycled.store).activity(Scope("demo", instrument_of(cycled)))
    assert rows
    assert {row.instrument_key for row in rows} == {instrument_of(cycled)}


async def test_activity_keeps_a_cycle_that_decided_nothing_for_the_selection(
    cycled: Application,
) -> None:
    """The instrument filter is in the join, not the `WHERE`: a cycle that reached no decision
    here — `DATA_STALE`, `QUARANTINED`, a degraded panel — must still appear, with an empty
    decision. A basket that stops appearing is a basket nobody can audit (ADR 0022)."""
    queries = Queries(cycled.store)
    rows = queries.activity(Scope("demo", "binance:NOTHING-HELD"))

    assert len(rows) == len(queries.cycles(basket_id="demo"))
    assert {row.instrument_key for row in rows} == {None}
    assert all(row.outcome for row in rows), "the cycle's own columns are still populated"


async def test_activity_is_newest_first(cycled: Application) -> None:
    await cycled.supervisor.run_once()
    started = [row.started_at for row in Queries(cycled.store).activity()]
    assert started == sorted(started, reverse=True)


async def test_day_realized_totals_only_what_closed_since_the_boundary(
    cycled: Application,
) -> None:
    """Takes the boundary rather than computing one, so it matches the daily-loss rule's day."""
    queries = Queries(cycled.store)
    trips = queries.round_trips()

    since_epoch = queries.day_realized(datetime(2000, 1, 1, tzinfo=UTC))
    assert since_epoch == sum((row.realized_pnl for row in trips), Decimal(0))
    assert isinstance(since_epoch, Decimal)

    assert queries.day_realized(datetime(2100, 1, 1, tzinfo=UTC)) == Decimal(0)


def test_day_realized_of_a_flat_day_is_zero_not_none(queries: Queries) -> None:
    """A day with nothing closed is zero; an absent number would render as an empty cell."""
    assert queries.day_realized(datetime(2000, 1, 1, tzinfo=UTC)) == Decimal(0)


async def test_chart_windows_return_one_instrument_from_a_moment(cycled: Application) -> None:
    queries = Queries(cycled.store)
    key = instrument_of(cycled)
    epoch = datetime(2000, 1, 1, tzinfo=UTC)

    assert {row.instrument_key for row in queries.orders_in(key, since=epoch)} == {key}
    assert {row.instrument_key for row in queries.fills_in(key, since=epoch)} == {key}


async def test_chart_windows_exclude_what_happened_before_them(cycled: Application) -> None:
    """The window is a filter, not a decoration: a chart must not draw off-screen markers."""
    queries = Queries(cycled.store)
    key = instrument_of(cycled)
    future = datetime(2100, 1, 1, tzinfo=UTC)

    assert queries.orders_in(key, since=future) == ()
    assert queries.fills_in(key, since=future) == ()


async def test_decisions_in_a_window_belong_to_the_basket_that_made_them(
    cycled: Application,
) -> None:
    """Two baskets may hold one instrument; a chart of one must not carry the other's marks."""
    queries = Queries(cycled.store)
    key = instrument_of(cycled)
    epoch = datetime(2000, 1, 1, tzinfo=UTC)

    assert queries.decisions_in(Scope("demo", key), since=epoch)
    assert queries.decisions_in(Scope("elsewhere", key), since=epoch) == ()


async def test_decisions_in_a_window_exclude_what_came_before_it(cycled: Application) -> None:
    future = datetime(2100, 1, 1, tzinfo=UTC)
    assert (
        Queries(cycled.store).decisions_in(Scope("demo", instrument_of(cycled)), since=future) == ()
    )


async def test_counters_are_per_basket_since_the_boundary(cycled: Application) -> None:
    queries = Queries(cycled.store)
    epoch = datetime(2000, 1, 1, tzinfo=UTC)

    assert queries.cycles_since(epoch)["demo"] == 1
    assert queries.entry_orders_since(epoch)["demo"] >= 1


async def test_counters_count_entries_only(cycled: Application) -> None:
    """The rule the daily cap meters by: a protective leg belongs to the decision that placed it,
    not to a second trade (`HistoryReader`)."""
    queries = Queries(cycled.store)
    epoch = datetime(2000, 1, 1, tzinfo=UTC)
    entries = queries.entry_orders_since(epoch)["demo"]

    assert entries < len(queries.orders())


async def test_counters_of_a_quiet_period_are_absent_not_zero(cycled: Application) -> None:
    """An absent key is absent. A fabricated zero would be a claim nothing happened."""
    queries = Queries(cycled.store)
    future = datetime(2100, 1, 1, tzinfo=UTC)

    assert queries.cycles_since(future) == {}
    assert queries.entry_orders_since(future) == {}
