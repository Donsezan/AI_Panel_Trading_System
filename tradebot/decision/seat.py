"""Running one seat: call, validate, one repair attempt, else abstain.

The output contract is hard on purpose. A response that fails validation is a **failed vote**,
never a best-effort parse — a partially understood answer is exactly how a hallucination
becomes a smaller, subtler, still-wrong order (DESIGN [L8]).

Failure semantics:
* provider down/slow  → try the seat's fallback chain, then abstain
* invalid JSON/schema → one repair attempt with the error fed back, then abstain
* an abstention is recorded with its reason, so a degraded panel is investigable afterwards
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from pydantic import ValidationError

from tradebot.core.clock import Clock
from tradebot.core.config import SeatConfig
from tradebot.core.decision import SeatResponse, SeatVote
from tradebot.core.errors import ProviderError, SchemaViolationError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel, Money
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.prompts import build_system_prompt, build_user_prompt
from tradebot.interfaces.llm import CompletionRequest, CompletionResult, LLMProvider

logger = get_logger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

REPAIR_INSTRUCTION = (
    "Your previous reply was rejected: {error}\n"
    "Reply again with JSON only, matching the schema exactly. No prose, no code fences."
)


class SeatOutcome(DomainModel):
    """A seat's response plus what it cost, so cost can be aggregated per cycle."""

    response: SeatResponse
    cost_usd: Money | None = None


def parse_vote(text: str) -> SeatVote:
    """Extract and validate a seat vote.

    Code fences and surrounding prose are tolerated — they are a provider habit, not a contract
    violation. Anything beyond that is a schema violation and therefore a failed vote.
    """
    match = _JSON_BLOCK.search(text)
    if match is None:
        raise SchemaViolationError("no JSON object found in the response")
    try:
        payload = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise SchemaViolationError(f"response is not valid JSON: {exc}") from exc
    try:
        return SeatVote.model_validate(payload)
    except ValidationError as exc:
        raise SchemaViolationError(f"response does not match the seat schema: {exc}") from exc


class SeatRunner:
    """Turns a `SeatConfig` plus a snapshot into a validated `SeatResponse`."""

    def __init__(self, providers: Mapping[str, LLMProvider], clock: Clock) -> None:
        self._providers = dict(providers)
        self._clock = clock

    async def run(
        self,
        seat: SeatConfig,
        snapshot: ContextSnapshot,
        instrument_key: str,
        *,
        round_index: int = 0,
        transcript: tuple[str, ...] = (),
    ) -> SeatOutcome:
        system = build_system_prompt(seat)
        user = build_user_prompt(snapshot, seat, instrument_key, transcript)

        try:
            provider, result = await self._complete(seat, system, user)
        except ProviderError as exc:
            return self._abstain(seat, instrument_key, round_index, f"provider unavailable: {exc}")

        try:
            vote = parse_vote(result.text)
        except SchemaViolationError as first_error:
            repaired = await self._repair(seat, provider, system, user, str(first_error))
            if repaired is None:
                return self._abstain(
                    seat,
                    instrument_key,
                    round_index,
                    f"schema violation: {first_error}",
                    result=result,
                )
            vote, result = repaired

        return SeatOutcome(
            response=SeatResponse(
                seat_id=seat.seat_id,
                role=seat.role,
                provider_id=provider.provider_id,
                model=seat.model,
                round_index=round_index,
                instrument_key=instrument_key,
                vote=vote,
                raw_text=result.text,
                latency_ms=result.latency_ms,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                responded_at=self._clock.now(),
            ),
            cost_usd=result.cost_usd,
        )

    async def _complete(
        self, seat: SeatConfig, system: str, user: str
    ) -> tuple[LLMProvider, CompletionResult]:
        """Try the seat's provider, then each fallback. A substitution is visible in the result.

        Fallbacks preserve the seat's role but can collapse panel heterogeneity; the consensus
        rule flags that as `PANEL_HOMOGENEOUS` (DESIGN §6.5, R11).
        """
        last_error: ProviderError | None = None
        for provider_id in (seat.provider_id, *seat.fallbacks):
            provider = self._providers.get(provider_id)
            if provider is None:
                continue
            try:
                result = await provider.complete(
                    CompletionRequest(
                        model=seat.model, system=system, user=user, temperature=seat.temperature
                    )
                )
            except ProviderError as exc:
                logger.warning(
                    "seat provider failed", extra={"seat_id": seat.seat_id, "provider": provider_id}
                )
                last_error = exc
                continue
            return provider, result
        raise last_error or ProviderError(f"no provider available for seat {seat.seat_id}")

    async def _repair(
        self, seat: SeatConfig, provider: LLMProvider, system: str, user: str, error: str
    ) -> tuple[SeatVote, CompletionResult] | None:
        """One repair attempt. A second failure is an abstention, not a third try."""
        try:
            result = await provider.complete(
                CompletionRequest(
                    model=seat.model,
                    system=system,
                    user=f"{user}\n\n{REPAIR_INSTRUCTION.format(error=error)}",
                    temperature=seat.temperature,
                )
            )
            return parse_vote(result.text), result
        except (ProviderError, SchemaViolationError):
            return None

    def _abstain(
        self,
        seat: SeatConfig,
        instrument_key: str,
        round_index: int,
        reason: str,
        result: CompletionResult | None = None,
    ) -> SeatOutcome:
        logger.warning("seat abstained", extra={"seat_id": seat.seat_id, "reason": reason})
        return SeatOutcome(
            response=SeatResponse(
                seat_id=seat.seat_id,
                role=seat.role,
                provider_id=seat.provider_id,
                model=seat.model,
                round_index=round_index,
                instrument_key=instrument_key,
                vote=None,
                abstain_reason=reason,
                raw_text=result.text if result else "",
                responded_at=self._clock.now(),
            ),
            cost_usd=result.cost_usd if result else None,
        )
