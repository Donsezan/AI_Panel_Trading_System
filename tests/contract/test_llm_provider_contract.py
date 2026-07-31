"""One contract suite, run against every `LLMProvider` (rung 2, PLAN §7).

Cassettes, not calls: each case is driven by a recorded response body played back through
`httpx.MockTransport`, so the suite is deterministic, free, and offline — the Phase 4 exit
criterion. Adding a provider means adding one `ProviderCase` and nothing else; a provider whose
semantics diverge fails CI.

What the interface promises, and what a seat therefore relies on:

* a successful call yields non-empty text, integer token counts, and a `Decimal` cost;
* **every** failure — outage, empty completion, junk body — raises `ProviderError`, so a seat
  falls back and then abstains rather than an exception escaping a trading cycle (DESIGN §8.1);
* the provider never validates, repairs, or interprets the vote. Junk in the body reaches the
  seat unchanged, because schema enforcement is ours and one repair attempt is the seat's
  (DESIGN [L8]).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import SeatConfig
from tradebot.core.errors import ProviderError
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.providers import FAIL, StubLLMProvider
from tradebot.decision.providers.anthropic import AnthropicProvider
from tradebot.decision.providers.gemini import GeminiProvider
from tradebot.decision.providers.http import HttpLLMProvider, LLMHttpTransport
from tradebot.decision.providers.openai_compat import OpenAICompatProvider
from tradebot.decision.seat import SeatRunner
from tradebot.interfaces.debate import PanelRequest
from tradebot.interfaces.llm import CompletionRequest, LLMProvider

pytestmark = pytest.mark.contract

VOTE = (
    '{"action": "BUY", "conviction": 4, "size_hint": "half", '
    '"thesis": "Momentum is constructive.", "key_risks": [], "invalidation": "none"}'
)

#: Recorded response bodies, one per vendor. Cassettes rather than live calls: a suite that needs
#: an API key and a network is a suite that stops running (PLAN Phase 4 exit).
CASSETTES: dict[str, Callable[[str], dict[str, Any]]] = {
    "openai_compat": lambda text: {
        "model": "recorded-model",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    },
    "anthropic": lambda text: {
        "model": "recorded-model",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    },
    "gemini": lambda text: {
        "modelVersion": "recorded-model",
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 300},
    },
}

_CLASSES: dict[str, type[HttpLLMProvider]] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def _http_provider(kind: str, clock: ManualClock, handler: Any) -> LLMProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = LLMHttpTransport(client, provider_id=kind, base_url="https://api.test")
    return _CLASSES[kind](transport, clock, provider_id=kind)


def _playing(kind: str, text: str) -> Callable[[ManualClock], LLMProvider]:
    def build(clock: ManualClock) -> LLMProvider:
        return _http_provider(
            kind, clock, lambda _r: httpx.Response(200, json=CASSETTES[kind](text))
        )

    return build


def _unreachable(kind: str) -> Callable[[ManualClock], LLMProvider]:
    def build(clock: ManualClock) -> LLMProvider:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        return _http_provider(kind, clock, handler)

    return build


@dataclass(frozen=True)
class ProviderCase:
    """One implementation, in the three states every provider must handle identically."""

    provider_id: str
    speaking: Callable[[ManualClock], LLMProvider]
    silent: Callable[[ManualClock], LLMProvider]
    down: Callable[[ManualClock], LLMProvider]
    rambling: Callable[[ManualClock], LLMProvider]


CASES = (
    ProviderCase(
        provider_id="stub",
        speaking=lambda _c: StubLLMProvider([VOTE]),
        silent=lambda _c: StubLLMProvider(["   "]),
        down=lambda _c: StubLLMProvider([FAIL]),
        rambling=lambda _c: StubLLMProvider(["I would probably buy some."]),
    ),
    *(
        ProviderCase(
            provider_id=kind,
            speaking=_playing(kind, VOTE),
            silent=_playing(kind, "  "),
            down=_unreachable(kind),
            rambling=_playing(kind, "I would probably buy some."),
        )
        for kind in CASSETTES
    ),
)


@pytest.fixture(params=CASES, ids=lambda case: case.provider_id)
def case(request: pytest.FixtureRequest) -> ProviderCase:
    return request.param  # type: ignore[no-any-return]


def a_request() -> CompletionRequest:
    return CompletionRequest(model="recorded-model", system="system", user="user")


class TestProviderContract:
    async def test_a_successful_call_returns_usable_text(
        self, case: ProviderCase, clock: ManualClock
    ) -> None:
        result = await case.speaking(clock).complete(a_request())
        assert result.text.strip()
        assert result.model_fingerprint.startswith(f"{case.provider_id}:")

    async def test_token_counts_are_non_negative_integers(
        self, case: ProviderCase, clock: ManualClock
    ) -> None:
        result = await case.speaking(clock).complete(a_request())
        for count in (result.prompt_tokens, result.completion_tokens):
            assert isinstance(count, int) and count >= 0

    async def test_cost_is_decimal_or_absent_but_never_float(
        self, case: ProviderCase, clock: ManualClock
    ) -> None:
        """A float on a money path is the defect `core/money` exists to prevent (PLAN §2.1)."""
        cost = (await case.speaking(clock).complete(a_request())).cost_usd
        assert cost is None or isinstance(cost, Decimal)

    async def test_an_outage_raises_provider_error(
        self, case: ProviderCase, clock: ManualClock
    ) -> None:
        with pytest.raises(ProviderError):
            await case.down(clock).complete(a_request())

    async def test_an_empty_completion_raises_provider_error(
        self, case: ProviderCase, clock: ManualClock
    ) -> None:
        """Silence is a failed call, not a considered non-answer."""
        with pytest.raises(ProviderError):
            await case.silent(clock).complete(a_request())

    async def test_a_provider_never_validates_the_vote_itself(
        self, case: ProviderCase, clock: ManualClock
    ) -> None:
        """Schema enforcement is ours. A provider that quietly repaired junk would hide [L8]."""
        result = await case.rambling(clock).complete(a_request())
        assert result.text == "I would probably buy some."


class TestSeatsDriveEveryProviderIdentically:
    """The seat layer must not care which vendor answered."""

    async def test_a_good_response_becomes_a_vote(
        self,
        case: ProviderCase,
        clock: ManualClock,
        snapshot: ContextSnapshot,
        request_for: PanelRequest,
    ) -> None:
        seat = SeatConfig(
            seat_id="s", role="Technical", provider_id=case.provider_id, model="recorded-model"
        )
        runner = SeatRunner({case.provider_id: case.speaking(clock)}, clock)

        (response,) = await runner.run(seat, snapshot, request_for)
        assert response.vote is not None
        assert response.provider_id == case.provider_id

    async def test_an_outage_becomes_an_abstention_not_an_exception(
        self,
        case: ProviderCase,
        clock: ManualClock,
        snapshot: ContextSnapshot,
        request_for: PanelRequest,
    ) -> None:
        seat = SeatConfig(
            seat_id="s", role="Technical", provider_id=case.provider_id, model="recorded-model"
        )
        runner = SeatRunner({case.provider_id: case.down(clock)}, clock)

        (response,) = await runner.run(seat, snapshot, request_for)
        assert response.abstained
        assert "provider unavailable" in (response.abstain_reason or "")

    async def test_junk_becomes_an_abstention_after_one_repair(
        self,
        case: ProviderCase,
        clock: ManualClock,
        snapshot: ContextSnapshot,
        request_for: PanelRequest,
    ) -> None:
        seat = SeatConfig(
            seat_id="s", role="Technical", provider_id=case.provider_id, model="recorded-model"
        )
        runner = SeatRunner({case.provider_id: case.rambling(clock)}, clock)

        (response,) = await runner.run(seat, snapshot, request_for)
        assert response.abstained
        assert "schema violation" in (response.abstain_reason or "")
