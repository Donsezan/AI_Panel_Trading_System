"""The decision engine: deliberate, then apply the deterministic consensus rule.

The split is the design's core claim. The panel is a black box that emits a *proposal*;
everything from here on — consensus, risk, sizing, execution — is deterministic, testable code
(DESIGN §3.1).

Failure semantics: the engine never propagates a panel failure. Provider outages and malformed
output become abstentions, and abstentions become `WAIT (PANEL_DEGRADED)`. A cycle that cannot
reach a view produces no order, which is the correct outcome.
"""

from __future__ import annotations

from tradebot.core.config import PanelConfig
from tradebot.core.decision import Decision, Deliberation
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.consensus import reach_consensus
from tradebot.interfaces.debate import DebateProtocol


class DecisionEngine:
    """Binds a debate protocol to the consensus rule."""

    def __init__(self, protocol: DebateProtocol) -> None:
        self._protocol = protocol

    async def decide(
        self, snapshot: ContextSnapshot, panel: PanelConfig, instrument_key: str
    ) -> tuple[Decision, Deliberation]:
        deliberation = await self._protocol.deliberate(snapshot, panel, instrument_key)
        decision = reach_consensus(deliberation.final_round, panel, instrument_key)
        return decision, deliberation
