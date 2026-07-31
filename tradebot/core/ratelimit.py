"""Weight-aware rate limiting and circuit breaking in front of every venue call.

Getting the API key banned is a first-class failure, not an inconvenience: a banned key is an
account we cannot flatten (PLAN §3.1, R4). Three mechanisms, in order of who stops us:

1. **We stop ourselves.** Sliding-window budgets set *below* the venue's published limit. Binance
   bans on request *weight*, not request count, so weight is what is metered — a single `klines`
   call with `limit=1000` costs far more than a ticker.
2. **The venue corrects us.** Every response carries the venue's own used-weight header. It is
   authoritative: `observe_used_weight` charges our window up to match, so a miscounted endpoint
   weight cannot quietly accumulate into a ban.
3. **We stop calling entirely.** A circuit breaker opens after N consecutive failures, and a
   `418`-class ban is `VenueBannedError` — fatal, because every further call extends the ban.

**Why a sliding window rather than a token bucket.** A refilling bucket permits a burst of the
full capacity followed by a further capacity's worth of drip, which is up to *twice* the budget
inside one 60-second window — precisely the overspend the limiter exists to prevent. A sliding
window bounds every 60-second interval to the budget, which is strictly stronger than the venue's
own fixed window (a fixed window allows a double spend across its boundary; this cannot).

Failure semantics: a budget that cannot be satisfied within `max_wait_seconds` raises
`RateLimitedError` rather than blocking a cycle indefinitely — a late order is a different
decision from the one that was approved. An open circuit raises `CircuitOpenError`. Both are
retryable classifications, so the caller's retry budget decides whether the basket halts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Final

from tradebot.core.clock import Clock
from tradebot.core.errors import CircuitOpenError, RateLimitedError, VenueBannedError

#: Venue headers reporting weight already consumed in the current window, most specific first.
USED_WEIGHT_HEADERS: Final = ("x-mbx-used-weight-1m", "x-mbx-used-weight")


@dataclass(frozen=True, slots=True)
class RateBudget:
    """Per-venue call budget. Every number sits below the venue's published limit.

    Defaults target Binance spot conservatively: the published IP weight allowance is far higher,
    but a research bot at minutes-scale cadence has no reason to approach it, and the headroom
    absorbs a miscounted endpoint weight without a ban.
    """

    weight_per_minute: int = 600
    orders_per_ten_seconds: int = 5
    orders_per_day: int = 500
    #: Consecutive failures that open the circuit. One flaky call is noise; five is a venue.
    failure_threshold: int = 5
    circuit_cooldown_seconds: float = 60.0
    #: Longest a caller will wait for budget before failing closed instead.
    max_wait_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("weight_per_minute", "orders_per_ten_seconds", "orders_per_day"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if self.failure_threshold < 1:
            raise ValueError(f"failure_threshold must be positive, got {self.failure_threshold}")

    def poll_interval(self, weight_per_poll: int, share_pct: int = 10) -> timedelta:
        """Cadence for a background poller allowed `share_pct` of the weight budget.

        The ExecutionMonitor's polling cadence is *derived* from the budget rather than hardcoded,
        so tightening the budget slows the pollers automatically instead of silently overspending
        it (PLAN §3.1).
        """
        if weight_per_poll < 1 or not 0 < share_pct <= 100:
            raise ValueError("weight_per_poll must be positive and share_pct within (0, 100]")
        polls_per_minute = max(self.weight_per_minute * share_pct / 100 / weight_per_poll, 1e-6)
        return timedelta(seconds=60.0 / polls_per_minute)


class CircuitState(StrEnum):
    """Breaker posture. `HALF_OPEN` lets exactly one probe call through."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    @property
    def allows_call(self) -> bool:
        return self is not CircuitState.OPEN


class SlidingWindow:
    """At most `capacity` units of spend in any `window_seconds` interval."""

    def __init__(self, capacity: int, window_seconds: float, clock: Clock, *, name: str) -> None:
        if capacity < 1 or window_seconds <= 0:
            raise ValueError(f"{name}: capacity and window must be positive")
        self._capacity = capacity
        self._window = window_seconds
        self._clock = clock
        self._name = name
        self._entries: deque[tuple[float, int]] = deque()
        self._used = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def used(self) -> int:
        self._evict()
        return self._used

    @property
    def remaining(self) -> int:
        return self._capacity - self.used

    def take(self, amount: int) -> bool:
        """Charge `amount` if it fits. Returns False without charging if it does not."""
        self._assert_affordable(amount)
        self._evict()
        if self._used + amount > self._capacity:
            return False
        self._charge(amount)
        return True

    def wait_seconds(self, amount: int) -> float:
        """Seconds until `amount` would fit. Zero when it already does.

        An `amount` above the whole capacity can never fit; the answer is then the time at which
        the window is completely clear, which is the most the caller can usefully be told. `take`
        still refuses such a call outright.
        """
        self._evict()
        shortfall = self._used + amount - self._capacity
        if shortfall <= 0:
            return 0.0
        now = self._clock.monotonic()
        freed = 0
        delay = 0.0
        for timestamp, weight in self._entries:
            freed += weight
            delay = timestamp + self._window - now
            if freed >= shortfall:
                break
        return max(delay, 0.0)

    def observe_external(self, venue_used: int) -> None:
        """Charge up to the venue's own figure when it exceeds ours.

        Our weight table can be wrong — endpoint weights change and a client library's table
        lags. The venue's header cannot be, so it wins whenever it says we have spent more.
        """
        self._evict()
        if venue_used > self._used:
            self._charge(venue_used - self._used)

    def _charge(self, amount: int) -> None:
        self._entries.append((self._clock.monotonic(), amount))
        self._used += amount

    def _evict(self) -> None:
        horizon = self._clock.monotonic() - self._window
        while self._entries and self._entries[0][0] <= horizon:
            self._used -= self._entries.popleft()[1]

    def _assert_affordable(self, amount: int) -> None:
        if amount > self._capacity:
            raise ValueError(
                f"{self._name}: a single call costs {amount}, above the whole budget "
                f"{self._capacity}; the budget is misconfigured for this endpoint"
            )


class CircuitBreaker:
    """Stops calling a venue that keeps failing, instead of hammering it into a ban."""

    def __init__(self, *, threshold: int, cooldown_seconds: float, clock: Clock) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        elapsed = self._clock.monotonic() - self._opened_at
        return CircuitState.HALF_OPEN if elapsed >= self._cooldown else CircuitState.OPEN

    @property
    def failures(self) -> int:
        return self._failures

    def guard(self, venue_id: str) -> None:
        """Raise while open. A half-open circuit lets one probe through to test the water."""
        if self.state is CircuitState.OPEN:
            remaining = self._cooldown - (self._clock.monotonic() - (self._opened_at or 0.0))
            raise CircuitOpenError(
                f"{venue_id}: circuit open after {self._failures} consecutive failures; "
                f"retry in {remaining:.1f}s",
                retry_after_seconds=max(remaining, 0.0),
            )

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock.monotonic()


class VenueRateLimiter:
    """The single gate every venue call passes through.

    One instance per venue, shared by the market-data provider and (from Phase 5) the broker,
    because a venue's ban applies to the *IP and key*, not to a code path.
    """

    def __init__(self, venue_id: str, clock: Clock, budget: RateBudget | None = None) -> None:
        self._venue_id = venue_id
        self._clock = clock
        self._budget = budget or RateBudget()
        self._weight = SlidingWindow(
            self._budget.weight_per_minute, 60.0, clock, name=f"{venue_id}:weight-1m"
        )
        self._order_burst = SlidingWindow(
            self._budget.orders_per_ten_seconds, 10.0, clock, name=f"{venue_id}:orders-10s"
        )
        self._order_day = SlidingWindow(
            self._budget.orders_per_day, 86_400.0, clock, name=f"{venue_id}:orders-24h"
        )
        self._breaker = CircuitBreaker(
            threshold=self._budget.failure_threshold,
            cooldown_seconds=self._budget.circuit_cooldown_seconds,
            clock=clock,
        )
        self._banned_reason: str | None = None
        self._penalty_until = 0.0

    @property
    def budget(self) -> RateBudget:
        return self._budget

    @property
    def circuit(self) -> CircuitState:
        return self._breaker.state

    @property
    def weight_used(self) -> int:
        return self._weight.used

    async def acquire(self, weight: int = 1, *, is_order: bool = False) -> None:
        """Block until this call fits every applicable budget, then charge it.

        Order windows are consulted *only* for order-placing calls, so a burst of market-data
        reads can never exhaust the allowance that submitting an order needs.
        """
        self._assert_not_banned()
        self._breaker.guard(self._venue_id)
        await self._serve_penalty()
        windows = [(self._weight, weight)]
        if is_order:
            windows += [(self._order_burst, 1), (self._order_day, 1)]
        await self._wait_for(windows)
        for window, amount in windows:
            window.take(amount)

    async def _wait_for(self, windows: list[tuple[SlidingWindow, int]]) -> None:
        waited = 0.0
        while True:
            delay, window = max(
                ((w.wait_seconds(amount), w) for w, amount in windows),
                key=lambda pair: pair[0],
            )
            if delay <= 0.0:
                return
            if waited + delay > self._budget.max_wait_seconds:
                raise RateLimitedError(
                    f"{self._venue_id}: {window.name} needs {waited + delay:.1f}s of budget, "
                    f"above the {self._budget.max_wait_seconds:.0f}s ceiling; failing closed "
                    f"instead of issuing a stale call",
                    retry_after_seconds=delay,
                )
            waited += delay
            await self._clock.sleep(delay)

    async def _serve_penalty(self) -> None:
        """Sit out a `Retry-After` the venue asked for."""
        remaining = self._penalty_until - self._clock.monotonic()
        if remaining <= 0:
            return
        if remaining > self._budget.max_wait_seconds:
            raise RateLimitedError(
                f"{self._venue_id}: penalised for another {remaining:.1f}s",
                retry_after_seconds=remaining,
            )
        await self._clock.sleep(remaining)

    def observe_used_weight(self, headers: dict[str, str]) -> None:
        """Reconcile our weight accounting against the venue's, from a response's headers."""
        lowered = {key.lower(): value for key, value in headers.items()}
        for header in USED_WEIGHT_HEADERS:
            raw = (lowered.get(header) or "").strip()
            if raw:
                if raw.isdigit():
                    self._weight.observe_external(int(raw))
                return

    def penalise(self, retry_after_seconds: float | None) -> None:
        """Honour a `429`/`Retry-After`: no further call until the penalty has been served."""
        delay = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else 1.0
        self._penalty_until = max(self._penalty_until, self._clock.monotonic() + delay)

    def ban(self, reason: str) -> None:
        """Record a hard venue ban. Terminal: only a human restarts from here."""
        self._banned_reason = reason

    def record_success(self) -> None:
        self._breaker.record_success()

    def record_failure(self) -> None:
        self._breaker.record_failure()

    def _assert_not_banned(self) -> None:
        if self._banned_reason is not None:
            raise VenueBannedError(
                f"{self._venue_id}: refusing to call a venue that banned us "
                f"({self._banned_reason}); calling again extends the ban"
            )
