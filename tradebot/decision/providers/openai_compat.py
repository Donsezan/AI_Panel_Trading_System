"""The OpenAI chat-completions shape. One adapter, many endpoints.

Covers OpenRouter, OpenAI, vLLM, LM Studio and `llama.cpp --server` without a line of
per-vendor code, because all of them speak `POST /chat/completions`. That is what makes a model
running on the operator's own machine a first-class fallback for a free hosted slot rather than
a separate integration — the substitution a seat needs when a free slot disappears (R11).

Failure semantics: inherited from `LLMHttpTransport` — every failure is a `ProviderError`, so
the seat falls back and then abstains. See [http.py](http.py).
"""

from __future__ import annotations

from typing import Any

from tradebot.core.clock import Clock
from tradebot.core.config import PriceList
from tradebot.decision.providers.http import HttpLLMProvider, LLMHttpTransport, dig, token_count
from tradebot.interfaces.llm import CompletionRequest


class OpenAICompatProvider(HttpLLMProvider):
    """An OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        transport: LLMHttpTransport,
        clock: Clock,
        *,
        provider_id: str,
        prices: PriceList | None = None,
        supports_json_mode: bool = True,
    ) -> None:
        super().__init__(transport, clock, provider_id=provider_id, prices=prices)
        # Local servers are the reason this is configurable: several reject `response_format`
        # outright, and a hard 400 on every call would take the fallback binding out of service
        # exactly when the hosted slot it backs up has already failed.
        self._supports_json_mode = supports_json_mode

    def _path(self, _request: CompletionRequest) -> str:
        return "chat/completions"

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.json_mode and self._supports_json_mode:
            # A convenience, never a control: the schema is enforced by our own validation on the
            # way back, because a provider's JSON mode guarantees syntax and nothing else [L8].
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _text(self, payload: dict[str, Any]) -> str:
        content = dig(payload, "choices", 0, "message", "content")
        return content if isinstance(content, str) else ""

    def _usage(self, payload: dict[str, Any]) -> tuple[int, int]:
        return (
            token_count(payload, "usage", "prompt_tokens"),
            token_count(payload, "usage", "completion_tokens"),
        )
