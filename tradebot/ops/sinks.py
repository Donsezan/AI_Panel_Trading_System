"""Where an alert actually goes: a generic webhook, and Telegram.

Both are **off unless their destination is configured**, and both read that destination from the
environment rather than the database (PLAN §3.2). A webhook URL is a bearer secret — anyone
holding it can post to your incident channel — and a Telegram bot token is worse, because the API
puts it in the *URL path*. So both are registered with the log redactor at construction, and
nothing here ever logs a URL.

`httpx` rather than a vendor SDK, for the reason the LLM providers give (ADR 0009): two small
JSON POSTs are not worth a dependency, and the suite drives the real client through
`MockTransport` so delivery is tested rather than mocked away.

Failure semantics: a delivery failure raises `VenueError` — retryable. The dispatcher leaves its
cursor unmoved and the same alert is attempted on the next poll (ADR 0019). A 4xx that is not
rate limiting raises `ConfigError` instead: a mistyped chat id will never succeed, and retrying
it forever would bury the alerts behind it.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping

import httpx

from tradebot.core.errors import ConfigError, RateLimitedError, VenueError
from tradebot.core.logging import SECRETS, get_logger
from tradebot.interfaces.alerts import Alert, AlertSink

logger = get_logger(__name__)

#: Where an alert goes. Names are environment variables, and their *values* are secrets.
WEBHOOK_URL_VAR = "TRADEBOT_ALERT_WEBHOOK_URL"
TELEGRAM_TOKEN_VAR = "TRADEBOT_TELEGRAM_BOT_TOKEN"  # noqa: S105 — a variable name, not a token
TELEGRAM_CHAT_VAR = "TRADEBOT_TELEGRAM_CHAT_ID"

TELEGRAM_API = "https://api.telegram.org"
DEFAULT_TIMEOUT = 15.0


class WebhookSink:
    """POSTs one JSON object per alert to an operator-configured URL."""

    sink_id = "webhook"

    def __init__(self, client: httpx.AsyncClient, url: str) -> None:
        self._client = client
        self._url = url
        SECRETS.register(url)

    async def send(self, alert: Alert) -> None:
        await _post(
            self._client,
            self.sink_id,
            self._url,
            {
                "kind": alert.kind.value,
                "at": alert.at.isoformat(),
                "scope": alert.scope,
                "title": alert.title,
                "body": alert.body,
                "urgent": alert.kind.is_urgent,
            },
        )


class TelegramSink:
    """Sends one `sendMessage` per alert.

    The bot token is registered for redaction *and* kept out of every log line here, because it
    sits in the request path: a transport error that echoed its own URL would put a live
    credential in the audit trail.
    """

    sink_id = "telegram"

    def __init__(
        self, client: httpx.AsyncClient, token: str, chat_id: str, *, api: str = TELEGRAM_API
    ) -> None:
        self._client = client
        self._url = f"{api}/bot{token}/sendMessage"
        self._chat_id = chat_id
        SECRETS.register(token)

    async def send(self, alert: Alert) -> None:
        await _post(
            self._client,
            self.sink_id,
            self._url,
            {"chat_id": self._chat_id, "text": alert.text, "disable_notification": False},
        )


async def _post(
    client: httpx.AsyncClient, sink_id: str, url: str, payload: dict[str, object]
) -> None:
    """One JSON POST, with the whole error taxonomy applied and the URL never named.

    Every message identifies the *sink*, not the endpoint. That is the control: an operator can
    still tell which destination failed, and no log line can carry the secret that reaches it.
    """
    try:
        response = await client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise VenueError(f"{sink_id} alert timed out: {type(exc).__name__}") from exc
    except httpx.HTTPError as exc:
        raise VenueError(f"{sink_id} alert transport failure: {type(exc).__name__}") from exc

    status = response.status_code
    if status < httpx.codes.BAD_REQUEST:
        return
    if status == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitedError(f"{sink_id} rate-limited the alert")
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise VenueError(f"{sink_id} returned HTTP {status}")
    raise ConfigError(
        f"{sink_id} rejected the alert with HTTP {status}; check the configured destination"
    )


def build_sinks(
    *,
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[tuple[AlertSink, ...], tuple[Callable[[], Awaitable[None]], ...]]:
    """Every destination the environment configures, and how to close what was opened.

    Returns nothing at all when nothing is configured — which is the default, and the reason a
    developer running the demo is never asked for a webhook. A partially configured Telegram (a
    token with no chat id, or the reverse) **refuses**: silently not alerting is the failure mode
    an operator only discovers during the incident they were meant to be told about.
    """
    env = os.environ if environ is None else environ
    webhook = (env.get(WEBHOOK_URL_VAR) or "").strip()
    token = (env.get(TELEGRAM_TOKEN_VAR) or "").strip()
    chat_id = (env.get(TELEGRAM_CHAT_VAR) or "").strip()
    if bool(token) is not bool(chat_id):
        raise ConfigError(
            f"telegram alerting needs both {TELEGRAM_TOKEN_VAR} and {TELEGRAM_CHAT_VAR}; "
            "half-configured alerting is alerting nobody receives"
        )
    if not webhook and not token:
        return (), ()

    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout, http2=False)
    sinks: list[AlertSink] = []
    if webhook:
        sinks.append(WebhookSink(http, webhook))
    if token:
        sinks.append(TelegramSink(http, token, chat_id))
    logger.warning("ops alerting enabled", extra={"sinks": [sink.sink_id for sink in sinks]})
    return tuple(sinks), ((http.aclose,) if owned else ())
