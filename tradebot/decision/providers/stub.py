"""A scripted LLM provider. No network, no cost, no non-determinism.

This is what makes the walking skeleton runnable and the whole suite free and repeatable. It is
also the fault-injection point for rung-3 chaos tests: script malformed JSON, a schema-violating
vote, or a provider outage and assert that the panel degrades to `WAIT` rather than trading on
junk (PLAN §7).

Failure semantics: raises `ProviderError` when scripted to, exactly as a real provider would on
a timeout, so the seat's fallback-then-abstain path is exercised by the same code path.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from itertools import cycle

from tradebot.core.errors import ProviderError
from tradebot.interfaces.llm import CompletionRequest, CompletionResult

DEFAULT_RESPONSE = """{
  "action": "BUY",
  "conviction": 4,
  "size_hint": "half",
  "thesis": "Momentum is constructive and the position is well within budget.",
  "key_risks": ["momentum can reverse without warning"],
  "invalidation": "RSI closing back below 45 on the 1h timeframe"
}"""

#: Sentinel entry: a scripted response equal to this raises instead of returning.
FAIL = "<<PROVIDER_FAILURE>>"


class StubLLMProvider:
    """Replays scripted responses in order, repeating the sequence once exhausted."""

    provider_id = "stub"

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        provider_id: str = "stub",
        cost_usd: Decimal = Decimal("0"),
        latency_ms: int = 0,
    ) -> None:
        self.provider_id = provider_id
        self._responses = cycle(list(responses) if responses else [DEFAULT_RESPONSE])
        self._cost_usd = cost_usd
        self._latency_ms = latency_ms
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        text = next(self._responses)
        if text == FAIL:
            raise ProviderError(f"scripted failure from {self.provider_id}")
        # Held to the same contract as a real provider, so the contract suite can run against
        # this one too and the stub cannot drift into being easier to satisfy than reality.
        if not text.strip():
            raise ProviderError(f"{self.provider_id} returned an empty completion")
        return CompletionResult(
            text=text,
            model_fingerprint=f"{self.provider_id}:{request.model}",
            prompt_tokens=len(request.user) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=self._latency_ms,
            cost_usd=self._cost_usd,
        )
