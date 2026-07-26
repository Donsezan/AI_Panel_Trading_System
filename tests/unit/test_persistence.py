"""Event log and projections.

The load-bearing property: the log alone reconstructs the read model. If a projector only works
forwards, the audit guarantee is gone and nobody notices until it is needed.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from tradebot.core.enums import CycleOutcome, OrderState, OrderType, Side
from tradebot.core.errors import MoneyError, SingleWriterViolationError
from tradebot.core.events import Event, EventFactory, EventType
from tradebot.core.orders import Fill, Order, OrderIntent
from tradebot.core.portfolio import Position
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.schema import cycles, fills, orders, positions
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 3, 1, tzinfo=UTC)
KEY = "sim:BTC/USDT"


def intent(clock: object) -> OrderIntent:
    return OrderIntent(
        client_order_id="sim-ABCDEF",
        basket_id="b1",
        cycle_id="c1",
        instrument_key=KEY,
        side=Side.BUY,
        qty=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("50000"),
        created_at=NOW,
    )


def fill_of(qty: str = "0.5") -> Fill:
    return Fill(
        fill_id="fill-1",
        client_order_id="sim-ABCDEF",
        instrument_key=KEY,
        side=Side.BUY,
        qty=Decimal(qty),
        price=Decimal("50000"),
        fee=Decimal("25"),
        fee_currency="USDT",
        filled_at=NOW,
    )


async def write_a_full_cycle(store: EventStore, clock: object) -> EventFactory:
    events = EventFactory(clock=clock, basket_id="b1", cycle_id="c1")  # type: ignore[arg-type]
    order = Order.from_intent(intent(clock))
    submitted = order.transition_to(OrderState.SUBMITTED, at=NOW)
    filled = submitted.with_fill(fill_of())

    await store.append(events.cycle_started())
    await store.append(events.order_submitted(order))
    await store.append(events.order_state_changed(submitted, OrderState.PENDING_SUBMIT))
    await store.append(events.fill_received(fill_of(), filled))
    await store.append(
        events.position_updated(
            Position(instrument_key=KEY, qty=Decimal("0.5"), avg_entry=Decimal("50000"))
        )
    )
    await store.append(
        events.risk_event(
            tier="tier2", rule="max_drawdown", scope="portfolio", action="halt", detail="test"
        )
    )
    await store.append(events.cycle_completed(CycleOutcome.ORDERS_PLACED, Decimal("0.02")))
    return events


class TestAppend:
    async def test_events_receive_a_monotonic_sequence(
        self, store: EventStore, clock: object
    ) -> None:
        events = EventFactory(clock=clock, basket_id="b1", cycle_id="c1")  # type: ignore[arg-type]
        stored = await store.append(events.cycle_started(), events.cycle_started())
        assert [event.seq for event in stored] == [1, 2]

    async def test_appending_nothing_is_a_no_op(self, store: EventStore) -> None:
        assert await store.append() == ()
        assert store.count() == 0

    async def test_the_log_is_the_ordered_record_of_a_cycle(
        self, store: EventStore, clock: object
    ) -> None:
        await write_a_full_cycle(store, clock)
        assert store.event_types("c1") == (
            EventType.CYCLE_STARTED,
            EventType.ORDER_SUBMITTED,
            EventType.ORDER_STATE_CHANGED,
            EventType.FILL_RECEIVED,
            EventType.POSITION_UPDATED,
            EventType.RISK_EVENT,
            EventType.CYCLE_COMPLETED,
        )

    async def test_payloads_survive_a_round_trip_exactly(
        self, store: EventStore, clock: object
    ) -> None:
        await write_a_full_cycle(store, clock)
        fill_event = next(e for e in store.read_all() if e.type is EventType.FILL_RECEIVED)
        assert fill_event.payload["fill"]["qty"] == "0.5"
        assert fill_event.payload["fill"]["price"] == "50000"


class TestProjections:
    async def test_an_order_projects_with_its_fills_applied(
        self, store: EventStore, clock: object
    ) -> None:
        await write_a_full_cycle(store, clock)
        with store.engine.connect() as connection:
            row = connection.execute(select(orders)).one()
        assert row.state == OrderState.FILLED.value
        assert row.filled_qty == Decimal("0.5")
        assert row.avg_fill_price == Decimal("50000")

    async def test_money_columns_come_back_as_exact_decimals(
        self, store: EventStore, clock: object
    ) -> None:
        """TEXT storage, not SQLite NUMERIC — the latter round-trips through a float."""
        await write_a_full_cycle(store, clock)
        with store.engine.connect() as connection:
            row = connection.execute(select(fills)).one()
        assert isinstance(row.qty, Decimal)
        assert row.fee == Decimal("25")

    async def test_cycle_summary_records_the_outcome_and_cost(
        self, store: EventStore, clock: object
    ) -> None:
        await write_a_full_cycle(store, clock)
        with store.engine.connect() as connection:
            row = connection.execute(select(cycles)).one()
        assert row.outcome == CycleOutcome.ORDERS_PLACED.value
        assert row.cost_usd == Decimal("0.02")
        assert row.completed_at is not None

    async def test_audit_only_events_have_no_projection_by_design(
        self, store: EventStore, clock: object
    ) -> None:
        events = EventFactory(clock=clock, basket_id="b1", cycle_id="c1")  # type: ignore[arg-type]
        await store.append(events.risk_checked(KEY, (), approved=True))
        assert store.count() == 1  # recorded in the log, nowhere else


class TestReplay:
    async def test_replaying_the_log_reproduces_identical_projections(
        self, store: EventStore, clock: object
    ) -> None:
        """The audit guarantee, asserted rather than assumed."""
        await write_a_full_cycle(store, clock)

        def read_all() -> dict[str, list[tuple[object, ...]]]:
            with store.engine.connect() as connection:
                return {
                    table.name: [tuple(row) for row in connection.execute(select(table))]
                    for table in (cycles, orders, fills, positions)
                }

        before = read_all()
        replayed = await store.rebuild()
        assert replayed == store.count()
        assert read_all() == before

    async def test_rebuilding_an_empty_log_is_safe(self, store: EventStore) -> None:
        assert await store.rebuild() == 0


class TestSingleWriter:
    async def test_a_write_from_another_thread_is_refused(self) -> None:
        """Enforced by assertion on the writer thread, not by convention (PLAN §2.6)."""
        engine = create_database(None)
        writer = SingleWriter(engine)
        try:
            failure: list[BaseException] = []

            def offend() -> None:
                try:
                    writer._execute(lambda _connection: None)
                except BaseException as exc:
                    failure.append(exc)

            thread = threading.Thread(target=offend, name="not-the-writer")
            thread.start()
            thread.join()
            assert isinstance(failure[0], SingleWriterViolationError)
        finally:
            writer.close()


class TestTypeGuards:
    async def test_a_float_cannot_reach_a_money_column(self, store: EventStore) -> None:
        """The last line of the float ban, at the database boundary."""
        with store.engine.begin() as connection, pytest.raises(Exception, match="float"):
            connection.execute(
                positions.insert().values(
                    instrument_key=KEY,
                    qty=0.1,
                    avg_entry=Decimal(1),
                    realized_pnl=Decimal(0),
                    held_cycles=0,
                    updated_at=NOW,
                )
            )

    async def test_a_naive_datetime_cannot_reach_a_time_column(self, store: EventStore) -> None:
        with store.engine.begin() as connection, pytest.raises(Exception, match="naive"):
            connection.execute(
                positions.insert().values(
                    instrument_key=KEY,
                    qty=Decimal(1),
                    avg_entry=Decimal(1),
                    realized_pnl=Decimal(0),
                    held_cycles=0,
                    updated_at=datetime(2026, 3, 1, 12, 0),
                )
            )


class TestEventModel:
    def test_sequencing_returns_a_stamped_copy(self) -> None:
        event = Event(ts=NOW, type=EventType.CYCLE_STARTED, aggregate_id="c1")
        assert event.seq is None
        assert event.sequenced(7).seq == 7

    def test_payload_json_is_canonical(self) -> None:
        event = Event(
            ts=NOW, type=EventType.CYCLE_STARTED, aggregate_id="c1", payload={"b": 1, "a": 2}
        )
        assert event.payload_json == '{"a":2,"b":1}'


def test_money_error_is_raised_for_floats_in_to_decimal() -> None:
    with pytest.raises(MoneyError):
        from tradebot.core.money import to_decimal

        to_decimal(1.5)  # type: ignore[arg-type]
