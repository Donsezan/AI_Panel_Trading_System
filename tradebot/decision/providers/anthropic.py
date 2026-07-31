"""Anthropic's Messages API.

Two shape differences from the OpenAI family matter: the system prompt is a top-level field
rather than a message, and there is no `response_format`. The second is not a gap — JSON mode
never was the control, since a syntactically valid object that fails our schema is still a failed
vote [L8]. The system prompt asks for JSON only and the seat's parser tolerates the code fences
some models add regardless.

Failure semantics: inherited from `LLMHttpTransport` — every failure is a `ProviderError`, so
the seat falls back and then abstains. See [http.py](http.py).
"""

from __future__ import annotations

from typing import Any, Final

from tradebot.decision.providers.http import HttpLLMProvider, dig, token_count
from tradebot.interfaces.llm import CompletionRequest

#: Pinned rather than tracking the newest: an API version is a wire contract, and a silent
#: upgrade mid-soak would change the panel's behaviour without a config change to point at.
API_VERSION: Final = "2023-06-01"

DEFAULT_BASE_URL: Final = "https://api.anthropic.com"


def auth_headers(api_key: str) -> dict[str, str]:
    return {"x-api-key": api_key, "anthropic-version": API_VERSION}


class AnthropicProvider(HttpLLMProvider):
    """The `/v1/messages` endpoint."""

    def _path(self, _request: CompletionRequest) -> str:
        return "v1/messages"

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        return {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }

    def _text(self, payload: dict[str, Any]) -> str:
        """The first text block. Non-text blocks are ignored, not concatenated blindly."""
        text = dig(payload, "content", 0, "text")
        return text if isinstance(text, str) else ""

    def _usage(self, payload: dict[str, Any]) -> tuple[int, int]:
        return (
            token_count(payload, "usage", "input_tokens"),
            token_count(payload, "usage", "output_tokens"),
        )
