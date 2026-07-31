"""The two signed transports, and the one behaviour that matters most in the system.

An order-placing call whose outcome cannot be determined must raise `SubmitUnknownError` and
nothing else. Every other classification — a rejection, a rate limit, a ban — is a definite answer
that nothing was placed, and treating one of those as ambiguous (or the reverse) is the
duplicate-order failure the whole design exists to prevent (PLAN §2.3, R1).

The mode assertions are here too: a transport that resolves to the wrong host is the mode confusion
PLAN §2.4 calls catastrophic, and it is caught at construction rather than at the first order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import ccxt.async_support as ccxt
import httpx
import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.enums import Mode
from tradebot.core.errors import (
    ConfigError,
    ModeConfusionError,
    OrderNotFoundError,
    OrderRejectedError,
    RateLimitedError,
    SubmitUnknownError,
    VenueBannedError,
    VenueError,
)
from tradebot.venues.alpaca_transport import ALPACA_HOSTS, AlpacaTransport
from tradebot.venues.ccxt_transport import (
    BINANCE_HOSTS,
    CCXT_METHODS,
    MODE_SANDBOX,
    CcxtSignedTransport,
    CcxtTransport,
    assert_mode_endpoint,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
ORDER_PARAMS = {"symbol": "BTCUSDT", "newClientOrderId": "pap-ABC123"}


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


class FakeExchange:
    """A ccxt-shaped exchange whose every method raises what a test asks it to."""

    def __init__(self, error: Exception | None = None, *, sandbox: bool = True) -> None:
        host = BINANCE_HOSTS[sandbox]
        self.urls = {
            "api": {"public": f"https://{host}/api/v3", "private": f"https://{host}/api/v3"}
        }
        self.apiKey = "key"
        self.secret = "secret"
        self.last_response_headers: dict[str, str] = {}
        self.closed = False
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _method(self, name: str) -> Any:
        async def call(params: dict[str, Any]) -> Any:
            self.calls.append((name, params))
            if self._error is not None:
                raise self._error
            return {"status": "NEW", "clientOrderId": params.get("newClientOrderId")}

        return call

    def __getattr__(self, name: str) -> Any:
        """Only the implicit methods the transport actually maps.

        Matching on a `private*` prefix instead would make `privateKey` — one of the attributes
        `assert_no_credentials` looks for — answer as a bound method and read as a held credential.
        """
        if name in set(CCXT_METHODS.values()):
            return self._method(name)
        raise AttributeError(name)

    async def close(self) -> None:
        self.closed = True


def signed(
    clock: ManualClock, error: Exception | None = None, **kwargs: Any
) -> CcxtSignedTransport:
    mode = kwargs.pop("mode", Mode.PAPER)
    exchange = FakeExchange(error, sandbox=MODE_SANDBOX[mode])
    return CcxtSignedTransport(
        exchange,
        clock,
        venue_id="binance",
        expected_host=BINANCE_HOSTS[MODE_SANDBOX[mode]],
        mode=mode,
        **kwargs,
    )


class TestAmbiguousSubmits:
    async def test_a_transient_failure_placing_an_order_becomes_submit_unknown(
        self, clock: ManualClock
    ) -> None:
        """The request may have reached the matching engine; retrying it opens a second position."""
        transport = signed(clock, ccxt.RequestTimeout("read timed out"))
        with pytest.raises(SubmitUnknownError) as raised:
            await transport.call("newOrder", ORDER_PARAMS, weight=1, is_order=True)
        assert raised.value.client_order_id == "pap-ABC123"

    async def test_an_oco_placement_is_named_by_its_list_id(self, clock: ManualClock) -> None:
        transport = signed(clock, ccxt.NetworkError("connection reset"))
        with pytest.raises(SubmitUnknownError) as raised:
            await transport.call(
                "newOco", {"listClientOrderId": "pap-GROUP"}, weight=1, is_order=True
            )
        assert raised.value.client_order_id == "pap-GROUP"

    async def test_a_rejection_stays_a_rejection(self, clock: ManualClock) -> None:
        """A definite "no" must not become an ambiguity: nothing was placed and we know it."""
        transport = signed(clock, ccxt.InsufficientFunds("balance too low"))
        with pytest.raises(OrderRejectedError):
            await transport.call("newOrder", ORDER_PARAMS, weight=1, is_order=True)

    async def test_a_rate_limit_stays_a_rate_limit(self, clock: ManualClock) -> None:
        """A 429 is refused *before* processing, so the order does not exist."""
        transport = signed(clock, ccxt.RateLimitExceeded("too many requests"))
        with pytest.raises(RateLimitedError):
            await transport.call("newOrder", ORDER_PARAMS, weight=1, is_order=True)

    async def test_a_ban_stays_fatal(self, clock: ManualClock) -> None:
        """Continuing to call a banned IP extends the ban; recovery adopts the order later."""
        transport = signed(clock, ccxt.DDoSProtection("418"))
        with pytest.raises(VenueBannedError):
            await transport.call("newOrder", ORDER_PARAMS, weight=1, is_order=True)

    async def test_a_transient_failure_on_a_read_is_merely_retryable(
        self, clock: ManualClock
    ) -> None:
        """Nothing was placed, so there is nothing ambiguous to resolve."""
        transport = signed(clock, ccxt.RequestTimeout("read timed out"))
        with pytest.raises(VenueError):
            await transport.call("queryOrder", ORDER_PARAMS, weight=4)

    async def test_a_placement_without_an_id_is_a_defect_not_a_runtime_case(
        self, clock: ManualClock
    ) -> None:
        """Recovery would have nothing to query the venue by, so this must never ship."""
        transport = signed(clock, ccxt.RequestTimeout("read timed out"))
        with pytest.raises(ConfigError, match="without a client order id"):
            await transport.call("newOrder", {"symbol": "BTCUSDT"}, weight=1, is_order=True)

    async def test_the_read_transport_can_never_produce_submit_unknown(
        self, clock: ManualClock
    ) -> None:
        """A data client that could report an ambiguous *order* would be one holding a key."""
        exchange = FakeExchange(ccxt.RequestTimeout("timed out"), sandbox=True)
        exchange.apiKey = ""
        exchange.secret = ""
        transport = CcxtTransport(
            exchange, clock, venue_id="binance", expected_host=BINANCE_HOSTS[True]
        )
        assert not transport.escalates_unknown_submits
        with pytest.raises(VenueError):
            await transport.get("klines", {}, weight=2)


class TestErrorTaxonomy:
    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (ccxt.OrderNotFound("unknown order"), OrderNotFoundError),
            (ccxt.InvalidOrder("filter failure"), OrderRejectedError),
            (ccxt.InsufficientFunds("no balance"), OrderRejectedError),
            (ccxt.AuthenticationError("bad key"), ConfigError),
            (ccxt.InvalidNonce("recvWindow"), ConfigError),
            (ccxt.BadRequest("nonsense"), ConfigError),
            (ccxt.ExchangeNotAvailable("maintenance"), VenueError),
        ],
    )
    async def test_every_venue_error_lands_in_our_taxonomy(
        self, clock: ManualClock, raised: Exception, expected: type[Exception]
    ) -> None:
        transport = signed(clock, raised)
        with pytest.raises(expected):
            await transport.call("queryOrder", ORDER_PARAMS, weight=4)

    async def test_a_skewed_clock_is_fatal_rather_than_retried(self, clock: ManualClock) -> None:
        """Every further signed call is rejected identically, and repeated auth failure bans."""
        transport = signed(clock, ccxt.InvalidNonce("timestamp outside recvWindow"))
        with pytest.raises(ConfigError, match="receive window"):
            await transport.call("queryOrder", ORDER_PARAMS, weight=4)


class TestRateBudgetSharing:
    async def test_an_order_charges_the_order_windows_too(self, clock: ManualClock) -> None:
        """Market-data reads must never be able to exhaust the allowance a submit needs."""
        transport = signed(clock)
        before = transport.limiter.weight_used
        await transport.call("newOrder", ORDER_PARAMS, weight=1, is_order=True)
        assert transport.limiter.weight_used > before

    async def test_both_transports_can_share_one_limiter(self, clock: ManualClock) -> None:
        """A venue bans an IP and a key, not a code path (PLAN §3.1)."""
        exchange = FakeExchange(sandbox=True)
        exchange.apiKey = ""
        exchange.secret = ""
        data = CcxtTransport(exchange, clock, venue_id="binance", expected_host=BINANCE_HOSTS[True])
        trading = signed(clock, limiter=data.limiter)
        await data.get("klines", {}, weight=5)
        assert trading.limiter is data.limiter
        assert trading.limiter.weight_used >= 5


class TestModeSafety:
    def test_a_sandbox_flag_contradicting_the_mode_refuses(self) -> None:
        with pytest.raises(ModeConfusionError, match="sandbox"):
            assert_mode_endpoint(Mode.LIVE, True, "binance")

    def test_paper_must_be_a_sandbox(self) -> None:
        with pytest.raises(ModeConfusionError):
            assert_mode_endpoint(Mode.PAPER, False, "binance")

    def test_live_is_the_only_mode_allowed_the_real_exchange(self) -> None:
        assert MODE_SANDBOX[Mode.LIVE] is False
        assert all(MODE_SANDBOX[mode] for mode in (Mode.SIM, Mode.PAPER))

    def test_a_trading_transport_without_a_key_refuses_to_be_built(
        self, clock: ManualClock
    ) -> None:
        """It would fail on its first order, mid-cycle, having already committed an intent."""
        exchange = FakeExchange(sandbox=True)
        exchange.apiKey = ""
        with pytest.raises(ConfigError, match="cannot sign"):
            CcxtSignedTransport(
                exchange,
                clock,
                venue_id="binance",
                expected_host=BINANCE_HOSTS[True],
                mode=Mode.PAPER,
            )

    def test_a_host_that_does_not_match_the_mode_refuses(self, clock: ManualClock) -> None:
        exchange = FakeExchange(sandbox=False)  # live host
        with pytest.raises(ConfigError, match="expected"):
            CcxtSignedTransport(
                exchange,
                clock,
                venue_id="binance",
                expected_host=BINANCE_HOSTS[True],  # but asked for the testnet
                mode=Mode.PAPER,
            )


class TestAlpacaTransport:
    def _transport(self, clock: ManualClock, handler: Any, **kwargs: Any) -> AlpacaTransport:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return AlpacaTransport(
            client,
            clock,
            mode=kwargs.pop("mode", Mode.PAPER),
            key_id="k",
            secret_key="s",
            **kwargs,
        )

    async def test_a_5xx_while_placing_an_order_becomes_submit_unknown(
        self, clock: ManualClock
    ) -> None:
        transport = self._transport(clock, lambda _r: httpx.Response(502, json={"message": "bad"}))
        with pytest.raises(SubmitUnknownError) as raised:
            await transport.call(
                "POST /v2/orders", {"client_order_id": "pap-XYZ"}, weight=1, is_order=True
            )
        assert raised.value.client_order_id == "pap-XYZ"

    async def test_a_422_is_a_rejection_not_an_ambiguity(self, clock: ManualClock) -> None:
        transport = self._transport(
            clock, lambda _r: httpx.Response(422, json={"message": "insufficient qty"})
        )
        with pytest.raises(OrderRejectedError):
            await transport.call(
                "POST /v2/orders", {"client_order_id": "pap-XYZ"}, weight=1, is_order=True
            )

    async def test_a_404_on_a_lookup_is_not_found(self, clock: ManualClock) -> None:
        transport = self._transport(clock, lambda _r: httpx.Response(404, json={}))
        with pytest.raises(OrderNotFoundError):
            await transport.call("GET /v2/orders:by_client_order_id", {}, weight=1)

    async def test_a_403_about_buying_power_is_a_rejection(self, clock: ManualClock) -> None:
        """Alpaca overloads 403; only the wording separates a refusal from a bad key."""
        transport = self._transport(
            clock, lambda _r: httpx.Response(403, json={"message": "insufficient buying power"})
        )
        with pytest.raises(OrderRejectedError):
            await transport.call("POST /v2/orders", {"client_order_id": "x"}, weight=1)

    async def test_a_403_about_permissions_is_a_configuration_error(
        self, clock: ManualClock
    ) -> None:
        transport = self._transport(
            clock, lambda _r: httpx.Response(403, json={"message": "account is not authorized"})
        )
        with pytest.raises(ConfigError):
            await transport.call("GET /v2/account", {}, weight=1)

    async def test_a_401_is_a_configuration_error(self, clock: ManualClock) -> None:
        transport = self._transport(clock, lambda _r: httpx.Response(401, json={}))
        with pytest.raises(ConfigError, match="credentials"):
            await transport.call("GET /v2/account", {}, weight=1)

    async def test_a_429_carries_the_venues_retry_after(self, clock: ManualClock) -> None:
        transport = self._transport(
            clock, lambda _r: httpx.Response(429, json={}, headers={"Retry-After": "7"})
        )
        with pytest.raises(RateLimitedError) as raised:
            await transport.call("GET /v2/account", {}, weight=1)
        assert raised.value.retry_after_seconds == 7

    async def test_a_non_json_body_is_not_the_endpoint_we_think(self, clock: ManualClock) -> None:
        transport = self._transport(clock, lambda _r: httpx.Response(200, text="<html>nope"))
        with pytest.raises(VenueError, match="non-JSON"):
            await transport.call("GET /v2/account", {}, weight=1)

    async def test_a_paper_transport_cannot_be_pointed_at_the_live_host(
        self, clock: ManualClock
    ) -> None:
        with pytest.raises(ModeConfusionError, match="paper"):
            self._transport(
                clock,
                lambda _r: httpx.Response(200, json={}),
                base_url=f"https://{ALPACA_HOSTS[Mode.LIVE]}",
            )

    async def test_plaintext_endpoints_are_refused(self, clock: ManualClock) -> None:
        """Credentials travel in headers; http would put them on the wire in clear."""
        with pytest.raises(ConfigError, match="https"):
            self._transport(
                clock,
                lambda _r: httpx.Response(200, json={}),
                base_url=f"http://{ALPACA_HOSTS[Mode.PAPER]}",
            )

    async def test_a_missing_key_refuses_construction(self, clock: ManualClock) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
        with pytest.raises(ConfigError, match="missing its key"):
            AlpacaTransport(client, clock, mode=Mode.PAPER, key_id="", secret_key="s")
