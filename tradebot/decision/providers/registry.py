"""Turning provider configuration into wired adapters.

A panel is data (DESIGN §6.5), so the set of endpoints it can reach has to be data too. This
module is the one place that maps a provider *kind* onto a concrete class, an auth header, and a
base URL — which is what lets a seat's fallback chain cross vendor families, and what makes a
model running under LM Studio or `llama.cpp` on the operator's own machine a legitimate backup
for a free hosted slot (R11).

One thing is asserted at wiring time, where a defect can still be fatal rather than becoming a
silently degraded panel on every cycle:

* **A remote endpoint must be TLS.** Prompts carry position sizes and unrealized PnL. Loopback
  over plain HTTP is fine — that is the local-model case — but plaintext to another host is not.

A **missing key is not one of them**
([ADR 0023](../../../docs/adr/0023-a-missing-provider-key-degrades-the-panel.md)).
`secret_ref` is an environment variable *name*; the value is read here, registered with the log
redactor, and never stored, logged, or put in a prompt (PLAN §3.2). When it is absent the endpoint
is simply *unreachable*, which is the same fact as the provider being down — and every seat's
fallback chain exists to survive exactly that (DESIGN §8.1). So it is left unwired and **reported**
via `reach_of`, rather than killing a process that would otherwise have run: refusing to start
costs an operator the dashboard, the log and the ledger view, which are the things they need in
order to fix it. Live is where that degradation is intolerable, and `control/readiness.py` refuses
on it there.

Failure semantics: construction raises `ConfigError` (fatal, refuse to start) for a panel that is
*malformed*. Everything after construction is the provider's own contract — every call failure is a
`ProviderError`, so a seat degrades to an abstention and the cycle to `WAIT`.
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
from tradebot.core.config import PanelConfig, ProviderSettings
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
    """Read the named key, register it for redaction, and hand it back to the caller only.

    `None` covers both "this provider needs no key" and "the key it names is absent"; the caller
    tells them apart from `secret_ref`, and `unconfigured_providers` is that caller. Deliberately
    does not raise — see the module docstring and ADR 0023.
    """
    if settings.secret_ref is None:
        return None
    value = (environ.get(settings.secret_ref) or "").strip()
    if not value:
        return None
    SECRETS.register(value)
    return value


@dataclass(frozen=True, slots=True)
class UnconfiguredProvider:
    """A declared endpoint whose named key is absent from this machine's environment.

    Not a malformed panel: the configuration is sound and resolves, and only the operator's own
    environment is short of a key. Which is why it is data rather than an exception.
    """

    provider_id: str
    #: The environment variable *name* that is missing. Safe to print; its value never is.
    secret_ref: str

    def __str__(self) -> str:
        return (
            f"provider {self.provider_id!r} has no {self.secret_ref} in the environment. Set it "
            "and restart, or edit the panel in Configure so no seat binds this provider"
        )


def unconfigured_providers(
    settings: Sequence[ProviderSettings], environ: Mapping[str, str]
) -> tuple[UnconfiguredProvider, ...]:
    """Declared endpoints that cannot be reached, because the key they name is not set.

    The single rule. Everything that asks "can this panel work" — the wiring, live readiness, the
    dashboard banner, the Start button — resolves it through here, so none of them can answer
    differently from another.
    """
    return tuple(
        UnconfiguredProvider(settings_.provider_id, settings_.secret_ref)
        for settings_ in settings
        if settings_.secret_ref and resolve_secret(settings_, environ) is None
    )


@dataclass(slots=True)
class ProviderPool:
    """The providers a panel can reach, plus the HTTP client they share."""

    providers: dict[str, LLMProvider] = field(default_factory=dict)
    #: Declared endpoints left unwired because their key is absent. A seat bound to one falls back
    #: down its chain; a seat whose *whole* chain is here abstains, and the cycle resolves to
    #: `WAIT (PANEL_DEGRADED)` — the DESIGN §8.1 response to a provider being down.
    unconfigured: tuple[UnconfiguredProvider, ...] = ()
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
    """Wire one endpoint that has everything it needs. Raises `ConfigError` when it does not.

    A missing key still refuses *here*, and only here: `build_providers` filters such an endpoint
    out before ever calling this, so the refusal is unreachable from the composition root and
    exists to stop a direct caller silently producing a provider that would call a paid endpoint
    with no credentials (ADR 0023).
    """
    if settings.kind is ProviderKind.STUB:
        return StubLLMProvider(provider_id=settings.provider_id)
    if client is None:
        raise ConfigError(f"provider {settings.provider_id!r} needs an HTTP client")
    assert_secure_endpoint(settings)
    secret = resolve_secret(settings, os.environ if environ is None else environ)
    if settings.secret_ref and secret is None:
        raise ConfigError(str(UnconfiguredProvider(settings.provider_id, settings.secret_ref)))
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
    accident — and a panel whose every key is missing opens none either.

    An endpoint with no key is **left out rather than fatal** (ADR 0023), and named on the pool's
    `unconfigured`. Nothing here decides what that means: sim and paper let the seats fall back or
    abstain, live refuses in `control/readiness.py`.
    """
    if not settings:
        return ProviderPool()
    duplicates = [
        pid for pid, count in Counter(s.provider_id for s in settings).items() if count > 1
    ]
    if duplicates:
        raise ConfigError(f"duplicate provider ids in panel configuration: {sorted(duplicates)}")

    env = os.environ if environ is None else environ
    absent = unconfigured_providers(settings, env)
    unreachable = {entry.provider_id for entry in absent}
    reachable = [s for s in settings if s.provider_id not in unreachable]

    needs_http = any(s.kind.needs_endpoint for s in reachable)
    owned = client is None and needs_http
    fresh = httpx.AsyncClient(timeout=timeout, http2=False) if needs_http else None
    http_client = client or fresh
    providers = {
        s.provider_id: build_provider(s, http_client, clock, environ=env) for s in reachable
    }
    logger.info("llm providers wired", extra={"providers": sorted(providers)})
    if absent:
        logger.warning(
            "declared providers have no key and were left unwired; seats bound to them fall "
            "back, and a seat whose whole chain is unwired abstains",
            extra={
                "unconfigured": [entry.provider_id for entry in absent],
                # The variable *names*, never their values. A name is what an operator has to set.
                "missing_env": [entry.secret_ref for entry in absent],
            },
        )
    return ProviderPool(providers, unconfigured=absent, _client=http_client if owned else None)


@dataclass(frozen=True, slots=True)
class PanelReach:
    """What a panel can reach with the keys currently in the environment.

    Answered from configuration and the environment alone — no call is made — so it costs nothing
    and can be asked on every page render. `decision/probe.py` is the stronger and far more
    expensive question of whether a *reachable* model id still resolves and is still accepted.
    """

    panel_id: str
    missing: tuple[UnconfiguredProvider, ...] = ()
    #: Seats that lost part of their chain but keep a binding they can still answer on.
    degraded: tuple[str, ...] = ()
    #: Seats with no reachable binding at all. They abstain on every cycle, so the panel is
    #: permanently short a voice rather than transiently — which is worth saying differently.
    silenced: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.missing

    @property
    def findings(self) -> tuple[str, ...]:
        """One sentence per problem, each naming what an operator has to do about it.

        The missing keys come first: they are the cause, and the seats below are the consequence.
        A silenced seat is reported separately from a degraded one because only the first has
        stopped voting — heterogeneity reduced is a warning, a voice gone is a panel changed.
        """
        consequences = (
            (
                self.silenced,
                "have no reachable binding left and will abstain on every cycle; the panel "
                "decides with fewer voices than it was configured with",
            ),
            (
                self.degraded,
                "lost part of their fallback chain and have less cover if their primary fails",
            ),
        )
        return (
            *(str(entry) for entry in self.missing),
            *(f"seat(s) {', '.join(seats)} {tail}" for seats, tail in consequences if seats),
        )


def reach_of(panel: PanelConfig, environ: Mapping[str, str] | None = None) -> PanelReach:
    """What this panel can reach right now.

    A panel declaring no providers at all is reported healthy, matching
    `PanelConfig._check_bindings_resolve`: its providers are supplied by the composition root, so
    there is nothing here to be missing.
    """
    missing = unconfigured_providers(panel.providers, os.environ if environ is None else environ)
    if not missing:
        return PanelReach(panel.panel_id)
    unreachable = {entry.provider_id for entry in missing}
    reached = {
        seat.seat_id: sum(b.provider_id not in unreachable for b in seat.bindings)
        for seat in panel.seats
    }
    return PanelReach(
        panel.panel_id,
        missing=missing,
        degraded=tuple(
            seat.seat_id for seat in panel.seats if 0 < reached[seat.seat_id] < len(seat.bindings)
        ),
        silenced=tuple(seat.seat_id for seat in panel.seats if not reached[seat.seat_id]),
    )
