"""What must be true about a venue before the system may trade on it (PLAN §3.1, §3.2).

A failing preflight leaves the process *up and halted*, which is the same contract the rest of the
startup sequence honours: an operator needs a running system they can ask what went wrong
(DESIGN §8.2).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradebot.control.preflight import SKEW_HALT, SKEW_WARN, VenuePreflight
from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode, OrderType
from tradebot.core.errors import VenueError
from tradebot.core.portfolio import AccountState
from tradebot.interfaces.broker import BrokerCapabilities, CancelAck, OrderAck, OrderStatus

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class StubBroker:
    """The smallest thing that satisfies `BrokerAdapter` and can misbehave on demand."""

    venue_id = "stub"

    def __init__(
        self,
        *,
        server_time: datetime | None = NOW,
        withdrawals: bool | None = None,
        reports_restrictions: bool = False,
        capabilities: BrokerCapabilities | None = None,
        clock_error: Exception | None = None,
        restrictions_error: Exception | None = None,
    ) -> None:
        self._server_time = server_time
        self._withdrawals = withdrawals
        self._capabilities = capabilities or BrokerCapabilities(
            venue_id="stub", order_types=(OrderType.LIMIT,)
        )
        self._clock_error = clock_error
        self._restrictions_error = restrictions_error
        if reports_restrictions:
            self.withdrawals_enabled = self._withdrawals_enabled  # type: ignore[method-assign]

    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities

    async def server_time(self) -> datetime:
        if self._clock_error is not None:
            raise self._clock_error
        assert self._server_time is not None
        return self._server_time

    async def _withdrawals_enabled(self) -> bool | None:
        if self._restrictions_error is not None:
            raise self._restrictions_error
        return self._withdrawals

    # The rest of the protocol, unused by preflight but needed to satisfy it.
    async def submit(self, intent: object) -> OrderAck: ...  # type: ignore[empty-body]
    async def submit_group(self, intents: object) -> tuple[OrderAck, ...]:
        return ()

    async def cancel(self, order_ref: object) -> CancelAck: ...  # type: ignore[empty-body]
    async def fetch_order(self, order_ref: object) -> OrderStatus: ...  # type: ignore[empty-body]
    async def fetch_open_orders(self) -> tuple[OrderStatus, ...]:
        return ()

    async def fetch_positions_and_balances(self) -> AccountState: ...  # type: ignore[empty-body]
    async def close(self) -> None: ...


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


def preflight(clock: ManualClock, broker: StubBroker, mode: Mode = Mode.PAPER) -> VenuePreflight:
    return VenuePreflight(broker, clock, mode=mode)  # type: ignore[arg-type]


class TestClockSkew:
    async def test_an_aligned_clock_passes(self, clock: ManualClock) -> None:
        assert await preflight(clock, StubBroker()).run() == ()

    async def test_a_small_skew_warns_but_does_not_halt(self, clock: ManualClock) -> None:
        broker = StubBroker(server_time=NOW + SKEW_WARN + timedelta(seconds=1))
        assert await preflight(clock, broker).run() == ()

    async def test_a_large_skew_halts(self, clock: ManualClock) -> None:
        """Signed requests would be rejected and candle alignment would be wrong (PLAN §3.1)."""
        broker = StubBroker(server_time=NOW + SKEW_HALT + timedelta(seconds=1))
        failures = await preflight(clock, broker).run()
        assert any("clock skew" in failure for failure in failures)

    async def test_skew_is_measured_in_both_directions(self, clock: ManualClock) -> None:
        broker = StubBroker(server_time=NOW - SKEW_HALT - timedelta(seconds=1))
        assert await preflight(clock, broker).run()

    async def test_an_unreadable_venue_clock_halts(self, clock: ManualClock) -> None:
        """Not knowing the skew is not the same as there being none."""
        broker = StubBroker(clock_error=VenueError("venue unreachable"))
        failures = await preflight(clock, broker).run()
        assert any("could not read the venue's clock" in failure for failure in failures)


class TestKeyRestrictions:
    async def test_a_live_key_that_may_withdraw_refuses_to_start(self, clock: ManualClock) -> None:
        """Trusting a checkbox set months ago is not a control (PLAN §3.2)."""
        broker = StubBroker(withdrawals=True, reports_restrictions=True)
        failures = await preflight(clock, broker, Mode.LIVE).run()
        assert any("withdrawals ENABLED" in failure for failure in failures)

    async def test_the_same_key_only_warns_outside_live(self, clock: ManualClock) -> None:
        """There is nothing to withdraw from a testnet, so this must not block a paper run."""
        broker = StubBroker(withdrawals=True, reports_restrictions=True)
        assert await preflight(clock, broker, Mode.PAPER).run() == ()

    async def test_a_key_that_cannot_withdraw_passes(self, clock: ManualClock) -> None:
        broker = StubBroker(withdrawals=False, reports_restrictions=True)
        assert await preflight(clock, broker, Mode.LIVE).run() == ()

    async def test_a_venue_that_will_not_answer_is_recorded_not_excused(
        self, clock: ManualClock
    ) -> None:
        """`None` means "the venue would not say", which is a warning rather than a pass."""
        broker = StubBroker(withdrawals=None, reports_restrictions=True)
        assert await preflight(clock, broker, Mode.LIVE).run() == ()

    async def test_a_venue_with_no_such_endpoint_leaves_it_to_the_operator(
        self, clock: ManualClock
    ) -> None:
        """Alpaca has no equivalent; the precondition becomes a documented human one."""
        assert await preflight(clock, StubBroker(), Mode.LIVE).run() == ()

    async def test_an_unverifiable_restriction_halts_live_only(self, clock: ManualClock) -> None:
        broker = StubBroker(reports_restrictions=True, restrictions_error=VenueError("500"))
        assert await preflight(clock, broker, Mode.LIVE).run()
        assert await preflight(clock, broker, Mode.PAPER).run() == ()


class TestCapabilities:
    async def test_a_venue_that_cannot_be_queried_by_our_id_is_refused(
        self, clock: ManualClock
    ) -> None:
        """`SUBMIT_UNKNOWN` would have no safe resolution at all (PLAN §2.3)."""
        broker = StubBroker(
            capabilities=BrokerCapabilities(
                venue_id="stub",
                order_types=(OrderType.LIMIT,),
                query_by_client_order_id=False,
            )
        )
        failures = await preflight(clock, broker).run()
        assert any("client_order_id" in failure for failure in failures)

    async def test_an_id_longer_than_the_venue_allows_is_refused(self, clock: ManualClock) -> None:
        """A truncated id at the venue can never be queried by afterwards."""
        broker = StubBroker(
            capabilities=BrokerCapabilities(
                venue_id="stub", order_types=(OrderType.LIMIT,), max_client_order_id_length=8
            )
        )
        failures = await preflight(clock, broker).run()
        assert any("characters" in failure for failure in failures)

    async def test_our_scheme_fits_every_supported_venue(self, clock: ManualClock) -> None:
        """36 characters is Binance's cap and the tightest we support."""
        broker = StubBroker(
            capabilities=BrokerCapabilities(
                venue_id="stub", order_types=(OrderType.LIMIT,), max_client_order_id_length=36
            )
        )
        assert await preflight(clock, broker).run() == ()

    async def test_a_venue_without_protective_orders_warns_rather_than_halting(
        self, clock: ManualClock
    ) -> None:
        """The sizing haircut is the response, not a refusal to trade (R12)."""
        broker = StubBroker(
            capabilities=BrokerCapabilities(
                venue_id="stub", order_types=(OrderType.LIMIT,), protective_orders=False
            )
        )
        assert await preflight(clock, broker).run() == ()


class TestReporting:
    async def test_every_failure_is_reported_at_once(self, clock: ManualClock) -> None:
        """One refusal at a time makes an operator fix four problems over four restarts."""
        broker = StubBroker(
            server_time=NOW + SKEW_HALT + timedelta(seconds=5),
            capabilities=BrokerCapabilities(
                venue_id="stub",
                order_types=(OrderType.LIMIT,),
                query_by_client_order_id=False,
                max_client_order_id_length=4,
            ),
        )
        assert len(await preflight(clock, broker).run()) == 3
