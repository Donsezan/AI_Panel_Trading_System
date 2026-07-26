"""LLM provider adapters. Phase 4 adds `openai_compat`, `anthropic` and `gemini`."""

from tradebot.decision.providers.stub import DEFAULT_RESPONSE, FAIL, StubLLMProvider

__all__ = ["DEFAULT_RESPONSE", "FAIL", "StubLLMProvider"]
