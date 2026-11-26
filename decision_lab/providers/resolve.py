"""Turn an `ArchivePolicy` binding into a provider, without touching the bot's registry.

The bot's `PRESETS` name the vendors a trading panel may reach. The Claude subscription is a
decision_lab-only binding (it spawns a local CLI, not an HTTP call), so it lives in `LAB_PROVIDERS`
here and the bot's registry never learns of it — `tradebot/` stays free of `decision_lab`.

Failure semantics: `None` means the binding is unwired (its key is unset), which the caller
reports as a refusal before any spend. A missing SDK for the subscription binding is *not*
`None`: `ClaudeSubscriptionProvider` raises `SdkMissing` (a `ConfigError`) when first used, naming
the extra to install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from decision_lab.providers.claude_subscription import PROVIDER_ID, ClaudeSubscriptionProvider
from tradebot.core.clock import SystemClock
from tradebot.decision.providers.registry import build_providers, preset
from tradebot.interfaces.llm import LLMProvider

if TYPE_CHECKING:
    from decision_lab.archive.config import ArchivePolicy

#: Providers the lab binds that the bot's `PRESETS` does not know.
LAB_PROVIDERS: Final = frozenset({PROVIDER_ID})


def build_summariser(policy: ArchivePolicy) -> LLMProvider | None:
    """The provider `policy` binds, or `None` when it is unreachable (key not set)."""
    if policy.provider_id == PROVIDER_ID:
        return ClaudeSubscriptionProvider()
    settings = preset(policy.provider_id)
    pool = build_providers((settings,), SystemClock())
    return pool.providers.get(policy.provider_id)
