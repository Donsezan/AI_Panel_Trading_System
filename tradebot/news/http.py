"""Polite, conditional, robots-respecting feed fetching.

Every behaviour here is a compliance requirement rather than an optimization (PLAN §3.3):

* **`robots.txt` is checked and honoured**, per host, cached with a TTL. An unreachable
  `robots.txt` (5xx, timeout) is treated as *disallow*, following RFC 9309: news is optional to
  this system and legal exposure is not.
* **Conditional GET.** `ETag`/`Last-Modified` are replayed on every request, so a feed that has
  not changed costs a `304` and no body. Publishers notice bots that re-download unchanged feeds.
* **A real `User-Agent`** identifying the client, not a spoofed browser string.
* **A response-size ceiling**, so a hostile or broken feed cannot exhaust memory.

Failure semantics, all classified: timeouts and 5xx are `VenueError` (retryable, the hub records
a coverage gap); `429` is `RateLimitedError` carrying the publisher's `Retry-After`; `401`/`403`
and a `robots.txt` denial are `SourceDisallowedError` (we stop asking); any other `4xx` is
`ConfigError`, because a feed URL that 404s is a configuration defect, not weather.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from tradebot.core.clock import Clock
from tradebot.core.errors import (
    ConfigError,
    RateLimitedError,
    SourceDisallowedError,
    VenueError,
)
from tradebot.core.logging import get_logger

logger = get_logger(__name__)

#: Identifies this client honestly. Operators should append a contact URL for their deployment;
#: a publisher that wants to reach whoever is polling them has no other channel.
DEFAULT_USER_AGENT: Final = "tradebot/0.1 (AI Panel Trading System; research bot; RSS only)"

DEFAULT_TIMEOUT: Final = 10.0
DEFAULT_ROBOTS_TTL: Final = timedelta(hours=12)
#: 4 MB. A feed above this is not a feed we should be parsing.
DEFAULT_MAX_BYTES: Final = 4 * 1024 * 1024

_BLOCKED_STATUSES: Final = frozenset({401, 403})


@dataclass(slots=True)
class _Validators:
    """Cached conditional-GET headers for one URL."""

    etag: str | None = None
    last_modified: str | None = None

    def headers(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("If-None-Match", self.etag),
                ("If-Modified-Since", self.last_modified),
            )
            if value
        }


@dataclass(slots=True)
class _Robots:
    parser: RobotFileParser | None
    expires_at: float

    def allows(self, user_agent: str, url: str) -> bool:
        """`None` means the fetch failed, which RFC 9309 says to treat as disallow."""
        return self.parser is not None and self.parser.can_fetch(user_agent, url)


class FeedFetcher:
    """Fetches feed bodies over HTTP, or explains why it will not."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        clock: Clock,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        robots_ttl: timedelta = DEFAULT_ROBOTS_TTL,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._client = client
        self._clock = clock
        self._user_agent = user_agent
        self._robots_ttl = robots_ttl.total_seconds()
        self._max_bytes = max_bytes
        self._validators: dict[str, _Validators] = {}
        self._robots: dict[str, _Robots] = {}

    async def fetch(self, url: str) -> str | None:
        """The feed body, or `None` when the publisher says it has not changed."""
        await self._assert_allowed(url)
        validators = self._validators.setdefault(url, _Validators())
        response = await self._request(url, validators.headers())
        if response.status_code == httpx.codes.NOT_MODIFIED:
            logger.debug("feed unchanged", extra={"url": url})
            return None
        self._raise_for_status(url, response)
        self._check_size(url, response)
        self._validators[url] = _Validators(
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )
        return response.text

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, url: str, headers: dict[str, str]) -> httpx.Response:
        try:
            return await self._client.get(
                url, headers={"User-Agent": self._user_agent, **headers}, follow_redirects=True
            )
        except httpx.TimeoutException as exc:
            raise VenueError(f"{url} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise VenueError(f"{url} transport failure: {exc}") from exc

    @staticmethod
    def _raise_for_status(url: str, response: httpx.Response) -> None:
        status = response.status_code
        if status < httpx.codes.BAD_REQUEST:
            return
        if status == httpx.codes.TOO_MANY_REQUESTS:
            raise RateLimitedError(
                f"{url} rate-limited us", retry_after_seconds=_retry_after(response)
            )
        if status in _BLOCKED_STATUSES:
            raise SourceDisallowedError(f"{url} refused this client (HTTP {status})")
        if status >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise VenueError(f"{url} returned HTTP {status}")
        raise ConfigError(f"{url} returned HTTP {status}; the feed URL looks wrong")

    def _check_size(self, url: str, response: httpx.Response) -> None:
        size = len(response.content)
        if size > self._max_bytes:
            raise ConfigError(f"{url} returned {size} bytes, above the {self._max_bytes} ceiling")

    async def _assert_allowed(self, url: str) -> None:
        robots = await self._robots_for(url)
        if not robots.allows(self._user_agent, url):
            raise SourceDisallowedError(f"robots.txt disallows fetching {url}")

    async def _robots_for(self, url: str) -> _Robots:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(host)
        now = self._clock.monotonic()
        if cached is not None and cached.expires_at > now:
            return cached
        fresh = _Robots(await self._load_robots(host), now + self._robots_ttl)
        self._robots[host] = fresh
        return fresh

    async def _load_robots(self, host: str) -> RobotFileParser | None:
        """`None` when we could not establish the rules, which means we do not fetch."""
        parser = RobotFileParser()
        try:
            response = await self._client.get(
                f"{host}/robots.txt",
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            logger.warning("robots.txt unreachable", extra={"host": host, "error": str(exc)})
            return None
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            logger.warning(
                "robots.txt errored", extra={"host": host, "status": response.status_code}
            )
            return None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            # No robots.txt published: RFC 9309 says full access is permitted.
            parser.parse([])
            return parser
        parser.parse(response.text.splitlines())
        return parser


def _retry_after(response: httpx.Response) -> float | None:
    raw = (response.headers.get("Retry-After") or "").strip()
    return float(raw) if raw.isdigit() else None


def build_fetcher(
    clock: Clock, *, timeout: float = DEFAULT_TIMEOUT, **kwargs: object
) -> FeedFetcher:
    """A fetcher on a real HTTP client. Called from the composition root only."""
    client = httpx.AsyncClient(timeout=timeout, http2=False)
    return FeedFetcher(client, clock, **kwargs)  # type: ignore[arg-type]
