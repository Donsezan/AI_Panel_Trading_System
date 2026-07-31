"""Seed panels, as data.

A panel is data, not code (DESIGN §6.5). These are starting points: a fresh database's demo
basket is seeded with one, and from then on the stored basket carries its own panel, which the
dashboard edits. The shapes here are the ones the engine consumes and the ConfigStore stores.

Each panel carries **its own providers**, so a panel is self-describing: the endpoints it may
reach and the seats that reach them are one editable tree, and validation proves every binding
resolves before anything runs.

> **Model ids need verifying before a real run.** OpenRouter's free slots appear and disappear
> without notice — that churn is R11, and it is why every seat below carries a cross-family
> fallback chain rather than a second model from the same vendor. Check the ids against
> openrouter.ai/models; a stale one costs nothing worse than a seat that falls back, because an
> unreachable model is a `ProviderError` and an unreachable *seat* is an abstention.

The three seats are three different model families on purpose, and so are their fallbacks.
Heterogeneity is a structural control against sycophantic convergence, not a preference: a panel
of one model debating itself is an expensive way to get one model's answer (DESIGN [L5]).
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.config import PanelConfig, ProviderBinding, SeatConfig
from tradebot.decision.providers.registry import preset

#: Free OpenRouter slots, one per seat, from three different families.
TECHNICAL_MODEL = "deepseek/deepseek-chat-v3-0324:free"
NEWS_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
SKEPTIC_MODEL = "qwen/qwen-2.5-72b-instruct:free"

#: Models loaded in LM Studio. Two different ones, because two seats falling back to the *same*
#: local model would collapse the panel's heterogeneity precisely when it is most needed — the
#: `PANEL_HOMOGENEOUS` flag would fire, and the debate would be one model arguing with itself.
LOCAL_TECHNICAL_MODEL = "qwen2.5-7b-instruct"
LOCAL_SKEPTIC_MODEL = "mistral-7b-instruct"

GEMINI_MODEL = "gemini-2.0-flash"

#: Endpoints the free panel may reach. Only `openrouter` needs a key; the others are optional
#: extras an operator enables by having them running (LM Studio) or by setting a key (Gemini).
FREE_PANEL_PROVIDERS = (
    preset("openrouter"),
    preset("gemini"),
    preset("lmstudio"),
)


def _seat(
    seat_id: str,
    role: str,
    model: str,
    evidence: tuple[str, ...],
    fallbacks: tuple[ProviderBinding, ...],
    *,
    devils_advocate: bool = False,
) -> SeatConfig:
    return SeatConfig(
        seat_id=seat_id,
        role=role,
        provider_id="openrouter",
        model=model,
        evidence=evidence,
        fallbacks=fallbacks,
        devils_advocate=devils_advocate,
    )


#: The DESIGN §6.5 panel.
#:
#: Every seat sees `position` — a seat that cannot see the position cannot distinguish HOLD
#: ("keep what we have") from WAIT ("no signal"), which is the one distinction the consensus rule
#: most depends on.
#:
#: The fallback chains differ per seat, which is the whole point of them being per-seat data:
#:
#: | Seat      | Primary               | Falls back to             |
#: |-----------|-----------------------|---------------------------|
#: | technical | OpenRouter (DeepSeek) | LM Studio (local Qwen)    |
#: | news      | OpenRouter (Llama)    | Gemini                    |
#: | skeptic   | OpenRouter (Qwen)     | LM Studio (local Mistral) |
#:
#: Three seats landing on three *different* backups is what keeps a single OpenRouter outage from
#: turning a heterogeneous panel into one model with three names. Two seats sharing a backup would
#: trip `PANEL_HOMOGENEOUS` exactly when the panel is already degraded (DESIGN §6.5, R11).
FREE_PANEL = PanelConfig(
    panel_id="free-heterogeneous",
    providers=FREE_PANEL_PROVIDERS,
    protocol="blind_then_debate",
    max_rounds=3,  # one blind round, then two debate rounds
    max_cost_usd_per_cycle=Decimal("0.50"),
    seats=(
        _seat(
            "technical",
            "Technical Analyst",
            TECHNICAL_MODEL,
            ("indicators", "position"),
            (ProviderBinding(provider_id="lmstudio", model=LOCAL_TECHNICAL_MODEL),),
        ),
        _seat(
            "news",
            "News/Sentiment Analyst",
            NEWS_MODEL,
            ("news", "position"),
            (ProviderBinding(provider_id="gemini", model=GEMINI_MODEL),),
        ),
        _seat(
            "skeptic",
            "Macro/Risk Skeptic",
            SKEPTIC_MODEL,
            ("indicators", "news", "position"),
            (ProviderBinding(provider_id="lmstudio", model=LOCAL_SKEPTIC_MODEL),),
            devils_advocate=True,
        ),
    ),
)

#: Every seat on the operator's own machine. No key, no cost, no egress — the panel to run when
#: the hosted slots are all unreliable, or when a soak should not depend on anyone else's uptime.
#: Two LM Studio entries on different ports, because one server serves one model at a time.
LOCAL_PANEL = PanelConfig(
    panel_id="local-only",
    providers=(
        preset("lmstudio"),
        preset("llamacpp"),
    ),
    protocol="blind_then_debate",
    max_rounds=3,
    max_cost_usd_per_cycle=Decimal(0),
    seats=(
        SeatConfig(
            seat_id="technical",
            role="Technical Analyst",
            provider_id="lmstudio",
            model=LOCAL_TECHNICAL_MODEL,
            evidence=("indicators", "position"),
            fallbacks=(ProviderBinding(provider_id="llamacpp", model=LOCAL_SKEPTIC_MODEL),),
        ),
        SeatConfig(
            seat_id="skeptic",
            role="Macro/Risk Skeptic",
            provider_id="llamacpp",
            model=LOCAL_SKEPTIC_MODEL,
            evidence=("indicators", "news", "position"),
            fallbacks=(ProviderBinding(provider_id="lmstudio", model=LOCAL_TECHNICAL_MODEL),),
            devils_advocate=True,
        ),
    ),
)

#: The offline panel. No network, no cost, no key — what the zero-configuration demo runs on.
STUB_PANEL = PanelConfig(
    panel_id="stub",
    providers=(preset("stub"),),
    seats=(
        SeatConfig(
            seat_id="technical",
            role="Technical Analyst",
            provider_id="stub",
            model="stub-technical",
            evidence=("indicators", "position"),
        ),
    ),
)

#: Panels selectable by name, and what a fresh database's demo basket is seeded with.
PANELS: dict[str, PanelConfig] = {
    "stub": STUB_PANEL,
    "free": FREE_PANEL,
    "local": LOCAL_PANEL,
}
