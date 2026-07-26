"""LLM provision. One adapter, many endpoints.

Failure semantics: a provider that is down, slow past its timeout, or returns junk raises
`ProviderError`. The seat then falls back per its chain and, failing that, abstains — a panel
where more than a third of seats abstain resolves to `WAIT (PANEL_DEGRADED)` (DESIGN §8.1).

Providers are never given tools and never asked to fetch a fact. Every number the model needs
is pre-computed and injected (DESIGN [L7]).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradebot.core.schema import DomainModel, Money


class CompletionRequest(DomainModel):
    """One call to one model."""

    model: str
    system: str
    user: str
    temperature: float = 0.3
    max_tokens: int = 1024
    timeout_seconds: float = 120.0
    #: Ask the provider to constrain output to JSON where it supports it. Never a substitute
    #: for validation — schema enforcement is ours, not the provider's (DESIGN [L8]).
    json_mode: bool = True


class CompletionResult(DomainModel):
    """What came back, plus what it cost.

    Cost is persisted per cycle: a research testbed comparing panel configurations has to be
    able to show $/decision (DESIGN §6.5).
    """

    text: str
    model_fingerprint: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Money | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """An LLM endpoint family: `openai_compat`, `anthropic`, `gemini`, or a stub."""

    provider_id: str

    async def complete(self, request: CompletionRequest) -> CompletionResult: ...
