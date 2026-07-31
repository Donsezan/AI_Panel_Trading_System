"""The ccxt transport: error taxonomy, rate-budget integration, and the startup assertions.

No network and no HTTP mocking — a fake exchange object with ccxt's method names is enough,
because the transport's whole job is classification and budgeting, not parsing.

The classification order matters and is tested for it: ccxt models both the `429` rate limit and
the `418` IP ban as `NetworkError` subclasses, so a broad match first would turn a ban into a
retry, and retrying a ban is what extends it.
"""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt
import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.errors import (
    CircuitOpenError,
    ConfigError,
    RateLimitedError,
    VenueBannedError,
    VenueError,
)
from tradebot.core.ratelimit import RateBudget
from tradebot.venues.ccxt_transport import (
    BINANCE_HOSTS,
    CcxtTransport,
    assert_host,
    assert_no_credentials,
    binance_spot_transport,
)


class FakeExchange:
    """The slice of ccxt's Exchange the transport touches."""

    def __init__(
        self,
        *,
        host: str = "api.binance.com",
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
        payload: Any = None,
    ) -> None:
        self.urls = {"api": {"public": f"https://{host}/api/v3"}}
        self.last_response_headers = headers
        self.apiKey = None
        self.secret = None
        self.error = error
        self.payload = payload if payload is not None else [[1, "1", "2", "0.5", "1.5", "10"]]
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def publicGetKlines(self, params: dict[str, Any]) -> Any:  # noqa: N802 — ccxt's name
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        return self.payload

    async def close(self) -> None:
        self.closed = True


def transport(exchange: FakeExchange, clock: ManualClock, **kwargs: Any) -> CcxtTransport:
    return CcxtTransport(
        exchange, clock, venue_id="binance", expected_host="api.binance.com", **kwargs
    )


class TestStartupAssertions:
    def test_a_data_client_holding_a_key_refuses_to_exist(self, clock: ManualClock) -> None:
        """A client that can sign is a client that could place an order (PLAN §2.4, §3.2)."""
        exchange = FakeExchange()
        exchange.apiKey = "AKIA-not-a-real-key"
        with pytest.raises(ConfigError, match="must not be able to sign"):
            transport(exchange, clock)

    def test_every_credential_shape_is_checked(self) -> None:
        exchange = FakeExchange()
        exchange.secret = "shhh"
        with pytest.raises(ConfigError, match="secret"):
            assert_no_credentials(exchange, "binance")

    def test_an_unexpected_host_refuses_to_start(self, clock: ManualClock) -> None:
        """A hijacked or fat-fingered config must not be able to redirect our price feed."""
        exchange = FakeExchange(host="evil.example.com")
        with pytest.raises(ConfigError, match="resolved its public endpoint"):
            transport(exchange, clock)

    def test_the_expected_host_passes(self, clock: ManualClock) -> None:
        assert_host(FakeExchange(), "api.binance.com", "binance")

    def test_sandbox_and_live_hosts_are_distinct(self) -> None:
        assert BINANCE_HOSTS[False] != BINANCE_HOSTS[True]


class TestSuccessPath:
    async def test_a_call_charges_its_weight_and_returns_the_payload(
        self, clock: ManualClock
    ) -> None:
        exchange = FakeExchange()
        client = transport(exchange, clock)
        payload = await client.get("klines", {"symbol": "BTCUSDT"}, weight=4)
        assert payload == exchange.payload
        assert client.limiter.weight_used == 4
        assert exchange.calls == [{"symbol": "BTCUSDT"}]

    async def test_the_venues_used_weight_header_wins(self, clock: ManualClock) -> None:
        """Our weight table can lag the venue's; the header is authoritative (PLAN §3.1)."""
        exchange = FakeExchange(headers={"x-mbx-used-weight-1m": "250"})
        client = transport(exchange, clock)
        await client.get("klines", {}, weight=2)
        assert client.limiter.weight_used == 250

    async def test_an_unmapped_endpoint_is_a_config_error(self, clock: ManualClock) -> None:
        with pytest.raises(ConfigError, match="no ccxt method mapped"):
            await transport(FakeExchange(), clock).get("orderbook", {}, weight=1)

    async def test_closing_is_idempotent(self, clock: ManualClock) -> None:
        """ccxt's async client leaks a connector unless it is closed, and exactly once."""
        exchange = FakeExchange()
        client = transport(exchange, clock)
        await client.close()
        await client.close()
        assert exchange.closed


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (ccxt.RequestTimeout("timed out"), VenueError),
            (ccxt.ExchangeNotAvailable("503"), VenueError),
            (ccxt.NetworkError("reset"), VenueError),
            (ccxt.ExchangeError("something"), VenueError),
            (ccxt.RateLimitExceeded("429"), RateLimitedError),
            (ccxt.DDoSProtection("418"), VenueBannedError),
            (ccxt.BadSymbol("no such symbol"), ConfigError),
            (ccxt.BadRequest("bad interval"), ConfigError),
            (ccxt.AuthenticationError("401"), ConfigError),
            (ccxt.PermissionDenied("403"), ConfigError),
        ],
    )
    async def test_each_ccxt_error_maps_to_our_taxonomy(
        self, clock: ManualClock, raised: Exception, expected: type[Exception]
    ) -> None:
        client = transport(FakeExchange(error=raised), clock)
        with pytest.raises(expected):
            await client.get("klines", {}, weight=1)

    async def test_a_ban_outranks_the_rate_limit_classification(self, clock: ManualClock) -> None:
        """Both are `NetworkError` subclasses; matching broadly first would retry a ban."""
        assert issubclass(ccxt.DDoSProtection, ccxt.NetworkError)
        assert issubclass(ccxt.RateLimitExceeded, ccxt.NetworkError)
        client = transport(FakeExchange(error=ccxt.DDoSProtection("418")), clock)
        with pytest.raises(VenueBannedError):
            await client.get("klines", {}, weight=1)

    async def test_a_ban_latches_the_limiter(self, clock: ManualClock) -> None:
        """There is no path back from a ban without a human (PLAN §3.1)."""
        exchange = FakeExchange(error=ccxt.DDoSProtection("418"))
        client = transport(exchange, clock)
        with pytest.raises(VenueBannedError):
            await client.get("klines", {}, weight=1)
        exchange.error = None
        with pytest.raises(VenueBannedError, match="extends the ban"):
            await client.get("klines", {}, weight=1)

    async def test_a_rate_limit_penalises_the_next_call(self, clock: ManualClock) -> None:
        exchange = FakeExchange(error=ccxt.RateLimitExceeded("429"), headers={"Retry-After": "20"})
        client = transport(exchange, clock, budget=RateBudget(max_wait_seconds=60))
        with pytest.raises(RateLimitedError) as caught:
            await client.get("klines", {}, weight=1)
        assert caught.value.retry_after_seconds == 20.0
        exchange.error = None
        await client.get("klines", {}, weight=1)
        assert clock.monotonic() == pytest.approx(20.0)

    async def test_a_non_numeric_retry_after_is_ignored(self, clock: ManualClock) -> None:
        exchange = FakeExchange(
            error=ccxt.RateLimitExceeded("429"), headers={"Retry-After": "later"}
        )
        with pytest.raises(RateLimitedError) as caught:
            await transport(exchange, clock).get("klines", {}, weight=1)
        assert caught.value.retry_after_seconds is None

    async def test_repeated_failures_open_the_circuit(self, clock: ManualClock) -> None:
        """After N failures we stop calling, rather than hammering a venue into banning us."""
        exchange = FakeExchange(error=ccxt.ExchangeNotAvailable("503"))
        client = transport(exchange, clock, budget=RateBudget(failure_threshold=2))
        for _ in range(2):
            with pytest.raises(VenueError):
                await client.get("klines", {}, weight=1)
        with pytest.raises(CircuitOpenError, match="circuit open"):
            await client.get("klines", {}, weight=1)
        assert len(exchange.calls) == 2  # the third never reached the venue

    async def test_a_success_closes_the_circuit_again(self, clock: ManualClock) -> None:
        exchange = FakeExchange(error=ccxt.ExchangeNotAvailable("503"))
        client = transport(exchange, clock, budget=RateBudget(failure_threshold=3))
        with pytest.raises(VenueError):
            await client.get("klines", {}, weight=1)
        exchange.error = None
        await client.get("klines", {}, weight=1)
        assert client.limiter.circuit.allows_call

    async def test_an_unclassified_error_is_still_a_venue_error(self, clock: ManualClock) -> None:
        """Fail closed on the unknown: no caller may receive a raw library exception."""
        client = transport(FakeExchange(error=RuntimeError("who knows")), clock)
        with pytest.raises(VenueError, match="failed"):
            await client.get("klines", {}, weight=1)


class TestRealTransportConstruction:
    """Constructs the actual ccxt client — no request is made, so this stays offline."""

    @pytest.mark.parametrize("sandbox", [False, True])
    async def test_the_resolved_host_matches_the_sandbox_flag(
        self, clock: ManualClock, sandbox: bool
    ) -> None:
        client = binance_spot_transport(clock, sandbox=sandbox)
        try:
            assert client.venue_id == "binance"
        finally:
            await client.close()

    async def test_it_carries_no_credentials(self, clock: ManualClock) -> None:
        client = binance_spot_transport(clock)
        try:
            assert_no_credentials(client._exchange, "binance")
        finally:
            await client.close()
