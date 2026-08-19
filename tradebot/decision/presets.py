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

#: The offline panel that *argues*. Same provider as `STUB_PANEL` — no network, no cost, no key —
#: but three seats bound to the stub's `varied-*` model family, so each draws its own vote from
#: `providers/stub_responses.json` and the panel reaches a different answer on different cycles.
#:
#: `STUB_PANEL` cannot exercise any of that, and not because it is a stub: one seat means
#: `required_votes` is 1, so there is no majority to miss, no dissent to record, no abstention
#: fraction to cross, and `has_converged` ends `blind_then_debate` after the blind round. Three
#: seats over a fifteen-entry catalogue reach a qualified majority on some cycles and resolve to
#: `WAIT` for want of one on others, which is what puts the consensus rule and the debate rounds
#: under a running system rather than only under a hand-written test.
#:
#: Three *different* model names on purpose, exactly as the hosted panels use three families:
#: two seats sharing a fingerprint would trip `PANEL_HOMOGENEOUS` on every cycle and make the
#: flag meaningless (DESIGN §6.5, R11). Nothing here reaches a venue or a vendor — a stub is
#: refused in live at `control/readiness.py`, primary or fallback.
SIM_PANEL = PanelConfig(
    panel_id="sim-varied",
    providers=(preset("stub"),),
    protocol="blind_then_debate",
    max_rounds=3,  # one blind round, then two debate rounds
    max_cost_usd_per_cycle=Decimal(0),
    seats=(
        SeatConfig(
            seat_id="technical",
            role="Technical Analyst",
            provider_id="stub",
            model="varied-technical",
            evidence=("indicators", "position"),
        ),
        SeatConfig(
            seat_id="news",
            role="News/Sentiment Analyst",
            provider_id="stub",
            model="varied-news",
            evidence=("news", "position"),
        ),
        SeatConfig(
            seat_id="skeptic",
            role="Macro/Risk Skeptic",
            provider_id="stub",
            model="varied-skeptic",
            evidence=("indicators", "news", "position"),
            devils_advocate=True,
        ),
    ),
)

#: Panels selectable by name, and what a fresh database's demo basket is seeded with.
PANELS: dict[str, PanelConfig] = {
    "stub": STUB_PANEL,
    "sim": SIM_PANEL,
    "free": FREE_PANEL,
    "local": LOCAL_PANEL,
}
