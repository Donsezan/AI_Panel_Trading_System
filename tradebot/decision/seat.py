"""Running one seat: call, validate, one repair attempt, else abstain.

The output contract is hard on purpose. A response that fails validation is a **failed vote**,
never a best-effort parse — a partially understood answer is exactly how a hallucination
becomes a smaller, subtler, still-wrong order (DESIGN [L8]).

A seat's fallback chain crosses provider families deliberately. A chain that stays inside one
vendor does not survive that vendor's outage, and free model slots disappear without notice
(R11); a local runtime at the end of the chain is the one binding no hosted outage can take
away. The binding that actually answered is recorded on the response, so a substitution is
visible in the transcript rather than inferred.

Failure semantics — none of these raise, all of them end in no trade:

* provider down/slow  → try the next binding in the chain, then abstain
* invalid JSON/schema → one repair attempt with the error fed back, then abstain
* basket mode, an instrument the seat did not assess → that instrument abstains, the rest stand
* an abstention is recorded with its reason, so a degraded panel is investigable afterwards
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Final

from pydantic import ValidationError

from tradebot.core.clock import Clock
from tradebot.core.config import ProviderBinding, SeatConfig
from tradebot.core.decision import BasketAssessment, SeatResponse, SeatVote
from tradebot.core.enums import DecisionMode
from tradebot.core.errors import ProviderError, SchemaViolationError
from tradebot.core.ids import new_uuid
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.prompts import build_system_prompt, build_user_prompt
from tradebot.interfaces.debate import PanelRequest
from tradebot.interfaces.llm import CompletionRequest, CompletionResult, LLMProvider

logger = get_logger(__name__)

#: Greedy from the first brace to the last, so a nested basket object survives surrounding prose.
_JSON_BLOCK: Final = re.compile(r"\{.*\}", re.DOTALL)

REPAIR_INSTRUCTION = (
    "Your previous reply was rejected: {error}\n"
    "Reply again with JSON only, matching the schema exactly. No prose, no code fences."
)

NO_ASSESSMENT = "the seat returned no assessment for this instrument"


def _extract_object(text: str) -> object:
    match = _JSON_BLOCK.search(text)
    if match is None:
        raise SchemaViolationError("no JSON object found in the response")
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise SchemaViolationError(f"response is not valid JSON: {exc}") from exc


def parse_vote(text: str) -> SeatVote:
    """Extract and validate a single-instrument seat vote.

    Code fences and surrounding prose are tolerated — they are a provider habit, not a contract
    violation. Anything beyond that is a schema violation and therefore a failed vote.
    """
    try:
        return SeatVote.model_validate(_extract_object(text))
    except ValidationError as exc:
        raise SchemaViolationError(f"response does not match the seat schema: {exc}") from exc


def parse_assessments(text: str, symbols: Mapping[str, str]) -> dict[str, SeatVote]:
    """Extract a basket-mode response and key it by instrument.

    `symbols` maps every accepted spelling — the venue symbol and the full instrument key — onto
    the instrument key. A symbol we never asked about is a schema violation: a confident opinion
    on an instrument that was not in the snapshot is a hallucination, and admitting it would let
    the panel vote on something the risk layer has no policy for.

    A *missing* instrument is not a violation. The seat simply did not cover it, which the caller
    turns into an abstention for that instrument alone — both directions fail closed, and one
    omission should not discard the assessments that were sound.
    """
    try:
        assessment = BasketAssessment.model_validate(_extract_object(text))
    except ValidationError as exc:
        raise SchemaViolationError(f"response does not match the basket schema: {exc}") from exc

    votes: dict[str, SeatVote] = {}
    for raw_symbol, vote in assessment.assessments.items():
        key = symbols.get(raw_symbol.strip().upper())
        if key is None:
            raise SchemaViolationError(
                f"response assesses {raw_symbol!r}, which was not in the snapshot; "
                f"expected any of {sorted(set(symbols.values()))}"
            )
        votes[key] = vote
    return votes


#: Mode → parser. Dispatch rather than a branch, so adding a decision mode adds a row.
_PARSERS: Final[
    Mapping[DecisionMode, Callable[[str, tuple[str, ...], Mapping[str, str]], dict[str, SeatVote]]]
] = {
    DecisionMode.PER_ASSET: lambda text, keys, _symbols: {keys[0]: parse_vote(text)},
    DecisionMode.BASKET: lambda text, _keys, symbols: parse_assessments(text, symbols),
}


class SeatRunner:
    """Turns a `SeatConfig` plus a snapshot into validated `SeatResponse`s — one per instrument."""

    def __init__(self, providers: Mapping[str, LLMProvider], clock: Clock) -> None:
        self._providers = dict(providers)
        self._clock = clock

    async def run(
        self,
        seat: SeatConfig,
        snapshot: ContextSnapshot,
        request: PanelRequest,
        *,
        round_index: int = 0,
        transcript: tuple[str, ...] = (),
        majority: str = "",
    ) -> tuple[SeatResponse, ...]:
        system = build_system_prompt(seat, request)
        user = build_user_prompt(snapshot, seat, request, transcript, majority)
        call_id = new_uuid()

        try:
            binding, result = await self._complete(seat, system, user)
        except ProviderError as exc:
            return self._abstain_all(
                seat,
                seat.bindings[0],
                request,
                round_index,
                f"provider unavailable: {exc}",
                call_id=call_id,
            )

        symbols = _symbol_lookup(snapshot, request)
        try:
            votes = _PARSERS[request.decision_mode](result.text, request.instrument_keys, symbols)
        except SchemaViolationError as first_error:
            repaired, result = await self._repair(
                seat, binding, system, user, request, symbols, first_error, result
            )
            if repaired is None:
                return self._abstain_all(
                    seat,
                    binding,
                    request,
                    round_index,
                    f"schema violation: {first_error}",
                    call_id=call_id,
                    result=result,
                )
            votes = repaired

        return tuple(
            self._response(
                seat,
                binding,
                key,
                round_index,
                call_id=call_id,
                result=result,
                vote=votes.get(key),
                abstain_reason=None if key in votes else NO_ASSESSMENT,
            )
            for key in request.instrument_keys
        )

    async def _complete(
        self, seat: SeatConfig, system: str, user: str
    ) -> tuple[ProviderBinding, CompletionResult]:
        """Try each binding in the seat's chain. The one that answered is returned with the result.

        A fallback preserves the seat's role but can collapse panel heterogeneity; the consensus
        rule flags that as `PANEL_HOMOGENEOUS` (DESIGN §6.5, R11).
        """
        last_error: ProviderError | None = None
        for binding in seat.bindings:
            provider = self._providers.get(binding.provider_id)
            if provider is None:
                logger.warning(
                    "seat binding names an unconfigured provider",
                    extra={"seat_id": seat.seat_id, "provider": binding.provider_id},
                )
                continue
            try:
                result = await provider.complete(
                    CompletionRequest(
                        model=binding.model,
                        system=system,
                        user=user,
                        temperature=seat.temperature,
                    )
                )
            except ProviderError as exc:
                logger.warning(
                    "seat provider failed",
                    extra={"seat_id": seat.seat_id, "binding": binding.fingerprint},
                )
                last_error = exc
                continue
            return binding, result
        raise last_error or ProviderError(f"no provider available for seat {seat.seat_id}")

    async def _repair(
        self,
        seat: SeatConfig,
        binding: ProviderBinding,
        system: str,
        user: str,
        request: PanelRequest,
        symbols: Mapping[str, str],
        error: SchemaViolationError,
        first: CompletionResult,
    ) -> tuple[dict[str, SeatVote] | None, CompletionResult]:
        """One repair attempt, on the binding that answered. A second failure abstains.

        Not a third try, and not a walk down the fallback chain: a model that has now produced
        two malformed answers to the same question is not going to produce a third that can be
        trusted, and every extra attempt is spend against the cycle's budget.

        Returns the merged accounting either way. The rejected first call was paid for whether or
        not the repair worked, and a $/decision figure that hides retries understates exactly the
        panels that are struggling most (DESIGN §6.5).
        """
        provider = self._providers.get(binding.provider_id)
        if provider is None:
            return None, first
        try:
            result = await provider.complete(
                CompletionRequest(
                    model=binding.model,
                    system=system,
                    user=f"{user}\n\n{REPAIR_INSTRUCTION.format(error=error)}",
                    temperature=seat.temperature,
                )
            )
        except ProviderError:
            return None, first

        merged = _merge_spend(first, result)
        try:
            votes = _PARSERS[request.decision_mode](result.text, request.instrument_keys, symbols)
        except SchemaViolationError:
            return None, merged
        return votes, merged

    def _response(
        self,
        seat: SeatConfig,
        binding: ProviderBinding,
        instrument_key: str,
        round_index: int,
        *,
        call_id: str,
        result: CompletionResult | None,
        vote: SeatVote | None,
        abstain_reason: str | None,
    ) -> SeatResponse:
        return SeatResponse(
            seat_id=seat.seat_id,
            role=seat.role,
            provider_id=binding.provider_id,
            model=binding.model,
            round_index=round_index,
            instrument_key=instrument_key,
            vote=vote,
            abstain_reason=abstain_reason,
            raw_text=result.text if result else "",
            latency_ms=result.latency_ms if result else 0,
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            responded_at=self._clock.now(),
            call_id=call_id,
            cost_usd=(result.cost_usd or ZERO) if result else ZERO,
        )

    def _abstain_all(
        self,
        seat: SeatConfig,
        binding: ProviderBinding,
        request: PanelRequest,
        round_index: int,
        reason: str,
        *,
        call_id: str,
        result: CompletionResult | None = None,
    ) -> tuple[SeatResponse, ...]:
        """The seat failed as a whole, so it abstains on every instrument it was asked about."""
        logger.warning("seat abstained", extra={"seat_id": seat.seat_id, "reason": reason})
        return tuple(
            self._response(
                seat,
                binding,
                key,
                round_index,
                call_id=call_id,
                result=result,
                vote=None,
                abstain_reason=reason,
            )
            for key in request.instrument_keys
        )


def _merge_spend(first: CompletionResult, second: CompletionResult) -> CompletionResult:
    """The second call's text, carrying what both calls cost."""
    return second.model_copy(
        update={
            "prompt_tokens": first.prompt_tokens + second.prompt_tokens,
            "completion_tokens": first.completion_tokens + second.completion_tokens,
            "latency_ms": first.latency_ms + second.latency_ms,
            "cost_usd": (first.cost_usd or ZERO) + (second.cost_usd or ZERO),
        }
    )


def _symbol_lookup(snapshot: ContextSnapshot, request: PanelRequest) -> dict[str, str]:
    """Every spelling a model might use for an instrument, mapped to its key.

    Both the venue symbol the prompt showed and the full `venue:symbol` key are accepted, because
    a model that echoes back the qualified key is being helpful rather than wrong.
    """
    lookup: dict[str, str] = {}
    for key in request.instrument_keys:
        instrument = snapshot.context_for(key).instrument
        lookup[instrument.symbol.upper()] = key
        lookup[key.upper()] = key
    return lookup
