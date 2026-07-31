"""Debate protocols. How the panel talks to itself, as a swappable research variable.

Two ship:

* **`single_round`** — every seat answers once, independently. The isolated-self-correction
  baseline that debate research repeatedly finds a homogeneous unguided debate fails to beat, so
  it is the control condition rather than a toy.
* **`blind_then_debate`** — DESIGN §6.5's default. Round 0 is blind: seats commit to a position
  before seeing anyone else's, which is what stops the first confident answer from anchoring the
  panel. Later rounds show an *anonymized* transcript, and the devil's-advocate seat is told what
  the panel is converging on and asked to argue against it. Everything that makes debate collapse
  into agreement — prestige cues, majority pressure, seeing the strongest voice first — is
  designed out here rather than hoped away (DESIGN [L5]).

**The blind round always runs; the budget truncates the debate.** A cycle that cannot afford to
argue still gets every seat's independent position, and fewer rounds can only make a qualified
majority *less* likely — so truncation biases toward `WAIT`, which is the safe direction
(DESIGN §6.5, R6).

Failure semantics: a protocol never raises for a seat failure. A failed seat becomes an
abstention in the transcript and the deterministic consensus rule decides what a degraded panel
means. Protocols do not decide; they only produce the transcript.
"""

from __future__ import annotations

import asyncio
import string
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from tradebot.core.budget import CycleBudget
from tradebot.core.config import PanelConfig, SeatConfig
from tradebot.core.decision import Deliberation, SeatResponse, total_cost
from tradebot.core.enums import Action
from tradebot.core.logging import get_logger
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.seat import SeatRunner
from tradebot.interfaces.debate import DebateProtocol, PanelRequest

logger = get_logger(__name__)


def alias_for(index: int) -> str:
    """A stable, contentless label for a seat.

    Seats are identified to each other by nothing but a letter — not the model, not the provider,
    not the seat id, and not even the role. Every one of those is a prestige or authority cue, and
    a debate in which one voice carries authority is a debate that converges on that voice rather
    than on the evidence (DESIGN [L5]).
    """
    letters = string.ascii_uppercase
    return f"Analyst {letters[index]}" if index < len(letters) else f"Analyst #{index + 1}"


def aliases_for(seats: Sequence[SeatConfig]) -> dict[str, str]:
    """Aliases assigned by configured seat order, so a transcript is reproducible on replay."""
    return {seat.seat_id: alias_for(index) for index, seat in enumerate(seats)}


def _describe(response: SeatResponse, alias: str, symbols: Mapping[str, str], qualify: bool) -> str:
    subject = f"{alias} on {symbols.get(response.instrument_key, response.instrument_key)}"
    who = subject if qualify else alias
    if response.vote is None:
        return f"{who}: did not respond this round."
    vote = response.vote
    risks = f" Key risks: {'; '.join(vote.key_risks)}." if vote.key_risks else ""
    return (
        f"{who} argued {vote.action.value} (conviction {vote.conviction}/5, "
        f"size {vote.size_hint.value}): {vote.thesis}{risks}"
    )


def anonymize(
    responses: Sequence[SeatResponse],
    aliases: Mapping[str, str],
    symbols: Mapping[str, str],
    *,
    qualify: bool,
) -> tuple[str, ...]:
    """Render a round as the next round's reading material, with every identity removed."""
    return tuple(
        _describe(response, aliases.get(response.seat_id, "Analyst ?"), symbols, qualify)
        for response in responses
    )


def majority_summary(responses: Sequence[SeatResponse], symbols: Mapping[str, str]) -> str:
    """What the panel is converging on, per instrument. Shown only to the devil's advocate."""
    by_instrument: dict[str, Counter[Action]] = {}
    for response in responses:
        if response.vote is not None:
            by_instrument.setdefault(response.instrument_key, Counter())[response.vote.action] += 1
    parts = [
        f"{symbols.get(key, key)}: {tally.most_common(1)[0][0].value}"
        for key, tally in by_instrument.items()
    ]
    return ", ".join(parts)


def has_converged(responses: Sequence[SeatResponse], seat_count: int) -> bool:
    """True when every seat voted and they all agree, on every instrument.

    Further rounds cannot change a decision the panel already agrees on, and spending a model
    call to confirm it is spending the cycle's budget on nothing. Deliberately strict: an
    abstention keeps the debate open, because a later round may recover the seat.
    """
    by_instrument: dict[str, set[Action]] = {}
    for response in responses:
        if response.vote is None:
            return False
        by_instrument.setdefault(response.instrument_key, set()).add(response.vote.action)
    return (
        bool(by_instrument)
        and all(len(actions) == 1 for actions in by_instrument.values())
        and len(responses) == seat_count * len(by_instrument)
    )


class _RoundRunner:
    """Shared round mechanics: fan every seat out concurrently and flatten what comes back."""

    def __init__(self, runner: SeatRunner) -> None:
        self._runner = runner

    async def run_round(
        self,
        panel: PanelConfig,
        snapshot: ContextSnapshot,
        request: PanelRequest,
        *,
        round_index: int,
        transcript: tuple[str, ...] = (),
        majority: str = "",
    ) -> tuple[SeatResponse, ...]:
        batches = await asyncio.gather(
            *(
                self._runner.run(
                    seat,
                    snapshot,
                    request,
                    round_index=round_index,
                    transcript=transcript,
                    majority=majority,
                )
                for seat in panel.seats
            )
        )
        return tuple(response for batch in batches for response in batch)


class SingleRoundProtocol(_RoundRunner):
    """Every seat answers once, independently and concurrently.

    Seats never see each other here, which makes this the cheapest protocol *and* the one most
    resistant to sycophancy — it is the isolated-self-correction baseline that debate research
    says a homogeneous unguided debate often fails to beat.
    """

    protocol_id = "single_round"

    async def deliberate(
        self,
        snapshot: ContextSnapshot,
        panel: PanelConfig,
        request: PanelRequest,
        budget: CycleBudget,
    ) -> Deliberation:
        responses = await self.run_round(panel, snapshot, request, round_index=0)
        budget.spend(total_cost(responses))
        return Deliberation(
            instrument_keys=request.instrument_keys,
            protocol_id=self.protocol_id,
            decision_mode=request.decision_mode,
            rounds=1,
            responses=responses,
        )


class BlindThenDebateProtocol(_RoundRunner):
    """Blind round 0, then anonymized debate rounds up to `panel.max_rounds`."""

    protocol_id = "blind_then_debate"

    async def deliberate(
        self,
        snapshot: ContextSnapshot,
        panel: PanelConfig,
        request: PanelRequest,
        budget: CycleBudget,
    ) -> Deliberation:
        aliases = aliases_for(panel.seats)
        symbols = _symbols(snapshot, request)

        # Round 0 is blind: no transcript, no majority, nothing to agree with.
        latest = await self.run_round(panel, snapshot, request, round_index=0)
        history = list(latest)
        round_cost = total_cost(latest)
        budget.spend(round_cost)
        truncated = False

        for round_index in range(1, panel.max_rounds):
            if has_converged(latest, panel.seat_count):
                logger.debug("panel converged; stopping early", extra={"round": round_index})
                break
            if not budget.can_afford(round_cost):
                logger.info(
                    "cost budget truncated the debate",
                    extra={"round": round_index, "spent": str(budget.spent)},
                )
                truncated = True
                break
            latest = await self.run_round(
                panel,
                snapshot,
                request,
                round_index=round_index,
                transcript=anonymize(latest, aliases, symbols, qualify=request.is_basket),
                majority=majority_summary(latest, symbols),
            )
            history.extend(latest)
            round_cost = total_cost(latest)
            budget.spend(round_cost)

        return Deliberation(
            instrument_keys=request.instrument_keys,
            protocol_id=self.protocol_id,
            decision_mode=request.decision_mode,
            rounds=max(response.round_index for response in history) + 1 if history else 0,
            responses=tuple(history),
            budget_truncated=truncated,
        )


def _symbols(snapshot: ContextSnapshot, request: PanelRequest) -> dict[str, str]:
    """Instrument key → the venue symbol a transcript should name it by."""
    return {key: snapshot.context_for(key).instrument.symbol for key in request.instrument_keys}


#: Protocol id → implementation. A panel names its protocol as *data*, so this is the table that
#: turns that name into behaviour; an unknown name is a configuration defect, not a fallback.
PROTOCOLS: Final[Mapping[str, Callable[[SeatRunner], DebateProtocol]]] = {
    SingleRoundProtocol.protocol_id: SingleRoundProtocol,
    BlindThenDebateProtocol.protocol_id: BlindThenDebateProtocol,
}
