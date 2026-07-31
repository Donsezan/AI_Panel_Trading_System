"""One contract suite, run against every `BrokerAdapter` (rung 2, PLAN §7). Phase 5's exit gate.

DESIGN [L11] says to budget most engineering effort at this layer, and this is why: the *only*
thing that makes a paper result predictive of live behaviour is that every adapter honours
identical semantics. An adapter that diverges here fails CI.

Each adapter is driven through its own wire format — Binance's `executedQty` strings through a
signed-transport fake, Alpaca's JSON through the real `AlpacaTransport` over
`httpx.MockTransport`, `SimBroker` through its own book. A mock returning `OrderStatus` objects
would prove nothing about any of them.

What is asserted, and which failure each one is standing in front of:

* **submit → ack → query by our own id.** Without this, `SUBMIT_UNKNOWN` has no resolution (§2.3).
* **partial fills, from fills only.** Positions may never be reconstructed from an order state
  reaching a terminal value (§2.5).
* **an ambiguous submit raises `SubmitUnknownError`, and the order is still findable.** The
  duplicate-order-after-retry failure that dominates incident reports (R1).
* **a vanished order reports `found=False`, not a rejection.** They demand opposite handling.
* **a rejection is a result, not an exception.**
* **a cancel race is reported, not raised.** Losing a cancel to a fill is the normal case.
* **linked exit legs: one fills, the venue cancels its sibling.** Two live exits over one holding
  is a double sell, which in a long-only system is an accidental short (DESIGN §6.7, R13).
* **the id scheme fits the venue's cap** (PLAN §5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.fake_venues import FakeAlpacaApi, FakeBinanceTransport, FakeVenueBook, alpaca_transport

from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode, OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import SubmitUnknownError
from tradebot.core.ids import client_order_id, protective_order_id
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
from tradebot.core.orders import OrderIntent
from tradebot.execution.brokers.alpaca import AlpacaBroker
from tradebot.execution.brokers.binance import BinanceSpotBroker
from tradebot.execution.brokers.sim import SimBroker, Tick
from tradebot.interfaces.broker import BrokerAdapter, OrderRef

pytestmark = pytest.mark.contract

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
ENTRY_PRICE = Decimal("100")
QTY = Decimal("2")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


def crypto(symbol: str = "BTC/USDT") -> Instrument:
    from tradebot.core.enums import AssetClass

    return Instrument(
        symbol=symbol,
        venue="binance",
        asset_class=AssetClass.CRYPTO,
        base_currency=symbol.split("/")[0],
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )


def equity(symbol: str = "AAPL") -> Instrument:
    from tradebot.core.enums import AssetClass

    return Instrument(
        symbol=symbol,
        venue="alpaca",
        asset_class=AssetClass.EQUITY,
        base_currency=symbol,
        quote_currency="USD",
        lot_size=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.001"),
        min_notional=Decimal(1),
    )


class Venue:
    """One adapter plus the handles a test needs to drive the venue behind it.

    The suite never touches adapter internals: it fills, rejects and breaks orders through the
    fake venue, exactly as reality would, and then asks the adapter what it now believes. Filling
    is expressed as *partly* or *fully* rather than as a quantity, because that is the vocabulary
    every venue can honour — a simulated venue fills by moving the market, not by decree.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        instrument: Instrument,
        book: FakeVenueBook,
        *,
        fill: Callable[[str, Decimal, Decimal], None],
        break_next_submit: Callable[[], None],
    ) -> None:
        self.broker = broker
        self.instrument = instrument
        self.book = book
        self._fill = fill
        self.break_next_submit = break_next_submit

    def fill_partly(self, client_order_id_: str, price: Decimal = ENTRY_PRICE) -> None:
        self._fill(client_order_id_, QTY / 2, price)

    def fill_fully(self, client_order_id_: str, price: Decimal = ENTRY_PRICE) -> None:
        self._fill(client_order_id_, QTY, price)

    def ref(self, client_order_id_: str) -> OrderRef:
        return OrderRef(client_order_id=client_order_id_, instrument_key=self.instrument.key)


@pytest.fixture
def sim_venue(clock: ManualClock) -> Venue:
    instrument = crypto()
    broker = SimBroker(clock, venue_id="binance", balances={"USDT": Decimal(10_000)})

    def tick(price: Decimal) -> None:
        broker.observe(
            Tick(
                instrument_key=instrument.key,
                bid=price,
                ask=price,
                last=price,
                high=price,
                low=price,
                covers_since=NOW,
                observed_at=clock.now(),
            )
        )

    def fill(_client_order_id: str, qty: Decimal, price: Decimal) -> None:
        """A resting order fills when the market trades through it, so filling means moving it.

        `fill_ratio` is what turns one market move into a partial fill — the same mechanism a thin
        book produces at a real venue.
        """
        broker.fill_ratio = Decimal("0.5") if qty < QTY else Decimal(1)
        tick(price)
        broker.fill_ratio = Decimal(1)

    # A starting book *above* the entry limit, so a buy at `ENTRY_PRICE` rests instead of crossing
    # on submit. Every assertion about a working order needs an order that is actually working.
    tick(ENTRY_PRICE + Decimal(5))
    return Venue(
        broker,
        instrument,
        FakeVenueBook(),
        fill=fill,
        break_next_submit=lambda: setattr(broker, "fail_next_submit", True),
    )


@pytest.fixture
def binance_venue(clock: ManualClock) -> Venue:
    instrument = crypto()
    book = FakeVenueBook()
    transport = FakeBinanceTransport(book, server_time=NOW)
    broker = BinanceSpotBroker(transport, clock, instruments=(instrument,))
    return Venue(
        broker,
        instrument,
        book,
        fill=lambda coid, qty, price: book.fill(coid, qty, price) and None,
        break_next_submit=lambda: setattr(book, "fail_next_submit", True),
    )


@pytest.fixture
async def alpaca_venue(clock: ManualClock) -> AsyncIterator[Venue]:
    instrument = equity()
    book = FakeVenueBook(currency="USD")
    api = FakeAlpacaApi(book, clock_time=NOW)
    transport = alpaca_transport(api, clock, mode=Mode.PAPER)
    broker = AlpacaBroker(transport, clock, instruments=(instrument,))
    yield Venue(
        broker,
        instrument,
        book,
        fill=lambda coid, qty, price: book.fill(coid, qty, price) and None,
        break_next_submit=lambda: setattr(book, "fail_next_submit", True),
    )
    await broker.close()


@pytest.fixture(params=["sim", "binance", "alpaca"])
def venue(request: pytest.FixtureRequest) -> Venue:
    """Every adapter in the system, held to the same assertions."""
    return request.getfixturevalue(f"{request.param}_venue")  # type: ignore[no-any-return]


def entry_intent(
    venue: Venue, *, seq: int = 0, side: Side = Side.BUY, qty: Decimal = QTY
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id(
            mode=Mode.PAPER,
            basket_id="contract",
            cycle_id="cycle-1",
            instrument=venue.instrument.key,
            seq=seq,
        ),
        basket_id="contract",
        cycle_id="cycle-1",
        instrument_key=venue.instrument.key,
        side=side,
        qty=qty,
        order_type=OrderType.LIMIT,
        limit_price=ENTRY_PRICE,
        created_at=NOW,
    )


def leg_intents(venue: Venue, entry: OrderIntent, qty: Decimal) -> tuple[OrderIntent, OrderIntent]:
    """A stop and a take-profit for a long position, as `plan_legs` would build them."""
    common = {
        "basket_id": entry.basket_id,
        "cycle_id": entry.cycle_id,
        "instrument_key": entry.instrument_key,
        "side": Side.SELL,
        "qty": qty,
        "group_id": entry.client_order_id,
        "created_at": NOW,
    }
    stop = OrderIntent(
        client_order_id=protective_order_id(entry.client_order_id, OrderRole.STOP_LOSS, 1),
        order_type=OrderType.STOP_LOSS_LIMIT,
        stop_price=Decimal("90"),
        limit_price=Decimal("89.5"),
        role=OrderRole.STOP_LOSS,
        **common,
    )
    target = OrderIntent(
        client_order_id=protective_order_id(entry.client_order_id, OrderRole.TAKE_PROFIT, 1),
        order_type=OrderType.TAKE_PROFIT_LIMIT,
        stop_price=Decimal("110"),
        limit_price=Decimal("110.5"),
        role=OrderRole.TAKE_PROFIT,
        **common,
    )
    return stop, target


class TestSubmitAndQuery:
    async def test_an_accepted_order_is_findable_by_our_own_id(self, venue: Venue) -> None:
        """The property `SUBMIT_UNKNOWN` recovery is built on (PLAN §2.3)."""
        intent = entry_intent(venue)
        ack = await venue.broker.submit(intent)
        assert ack.client_order_id == intent.client_order_id
        assert not ack.state.is_terminal or ack.state is OrderState.FILLED

        status = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        assert status.found
        assert status.client_order_id == intent.client_order_id
        assert status.requested_qty == intent.qty

    async def test_an_order_the_venue_never_heard_of_is_not_a_rejection(self, venue: Venue) -> None:
        """`found=False` and `REJECTED` demand opposite handling (DESIGN §8.1)."""
        status = await venue.broker.fetch_order(venue.ref("pap-NEVEREXISTED"))
        assert not status.found

    async def test_a_working_order_reports_its_side_and_price(self, venue: Venue) -> None:
        """The self-trade check reads these off the venue, so every adapter must report them."""
        intent = entry_intent(venue)
        await venue.broker.submit(intent)
        resting = await venue.broker.fetch_open_orders()
        ours = [status for status in resting if status.client_order_id == intent.client_order_id]
        assert ours, "a working order must appear in open orders"
        assert ours[0].side is Side.BUY
        assert ours[0].limit_price == ENTRY_PRICE

    async def test_our_client_order_id_fits_the_venue_cap(self, venue: Venue) -> None:
        """Asserted, not assumed: a truncated id can never be queried by again (PLAN §5)."""
        intent = entry_intent(venue)
        legs = leg_intents(venue, intent, QTY)
        cap = venue.broker.capabilities().max_client_order_id_length
        for identifier in (intent.client_order_id, *(leg.client_order_id for leg in legs)):
            assert len(identifier) <= cap


class TestFills:
    async def test_a_partial_fill_is_reported_as_fills_not_as_a_terminal_state(
        self, venue: Venue
    ) -> None:
        """Positions move from fills only; a partial fill is the normal case (PLAN §2.5)."""
        intent = entry_intent(venue, qty=QTY)
        await venue.broker.submit(intent)
        venue.fill_partly(intent.client_order_id)

        status = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        assert status.state is OrderState.PARTIALLY_FILLED
        assert status.filled_qty == QTY / 2
        assert sum((fill.qty for fill in status.fills), start=ZERO) == QTY / 2

    async def test_fills_carry_stable_ids_so_booking_is_idempotent(self, venue: Venue) -> None:
        """The monitor re-reads the same order every poll; a fill counted twice is a phantom."""
        intent = entry_intent(venue)
        await venue.broker.submit(intent)
        venue.fill_fully(intent.client_order_id)

        first = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        second = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        assert [f.fill_id for f in first.fills] == [f.fill_id for f in second.fills]
        assert all(fill.fill_id for fill in first.fills)

    async def test_a_completed_order_is_terminal_and_fully_filled(self, venue: Venue) -> None:
        intent = entry_intent(venue)
        await venue.broker.submit(intent)
        venue.fill_fully(intent.client_order_id)

        status = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        assert status.state is OrderState.FILLED
        assert status.state.is_terminal
        assert status.filled_qty == QTY


class TestSubmitUnknown:
    async def test_an_ambiguous_submit_raises_and_leaves_the_order_findable(
        self, venue: Venue
    ) -> None:
        """The single most important behaviour in the system (PLAN §2.3, R1).

        The adapter must raise `SubmitUnknownError` — never a retryable error a caller could act
        on — and the order it may have created must still be resolvable by our own id.
        """
        venue.break_next_submit()

        intent = entry_intent(venue)
        with pytest.raises(SubmitUnknownError) as raised:
            await venue.broker.submit(intent)
        assert raised.value.client_order_id == intent.client_order_id

        status = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        assert status.found, "the order the venue may have taken must still be findable"

    async def test_the_error_names_the_id_recovery_must_query_by(self, venue: Venue) -> None:
        venue.break_next_submit()
        intent = entry_intent(venue)
        with pytest.raises(SubmitUnknownError) as raised:
            await venue.broker.submit(intent)
        assert raised.value.client_order_id.startswith(Mode.PAPER.id_prefix)


class TestRejections:
    async def test_a_venue_rejection_is_a_result_not_an_exception(self, venue: Venue) -> None:
        """A definite answer that nothing executed, so the cycle records it and moves on."""
        if isinstance(venue.broker, SimBroker):
            # The simulated venue rejects a duplicate id, which is its own rejection path.
            intent = entry_intent(venue)
            await venue.broker.submit(intent)
            ack = await venue.broker.submit(intent)
        else:
            venue.book.reject_next = "Account has insufficient balance"
            ack = await venue.broker.submit(entry_intent(venue))

        assert ack.state is OrderState.REJECTED
        assert ack.reject_reason


class TestCancels:
    async def test_cancelling_a_working_order_succeeds(self, venue: Venue) -> None:
        intent = entry_intent(venue)
        ack = await venue.broker.submit(intent)
        cancelled = await venue.broker.cancel(
            OrderRef(
                client_order_id=intent.client_order_id,
                instrument_key=venue.instrument.key,
                venue_order_id=ack.venue_order_id,
            )
        )
        assert cancelled.cancelled
        status = await venue.broker.fetch_order(venue.ref(intent.client_order_id))
        assert status.state is OrderState.CANCELLED

    async def test_losing_a_cancel_race_is_reported_not_raised(self, venue: Venue) -> None:
        """A cancel that arrives after the fill is routine; the next poll books the fill."""
        intent = entry_intent(venue)
        ack = await venue.broker.submit(intent)
        venue.fill_fully(intent.client_order_id)

        cancelled = await venue.broker.cancel(
            OrderRef(
                client_order_id=intent.client_order_id,
                instrument_key=venue.instrument.key,
                venue_order_id=ack.venue_order_id,
            )
        )
        assert not cancelled.cancelled
        assert cancelled.detail

    async def test_cancelling_an_unknown_order_is_reported_not_raised(self, venue: Venue) -> None:
        cancelled = await venue.broker.cancel(venue.ref("pap-NEVEREXISTED"))
        assert not cancelled.cancelled


class TestProtectiveGroups:
    async def test_linked_legs_are_placed_together(self, venue: Venue) -> None:
        if not venue.broker.capabilities().oco_groups:
            pytest.skip("venue does not link protective legs; only a stop is ever placed")
        entry = entry_intent(venue)
        await venue.broker.submit(entry)
        venue.fill_fully(entry.client_order_id)

        acks = await venue.broker.submit_group(leg_intents(venue, entry, QTY))
        assert len(acks) == 2
        assert all(ack.state is not OrderState.REJECTED for ack in acks)

    async def test_one_leg_filling_cancels_its_sibling_inside_the_venue(self, venue: Venue) -> None:
        """Two live exits over one holding sell it twice — an accidental short (R13)."""
        if not venue.broker.capabilities().oco_groups:
            pytest.skip("venue does not link protective legs")
        entry = entry_intent(venue)
        await venue.broker.submit(entry)
        venue.fill_fully(entry.client_order_id)
        stop, target = leg_intents(venue, entry, QTY)
        await venue.broker.submit_group((stop, target))

        venue.fill_fully(target.client_order_id, Decimal("110.5"))

        sibling = await venue.broker.fetch_order(venue.ref(stop.client_order_id))
        assert sibling.state.is_terminal, "the unfilled leg must not outlive its group"

    async def test_a_single_leg_group_takes_the_ordinary_submit_path(self, venue: Venue) -> None:
        """A venue without linked legs still gets its stop, through the same call (DESIGN §6.7)."""
        entry = entry_intent(venue)
        await venue.broker.submit(entry)
        venue.fill_fully(entry.client_order_id)
        stop, _ = leg_intents(venue, entry, QTY)

        acks = await venue.broker.submit_group((stop,))
        assert len(acks) == 1
        assert acks[0].client_order_id == stop.client_order_id


class TestAccountAndCapabilities:
    async def test_the_account_state_is_readable_and_stamped(self, venue: Venue) -> None:
        state = await venue.broker.fetch_positions_and_balances()
        assert state.venue == venue.broker.venue_id
        assert state.observed_at.tzinfo is not None
        assert state.balances

    async def test_a_filled_buy_shows_up_as_a_position(self, venue: Venue) -> None:
        intent = entry_intent(venue)
        await venue.broker.submit(intent)
        venue.fill_fully(intent.client_order_id)

        state = await venue.broker.fetch_positions_and_balances()
        assert state.qty(venue.instrument.key) == QTY

    async def test_capabilities_are_honest_about_query_by_client_order_id(
        self, venue: Venue
    ) -> None:
        """Declaring this falsely would mean an unresolvable `SUBMIT_UNKNOWN` in production."""
        assert venue.broker.capabilities().query_by_client_order_id

    async def test_no_venue_claims_a_server_side_ttl(self, venue: Venue) -> None:
        """Neither venue has good-till-time, so TTL stays bot-enforced everywhere (REVIEW B7)."""
        assert not venue.broker.capabilities().venue_side_ttl

    async def test_the_venue_clock_is_readable(self, venue: Venue) -> None:
        """The startup skew check depends on it (PLAN §3.1)."""
        assert (await venue.broker.server_time()).tzinfo is not None
