"""Rate limiting and circuit breaking: the controls that keep the API key alive.

The load-bearing test here is `test_a_burst_never_exceeds_the_budget_in_any_window`, which is
PLAN §3.1's exit criterion. It asserts the property that matters — *no* 60-second window ever
exceeds the budget — rather than the mechanism, so a future change to the mechanism still has to
honour the guarantee. A token bucket with continuous refill fails this test: it allows a full
capacity burst plus a further capacity of drip inside one window.
"""

from __future__ import annotations

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.errors import CircuitOpenError, RateLimitedError, VenueBannedError
from tradebot.core.ratelimit import (
    CircuitBreaker,
    CircuitState,
    RateBudget,
    SlidingWindow,
    VenueRateLimiter,
)

WINDOW = 60.0


def window(clock: ManualClock, capacity: int = 10) -> SlidingWindow:
    return SlidingWindow(capacity, WINDOW, clock, name="test:weight")


class TestRateBudget:
    def test_non_positive_limits_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="weight_per_minute must be positive"):
            RateBudget(weight_per_minute=0)

    def test_a_zero_failure_threshold_would_open_the_circuit_forever(self) -> None:
        with pytest.raises(ValueError, match="failure_threshold must be positive"):
            RateBudget(failure_threshold=0)

    def test_poll_cadence_is_derived_from_the_budget(self) -> None:
        """A hardcoded cadence silently overspends a tightened budget (PLAN §3.1)."""
        # 10% of 600 weight/min = 60 weight/min; at 2 per poll that is 30 polls/min, one every 2s.
        budget = RateBudget(weight_per_minute=600)
        assert budget.poll_interval(weight_per_poll=2, share_pct=10).total_seconds() == 2.0

    def test_a_tighter_budget_slows_the_poller(self) -> None:
        loose = RateBudget(weight_per_minute=600).poll_interval(2)
        tight = RateBudget(weight_per_minute=60).poll_interval(2)
        assert tight > loose

    @pytest.mark.parametrize(("weight", "share"), [(0, 10), (2, 0), (2, 101)])
    def test_nonsense_poll_parameters_are_rejected(self, weight: int, share: int) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            RateBudget().poll_interval(weight, share)


class TestSlidingWindow:
    def test_spend_is_capped_at_capacity(self, clock: ManualClock) -> None:
        budget = window(clock, capacity=10)
        assert budget.take(10)
        assert not budget.take(1)
        assert budget.used == 10
        assert budget.remaining == 0

    def test_spend_frees_only_when_the_window_has_passed(self, clock: ManualClock) -> None:
        budget = window(clock, capacity=10)
        budget.take(10)
        clock.advance(WINDOW - 1)
        assert not budget.take(1)
        clock.advance(1)
        assert budget.take(10)

    def test_capacity_is_never_exceeded_by_a_partial_expiry(self, clock: ManualClock) -> None:
        budget = window(clock, capacity=10)
        budget.take(6)
        clock.advance(30)
        budget.take(4)
        clock.advance(30.1)  # the first charge has aged out, the second has not
        assert budget.used == 4
        assert budget.take(6)
        assert not budget.take(1)

    def test_wait_time_is_when_enough_spend_expires(self, clock: ManualClock) -> None:
        budget = window(clock, capacity=10)
        budget.take(10)
        clock.advance(20)
        assert budget.wait_seconds(1) == pytest.approx(WINDOW - 20)

    def test_nothing_to_wait_for_when_it_already_fits(self, clock: ManualClock) -> None:
        assert window(clock).wait_seconds(5) == 0.0

    def test_an_unaffordable_amount_reports_a_clear_window(self, clock: ManualClock) -> None:
        """More than the whole budget can never fit; the honest answer is the full window."""
        budget = window(clock, capacity=10)
        budget.take(4)
        assert budget.wait_seconds(20) == pytest.approx(WINDOW)

    def test_a_call_costing_more_than_the_budget_is_a_configuration_error(
        self, clock: ManualClock
    ) -> None:
        with pytest.raises(ValueError, match="above the whole budget"):
            window(clock, capacity=10).take(11)

    def test_a_non_positive_capacity_is_rejected(self, clock: ManualClock) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            SlidingWindow(0, WINDOW, clock, name="bad")

    def test_the_venue_figure_wins_when_it_is_higher(self, clock: ManualClock) -> None:
        """Our weight table can lag the venue's; the venue's header cannot be wrong."""
        budget = window(clock, capacity=10)
        budget.take(2)
        budget.observe_external(9)
        assert budget.used == 9
        assert not budget.take(2)

    def test_a_lower_venue_figure_is_ignored(self, clock: ManualClock) -> None:
        """Never relax our own accounting: the header may describe a different window."""
        budget = window(clock, capacity=10)
        budget.take(8)
        budget.observe_external(1)
        assert budget.used == 8


class TestCircuitBreaker:
    def test_it_opens_only_after_the_threshold(self, clock: ManualClock) -> None:
        breaker = CircuitBreaker(threshold=3, cooldown_seconds=30, clock=clock)
        for _ in range(2):
            breaker.record_failure()
            breaker.guard("binance")
        breaker.record_failure()
        with pytest.raises(CircuitOpenError, match="circuit open after 3"):
            breaker.guard("binance")

    def test_a_success_resets_the_streak(self, clock: ManualClock) -> None:
        breaker = CircuitBreaker(threshold=2, cooldown_seconds=30, clock=clock)
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.guard("binance")
        assert breaker.state is CircuitState.CLOSED

    def test_the_cooldown_lets_one_probe_through(self, clock: ManualClock) -> None:
        breaker = CircuitBreaker(threshold=1, cooldown_seconds=30, clock=clock)
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN
        clock.advance(30)
        assert breaker.state is CircuitState.HALF_OPEN
        breaker.guard("binance")

    def test_a_probe_failure_reopens_the_circuit(self, clock: ManualClock) -> None:
        breaker = CircuitBreaker(threshold=1, cooldown_seconds=30, clock=clock)
        breaker.record_failure()
        clock.advance(30)
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

    def test_only_an_open_circuit_blocks_a_call(self) -> None:
        assert CircuitState.OPEN.allows_call is False
        assert CircuitState.HALF_OPEN.allows_call is True
        assert CircuitState.CLOSED.allows_call is True


class TestVenueRateLimiter:
    async def test_a_burst_never_exceeds_the_budget_in_any_window(self, clock: ManualClock) -> None:
        """PLAN §3.1 exit criterion, asserted as a property of every 60-second interval."""
        budget = RateBudget(weight_per_minute=60, max_wait_seconds=3600)
        limiter = VenueRateLimiter("binance", clock, budget)
        charges: list[tuple[float, int]] = []

        for _ in range(50):
            await limiter.acquire(weight=5)
            charges.append((clock.monotonic(), 5))

        for moment, _ in charges:
            spent = sum(w for stamp, w in charges if moment - 60.0 < stamp <= moment)
            assert spent <= budget.weight_per_minute, f"window ending {moment} spent {spent}"

    async def test_a_burst_within_budget_never_sleeps(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter("binance", clock, RateBudget(weight_per_minute=100))
        for _ in range(10):
            await limiter.acquire(weight=10)
        assert clock.monotonic() == 0.0

    async def test_exhausting_the_budget_waits_for_it_to_free(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter(
            "binance", clock, RateBudget(weight_per_minute=10, max_wait_seconds=WINDOW)
        )
        await limiter.acquire(weight=10)
        await limiter.acquire(weight=10)
        assert clock.monotonic() == pytest.approx(WINDOW)

    async def test_waiting_longer_than_the_ceiling_fails_closed(self, clock: ManualClock) -> None:
        """A call delayed past its usefulness is a different decision from the approved one."""
        limiter = VenueRateLimiter(
            "binance", clock, RateBudget(weight_per_minute=10, max_wait_seconds=5)
        )
        await limiter.acquire(weight=10)
        with pytest.raises(RateLimitedError, match="above the 5s ceiling"):
            await limiter.acquire(weight=10)

    async def test_market_data_cannot_exhaust_the_order_allowance(self, clock: ManualClock) -> None:
        """An order must not be blocked because a poller spent the order-count budget."""
        limiter = VenueRateLimiter(
            "binance", clock, RateBudget(weight_per_minute=1000, orders_per_ten_seconds=1)
        )
        for _ in range(20):
            await limiter.acquire(weight=2)
        await limiter.acquire(weight=1, is_order=True)
        assert clock.monotonic() == 0.0

    async def test_the_order_burst_window_throttles_orders(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter(
            "binance",
            clock,
            RateBudget(weight_per_minute=1000, orders_per_ten_seconds=1, max_wait_seconds=60),
        )
        await limiter.acquire(is_order=True)
        await limiter.acquire(is_order=True)
        assert clock.monotonic() == pytest.approx(10.0)

    async def test_the_used_weight_header_tightens_our_accounting(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter(
            "binance", clock, RateBudget(weight_per_minute=100, max_wait_seconds=1)
        )
        await limiter.acquire(weight=1)
        limiter.observe_used_weight({"X-MBX-USED-WEIGHT-1M": "100"})
        assert limiter.weight_used == 100
        with pytest.raises(RateLimitedError):
            await limiter.acquire(weight=1)

    @pytest.mark.parametrize("headers", [{}, {"X-MBX-USED-WEIGHT-1M": "n/a"}, {"other": "5"}])
    async def test_unusable_headers_are_ignored(
        self, clock: ManualClock, headers: dict[str, str]
    ) -> None:
        limiter = VenueRateLimiter("binance", clock, RateBudget(weight_per_minute=100))
        await limiter.acquire(weight=3)
        limiter.observe_used_weight(headers)
        assert limiter.weight_used == 3

    async def test_a_retry_after_penalty_is_served_before_the_next_call(
        self, clock: ManualClock
    ) -> None:
        limiter = VenueRateLimiter("binance", clock, RateBudget(max_wait_seconds=60))
        limiter.penalise(12.0)
        await limiter.acquire(weight=1)
        assert clock.monotonic() == pytest.approx(12.0)

    async def test_a_penalty_longer_than_the_ceiling_fails_closed(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter("binance", clock, RateBudget(max_wait_seconds=5))
        limiter.penalise(600.0)
        with pytest.raises(RateLimitedError, match="penalised for another"):
            await limiter.acquire(weight=1)

    async def test_a_missing_retry_after_still_penalises(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter("binance", clock, RateBudget(max_wait_seconds=60))
        limiter.penalise(None)
        await limiter.acquire(weight=1)
        assert clock.monotonic() == pytest.approx(1.0)

    async def test_an_open_circuit_refuses_without_calling(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter("binance", clock, RateBudget(failure_threshold=2))
        limiter.record_failure()
        limiter.record_failure()
        assert limiter.circuit is CircuitState.OPEN
        with pytest.raises(CircuitOpenError):
            await limiter.acquire(weight=1)

    async def test_a_ban_is_fatal_and_sticky(self, clock: ManualClock) -> None:
        """Every further call extends the ban, so there is no path back without a human."""
        limiter = VenueRateLimiter("binance", clock)
        limiter.ban("HTTP 418")
        for _ in range(2):
            with pytest.raises(VenueBannedError, match="extends the ban"):
                await limiter.acquire(weight=1)

    async def test_a_ban_outranks_a_healthy_circuit(self, clock: ManualClock) -> None:
        limiter = VenueRateLimiter("binance", clock)
        limiter.record_success()
        limiter.ban("HTTP 418")
        with pytest.raises(VenueBannedError):
            await limiter.acquire(weight=1)
