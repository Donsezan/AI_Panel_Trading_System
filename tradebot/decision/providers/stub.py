"""A scripted LLM provider. No network, no cost, no non-determinism.

This is what makes the walking skeleton runnable and the whole suite free and repeatable. It is
also the fault-injection point for rung-3 chaos tests: script malformed JSON, a schema-violating
vote, or a provider outage and assert that the panel degrades to `WAIT` rather than trading on
junk (PLAN §7).

An *unscripted* stub answers in the schema the prompt asked for, because a real model does:
it reads the schema out of its system prompt. A stub that always answered in the per-asset
schema would fail `BasketAssessment` on every seat of a `basket`-mode basket, replay the same
canned text for the repair attempt, and leave the panel resolving `WAIT (PANEL_DEGRADED)` on
every cycle forever — the default panel being unable to run one of the two decision modes.
A *scripted* response is returned verbatim, always: scripting a per-asset vote into a basket
run is the rung-3 fault injection, and adapting it would delete the only way to assert that a
malformed answer fails closed.

Failure semantics: raises `ProviderError` when scripted to, exactly as a real provider would on
a timeout, so the seat's fallback-then-abstain path is exercised by the same code path.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from itertools import cycle

from tradebot.core.errors import ProviderError
from tradebot.decision.prompts import symbols_requested
from tradebot.interfaces.llm import CompletionRequest, CompletionResult

DEFAULT_RESPONSE = """{
  "action": "BUY",
  "conviction": 4,
  "size_hint": "half",
  "thesis": "Momentum is constructive and the position is well within budget.",
  "key_risks": ["momentum can reverse without warning"],
  "invalidation": "RSI closing back below 45 on the 1h timeframe"
}"""

#: The cross-asset half of a `basket`-mode answer. The per-instrument half is `DEFAULT_RESPONSE`
#: itself, so the stub says the same thing about an instrument in either decision mode and a
#: scenario's outcome does not depend on which mode its basket is in.
DEFAULT_BASKET_VIEW = "These legs are one momentum view; taken together they double a single bet."

#: Sentinel entry: a scripted response equal to this raises instead of returning.
FAIL = "<<PROVIDER_FAILURE>>"


def _default_response(request: CompletionRequest) -> str:
    """The answer an unscripted stub gives, in whichever schema the prompt asked for.

    Reading the symbols back out of the prompt is what a model does with the same text, and it
    is the only source of them: `parse_assessments` refuses a symbol that was not in the
    snapshot, so a placeholder key would be a hallucination and abstain the seat.
    """
    symbols = symbols_requested(request.user)
    if not symbols:
        return DEFAULT_RESPONSE
    vote = json.loads(DEFAULT_RESPONSE)
    return json.dumps(
        {"assessments": dict.fromkeys(symbols, vote), "basket_view": DEFAULT_BASKET_VIEW},
        indent=2,
    )


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
        self._responses = cycle(list(responses)) if responses else None
        self._cost_usd = cost_usd
        self._latency_ms = latency_ms
        self.calls: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        text = _default_response(request) if self._responses is None else next(self._responses)
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
