"""A Continue's panel: the recorded answers up to the last month end, the real engine after (§5.7).

A Continue re-executes its period from the start in a fresh database, so every piece of in-memory
state is rebuilt by the code that built it the first time. What must not be redone is the asking:
those answers were paid for once. A replayed cycle therefore costs nothing new, and its recorded
responses and costs reach the new log unchanged, which is why nothing is billed twice.

Failure semantics: every replayed cycle compares its snapshot with the recorded one, generated id
aside, and any difference raises `ConfigError` naming the first cycle that differs. Answers applied
to a different market would be evidence nobody could name. `BasketWorker` absorbs what a cycle
raises (a supervised run must survive a defect), so the refusal is also kept here, and the caller
reads it back with `raise_if_diverged` after each cycle. A recorded cycle that failed before it
decided has no answer to recite, and a replay asking the panel there refuses naming that cycle as
failed rather than as a market that differed. Nothing here writes. A model unreachable
after the boundary behaves as it does in any run, because that part is the real engine.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from decision_lab.records import CycleRecord, records_from_store
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.decision import Deliberation, PanelOutcome
from tradebot.core.errors import ConfigError
from tradebot.core.snapshot import ContextSnapshot, InstrumentContext
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.providers.registry import ProviderPool, build_providers
from tradebot.decision.seat import SeatRunner
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore

#: Fresh in every process, so it can only ever differ and says nothing when it does.
_GENERATED = "snapshot_id"


#: A cycle that froze its snapshot and then ended without a decision: `CycleOutcome.FAILED`, or no
#: completion at all because `BasketWorker` absorbed what it raised.
_FAILED_OUTCOMES = frozenset({"failed", ""})


def _records(database: Path) -> tuple[CycleRecord, ...]:
    engine = open_database(database)
    writer = SingleWriter(engine)
    try:
        return records_from_store(EventStore(engine, writer))
    finally:
        writer.close()
        engine.dispose()


def recorded_before(database: Path, boundary: datetime) -> tuple[CycleRecord, ...]:
    """The cycles before `boundary` that asked the panel, in order.

    A decision is what marks one: a quarantined or blocked cycle never reached the panel, and the
    re-executed run reaches the same conclusion for itself. A compacted snapshot refuses, in
    `records_from_store`, since there would be nothing to compare against.
    """
    return tuple(one for one in _records(database) if one.decisions and one.as_of < boundary)


def failed_before(database: Path, boundary: datetime) -> tuple[datetime, ...]:
    """When a cycle before `boundary` froze its snapshot and then failed without deciding.

    The recorded run holds no answer for it, so a replay that asks the panel there has nothing to
    recite. The refusal is the same either way; this is what lets it name the real cause rather
    than a market difference that never happened.
    """
    return tuple(
        one.as_of
        for one in _records(database)
        if not one.decisions and one.outcome in _FAILED_OUTCOMES and one.as_of < boundary
    )


def _instrument_difference(recorded: ContextSnapshot, now: ContextSnapshot) -> str:
    for before, after in zip(recorded.instruments, now.instruments, strict=False):
        for name in InstrumentContext.model_fields:
            if getattr(before, name) != getattr(after, name):
                return f"{before.instrument.key} {name}"
    return "the instruments held"


def first_difference(recorded: ContextSnapshot, now: ContextSnapshot) -> str | None:
    """What differs between two snapshots, generated id aside; `None` when nothing does."""
    for name in ContextSnapshot.model_fields:
        if name != _GENERATED and getattr(recorded, name) != getattr(now, name):
            return _instrument_difference(recorded, now) if name == "instruments" else name
    return None


def _outcome_of(record: CycleRecord, basket: Basket) -> PanelOutcome:
    """The log keeps responses flat, and the runner reads them flat, so one deliberation holds them.

    Each `call_id` comes back with its response, so `total_cost` gives the recorded figure.
    """
    return PanelOutcome(
        decisions=record.decisions,
        deliberations=(
            Deliberation(
                instrument_keys=tuple(one.instrument_key for one in record.decisions),
                protocol_id=basket.panel.protocol,
                decision_mode=basket.decision_mode,
                rounds=record.rounds,
                responses=record.responses,
            ),
        ),
    )


class Replayer:
    """Stands in for `DecisionEngine.deliberate` through `build_sim(deliberate=...)`."""

    def __init__(
        self,
        recorded: tuple[CycleRecord, ...],
        *,
        boundary: datetime,
        clock: Clock,
        failed: tuple[datetime, ...] = (),
    ) -> None:
        self._recorded = recorded
        self._failed = frozenset(failed)
        self._boundary = boundary
        self._clock = clock
        self._next = 0
        self._divergence: ConfigError | None = None
        self._engine: DecisionEngine | None = None
        self._pool: ProviderPool | None = None

    async def __call__(self, snapshot: ContextSnapshot, basket: Basket) -> PanelOutcome:
        if snapshot.as_of >= self._boundary:
            return await self._live(basket).deliberate(snapshot, basket)
        self.raise_if_diverged()
        if snapshot.as_of in self._failed:
            self._divergence = ConfigError(
                f"the recorded month holds a FAILED cycle at {snapshot.as_of.isoformat()}, so it "
                "cannot be replayed: the recorded run paid for no decision there, and there is no "
                "answer to recite into it"
            )
            raise self._divergence
        if self._next >= len(self._recorded):
            self._refuse(snapshot.as_of, "the recorded run asked the panel nothing more")
        expected = self._recorded[self._next]
        if expected.as_of != snapshot.as_of:
            self._refuse(
                min(expected.as_of, snapshot.as_of),
                f"the recorded run asked the panel at {expected.as_of.isoformat()}, this one at "
                f"{snapshot.as_of.isoformat()}",
            )
        difference = first_difference(expected.snapshot, snapshot)
        if difference is not None:
            self._refuse(snapshot.as_of, f"{difference} differs")
        self._next += 1
        return _outcome_of(expected, basket)

    def raise_if_diverged(self) -> None:
        if self._divergence is not None:
            raise self._divergence

    def require_consumed(self) -> None:
        """At the boundary: a recorded answer nobody asked for is a run that went elsewhere."""
        self.raise_if_diverged()
        if self._next < len(self._recorded):
            pending = self._recorded[self._next]
            self._refuse(
                pending.as_of, "the recorded run asked the panel here, and this one did not"
            )

    def _refuse(self, at: datetime, why: str) -> None:
        self._divergence = ConfigError(
            f"the replay differs from the recorded run at cycle {at.isoformat()}: {why}. Answers "
            "applied to a different market would be evidence nobody could name"
        )
        raise self._divergence

    def _live(self, basket: Basket) -> DecisionEngine:
        """Built at the first live cycle, so a run that only replays opens no connection."""
        if self._engine is None:
            self._pool = build_providers(basket.panel.providers, self._clock)
            self._engine = DecisionEngine(SeatRunner(self._pool.providers, self._clock))
        return self._engine

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
