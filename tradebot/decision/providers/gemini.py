"""Google's Generative Language `generateContent` endpoint.

The one behaviour worth naming: Gemini can return **no candidate at all** and a
`promptFeedback.blockReason` instead, when a safety filter fires. That is entirely plausible
here — the prompt carries third-party news headlines we do not control. It is classified as a
provider failure so the seat falls back and then abstains, which resolves to no trade; a safety
block must never look like a considered `WAIT`.

Failure semantics: inherited from `LLMHttpTransport` — every failure is a `ProviderError`, so
the seat falls back and then abstains. See [http.py](http.py).
"""

from __future__ import annotations

from typing import Any, Final

from tradebot.core.errors import ProviderError
from tradebot.decision.providers.http import HttpLLMProvider, dig, token_count
from tradebot.interfaces.llm import CompletionRequest

DEFAULT_BASE_URL: Final = "https://generativelanguage.googleapis.com"

JSON_MIME_TYPE: Final = "application/json"


def auth_headers(api_key: str) -> dict[str, str]:
    """Header auth, never a query parameter: a key in a URL lands in every access log there is."""
    return {"x-goog-api-key": api_key}


class GeminiProvider(HttpLLMProvider):
    """The `v1beta/models/{model}:generateContent` endpoint."""

    def _path(self, request: CompletionRequest) -> str:
        return f"v1beta/models/{request.model}:generateContent"

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        config: dict[str, Any] = {
            "temperature": request.temperature,
            "maxOutputTokens": request.max_tokens,
        }
        if request.json_mode:
            config["responseMimeType"] = JSON_MIME_TYPE
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": config,
        }

    def _text(self, payload: dict[str, Any]) -> str:
        self._assert_not_blocked(payload)
        text = dig(payload, "candidates", 0, "content", "parts", 0, "text")
        return text if isinstance(text, str) else ""

    def _usage(self, payload: dict[str, Any]) -> tuple[int, int]:
        return (
            token_count(payload, "usageMetadata", "promptTokenCount"),
            token_count(payload, "usageMetadata", "candidatesTokenCount"),
        )

    def _assert_not_blocked(self, payload: dict[str, Any]) -> None:
        reason = dig(payload, "promptFeedback", "blockReason")
        if reason:
            raise ProviderError(f"{self.provider_id} blocked the prompt: {reason}")

    def _served_model(self, payload: dict[str, Any], request: CompletionRequest) -> str:
        served = dig(payload, "modelVersion")
        return str(served) if isinstance(served, str) and served else request.model
