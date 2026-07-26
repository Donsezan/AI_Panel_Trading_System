"""Debate protocols. v1 ships the single round the walking skeleton needs.

Phase 4 adds `blind_then_debate`: a blind round 0 where seats produce independent positions
before seeing each other, then anonymized-transcript rounds with a devil's-advocate seat. Both
implement the same interface, so a protocol becomes a comparable research variable rather than
a rewrite (DESIGN §6.5).

Failure semantics: a protocol never raises for a seat failure. A failed seat becomes an
abstention in the transcript and the consensus rule decides what a degraded panel means.
"""

from __future__ import annotations

import asyncio

from tradebot.core.config import PanelConfig
from tradebot.core.decision import Deliberation
from tradebot.core.money import ZERO
from tradebot.core.snapshot import ContextSnapshot
from tradebot.decision.seat import SeatOutcome, SeatRunner


class SingleRoundProtocol:
    """Every seat answers once, independently and concurrently.

    Seats never see each other here, which makes this the cheapest protocol *and* the one most
    resistant to sycophancy — it is the isolated-self-correction baseline that debate research
    says a homogeneous unguided debate often fails to beat.
    """

    protocol_id = "single_round"

    def __init__(self, runner: SeatRunner) -> None:
        self._runner = runner

    async def deliberate(
        self, snapshot: ContextSnapshot, panel: PanelConfig, instrument_key: str
    ) -> Deliberation:
        outcomes: tuple[SeatOutcome, ...] = tuple(
            await asyncio.gather(
                *(
                    self._runner.run(seat, snapshot, instrument_key, round_index=0)
                    for seat in panel.seats
                )
            )
        )
        return Deliberation(
            instrument_key=instrument_key,
            protocol_id=self.protocol_id,
            rounds=1,
            responses=tuple(outcome.response for outcome in outcomes),
            cost_usd=sum((o.cost_usd for o in outcomes if o.cost_usd is not None), start=ZERO),
        )
