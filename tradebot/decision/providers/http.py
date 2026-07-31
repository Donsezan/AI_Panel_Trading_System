"""HTTP plumbing shared by every LLM provider adapter.

Three vendor SDKs were deliberately not taken as dependencies. The three request shapes are
small and stable, and every one of them is a plain JSON POST; against that, three more
hash-pinned dependency trees in a process that can move money is a poor trade (PLAN §4,
[ADR 0009](../../../docs/adr/0009-llm-providers-over-plain-http.md)). One consequence matters
for testing: the whole provider layer exercises the *real* client through `httpx.MockTransport`,
so the suite proves the wire format rather than proving a mock returns what it was handed.

Failure semantics — **every** failure here raises `ProviderError`, without exception:

* timeout, connection reset, 5xx  → the provider is down
* 429                            → rate-limited, carrying the provider's `Retry-After`
* 401/403                        → bad or missing credentials
* 400/404                        → the model id is wrong or the slot has disappeared (R11)
* oversized or non-JSON body     → the endpoint is not what we think it is

That uniformity is the point. A seat reacts to a failed provider by trying the next binding in
its chain and then abstaining, and an abstention resolves to `WAIT` — so every one of these ends
in *no trade* rather than in an exception escaping a cycle (DESIGN §8.1). A configuration defect
that should refuse to start is caught in the registry, at wiring time, where it can still be
fatal.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Mapping
from typing import Any, Final

import httpx

from tradebot.core.clock import Clock
from tradebot.core.config import PriceList
from tradebot.core.errors import ProviderError
from tradebot.core.logging import SECRETS, get_logger
from tradebot.interfaces.llm import CompletionRequest, CompletionResult

logger = get_logger(__name__)

#: 8 MB. A completion above this is not a completion; it is a broken or hostile endpoint.
DEFAULT_MAX_BYTES: Final = 8 * 1024 * 1024

#: How much of an error body is quoted back. The excerpt lands in an abstain reason, which is
#: persisted, so it is both truncated and scrubbed before it goes anywhere.
ERROR_EXCERPT_CHARS: Final = 200


def dig(payload: Any, *path: str | int) -> Any:
    """Walk a nested response, returning `None` at the first step that does not exist.

    Provider responses are third-party JSON: a missing key is a bad response to be classified,
    never a `KeyError` escaping into a trading cycle.
    """
    for step in path:
        if isinstance(step, int):
            if not isinstance(payload, list) or len(payload) <= step:
                return None
            payload = payload[step]
            continue
        if not isinstance(payload, dict):
            return None
        payload = payload.get(step)
    return payload


def token_count(payload: Any, *path: str | int) -> int:
    """A usage counter, or zero. Never a float — token counts are integers everywhere."""
    value = dig(payload, *path)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class LLMHttpTransport:
    """Posts JSON to one provider's endpoint and classifies whatever comes back.

    Does not own its `httpx.AsyncClient`: the registry creates one client for the whole panel and
    closes it, so a cycle cannot leak a connection pool per seat.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        provider_id: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.provider_id = provider_id
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self._max_bytes = max_bytes

    async def post(
        self, path: str, payload: Mapping[str, Any], *, timeout_seconds: float
    ) -> dict[str, Any]:
        url = f"{self._base_url}/{path.lstrip('/')}"
        response = await self._send(url, payload, timeout_seconds)
        self._raise_for_status(response)
        self._check_size(response)
        return self._decode(response)

    async def _send(
        self, url: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> httpx.Response:
        """Post under both httpx's own timeouts and one hard ceiling on the whole call.

        httpx's read timeout bounds each individual read, not the response as a whole, so a
        server trickling bytes indefinitely would never trip it. A decision cycle that stalls
        forever on one seat is worse than a seat that abstains, so the wall-clock ceiling is
        what actually holds (DESIGN §8.1).
        """
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._client.post(
                    url, json=dict(payload), headers=self._headers, timeout=timeout_seconds
                )
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ProviderError(f"{self.provider_id} timed out after {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.provider_id} transport failure: {exc}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < httpx.codes.BAD_REQUEST:
            return
        if status == httpx.codes.TOO_MANY_REQUESTS:
            raise ProviderError(
                f"{self.provider_id} rate-limited us", retry_after_seconds=_retry_after(response)
            )
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            logger.error(
                "llm provider rejected our credentials",
                extra={"provider": self.provider_id, "status": status},
            )
            raise ProviderError(f"{self.provider_id} rejected our credentials (HTTP {status})")
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise ProviderError(f"{self.provider_id} returned HTTP {status}")
        # A 4xx here is usually a model id that no longer exists — the free-slot churn of R11.
        # The seat falls back to the next binding, so this abstains rather than halting anything.
        raise ProviderError(
            f"{self.provider_id} rejected the request (HTTP {status}): {self._excerpt(response)}"
        )

    def _check_size(self, response: httpx.Response) -> None:
        size = len(response.content)
        if size > self._max_bytes:
            raise ProviderError(
                f"{self.provider_id} returned {size} bytes, above the {self._max_bytes} ceiling"
            )

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.provider_id} returned a non-JSON body: {self._excerpt(response)}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                f"{self.provider_id} returned {type(payload).__name__}, not an object"
            )
        return payload

    @staticmethod
    def _excerpt(response: httpx.Response) -> str:
        """A short, scrubbed quote of a failing body.

        Scrubbed because this text reaches an abstain reason and therefore a database row, and a
        provider that echoes the rejected `Authorization` header back is not hypothetical
        (PLAN §3.2).
        """
        text = response.text[:ERROR_EXCERPT_CHARS].replace("\n", " ").strip()
        return SECRETS.scrub(text)


def _retry_after(response: httpx.Response) -> float | None:
    raw = (response.headers.get("Retry-After") or "").strip()
    return float(raw) if raw.isdigit() else None


class HttpLLMProvider(abc.ABC):
    """Template for a JSON-over-HTTP completion endpoint.

    Subclasses supply only what actually differs between vendors: the path, the request body, and
    where the text and token counts sit in the response. Everything shared — latency, cost,
    the empty-completion check, the fingerprint — is here once.
    """

    def __init__(
        self,
        transport: LLMHttpTransport,
        clock: Clock,
        *,
        provider_id: str,
        prices: PriceList | None = None,
    ) -> None:
        self.provider_id = provider_id
        self._transport = transport
        self._clock = clock
        self._prices = prices or PriceList()

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        started = self._clock.monotonic()
        payload = await self._transport.post(
            self._path(request), self._payload(request), timeout_seconds=request.timeout_seconds
        )
        latency_ms = int((self._clock.monotonic() - started) * 1000)

        text = self._text(payload)
        if not text.strip():
            raise ProviderError(f"{self.provider_id} returned an empty completion")

        prompt_tokens, completion_tokens = self._usage(payload)
        return CompletionResult(
            text=text,
            model_fingerprint=f"{self.provider_id}:{self._served_model(payload, request)}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cost_usd=self._prices.for_model(request.model).cost(prompt_tokens, completion_tokens),
        )

    @abc.abstractmethod
    def _path(self, request: CompletionRequest) -> str: ...

    @abc.abstractmethod
    def _payload(self, request: CompletionRequest) -> dict[str, Any]: ...

    @abc.abstractmethod
    def _text(self, payload: dict[str, Any]) -> str: ...

    @abc.abstractmethod
    def _usage(self, payload: dict[str, Any]) -> tuple[int, int]: ...

    def _served_model(self, payload: dict[str, Any], request: CompletionRequest) -> str:
        """What the provider says it actually ran.

        Worth recording separately from what we asked for: a router that silently substitutes a
        model has changed the panel's composition, and the transcript is where that has to show
        up (DESIGN §6.5).
        """
        served = dig(payload, "model")
        return str(served) if isinstance(served, str) and served else request.model
