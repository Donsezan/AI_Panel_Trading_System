"""How a panel deliberates. Pluggable, so protocols can be compared as a research variable.

Failure semantics: a protocol never raises for a seat failure — a failed seat becomes an
abstention in the returned transcript, and the deterministic consensus rule decides what a
degraded panel means. Protocols do not decide; they only produce the transcript.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import model_validator

from tradebot.core.budget import CycleBudget
from tradebot.core.config import PanelConfig
from tradebot.core.decision import Deliberation
from tradebot.core.enums import DecisionMode
from tradebot.core.schema import DomainModel
from tradebot.core.snapshot import ContextSnapshot


class PanelRequest(DomainModel):
    """What one panel run has to answer, and in which decision mode.

    `per_asset` runs the panel once per instrument; `basket` runs it once over all of them and
    gets an assessment per instrument back. Carrying the mode explicitly is what keeps every
    layer below the engine — protocols, seats, prompts — from having to know which it is
    (DESIGN §4).
    """

    instrument_keys: tuple[str, ...]
    decision_mode: DecisionMode = DecisionMode.PER_ASSET

    @model_validator(mode="after")
    def _check_shape(self) -> PanelRequest:
        if not self.instrument_keys:
            raise ValueError("a panel run covers at least one instrument")
        if len(set(self.instrument_keys)) != len(self.instrument_keys):
            raise ValueError("an instrument may appear in a panel request only once")
        if self.decision_mode is DecisionMode.PER_ASSET and len(self.instrument_keys) != 1:
            raise ValueError("per_asset mode runs the panel over exactly one instrument")
        return self

    @classmethod
    def for_instrument(cls, instrument_key: str) -> PanelRequest:
        return cls(instrument_keys=(instrument_key,), decision_mode=DecisionMode.PER_ASSET)

    @property
    def is_basket(self) -> bool:
        return self.decision_mode is DecisionMode.BASKET


@runtime_checkable
class DebateProtocol(Protocol):
    """Runs the rounds and returns every seat response, including abstentions.

    Implementations: `single_round` (the isolated baseline) and `blind_then_debate` (blind round
    0, anonymized transcripts, a devil's-advocate seat).

    The budget is passed in rather than owned because it is scoped to the *cycle*, not to one
    panel run: in `per_asset` mode a basket of five instruments runs five panels against one
    ceiling (DESIGN §6.5).
    """

    protocol_id: str

    async def deliberate(
        self,
        snapshot: ContextSnapshot,
        panel: PanelConfig,
        request: PanelRequest,
        budget: CycleBudget,
    ) -> Deliberation: ...
