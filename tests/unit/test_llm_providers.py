"""The three provider adapters, exercised over the real httpx client.

`httpx.MockTransport` puts the assertion on the *wire format* — the URL, the headers, and the
JSON body a vendor will actually receive — rather than on a mock returning what it was handed.
That is the whole reason these adapters are hand-rolled rather than three SDKs (ADR 0009): the
contract is small enough to own, and owning it means it can be tested offline and for free.

Every failure classification here has one job: end in `ProviderError`, so a seat falls back and
then abstains, and the cycle resolves to `WAIT`. Nothing a provider can do may escape as an
unclassified exception into a trading cycle (DESIGN §8.1).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import (
    FREE,
    ModelPricing,
    PanelConfig,
    PriceList,
    ProviderBinding,
    ProviderSettings,
    SeatConfig,
)
from tradebot.core.enums import ProviderKind
from tradebot.core.errors import ConfigError, ProviderError
from tradebot.core.logging import REDACTED, SECRETS
from tradebot.decision.providers.anthropic import API_VERSION, AnthropicProvider
from tradebot.decision.providers.gemini import GeminiProvider
from tradebot.decision.providers.http import DEFAULT_MAX_BYTES, LLMHttpTransport, dig, token_count
from tradebot.decision.providers.openai_compat import OpenAICompatProvider
from tradebot.decision.providers.registry import (
    PRESETS,
    assert_secure_endpoint,
    build_provider,
    build_providers,
    preset,
    reach_of,
    resolve_secret,
    unconfigured_providers,
)
from tradebot.interfaces.llm import CompletionRequest

VOTE = '{"action": "BUY", "conviction": 4, "size_hint": "half", "thesis": "t"}'

Handler = Callable[[httpx.Request], httpx.Response]


def request_for(model: str = "test-model") -> CompletionRequest:
    return CompletionRequest(
        model=model, system="you are a seat", user="the snapshot", temperature=0.2, max_tokens=512
    )


def client_for(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def responder(payload: dict[str, Any], *, status: int = 200) -> tuple[Handler, list[httpx.Request]]:
    """A handler that records what it was asked, so the request shape can be asserted."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    return handler, seen


def openai_payload(content: str | None = VOTE) -> dict[str, Any]:
    return {
        "model": "test-model-0125",
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    }


ANTHROPIC_PAYLOAD = {
    "model": "claude-x",
    "content": [{"type": "text", "text": VOTE}],
    "usage": {"input_tokens": 1200, "output_tokens": 300},
}

GEMINI_PAYLOAD = {
    "modelVersion": "gemini-x-001",
    "candidates": [{"content": {"parts": [{"text": VOTE}]}}],
    "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 300},
}


def transport(
    handler: Handler,
    *,
    provider_id: str = "p",
    base_url: str = "https://api.test/v1",
    **kwargs: Any,
) -> LLMHttpTransport:
    return LLMHttpTransport(
        client_for(handler), provider_id=provider_id, base_url=base_url, **kwargs
    )


class TestOpenAICompat:
    async def test_the_request_matches_the_chat_completions_contract(
        self, clock: ManualClock
    ) -> None:
        handler, seen = responder(openai_payload())
        provider = OpenAICompatProvider(
            transport(handler, base_url="https://openrouter.ai/api/v1"),
            clock,
            provider_id="openrouter",
        )

        await provider.complete(request_for())

        (sent,) = seen
        body = json.loads(sent.content)
        assert str(sent.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert body["messages"] == [
            {"role": "system", "content": "you are a seat"},
            {"role": "user", "content": "the snapshot"},
        ]
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0.2
        assert body["max_tokens"] == 512

    async def test_a_local_server_can_refuse_json_mode(self, clock: ManualClock) -> None:
        """LM Studio and llama.cpp differ here; a hard 400 would break the last fallback."""
        handler, seen = responder(openai_payload())
        provider = OpenAICompatProvider(
            transport(handler, base_url="http://localhost:1234/v1"),
            clock,
            provider_id="lmstudio",
            supports_json_mode=False,
        )

        await provider.complete(request_for())
        assert "response_format" not in json.loads(seen[0].content)

    async def test_the_response_carries_text_usage_and_the_model_that_answered(
        self, clock: ManualClock
    ) -> None:
        handler, _ = responder(openai_payload())
        provider = OpenAICompatProvider(transport(handler), clock, provider_id="openrouter")

        result = await provider.complete(request_for())

        assert result.text == VOTE
        assert (result.prompt_tokens, result.completion_tokens) == (1200, 300)
        assert result.model_fingerprint == "openrouter:test-model-0125"

    async def test_a_router_substituting_a_model_is_visible(self, clock: ManualClock) -> None:
        """A silent substitution changes the panel's composition; the transcript must show it."""
        handler, _ = responder({**openai_payload(), "model": "some-other-model"})
        provider = OpenAICompatProvider(transport(handler), clock, provider_id="openrouter")
        assert (await provider.complete(request_for())).model_fingerprint.endswith(
            "some-other-model"
        )

    @pytest.mark.parametrize("content", [None, "", "   "])
    async def test_an_empty_completion_is_a_provider_failure(
        self, clock: ManualClock, content: str | None
    ) -> None:
        handler, _ = responder(openai_payload(content))
        provider = OpenAICompatProvider(transport(handler), clock, provider_id="p")
        with pytest.raises(ProviderError, match="empty completion"):
            await provider.complete(request_for())


class TestAnthropic:
    async def test_the_request_matches_the_messages_contract(self, clock: ManualClock) -> None:
        handler, seen = responder(ANTHROPIC_PAYLOAD)
        provider = AnthropicProvider(
            transport(
                handler,
                base_url="https://api.anthropic.com",
                headers={"x-api-key": "k", "anthropic-version": API_VERSION},
            ),
            clock,
            provider_id="anthropic",
        )

        result = await provider.complete(request_for("claude-x"))

        (sent,) = seen
        body = json.loads(sent.content)
        assert str(sent.url) == "https://api.anthropic.com/v1/messages"
        assert sent.headers["anthropic-version"] == API_VERSION
        assert body["system"] == "you are a seat", "the system prompt is top-level, not a message"
        assert body["messages"] == [{"role": "user", "content": "the snapshot"}]
        assert result.text == VOTE
        assert (result.prompt_tokens, result.completion_tokens) == (1200, 300)


class TestGemini:
    async def test_the_request_matches_the_generate_content_contract(
        self, clock: ManualClock
    ) -> None:
        handler, seen = responder(GEMINI_PAYLOAD)
        provider = GeminiProvider(
            transport(
                handler,
                base_url="https://generativelanguage.googleapis.com",
                headers={"x-goog-api-key": "k"},
            ),
            clock,
            provider_id="gemini",
        )

        result = await provider.complete(request_for("gemini-2.0-flash"))

        (sent,) = seen
        body = json.loads(sent.content)
        assert sent.url.path == "/v1beta/models/gemini-2.0-flash:generateContent"
        assert sent.headers["x-goog-api-key"] == "k"
        assert body["systemInstruction"]["parts"][0]["text"] == "you are a seat"
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert result.text == VOTE
        assert result.model_fingerprint == "gemini:gemini-x-001"

    async def test_a_safety_block_is_a_provider_failure_not_a_considered_wait(
        self, clock: ManualClock
    ) -> None:
        """News headlines are third-party text; a filter firing must not read as a decision."""
        handler, _ = responder({"promptFeedback": {"blockReason": "SAFETY"}})
        provider = GeminiProvider(transport(handler), clock, provider_id="gemini")
        with pytest.raises(ProviderError, match="SAFETY"):
            await provider.complete(request_for())


class TestFailureClassification:
    """Every one of these must end as `ProviderError`, never as a naked exception."""

    async def _post(self, handler: Handler, **kwargs: Any) -> dict[str, Any]:
        return await transport(handler, **kwargs).post("x", {}, timeout_seconds=5.0)

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_server_errors(self, status: int) -> None:
        with pytest.raises(ProviderError, match=str(status)):
            await self._post(lambda _r: httpx.Response(status, text="down"))

    async def test_rate_limiting_carries_the_retry_after(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"}, text="slow down")

        with pytest.raises(ProviderError) as caught:
            await self._post(handler)
        assert caught.value.retry_after_seconds == 30.0

    @pytest.mark.parametrize("status", [401, 403])
    async def test_bad_credentials(self, status: int) -> None:
        with pytest.raises(ProviderError, match="credentials"):
            await self._post(lambda _r: httpx.Response(status, text="nope"))

    async def test_a_vanished_model_slot(self) -> None:
        """R11: a free slot that disappears is a 404, and the seat falls back."""
        with pytest.raises(ProviderError, match="404"):
            await self._post(lambda _r: httpx.Response(404, text="no such model"))

    async def test_a_timeout(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        with pytest.raises(ProviderError, match="timed out"):
            await self._post(handler)

    async def test_a_transport_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(ProviderError, match="transport failure"):
            await self._post(handler)

    async def test_a_non_json_body(self) -> None:
        with pytest.raises(ProviderError, match="non-JSON"):
            await self._post(lambda _r: httpx.Response(200, text="<html>gateway</html>"))

    async def test_a_json_body_that_is_not_an_object(self) -> None:
        with pytest.raises(ProviderError, match="not an object"):
            await self._post(lambda _r: httpx.Response(200, json=["nope"]))

    async def test_an_oversized_body(self) -> None:
        big = {"padding": "x" * 5000}
        with pytest.raises(ProviderError, match="ceiling"):
            await self._post(lambda _r: httpx.Response(200, json=big), max_bytes=1000)

    def test_the_default_ceiling_is_stated_in_bytes(self) -> None:
        assert DEFAULT_MAX_BYTES == 8 * 1024 * 1024

    async def test_an_error_body_echoing_our_key_is_scrubbed_before_it_is_persisted(self) -> None:
        """The excerpt reaches an abstain reason and therefore a database row (PLAN §3.2)."""
        secret = "sk-live-abcdefghijklmnop"
        SECRETS.register(secret)
        try:
            with pytest.raises(ProviderError) as caught:
                await self._post(
                    lambda _r: httpx.Response(400, text=f"bad Authorization: Bearer {secret}")
                )
            assert secret not in str(caught.value)
            assert REDACTED in str(caught.value)
        finally:
            SECRETS.clear()


class TestResponseDigging:
    """A missing key in third-party JSON must never surface as a `KeyError`."""

    @pytest.mark.parametrize(
        "path",
        [("choices", 0, "message", "content"), ("choices", 5), ("missing",), ("choices", 0, "x")],
    )
    def test_absent_paths_return_none(self, path: tuple[str | int, ...]) -> None:
        assert dig({"choices": []}, *path) is None

    def test_token_counts_reject_anything_that_is_not_an_integer(self) -> None:
        assert token_count({"usage": {"n": "1200"}}, "usage", "n") == 0
        assert token_count({"usage": {"n": True}}, "usage", "n") == 0
        assert token_count({"usage": {"n": 1200}}, "usage", "n") == 1200


class TestPricing:
    def test_an_unpriced_model_is_free(self) -> None:
        assert PriceList().for_model("anything") is FREE
        assert FREE.cost(1_000_000, 1_000_000) == Decimal(0)

    def test_cost_is_per_million_tokens_in_exact_decimal(self) -> None:
        pricing = ModelPricing(
            prompt_per_million=Decimal("3"), completion_per_million=Decimal("15")
        )
        assert pricing.cost(1_000_000, 1_000_000) == Decimal("18")
        assert pricing.cost(1200, 300) == Decimal("0.0081")

    async def test_a_provider_prices_what_it_used(self, clock: ManualClock) -> None:
        handler, _ = responder(openai_payload())
        provider = OpenAICompatProvider(
            transport(handler),
            clock,
            provider_id="openai",
            prices=PriceList(
                models={
                    "test-model": ModelPricing(
                        prompt_per_million=Decimal("3"), completion_per_million=Decimal("15")
                    )
                }
            ),
        )
        assert (await provider.complete(request_for())).cost_usd == Decimal("0.0081")


class TestRegistry:
    def test_every_preset_endpoint_is_acceptable(self) -> None:
        for settings in PRESETS.values():
            assert_secure_endpoint(settings)

    def test_a_remote_plaintext_endpoint_refuses_to_start(self) -> None:
        """Prompts carry position size and unrealized PnL; plaintext to a remote host leaks them."""
        with pytest.raises(ConfigError, match="https"):
            assert_secure_endpoint(
                ProviderSettings(
                    provider_id="sketchy",
                    kind=ProviderKind.OPENAI_COMPAT,
                    base_url="http://example.com/v1",
                )
            )

    def test_loopback_plaintext_is_allowed_because_it_never_leaves_the_machine(self) -> None:
        assert_secure_endpoint(preset("lmstudio"))
        assert_secure_endpoint(preset("llamacpp"))

    def test_a_local_port_can_be_overridden(self) -> None:
        assert preset("lmstudio", base_url="http://127.0.0.1:9999/v1").base_url.endswith("9999/v1")

    def test_an_unknown_provider_refuses_to_start(self) -> None:
        with pytest.raises(ConfigError, match="unknown provider"):
            preset("psychic-friends-network")

    def test_a_missing_key_reads_as_absent_rather_than_raising(self) -> None:
        """ADR 0023: an endpoint with no key is unreachable, which is a runtime fact every
        fallback chain already survives — not a malformed panel that must kill the process."""
        assert resolve_secret(preset("openrouter"), {}) is None

    def test_a_blank_key_is_treated_as_missing(self) -> None:
        assert resolve_secret(preset("openrouter"), {"OPENROUTER_API_KEY": "   "}) is None

    def test_a_resolved_key_is_registered_with_the_log_redactor(self) -> None:
        SECRETS.clear()
        try:
            key = "sk-test-0123456789abcdef"
            assert resolve_secret(preset("openrouter"), {"OPENROUTER_API_KEY": key}) == key
            assert SECRETS.scrub(f"used {key}") == f"used {REDACTED}"
        finally:
            SECRETS.clear()

    def test_a_local_provider_needs_no_key(self) -> None:
        assert resolve_secret(preset("lmstudio"), {}) is None

    def test_wiring_nothing_opens_no_client(self, clock: ManualClock) -> None:
        """A stub panel must not create a connection pool it will never use."""
        pool = build_providers((), clock)
        assert pool.providers == {}

    def test_duplicate_provider_ids_refuse_to_start(self, clock: ManualClock) -> None:
        with pytest.raises(ConfigError, match="duplicate"):
            build_providers((preset("lmstudio"), preset("lmstudio")), clock)

    async def test_a_wired_pool_closes_its_client(self, clock: ManualClock) -> None:
        pool = build_providers((preset("lmstudio"),), clock)
        assert set(pool.providers) == {"lmstudio"}
        await pool.close()
        await pool.close()  # idempotent: shutdown runs after a failed startup too


class TestUnconfiguredProviders:
    """A declared endpoint whose key is absent is left unwired, never fatal (ADR 0023).

    The whole point is that the process comes up: refusing to start costs an operator the
    dashboard, the log and the ledger view, which are the things they need to fix it with.
    """

    def test_an_endpoint_with_no_key_is_named_with_the_variable_to_set(self) -> None:
        absent = unconfigured_providers((preset("openrouter"), preset("lmstudio")), {})
        assert [(entry.provider_id, entry.secret_ref) for entry in absent] == [
            ("openrouter", "OPENROUTER_API_KEY")
        ]
        assert "OPENROUTER_API_KEY" in str(absent[0])

    def test_a_keyless_provider_is_never_reported(self) -> None:
        """Local runtimes need no key, so their absence from the environment means nothing."""
        assert unconfigured_providers((preset("lmstudio"), preset("stub")), {}) == ()

    async def test_wiring_leaves_it_out_instead_of_raising(self, clock: ManualClock) -> None:
        pool = build_providers((preset("openrouter"), preset("lmstudio")), clock, environ={})
        try:
            assert set(pool.providers) == {"lmstudio"}
            assert [entry.provider_id for entry in pool.unconfigured] == ["openrouter"]
        finally:
            await pool.close()

    def test_a_panel_with_no_reachable_endpoint_opens_no_client(self, clock: ManualClock) -> None:
        """No socket is held for endpoints that were never wired — the same invariant an
        all-stub panel relies on."""
        pool = build_providers((preset("openrouter"),), clock, environ={})
        assert pool.providers == {}
        assert not pool.owns_client

    async def test_the_key_is_read_when_it_is_present(self, clock: ManualClock) -> None:
        SECRETS.clear()
        pool = build_providers(
            (preset("openrouter"),), clock, environ={"OPENROUTER_API_KEY": "sk-present"}
        )
        try:
            assert set(pool.providers) == {"openrouter"}
            assert pool.unconfigured == ()
        finally:
            await pool.close()
            SECRETS.clear()

    def test_building_one_directly_still_refuses(self, clock: ManualClock) -> None:
        """`build_providers` filters first, so this is unreachable from the composition root. It
        exists so a direct caller cannot get a provider that calls a paid endpoint with no key."""
        with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
            build_provider(
                preset("openrouter"),
                client_for(lambda _: httpx.Response(200)),
                clock,
                environ={},
            )


def _seat(seat_id: str, provider_id: str, *fallbacks: str) -> SeatConfig:
    return SeatConfig(
        seat_id=seat_id,
        role=seat_id,
        provider_id=provider_id,
        model=f"{provider_id}-model",
        fallbacks=tuple(
            ProviderBinding(provider_id=pid, model=f"{pid}-model") for pid in fallbacks
        ),
    )


#: One seat that keeps a binding when `gemini` is gone, and one that loses its only one.
MIXED_PANEL = PanelConfig(
    panel_id="mixed",
    providers=(preset("lmstudio"), preset("gemini")),
    seats=(_seat("technical", "lmstudio", "gemini"), _seat("news", "gemini")),
)


class TestPanelReach:
    """Which seats can still reach a model — the question every consumer of ADR 0023 asks.

    Answered from configuration and the environment alone, so it costs nothing and the dashboard
    can ask it on every page render. `decision/probe.py` is the expensive question of whether a
    *reachable* model id still resolves.
    """

    def test_every_key_present_is_healthy(self) -> None:
        assert reach_of(MIXED_PANEL, {"GEMINI_API_KEY": "k"}).healthy
        assert reach_of(MIXED_PANEL, {"GEMINI_API_KEY": "k"}).findings == ()

    def test_a_seat_keeping_a_binding_is_degraded_not_silenced(self) -> None:
        reach = reach_of(MIXED_PANEL, {})
        assert reach.degraded == ("technical",)
        assert "technical" not in reach.silenced

    def test_a_seat_losing_its_whole_chain_is_silenced(self) -> None:
        """It abstains on every cycle, so the panel is permanently short a voice rather than
        transiently — a different fact from a shortened chain, and reported as one."""
        reach = reach_of(MIXED_PANEL, {})
        assert reach.silenced == ("news",)
        assert any("abstain on every cycle" in finding for finding in reach.findings)

    def test_the_findings_name_the_variable_to_set(self) -> None:
        assert any("GEMINI_API_KEY" in finding for finding in reach_of(MIXED_PANEL, {}).findings)

    def test_a_panel_declaring_no_providers_is_healthy(self) -> None:
        """Its providers are supplied by the composition root, so there is nothing to be missing —
        the same exemption `PanelConfig._check_bindings_resolve` makes."""
        panel = PanelConfig(panel_id="injected", seats=(_seat("technical", "somewhere"),))
        assert reach_of(panel, {}).healthy


async def test_anthropic_wiring_sends_the_key_as_x_api_key(clock: ManualClock) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANTHROPIC_PAYLOAD)

    provider = build_provider(
        preset("anthropic"), client_for(handler), clock, environ={"ANTHROPIC_API_KEY": "k-1"}
    )
    await provider.complete(request_for("claude-x"))

    assert seen[0].headers["x-api-key"] == "k-1"
    assert "authorization" not in seen[0].headers


async def test_openai_compat_wiring_sends_a_bearer_token(clock: ManualClock) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=openai_payload())

    provider = build_provider(
        preset("openrouter"), client_for(handler), clock, environ={"OPENROUTER_API_KEY": "k-2"}
    )
    await provider.complete(request_for())

    assert seen[0].headers["authorization"] == "Bearer k-2"


async def test_a_local_provider_sends_no_authorization_header(clock: ManualClock) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=openai_payload())

    provider = build_provider(preset("lmstudio"), client_for(handler), clock, environ={})
    await provider.complete(request_for())

    assert "authorization" not in seen[0].headers
