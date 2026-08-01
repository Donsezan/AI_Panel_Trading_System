"""The shadow A/B harness: a challenger panel judged on the champion's own snapshot.

DESIGN §12, promoted into PLAN Phase 7. Comparing panel configurations by their forward PnL over
a few weeks is statistically hopeless — the market moves more than the panels differ. Running both
on the **same frozen snapshot** every cycle removes the market from the comparison entirely: what
is left is the reasoning, which is the thing under test.

Three properties this module exists to guarantee, in the order they matter:

* **The challenger never trades.** It produces `Decision`s and they go into the log. Nothing here
  can reach Tier-1, and there is no code path from a shadow decision to an `OrderIntent`.
* **A shadow failure is not a cycle failure.** The champion has already deliberated, acted and
  been recorded by the time this runs. A challenger that times out, exhausts its fallback chain or
  returns junk is caught and written down as a failed evaluation; the cycle's outcome is whatever
  the champion made it.
* **Its cost is its own.** The challenger's `PanelConfig` carries its own per-cycle ceiling and the
  engine builds a fresh `CycleBudget` from it, so a challenger cannot spend the champion's budget
  and cannot inflate the `$/decision` figure of the panel that actually traded.

Failure semantics: `evaluate` never raises. Every exception — classified or not — becomes a
`SHADOW_EVALUATED` event carrying the error, because a challenger that silently stopped being
evaluated would leave a comparison report quietly built on fewer cycles than it claims.
"""

from __future__ import annotations

from tradebot.core.config import Basket
from tradebot.core.events import EventFactory
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.engine import DecisionEngine
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)


class ShadowEvaluator:
    """Runs a basket's challenger panel over a snapshot the champion has already been judged on."""

    def __init__(self, engine: DecisionEngine, store: EventStore) -> None:
        self._engine = engine
        self._store = store

    async def evaluate(
        self, snapshot: ContextSnapshot, basket: Basket, events: EventFactory
    ) -> None:
        """Deliberate the challenger and record it. A basket without one does nothing at all."""
        challenger = basket.challenger
        if challenger is None:
            return
        panel_id = challenger.panel.panel_id
        try:
            outcome = await self._engine.deliberate(snapshot, challenger)
        # Deliberately broad: the champion's cycle is already complete, and no failure of a
        # research comparison may change what a completed cycle recorded.
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "shadow panel failed; the cycle is unaffected",
                extra={"panel_id": panel_id, "error": detail},
            )
            await self._store.append(events.shadow_evaluated(panel_id, (), ZERO, error=detail))
            return

        await self._store.append(
            events.shadow_evaluated(panel_id, outcome.decisions, outcome.cost_usd)
        )
        logger.info(
            "shadow panel evaluated",
            extra={
                "panel_id": panel_id,
                "actions": [decision.action.value for decision in outcome.decisions],
                "cost_usd": str(outcome.cost_usd),
            },
        )
