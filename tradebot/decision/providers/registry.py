"""Turning provider configuration into wired adapters.

A panel is data (DESIGN §6.5), so the set of endpoints it can reach has to be data too. This
module is the one place that maps a provider *kind* onto a concrete class, an auth header, and a
base URL — which is what lets a seat's fallback chain cross vendor families, and what makes a
model running under LM Studio or `llama.cpp` on the operator's own machine a legitimate backup
for a free hosted slot (R11).

Two things are asserted at wiring time, where a defect can still be fatal rather than becoming a
silently degraded panel on every cycle:

* **A named secret must exist.** `secret_ref` is an environment variable *name*; the value is
  read at startup, registered with the log redactor, and never stored, logged, or put in a prompt
  (PLAN §3.2). A missing one refuses to start.
* **A remote endpoint must be TLS.** Prompts carry position sizes and unrealized PnL. Loopback
  over plain HTTP is fine — that is the local-model case — but plaintext to another host is not.

Failure semantics: construction raises `ConfigError` (fatal, refuse to start). Everything after
construction is the provider's own contract — every call failure is a `ProviderError`, so a seat
degrades to an abstention and the cycle to `WAIT`.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

import httpx

from tradebot.core.clock import Clock
from tradebot.core.config import ProviderSettings
from tradebot.core.enums import ProviderKind
from tradebot.core.errors import ConfigError
from tradebot.core.logging import SECRETS, get_logger
from tradebot.decision.providers import anthropic as anthropic_provider
from tradebot.decision.providers import gemini as gemini_provider
from tradebot.decision.providers.http import HttpLLMProvider, LLMHttpTransport
from tradebot.decision.providers.openai_compat import OpenAICompatProvider
from tradebot.decision.providers.stub import StubLLMProvider
from tradebot.interfaces.llm import LLMProvider

logger = get_logger(__name__)

DEFAULT_TIMEOUT: Final = 120.0

#: Hosts for which plain HTTP is acceptable, because the traffic never leaves the machine.
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


#: Endpoints known out of the box. Everything here is overridable and nothing is implied: a
#: provider that is not in a basket's panel is never constructed and never contacted.
PRESETS: Final[Mapping[str, ProviderSettings]] = {
    "openrouter": ProviderSettings(
        provider_id="openrouter",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="https://openrouter.ai/api/v1",
        secret_ref="OPENROUTER_API_KEY",
    ),
    "openai": ProviderSettings(
        provider_id="openai",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="https://api.openai.com/v1",
        secret_ref="OPENAI_API_KEY",
    ),
    "anthropic": ProviderSettings(
        provider_id="anthropic",
        kind=ProviderKind.ANTHROPIC,
        base_url=anthropic_provider.DEFAULT_BASE_URL,
        secret_ref="ANTHROPIC_API_KEY",
    ),
    "gemini": ProviderSettings(
        provider_id="gemini",
        kind=ProviderKind.GEMINI,
        base_url=gemini_provider.DEFAULT_BASE_URL,
        secret_ref="GEMINI_API_KEY",
    ),
    # Local runtimes. No key, no cost, no network egress — the fallback of last resort that
    # keeps a seat answering when every hosted slot in its chain is down.
    "lmstudio": ProviderSettings(
        provider_id="lmstudio",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://localhost:1234/v1",
    ),
    "llamacpp": ProviderSettings(
        provider_id="llamacpp",
        kind=ProviderKind.OPENAI_COMPAT,
        base_url="http://localhost:8080/v1",
    ),
    # Not a vendor: the scripted provider the offline demo and the whole test suite run on.
    "stub": ProviderSettings(provider_id="stub", kind=ProviderKind.STUB),
}


def _openai_compat(
    settings: ProviderSettings, transport: LLMHttpTransport, clock: Clock
) -> HttpLLMProvider:
    return OpenAICompatProvider(
        transport,
        clock,
        provider_id=settings.provider_id,
        prices=settings.prices,
        supports_json_mode=settings.supports_json_mode,
    )


def _anthropic(
    settings: ProviderSettings, transport: LLMHttpTransport, clock: Clock
) -> HttpLLMProvider:
    return anthropic_provider.AnthropicProvider(
        transport, clock, provider_id=settings.provider_id, prices=settings.prices
    )


def _gemini(
    settings: ProviderSettings, transport: LLMHttpTransport, clock: Clock
) -> HttpLLMProvider:
    return gemini_provider.GeminiProvider(
        transport, clock, provider_id=settings.provider_id, prices=settings.prices
    )


_FACTORIES: Final[
    Mapping[ProviderKind, Callable[[ProviderSettings, LLMHttpTransport, Clock], HttpLLMProvider]]
] = {
    ProviderKind.OPENAI_COMPAT: _openai_compat,
    ProviderKind.ANTHROPIC: _anthropic,
    ProviderKind.GEMINI: _gemini,
}

_AUTH_HEADERS: Final[Mapping[ProviderKind, Callable[[str], dict[str, str]]]] = {
    ProviderKind.OPENAI_COMPAT: lambda key: {"Authorization": f"Bearer {key}"},
    ProviderKind.ANTHROPIC: anthropic_provider.auth_headers,
    ProviderKind.GEMINI: gemini_provider.auth_headers,
}


def preset(provider_id: str, **overrides: object) -> ProviderSettings:
    """A known endpoint, optionally adjusted — e.g. a different local port."""
    known = PRESETS.get(provider_id)
    if known is None:
        raise ConfigError(
            f"unknown provider {provider_id!r}; known providers: {', '.join(sorted(PRESETS))}"
        )
    return known.model_copy(update=dict(overrides)) if overrides else known


def assert_secure_endpoint(settings: ProviderSettings) -> None:
    """Refuse to send a prompt containing position data to a remote host in plaintext."""
    if not settings.kind.needs_endpoint:
        return
    parts = urlsplit(settings.base_url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS:
        return
    raise ConfigError(
        f"provider {settings.provider_id!r} resolves to {settings.base_url!r}; prompts carry "
        "position and PnL context, so a non-loopback endpoint must be https"
    )


def resolve_secret(settings: ProviderSettings, environ: Mapping[str, str]) -> str | None:
    """Read the named key, register it for redaction, and hand it back to the caller only."""
    if settings.secret_ref is None:
        return None
    value = (environ.get(settings.secret_ref) or "").strip()
    if not value:
        raise ConfigError(
            f"provider {settings.provider_id!r} needs {settings.secret_ref} in the environment; "
            "refusing to start a panel whose seat cannot reach its model"
        )
    SECRETS.register(value)
    return value


@dataclass(slots=True)
class ProviderPool:
    """The providers a panel can reach, plus the HTTP client they share."""

    providers: dict[str, LLMProvider] = field(default_factory=dict)
    _client: httpx.AsyncClient | None = None

    @property
    def owns_client(self) -> bool:
        """Whether this pool holds a socket that has to be released.

        False for an all-stub panel, which is how the composition root keeps its invariant that a
        closer exists if and only if a resource does.
        """
        return self._client is not None

    async def close(self) -> None:
        """Release the connection pool. A leaked client keeps the process alive after a cycle."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_provider(
    settings: ProviderSettings,
    client: httpx.AsyncClient | None,
    clock: Clock,
    *,
    environ: Mapping[str, str] | None = None,
) -> LLMProvider:
    """Wire one endpoint. Raises `ConfigError` on anything that should refuse to start."""
    if settings.kind is ProviderKind.STUB:
        return StubLLMProvider(provider_id=settings.provider_id)
    if client is None:
        raise ConfigError(f"provider {settings.provider_id!r} needs an HTTP client")
    assert_secure_endpoint(settings)
    secret = resolve_secret(settings, os.environ if environ is None else environ)
    headers = _AUTH_HEADERS[settings.kind](secret) if secret else {}
    transport = LLMHttpTransport(
        client,
        provider_id=settings.provider_id,
        base_url=settings.base_url,
        headers=headers,
    )
    return _FACTORIES[settings.kind](settings, transport, clock)


def build_providers(
    settings: Sequence[ProviderSettings],
    clock: Clock,
    *,
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ProviderPool:
    """Wire every endpoint a panel declares onto one shared HTTP client.

    Called from the composition root only. A client is opened **only if some provider actually
    needs one**, so an all-stub panel holds no socket and cannot reach the network even by
    accident.
    """
    if not settings:
        return ProviderPool()
    duplicates = [
        pid for pid, count in Counter(s.provider_id for s in settings).items() if count > 1
    ]
    if duplicates:
        raise ConfigError(f"duplicate provider ids in panel configuration: {sorted(duplicates)}")

    needs_http = any(s.kind.needs_endpoint for s in settings)
    owned = client is None and needs_http
    fresh = httpx.AsyncClient(timeout=timeout, http2=False) if needs_http else None
    http_client = client or fresh
    providers = {
        s.provider_id: build_provider(s, http_client, clock, environ=environ) for s in settings
    }
    logger.info("llm providers wired", extra={"providers": sorted(providers)})
    return ProviderPool(providers, http_client if owned else None)
