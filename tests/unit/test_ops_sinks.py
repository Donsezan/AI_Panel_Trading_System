"""Alert delivery: the wire format, the error taxonomy, and the redaction guarantee.

The redaction test pushes a real-shaped bot token through the *real* sink and the *real* logger,
because a Telegram token lives in the request URL — a transport error that echoed its own URL
would put a live credential in the audit trail (PLAN §3.2).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest

from tradebot.core.errors import ConfigError, RateLimitedError, VenueError
from tradebot.core.logging import REDACTED, SECRETS, configure_logging, get_logger
from tradebot.interfaces.alerts import Alert, AlertKind, AlertSink
from tradebot.ops.sinks import (
    TELEGRAM_CHAT_VAR,
    TELEGRAM_TOKEN_VAR,
    WEBHOOK_URL_VAR,
    TelegramSink,
    WebhookSink,
    build_sinks,
)

AT = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
WEBHOOK = "https://hooks.example/services/T000/B000/verysecrettoken"
BOT_TOKEN = "8123456789:AAH-fake-bot-token-for-tests-only-xyz"
CHAT_ID = "-1001234567890"

ALERT = Alert(
    kind=AlertKind.KILL_SWITCH,
    at=AT,
    scope="max_drawdown",
    title="KILL SWITCH TRIPPED",
    body="equity 8800 is 12% below the high-water mark 10000",
)


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    SECRETS.clear()
    yield
    SECRETS.clear()
    logging.getLogger().handlers = []


def responder(status: int = 200, record: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json={"ok": status < 400})

    return httpx.MockTransport(handle)


def client_for(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport)


class TestWebhook:
    async def test_it_posts_the_alert_as_json(self) -> None:
        seen: list[httpx.Request] = []
        async with client_for(responder(record=seen)) as client:
            await WebhookSink(client, WEBHOOK).send(ALERT)

        (request,) = seen
        payload = json.loads(request.content)
        assert str(request.url) == WEBHOOK
        assert payload["kind"] == "kill_switch"
        assert payload["urgent"] is True
        assert payload["title"] == "KILL SWITCH TRIPPED"


class TestTelegram:
    async def test_it_sends_one_message_to_the_configured_chat(self) -> None:
        seen: list[httpx.Request] = []
        async with client_for(responder(record=seen)) as client:
            await TelegramSink(client, BOT_TOKEN, CHAT_ID).send(ALERT)

        (request,) = seen
        payload = json.loads(request.content)
        assert request.url.path == f"/bot{BOT_TOKEN}/sendMessage"
        assert payload["chat_id"] == CHAT_ID
        assert "KILL SWITCH TRIPPED" in payload["text"]

    async def test_the_bot_token_never_reaches_a_log_line(self) -> None:
        """The token is in the URL path, so a failure that named the endpoint would leak it."""
        stream = io.StringIO()
        configure_logging(mode="paper", stream=stream, level=logging.DEBUG)

        async with client_for(responder(500)) as client:
            sink = TelegramSink(client, BOT_TOKEN, CHAT_ID)
            with pytest.raises(VenueError) as failure:
                await sink.send(ALERT)
            get_logger("t").error("alert failed", extra={"error": str(failure.value)})

        output = stream.getvalue()
        assert BOT_TOKEN not in output
        assert "telegram" in output
        assert REDACTED in json.dumps(SECRETS.scrub(f"leaked {BOT_TOKEN}"))

    async def test_a_webhook_url_is_registered_for_redaction_too(self) -> None:
        """A webhook URL is a bearer credential: whoever holds it can post to the channel."""
        async with client_for(responder()) as client:
            WebhookSink(client, WEBHOOK)

        assert SECRETS.scrub(f"posting to {WEBHOOK}") == f"posting to {REDACTED}"


class TestErrorTaxonomy:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, RateLimitedError),
            (500, VenueError),
            (503, VenueError),
            (400, ConfigError),
            (404, ConfigError),
        ],
    )
    async def test_a_status_maps_to_its_handling_instruction(
        self, status: int, expected: type[Exception]
    ) -> None:
        async with client_for(responder(status)) as client:
            with pytest.raises(expected):
                await WebhookSink(client, WEBHOOK).send(ALERT)

    async def test_a_timeout_is_retryable(self) -> None:
        def timeout(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow")

        async with client_for(httpx.MockTransport(timeout)) as client:
            with pytest.raises(VenueError, match="timed out"):
                await WebhookSink(client, WEBHOOK).send(ALERT)


class TestBuilding:
    def _build(self, env: dict[str, str], client: httpx.AsyncClient) -> tuple[AlertSink, ...]:
        sinks, _ = build_sinks(environ=env, client=client)
        return sinks

    async def test_nothing_configured_means_no_sinks_and_no_client(self) -> None:
        sinks, closers = build_sinks(environ={})

        assert sinks == ()
        assert closers == ()

    async def test_each_destination_is_built_when_configured(self) -> None:
        async with client_for(responder()) as client:
            both = self._build(
                {
                    WEBHOOK_URL_VAR: WEBHOOK,
                    TELEGRAM_TOKEN_VAR: BOT_TOKEN,
                    TELEGRAM_CHAT_VAR: CHAT_ID,
                },
                client,
            )
            webhook_only = self._build({WEBHOOK_URL_VAR: WEBHOOK}, client)

        assert [sink.sink_id for sink in both] == ["webhook", "telegram"]
        assert [sink.sink_id for sink in webhook_only] == ["webhook"]

    async def test_half_configured_telegram_refuses_rather_than_staying_quiet(self) -> None:
        """Discovering alerting was off during the incident is the failure this prevents."""
        async with client_for(responder()) as client:
            with pytest.raises(ConfigError, match="needs both"):
                self._build({TELEGRAM_TOKEN_VAR: BOT_TOKEN}, client)
            with pytest.raises(ConfigError, match="needs both"):
                self._build({TELEGRAM_CHAT_VAR: CHAT_ID}, client)

    async def test_a_supplied_client_is_not_ours_to_close(self) -> None:
        async with client_for(responder()) as client:
            _, closers = build_sinks(environ={WEBHOOK_URL_VAR: WEBHOOK}, client=client)

        assert closers == ()
