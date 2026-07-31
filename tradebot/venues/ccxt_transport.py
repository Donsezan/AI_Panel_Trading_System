"""ccxt transport: the only module in the system that imports an exchange client library.

Two transports, deliberately separate classes over the same machinery:

* `CcxtTransport` — public reads, and it **asserts it holds no credentials**. A data client
  carrying an API key is a data client that could sign an order; the cheapest way to guarantee it
  never does is to refuse to hold the key (PLAN §2.4, §3.2).
* `CcxtSignedTransport` — the calls that can move money, and it asserts the opposite: credentials
  present, host matching the declared mode, order-count windows charged.

Both do the same four jobs and no parsing:

1. **Spend the rate budget** before every call and reconcile it against the venue's own
   used-weight header afterwards (PLAN §3.1). When the two transports are built together they
   share one limiter and one circuit breaker, because a ban applies to the IP and key.
2. **Classify failures into our taxonomy** so that no caller ever inspects an HTTP status or a
   library-specific exception. Binance's `418` — the IP auto-ban — becomes `VenueBannedError`
   and latches the limiter: continuing to call a banned IP extends the ban.
3. **Assert the resolved endpoint**, so a hijacked or fat-fingered config cannot point the
   process at an arbitrary host, and so live can never be reached from a paper run (PLAN §2.4).
4. **Assert the credential posture** each transport is entitled to.

ccxt's own `enableRateLimit` is left on as a *floor*. Our budget is the ceiling: ccxt's throttle
counts requests, and Binance bans on weight.

Failure semantics: every raised error is one of `VenueError` (retry), `RateLimitedError` (retry
after the venue's delay), `CircuitOpenError` (stop calling for now), `ConfigError` (refuse — a
defect in what we asked for), `VenueBannedError` (fatal, trips the kill switch), or — for an
order-placing call whose outcome is unknowable — `SubmitUnknownError`, which may only be resolved
by querying the venue for our own `client_order_id` (PLAN §2.3).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

import ccxt.async_support as ccxt

from tradebot.core.clock import Clock
from tradebot.core.enums import Mode
from tradebot.core.errors import (
    ConfigError,
    ModeConfusionError,
    OrderNotFoundError,
    OrderRejectedError,
    RateLimitedError,
    SubmitUnknownError,
    TradebotError,
    VenueBannedError,
    VenueError,
)
from tradebot.core.logging import get_logger
from tradebot.core.ratelimit import RateBudget, VenueRateLimiter

logger = get_logger(__name__)

#: Symbolic endpoint → ccxt implicit method. Dispatch rather than string building, so an
#: endpoint this transport does not know about fails at the call site and not at the venue.
CCXT_METHODS: Final[Mapping[str, str]] = {
    "klines": "publicGetKlines",
    "ticker24h": "publicGetTicker24hr",
    "exchangeInfo": "publicGetExchangeInfo",
    "time": "publicGetTime",
    # Signed. `order` is POST-new / GET-query / DELETE-cancel on the same path, so the verb is
    # part of the symbolic name rather than a parameter — a typo then cannot cancel a submit.
    "newOrder": "privatePostOrder",
    "queryOrder": "privateGetOrder",
    "cancelOrder": "privateDeleteOrder",
    "openOrders": "privateGetOpenOrders",
    "myTrades": "privateGetMyTrades",
    "account": "privateGetAccount",
    "newOco": "privatePostOrderListOco",
    "cancelOrderList": "privateDeleteOrderList",
    "apiRestrictions": "sapiGetAccountApiRestrictions",
}

#: The ccxt methods that place an order. They charge the order-count windows, and an ambiguous
#: outcome on one of them is `SUBMIT_UNKNOWN` rather than a retryable failure.
ORDER_PLACING_ENDPOINTS: Final = frozenset({"newOrder", "newOco"})

#: Public host per sandbox setting. Anything else means the config is not describing Binance.
BINANCE_HOSTS: Final[Mapping[bool, str]] = {
    False: "api.binance.com",
    True: "testnet.binance.vision",
}

#: Which Binance host each mode is allowed to reach. Live is the only mode permitted the real
#: exchange, and it is spelled out here rather than derived, so no boolean flip can promote a
#: paper run to a live one (PLAN §2.4, R2).
MODE_SANDBOX: Final[Mapping[Mode, bool]] = {Mode.SIM: True, Mode.PAPER: True, Mode.LIVE: False}

DEFAULT_TIMEOUT_MS: Final = 15_000


def _banned(venue: str, endpoint: str, exc: Exception, _retry: float | None) -> TradebotError:
    return VenueBannedError(f"{venue} banned this IP on {endpoint} (HTTP 418): {exc}")


def _rate_limited(venue: str, endpoint: str, exc: Exception, retry: float | None) -> TradebotError:
    return RateLimitedError(f"{venue} rate-limited {endpoint}: {exc}", retry_after_seconds=retry)


def _clock_skew(venue: str, endpoint: str, exc: Exception, _retry: float | None) -> TradebotError:
    """A signature the venue timed out of its receive window.

    Fatal rather than retryable: every further signed call is rejected the same way, and repeated
    auth failure is itself a ban vector (PLAN §3.1). The startup skew check exists to catch this
    before an order depends on it; reaching it here means the clock has drifted since.
    """
    return ConfigError(
        f"{venue} rejected the signature on {endpoint} as outside its receive window: {exc}. "
        "The system clock has drifted from the venue's; fix the clock before trading."
    )


def _auth(venue: str, endpoint: str, exc: Exception, _retry: float | None) -> TradebotError:
    return ConfigError(f"{venue} rejected our credentials on {endpoint}: {exc}")


def _not_found(venue: str, endpoint: str, exc: Exception, _retry: float | None) -> TradebotError:
    return OrderNotFoundError(f"{venue} has no record of the order queried by {endpoint}: {exc}")


def _rejected(venue: str, endpoint: str, exc: Exception, _retry: float | None) -> TradebotError:
    return OrderRejectedError(f"{venue} rejected the order on {endpoint}: {exc}", reason=str(exc))


def _bad_request(venue: str, endpoint: str, exc: Exception, _retry: float | None) -> TradebotError:
    return ConfigError(f"{venue} rejected our request to {endpoint} as invalid: {exc}")


def _transient(venue: str, endpoint: str, exc: Exception, retry: float | None) -> TradebotError:
    return VenueError(f"{venue} {endpoint} failed transiently: {exc}", retry_after_seconds=retry)


#: Checked in order, most specific first — the order is the contract. ccxt's rate-limit, ban and
#: nonce errors are all `NetworkError` subclasses and `OrderNotFound` is an `InvalidOrder`, so a
#: broad match placed early would swallow the specific case it matters most to distinguish.
_CLASSIFIERS: Final[
    tuple[tuple[type[Exception], Callable[[str, str, Exception, float | None], TradebotError]], ...]
] = (
    (ccxt.DDoSProtection, _banned),
    (ccxt.RateLimitExceeded, _rate_limited),
    (ccxt.InvalidNonce, _clock_skew),
    (ccxt.AuthenticationError, _auth),
    (ccxt.OrderNotFound, _not_found),
    (ccxt.InvalidOrder, _rejected),
    (ccxt.InsufficientFunds, _rejected),
    (ccxt.BadRequest, _bad_request),
    (ccxt.NotSupported, _bad_request),
    (ccxt.NetworkError, _transient),
    (ccxt.ExchangeError, _transient),
)

#: Params a venue-safe `client_order_id` can travel in, so an ambiguous submit can name the order
#: it may have created. Binance uses one key for a single order and another for an OCO list.
_CLIENT_ID_PARAMS: Final = ("newClientOrderId", "listClientOrderId")


class _CcxtCalls:
    """Shared machinery: budget, header reconciliation, error taxonomy, session lifetime.

    Subclasses differ only in their credential posture and in what a failed call *means* — which
    is exactly the distinction worth having in the type system, since one of them can move money.
    """

    #: Whether an ambiguous outcome on an order-placing endpoint becomes `SUBMIT_UNKNOWN`. Only a
    #: signed transport can place an order, so only that subclass turns this on — and the read path
    #: therefore has no code that could produce the state at all (PLAN §2.3).
    escalates_unknown_submits: ClassVar[bool] = False

    def __init__(
        self,
        exchange: Any,
        clock: Clock,
        *,
        venue_id: str,
        budget: RateBudget | None = None,
        limiter: VenueRateLimiter | None = None,
    ) -> None:
        self.venue_id = venue_id
        self._exchange = exchange
        #: Shared with the sibling transport when one is passed in: one venue, one budget, one
        #: breaker, because a ban applies to the IP and key rather than to a code path.
        self._limiter = limiter or VenueRateLimiter(venue_id, clock, budget)
        self._closed = False

    @property
    def limiter(self) -> VenueRateLimiter:
        return self._limiter

    async def _invoke(
        self, endpoint: str, params: Mapping[str, Any], *, weight: int, is_order: bool = False
    ) -> Any:
        method = self._method(endpoint)
        await self._limiter.acquire(weight, is_order=is_order)
        try:
            payload = await method(dict(params))
        except Exception as exc:
            self._observe_headers()
            self._limiter.record_failure()
            error = self._classify(endpoint, exc)
            if self.escalates_unknown_submits and endpoint in ORDER_PLACING_ENDPOINTS:
                error = _as_submit_unknown(self.venue_id, endpoint, error, params)
            raise error from exc
        self._observe_headers()
        self._limiter.record_success()
        return payload

    async def close(self) -> None:
        """Release the HTTP session. ccxt's async client leaks a connector without this."""
        if not self._closed:
            self._closed = True
            await self._exchange.close()

    def _method(self, endpoint: str) -> Callable[[dict[str, Any]], Any]:
        name = CCXT_METHODS.get(endpoint)
        if name is None:
            raise ConfigError(f"no ccxt method mapped for endpoint {endpoint!r}")
        return getattr(self._exchange, name)  # type: ignore[no-any-return]

    def _observe_headers(self) -> None:
        headers = getattr(self._exchange, "last_response_headers", None) or {}
        self._limiter.observe_used_weight({str(k): str(v) for k, v in dict(headers).items()})

    def _retry_after(self) -> float | None:
        headers = getattr(self._exchange, "last_response_headers", None) or {}
        for key, value in dict(headers).items():
            if str(key).lower() == "retry-after" and str(value).strip().isdigit():
                return float(str(value).strip())
        return None

    def _classify(self, endpoint: str, exc: Exception) -> TradebotError:
        retry_after = self._retry_after()
        error = self._build(endpoint, exc, retry_after)
        self._react(error, retry_after)
        return error

    def _build(self, endpoint: str, exc: Exception, retry_after: float | None) -> TradebotError:
        for error_type, build in _CLASSIFIERS:
            if isinstance(exc, error_type):
                return build(self.venue_id, endpoint, exc, retry_after)
        logger.error(
            "unclassified venue failure", extra={"endpoint": endpoint, "error": type(exc).__name__}
        )
        return VenueError(f"{self.venue_id} {endpoint} failed: {exc}")

    def _react(self, error: TradebotError, retry_after: float | None) -> None:
        """Apply the limiter-side consequence of a classified failure."""
        if isinstance(error, VenueBannedError):
            self._limiter.ban(str(error))
        elif isinstance(error, RateLimitedError):
            self._limiter.penalise(retry_after)


class CcxtTransport(_CcxtCalls):
    """`VenueTransport` over a ccxt async exchange. Reads public books, holds no key."""

    def __init__(
        self,
        exchange: Any,
        clock: Clock,
        *,
        venue_id: str,
        expected_host: str,
        budget: RateBudget | None = None,
        limiter: VenueRateLimiter | None = None,
    ) -> None:
        super().__init__(exchange, clock, venue_id=venue_id, budget=budget, limiter=limiter)
        assert_no_credentials(exchange, venue_id)
        assert_host(exchange, expected_host, venue_id, "public")

    async def get(self, endpoint: str, params: Mapping[str, Any], *, weight: int) -> Any:
        return await self._invoke(endpoint, params, weight=weight)


class CcxtSignedTransport(_CcxtCalls):
    """`TradingTransport` over a ccxt async exchange. Signs, and can therefore move money.

    Two assertions at construction, both of which refuse to start rather than warn: credentials are
    present (an unsigned trading client fails on its first order, mid-cycle, having already
    committed an intent), and the resolved *private* host matches the mode (PLAN §2.4). The third
    live precondition — the venue's own report that withdrawals are disabled — needs a call, so it
    belongs to the startup preflight.
    """

    escalates_unknown_submits = True

    def __init__(
        self,
        exchange: Any,
        clock: Clock,
        *,
        venue_id: str,
        expected_host: str,
        mode: Mode,
        budget: RateBudget | None = None,
        limiter: VenueRateLimiter | None = None,
    ) -> None:
        super().__init__(exchange, clock, venue_id=venue_id, budget=budget, limiter=limiter)
        self.mode = mode
        assert_credentials(exchange, venue_id)
        assert_host(exchange, expected_host, venue_id, "private")

    async def call(
        self, endpoint: str, params: Mapping[str, Any], *, weight: int, is_order: bool = False
    ) -> Any:
        return await self._invoke(endpoint, params, weight=weight, is_order=is_order)


def _as_submit_unknown(
    venue_id: str, endpoint: str, error: TradebotError, params: Mapping[str, Any]
) -> TradebotError:
    """A transient failure on a call that *places* an order is not transient — it is unknown.

    The request may have reached the matching engine before the connection died. Retrying it is how
    one decision becomes two positions, so the only legal next step is querying the venue for the
    id we sent (PLAN §2.3, R1). Rejections, bans and rate limits are left alone: each of those is a
    definite answer that nothing was placed.
    """
    if not isinstance(error, VenueError) or isinstance(error, RateLimitedError):
        return error
    return SubmitUnknownError(
        f"{venue_id} {endpoint} left the order's outcome unknown: {error}",
        client_order_id=_client_order_id_of(params),
    )


def _client_order_id_of(params: Mapping[str, Any]) -> str:
    """The id an ambiguous submit must be queried by. Absent is a defect, not a runtime case."""
    for key in _CLIENT_ID_PARAMS:
        value = params.get(key)
        if value:
            return str(value)
    raise ConfigError(
        "an order was submitted without a client order id; recovery would have nothing to query "
        f"the venue by (expected one of {', '.join(_CLIENT_ID_PARAMS)})"
    )


def assert_credentials(exchange: Any, venue_id: str) -> None:
    """Refuse a trading client that cannot sign. It would fail on its first order, mid-cycle."""
    missing = [name for name in ("apiKey", "secret") if not getattr(exchange, name, None)]
    if missing:
        raise ConfigError(
            f"{venue_id} trading transport is missing {', '.join(missing)}; it cannot sign an "
            "order and must not be constructed at all"
        )


def assert_no_credentials(exchange: Any, venue_id: str) -> None:
    """Refuse a market-data client that holds a key. It has no use for one."""
    held = [
        name
        for name in ("apiKey", "secret", "password", "privateKey", "walletAddress")
        if getattr(exchange, name, None)
    ]
    if held:
        raise ConfigError(
            f"{venue_id} market-data transport was given credentials ({', '.join(held)}); "
            "it reads public books only and must not be able to sign anything"
        )


def assert_host(exchange: Any, expected_host: str, venue_id: str, section: str = "public") -> None:
    """Assert the resolved endpoint is the host we intended (PLAN §2.4).

    Checked per API section, because the section a call lands in is what decides which host it
    reaches: a client whose public reads point at a testnet while its signed calls point at the
    real exchange is precisely the mode confusion this assertion exists to catch.
    """
    url = str(exchange.urls["api"][section])
    host = urlsplit(url).hostname
    if host != expected_host:
        raise ConfigError(
            f"{venue_id} resolved its {section} endpoint to {host!r}, expected {expected_host!r}; "
            "refusing to talk to an unexpected host"
        )


def assert_mode_endpoint(mode: Mode, sandbox: bool, venue_id: str) -> None:
    """Refuse a sandbox flag that contradicts the mode (PLAN §2.4, R2).

    The one failure that loses real money without any bug being visible is running live while
    believing you are on a testnet. Asserted here, at the transport, because this is the object
    that actually knows which host it resolved.
    """
    if MODE_SANDBOX[mode] != sandbox:
        raise ModeConfusionError(
            f"{venue_id} was asked for sandbox={sandbox} in {mode.value} mode, which requires "
            f"sandbox={MODE_SANDBOX[mode]}. Refusing to start: this is the mode confusion that "
            "sends a paper order to a live exchange."
        )


def binance_exchange(
    *, sandbox: bool, timeout_ms: int, credentials: tuple[str, str] | None = None
) -> Any:
    """One configured ccxt Binance spot client. `adjustForTimeDifference` stays off deliberately:
    silently rewriting our timestamps would hide the clock skew the startup check must see."""
    settings: dict[str, Any] = {
        "enableRateLimit": True,  # a floor under our own budget, never the ceiling
        "timeout": timeout_ms,
        "options": {"defaultType": "spot", "adjustForTimeDifference": False},
    }
    if credentials is not None:
        settings["apiKey"], settings["secret"] = credentials
    exchange = ccxt.binance(settings)
    if sandbox:
        exchange.set_sandbox_mode(True)
    return exchange


def binance_spot_transport(
    clock: Clock,
    *,
    sandbox: bool = False,
    budget: RateBudget | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> CcxtTransport:
    """Build the real Binance spot market-data transport. Composition root only."""
    return CcxtTransport(
        binance_exchange(sandbox=sandbox, timeout_ms=timeout_ms),
        clock,
        venue_id="binance",
        expected_host=BINANCE_HOSTS[sandbox],
        budget=budget,
    )


def binance_spot_trading_transport(
    clock: Clock,
    credentials: tuple[str, str],
    *,
    mode: Mode,
    budget: RateBudget | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    limiter: VenueRateLimiter | None = None,
) -> CcxtSignedTransport:
    """Build the real Binance spot trading transport. Composition root only.

    Pass the market-data transport's `limiter` so both spend one budget — the whole reason the
    two transports are built from the same module (PLAN §3.1).
    """
    sandbox = MODE_SANDBOX[mode]
    assert_mode_endpoint(mode, sandbox, "binance")
    return CcxtSignedTransport(
        binance_exchange(sandbox=sandbox, timeout_ms=timeout_ms, credentials=credentials),
        clock,
        venue_id="binance",
        expected_host=BINANCE_HOSTS[sandbox],
        mode=mode,
        budget=budget,
        limiter=limiter,
    )
