"""How a panel deliberates. Pluggable, so protocols can be compared as a research variable.

Failure semantics: a protocol never raises for a seat failure — a failed seat becomes an
abstention in the returned transcript, and the deterministic consensus rule decides what a
degraded panel means. Protocols do not decide; they only produce the transcript.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradebot.core.config import PanelConfig
from tradebot.core.decision import Deliberation
from tradebot.core.snapshot import ContextSnapshot


@runtime_checkable
class DebateProtocol(Protocol):
    """Runs the rounds and returns every seat response, including abstentions.

    Implementations: `single_round` (v1), `blind_then_debate` (Phase 4 — blind round 0,
    anonymized transcripts, devil's-advocate seat).
    """

    protocol_id: str

    async def deliberate(
        self, snapshot: ContextSnapshot, panel: PanelConfig, instrument_key: str
    ) -> Deliberation: ...
