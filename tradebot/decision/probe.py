"""Proving a panel can actually reach a model, before live trading depends on it.

A seat that cannot reach any of its bindings abstains, and a panel where more than a third of
seats abstain resolves to `WAIT (PANEL_DEGRADED)`. That is correct behaviour and it costs nothing
in sim or paper — a wasted cycle. In live it is different in kind: the process is holding real
positions whose protective legs are at the venue, and a panel that was never going to work is a
system that will not decide to *exit* either. So live proves the panel before it starts, rather
than discovering it one degraded cycle at a time (PLAN §2.4 fail-closed).

The probe is a real completion, deliberately. A reachability check that only opened a socket
would pass for a model id that no longer exists — which is R11's exact failure, free slots
disappearing without notice — and would pass for a key the endpoint rejects. Sixteen tokens buys
the answer to "can this seat get an answer at all", including whether the model id still resolves.

A seat passes on its **first reachable binding**, walking the chain in order, because that is what
a cycle would do. A seat running on its backup is a warning, not a failure: the chain exists
precisely so an outage is survivable, and refusing to start over a healthy fallback would make the
fallback pointless.

Failure semantics: nothing here raises. `probe_panel` returns one finding per seat that could not
be reached at all, plus the substitutions it noticed, and the caller decides what halts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tradebot.core.config import PanelConfig, ProviderBinding
from tradebot.core.errors import ProviderError
from tradebot.core.logging import get_logger
from tradebot.interfaces.llm import CompletionRequest, LLMProvider

logger = get_logger(__name__)

#: What the probe asks. Not a trading question: the answer is discarded, and asking a real one
#: would put an unvalidated model opinion in the log with no cycle to gate it.
PROBE_SYSTEM = "You are a connectivity probe. Reply with the single word OK."
PROBE_USER = "Reply with OK."

#: Small and short on purpose. This is a liveness check that must not become a cost line, and a
#: provider that needs longer than this to emit one token cannot serve a cycle either.
PROBE_MAX_TOKENS = 16
PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class PanelProbeResult:
    """Which seats could not be reached, and which answered on other than their primary binding."""

    failures: tuple[str, ...] = ()
    substitutions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures


async def probe_panel(
    panel: PanelConfig, providers: Mapping[str, LLMProvider], *, label: str = "panel"
) -> PanelProbeResult:
    """Call every seat's chain until something answers. One finding per seat that never does."""
    failures: list[str] = []
    substitutions: list[str] = []
    for seat in panel.seats:
        # `bindings` is computed, so it is read once: a second read is a different tuple of equal
        # objects, and "did it answer on its primary" would then always be false.
        chain = seat.bindings
        binding, error = await _first_reachable(seat.seat_id, chain, providers)
        if binding is None:
            failures.append(
                f"{label} seat {seat.seat_id!r} could not reach any of its "
                f"{len(chain)} bindings: {error}"
            )
            continue
        if binding != chain[0]:
            substitutions.append(f"{seat.seat_id} on {binding.fingerprint}")
    return PanelProbeResult(tuple(failures), tuple(substitutions))


async def _first_reachable(
    seat_id: str, bindings: Sequence[ProviderBinding], providers: Mapping[str, LLMProvider]
) -> tuple[ProviderBinding | None, str]:
    """The first binding that answers, or the last reason none did."""
    reason = "the seat declares no bindings"
    for binding in bindings:
        provider = providers.get(binding.provider_id)
        if provider is None:
            reason = f"{binding.provider_id} is not among the panel's declared providers"
            continue
        try:
            await provider.complete(
                CompletionRequest(
                    model=binding.model,
                    system=PROBE_SYSTEM,
                    user=PROBE_USER,
                    max_tokens=PROBE_MAX_TOKENS,
                    timeout_seconds=PROBE_TIMEOUT_SECONDS,
                    json_mode=False,
                )
            )
        except ProviderError as exc:
            logger.warning(
                "probe could not reach a seat binding",
                extra={"seat_id": seat_id, "provider": binding.provider_id, "error": str(exc)},
            )
            reason = str(exc)
            continue
        return binding, ""
    return None, reason
