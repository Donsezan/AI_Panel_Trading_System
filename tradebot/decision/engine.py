"""The decision engine: deliberate, then apply the deterministic consensus rule.

The split is the design's core claim. The panel is a black box that emits a *proposal*;
everything from here on — consensus, risk, sizing, execution — is deterministic, testable code
(DESIGN §3.1).

The engine owns the two things that are properly per-*cycle* rather than per-instrument: the
choice of decision mode, and the cost budget. `per_asset` runs one panel per instrument against
a single shared ceiling; `basket` runs one panel over all of them and fans the result out. The
caller asks for a basket and gets decisions back, without knowing which mode ran (DESIGN §4).

Instruments are deliberated **sequentially** in `per_asset` mode. Running them concurrently would
be faster and would make the shared budget a race — the ceiling has to mean something, and a
research testbed that reports a different cost for the same inputs is not reproducible.

Failure semantics: the engine never propagates a panel failure. Provider outages and malformed
output become abstentions, and abstentions become `WAIT (PANEL_DEGRADED)`. A cycle that cannot
reach a view produces no order, which is the correct outcome. The one thing that *does* raise is
a panel naming a protocol that does not exist — a configuration defect, caught at wiring time.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from tradebot.core.budget import CycleBudget
from tradebot.core.config import Basket, PanelConfig
from tradebot.core.decision import Deliberation, PanelOutcome
from tradebot.core.enums import DecisionMode
from tradebot.core.errors import ConfigError
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.consensus import reach_consensus
from tradebot.decision.protocols import PROTOCOLS
from tradebot.decision.seat import SeatRunner
from tradebot.interfaces.debate import DebateProtocol, PanelRequest

_Mode = Callable[
    [ContextSnapshot, PanelConfig, DebateProtocol, tuple[str, ...], CycleBudget],
    Coroutine[Any, Any, tuple[Deliberation, ...]],
]


class DecisionEngine:
    """Binds the configured debate protocol to the consensus rule."""

    def __init__(self, runner: SeatRunner) -> None:
        self._protocols: dict[str, DebateProtocol] = {
            protocol_id: implementation(runner) for protocol_id, implementation in PROTOCOLS.items()
        }
        self._modes: dict[DecisionMode, _Mode] = {
            DecisionMode.PER_ASSET: self._per_asset,
            DecisionMode.BASKET: self._basket,
        }

    def protocol_for(self, panel: PanelConfig) -> DebateProtocol:
        """The protocol a panel names. Raises rather than falling back to a default.

        A silent fallback would run a different experiment from the configured one and record the
        configured name against it, which is worse than refusing to start.
        """
        protocol = self._protocols.get(panel.protocol)
        if protocol is None:
            raise ConfigError(
                f"panel {panel.panel_id!r} names unknown protocol {panel.protocol!r}; "
                f"available: {', '.join(sorted(self._protocols))}"
            )
        return protocol

    def validate(self, basket: Basket) -> None:
        """Fail at wiring time on a basket this engine cannot run.

        Called by the composition root, so a panel naming a protocol that does not exist refuses
        to start rather than failing on the first cycle. Both panels are checked: a challenger
        that could never be deliberated would leave a comparison report empty and blame the log.
        """
        for panel in basket.panels:
            self.protocol_for(panel)
        if basket.decision_mode not in self._modes:
            raise ConfigError(f"unsupported decision mode {basket.decision_mode.value!r}")

    async def deliberate(self, snapshot: ContextSnapshot, basket: Basket) -> PanelOutcome:
        """Run the panel over the whole basket and fold each instrument's votes into a decision."""
        panel = basket.panel
        protocol = self.protocol_for(panel)
        budget = CycleBudget(panel.max_cost_usd_per_cycle)
        keys = tuple(instrument.key for instrument in basket.instruments)

        deliberations = await self._modes[basket.decision_mode](
            snapshot, panel, protocol, keys, budget
        )
        decisions = tuple(
            reach_consensus(deliberation.final_round_for(key), panel, key)
            for deliberation in deliberations
            for key in deliberation.instrument_keys
        )
        return PanelOutcome(decisions=decisions, deliberations=deliberations)

    @staticmethod
    async def _per_asset(
        snapshot: ContextSnapshot,
        panel: PanelConfig,
        protocol: DebateProtocol,
        keys: tuple[str, ...],
        budget: CycleBudget,
    ) -> tuple[Deliberation, ...]:
        deliberations = []
        for key in keys:
            deliberations.append(
                await protocol.deliberate(snapshot, panel, PanelRequest.for_instrument(key), budget)
            )
        return tuple(deliberations)

    @staticmethod
    async def _basket(
        snapshot: ContextSnapshot,
        panel: PanelConfig,
        protocol: DebateProtocol,
        keys: tuple[str, ...],
        budget: CycleBudget,
    ) -> tuple[Deliberation, ...]:
        request = PanelRequest(instrument_keys=keys, decision_mode=DecisionMode.BASKET)
        return (await protocol.deliberate(snapshot, panel, request, budget),)
