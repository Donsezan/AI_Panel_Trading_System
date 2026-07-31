"""Alpaca transport: signed JSON over `httpx`, no vendor SDK.

The same trade ADR 0009 made for the LLM providers, for the same reasons. Alpaca's trading API is
a handful of REST calls; against that, a vendor SDK adds a dependency tree to a process that can
move money and brings retry machinery we specifically do not want — retries on an order-placing
call are the duplicate-order failure this design exists to prevent (PLAN §2.3, R1). Owning the
wire format also means we can *test* it: the whole adapter runs through `httpx.MockTransport`, so
the contract suite asserts the exact URL, headers and body Alpaca will receive, offline and free.

Mode safety is structural: the paper and live hosts are different domains, listed here per mode
and asserted at construction. There is no flag that turns one into the other (PLAN §2.4, R2).

Rate limiting shares the same `VenueRateLimiter` the rest of the system uses. Alpaca's published
limit is a request count per minute rather than a weight, so every call costs one unit of a budget
set below it; order-placing calls additionally charge the order windows.

Failure semantics — the taxonomy is the handling instruction, and an ambiguous *order placement*
is the one case that must never look retryable:

* timeout, connection reset, 5xx on a placement → `SubmitUnknownError` (query, never resubmit)
* timeout, reset, 5xx elsewhere                → `VenueError`
* 429                                          → `RateLimitedError`, honouring `Retry-After`
* 401/403                                      → `ConfigError` (bad key; refuse)
* 404 on an order lookup                       → `OrderNotFoundError`
* 403 insufficient buying power, 422           → `OrderRejectedError` (a definite "no")
* non-JSON or oversized body                   → `VenueError` (this is not the endpoint we think)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

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
    VenueError,
)
from tradebot.core.logging import SECRETS, get_logger
from tradebot.core.ratelimit import RateBudget, VenueRateLimiter

logger = get_logger(__name__)

VENUE_ID: Final = "alpaca"

#: Trading host per mode. Two different domains, not one host with a flag — which is what makes a
#: paper key physically unable to reach the live exchange (PLAN §2.4).
ALPACA_HOSTS: Final[Mapping[Mode, str]] = {
    Mode.SIM: "paper-api.alpaca.markets",
    Mode.PAPER: "paper-api.alpaca.markets",
    Mode.LIVE: "api.alpaca.markets",
}

#: Alpaca's basic plan allows 200 requests/minute. The budget sits below it; a research bot at
#: minutes-scale cadence has no reason to approach a venue's ceiling (PLAN §3.1).
DEFAULT_ALPACA_BUDGET: Final = RateBudget(
    weight_per_minute=120, orders_per_ten_seconds=5, orders_per_day=200
)

DEFAULT_TIMEOUT_SECONDS: Final = 15.0

#: 4 MB. A trading response above this is not a trading response.
MAX_BYTES: Final = 4 * 1024 * 1024

ERROR_EXCERPT_CHARS: Final = 200

#: Alpaca returns 403 for both "your key may not do that" and "insufficient buying power". Only
#: the wording separates a configuration defect from an ordinary rejection, so the wording is
#: matched — and the default is the *rejection*, because treating a real permission problem as a
#: rejection merely stops trading, while the reverse would keep retrying a key that cannot trade.
_REJECTION_MARKERS: Final = ("insufficient", "buying power", "not allowed", "cost basis")


class AlpacaTransport:
    """`TradingTransport` for Alpaca. Owns no client; the caller creates and closes one."""

    venue_id = VENUE_ID

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        *,
        mode: Mode,
        key_id: str,
        secret_key: str,
        base_url: str | None = None,
        budget: RateBudget | None = None,
        limiter: VenueRateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.mode = mode
        self._client = client
        self._base_url = (base_url or f"https://{ALPACA_HOSTS[mode]}").rstrip("/")
        self._limiter = limiter or VenueRateLimiter(
            VENUE_ID, clock, budget or DEFAULT_ALPACA_BUDGET
        )
        self._timeout = timeout_seconds
        assert_credentials(key_id, secret_key)
        assert_host(self._base_url, mode)
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
        }

    @property
    def limiter(self) -> VenueRateLimiter:
        return self._limiter

    async def call(
        self, endpoint: str, params: Mapping[str, Any], *, weight: int, is_order: bool = False
    ) -> Any:
        """Perform one signed call. `endpoint` is `"VERB path"`, e.g. `"POST /v2/orders"`.

        The verb travels with the path because Alpaca overloads paths by method — `DELETE
        /v2/orders/{id}` cancels what `GET /v2/orders/{id}` reads. Keeping them one token means a
        wrong verb is a wrong endpoint name, caught at the call site rather than at the venue.
        """
        method, _, path = endpoint.partition(" ")
        await self._limiter.acquire(weight, is_order=is_order)
        try:
            response = await self._send(method, path, params)
        except TradebotError as error:
            self._limiter.record_failure()
            raise self._escalate(error, is_order=is_order, params=params) from error

        self._limiter.observe_used_weight(dict(response.headers))
        failure = self._classify(method, path, response)
        if failure is not None:
            self._limiter.record_failure()
            self._react(failure, response)
            raise self._escalate(failure, is_order=is_order, params=params)
        self._limiter.record_success()
        return self._decode(response)

    async def close(self) -> None:
        """Nothing to release: the client belongs to whoever created it."""

    async def _send(self, method: str, path: str, params: Mapping[str, Any]) -> httpx.Response:
        """One request. A `None` parameter is omitted rather than sent as the string `"None"`."""
        body = {key: value for key, value in params.items() if value is not None}
        # A body for the verbs that carry one, a query string for the rest. Alpaca reads `POST`
        # bodies as JSON and everything else from the query, so the choice is the venue's, not ours.
        writes = method in ("POST", "PATCH", "PUT")
        request = self._client.build_request(
            method,
            f"{self._base_url}{path}",
            headers=self._headers,
            timeout=self._timeout,
            json=body if writes else None,
            params=None if writes else body,
        )
        try:
            return await self._client.send(request)
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise VenueError(f"alpaca {method} {path} timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise VenueError(f"alpaca {method} {path} transport failure: {exc}") from exc

    def _classify(self, method: str, path: str, response: httpx.Response) -> TradebotError | None:
        status = response.status_code
        if status < httpx.codes.BAD_REQUEST:
            return None
        excerpt = self._excerpt(response)
        if status == httpx.codes.TOO_MANY_REQUESTS:
            return RateLimitedError(
                f"alpaca rate-limited {path}", retry_after_seconds=_retry_after(response)
            )
        if status == httpx.codes.NOT_FOUND:
            return OrderNotFoundError(f"alpaca has no record for {method} {path}: {excerpt}")
        if status == httpx.codes.UNPROCESSABLE_ENTITY:
            return OrderRejectedError(f"alpaca rejected the order: {excerpt}", reason=excerpt)
        if status == httpx.codes.FORBIDDEN:
            return self._forbidden(path, excerpt)
        if status == httpx.codes.UNAUTHORIZED:
            return ConfigError(f"alpaca rejected our credentials on {path}: {excerpt}")
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            return VenueError(f"alpaca returned HTTP {status} on {path}: {excerpt}")
        return OrderRejectedError(f"alpaca refused {method} {path}: {excerpt}", reason=excerpt)

    def _forbidden(self, path: str, excerpt: str) -> TradebotError:
        if any(marker in excerpt.lower() for marker in _REJECTION_MARKERS):
            return OrderRejectedError(f"alpaca refused the order: {excerpt}", reason=excerpt)
        logger.error("alpaca refused the request outright", extra={"path": path})
        return ConfigError(f"alpaca forbade {path}: {excerpt}")

    def _escalate(
        self, error: TradebotError, *, is_order: bool, params: Mapping[str, Any]
    ) -> TradebotError:
        """A transient failure while *placing* an order is unknown, not transient (PLAN §2.3)."""
        if not is_order or not isinstance(error, VenueError):
            return error
        if isinstance(error, RateLimitedError):
            return error
        client_order_id = params.get("client_order_id")
        if not client_order_id:
            raise ConfigError(
                "an alpaca order was submitted without a client_order_id; recovery would have "
                "nothing to query the venue by"
            )
        return SubmitUnknownError(
            f"alpaca left the order's outcome unknown: {error}",
            client_order_id=str(client_order_id),
        )

    def _react(self, error: TradebotError, response: httpx.Response) -> None:
        if isinstance(error, RateLimitedError):
            self._limiter.penalise(_retry_after(response))

    def _decode(self, response: httpx.Response) -> Any:
        if len(response.content) > MAX_BYTES:
            raise VenueError(f"alpaca returned {len(response.content)} bytes, above the ceiling")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise VenueError(f"alpaca returned a non-JSON body: {self._excerpt(response)}") from exc

    @staticmethod
    def _excerpt(response: httpx.Response) -> str:
        """A short, scrubbed quote. Error bodies reach event rows, and a venue that echoes back a
        rejected credential header is not hypothetical (PLAN §3.2)."""
        return SECRETS.scrub(response.text[:ERROR_EXCERPT_CHARS].replace("\n", " ").strip())


def _retry_after(response: httpx.Response) -> float | None:
    raw = (response.headers.get("Retry-After") or "").strip()
    return float(raw) if raw.isdigit() else None


def assert_credentials(key_id: str, secret_key: str) -> None:
    if not key_id or not secret_key:
        raise ConfigError(
            "alpaca transport is missing its key id or secret; it cannot sign a request and must "
            "not be constructed at all"
        )


def assert_host(base_url: str, mode: Mode) -> None:
    """Refuse a host that contradicts the mode, and refuse plaintext outright (PLAN §2.4)."""
    parsed = httpx.URL(base_url)
    if parsed.scheme != "https":
        raise ConfigError(
            f"alpaca endpoint {base_url!r} is not https; credentials would cross the wire in clear"
        )
    if parsed.host != ALPACA_HOSTS[mode]:
        raise ModeConfusionError(
            f"alpaca resolved to {parsed.host!r} in {mode.value} mode, which requires "
            f"{ALPACA_HOSTS[mode]!r}. Refusing to start: a paper run must not be able to reach "
            "the live exchange."
        )
