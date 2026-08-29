"""One cycle of the reference pass, folded from the log (spec §5.1).

Slice A's `Corpus` holds the frozen contexts; scoring needs three more things per cycle: what the
panel decided, what every seat voted in every round, and how the cycle ended. All four come from
the same workspace database, grouped by `cycle_id`.

Read from the **log**, not from a projection, for the reason ADR 0016 gives: the facts a score
turns on — an abstention, a losing argument, the round a vote was cast in — have no projector at
all. `EventStore.read_types` narrows to exactly the four types, which is what keeps loading a
six-month corpus affordable.

Failure semantics: a compacted `SEAT_RESPONDED` has lost its `raw_text` and nothing else, so it
still scores; a compacted `SNAPSHOT_FROZEN` has lost the whole context and refuses, in
`corpus.entry_from_payload`. Nothing here writes.
"""

from __future__ import annotations

from pathlib import Path

from decision_lab.corpus import CORPUS_DB, CorpusMeta, corpus_dir, entry_from_payload
from decision_lab.params import CORPUS_META
from tradebot.core.decision import Decision, SeatResponse, total_cost
from tradebot.core.errors import ConfigError
from tradebot.core.events import Event, EventType
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.core.snapshot import ContextSnapshot

# The flag `reach_consensus` sets on a WAIT from a panel that could not answer. Imported rather
# than restated: a second copy of the string would silently stop matching if the bot renamed it,
# and the degradation rate would quietly read zero.
from tradebot.decision.consensus import PANEL_DEGRADED
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore

_TYPES = (
    EventType.SNAPSHOT_FROZEN,
    EventType.DECISION_MADE,
    EventType.SEAT_RESPONDED,
    EventType.CYCLE_COMPLETED,
)


class CycleRecord(DomainModel):
    """Everything one cycle of the reference pass produced."""

    cycle_id: str
    basket_id: str
    as_of: UtcDatetime
    snapshot: ContextSnapshot
    decisions: tuple[Decision, ...] = ()
    responses: tuple[SeatResponse, ...] = ()
    outcome: str = ""
    cost_usd: Money = ZERO

    def decision_for(self, instrument_key: str) -> Decision | None:
        return next((d for d in self.decisions if d.instrument_key == instrument_key), None)

    def responses_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        return tuple(r for r in self.responses if r.instrument_key == instrument_key)

    def round_zero_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        """The seat's own independent opinion, before any peer argued with it (§9.7)."""
        return tuple(r for r in self.responses_for(instrument_key) if r.round_index == 0)

    def final_round_for(self, instrument_key: str) -> tuple[SeatResponse, ...]:
        """The votes the consensus rule actually read. Mirrors `Deliberation.final_round_for`."""
        about = self.responses_for(instrument_key)
        if not about:
            return ()
        last = max(r.round_index for r in about)
        return tuple(r for r in about if r.round_index == last)

    @property
    def rounds(self) -> int:
        return max((r.round_index for r in self.responses), default=0) + 1

    @property
    def degraded(self) -> bool:
        """Did the panel fail to answer? `WAIT (PANEL_DEGRADED)` on any instrument (§9.5)."""
        return any(PANEL_DEGRADED in d.flags for d in self.decisions)


def records_from_store(store: EventStore) -> tuple[CycleRecord, ...]:
    """Fold the log's four relevant types into one record per cycle, in cycle order."""
    grouped: dict[str, list[Event]] = {}
    for event in store.read_types(*_TYPES):
        grouped.setdefault(event.cycle_id or "", []).append(event)

    records = []
    for cycle_id, events in grouped.items():
        frozen = next((e for e in events if e.type is EventType.SNAPSHOT_FROZEN), None)
        if frozen is None:
            # A cycle that failed before freezing its snapshot. There is nothing to score it on,
            # and counting it as a decision would flatter or damn a panel that never ran.
            continue
        entry = entry_from_payload(
            seq=frozen.seq or 0,
            cycle_id=cycle_id,
            basket_id=frozen.basket_id or "",
            payload=frozen.payload,
        )
        completed = next((e for e in events if e.type is EventType.CYCLE_COMPLETED), None)
        responses = tuple(
            SeatResponse.model_validate(e.payload["response"])
            for e in events
            if e.type is EventType.SEAT_RESPONDED
        )
        records.append(
            CycleRecord(
                cycle_id=cycle_id,
                basket_id=entry.basket_id,
                as_of=entry.as_of,
                snapshot=entry.snapshot,
                decisions=tuple(
                    Decision.model_validate(e.payload["decision"])
                    for e in events
                    if e.type is EventType.DECISION_MADE
                ),
                responses=responses,
                outcome=str(completed.payload.get("outcome", "")) if completed else "",
                # From the responses rather than from the event's own field, so `basket` mode —
                # one provider call answering for N instruments — is not counted N times. That
                # de-duplication is `total_cost`'s job and only its job (DESIGN §6.5).
                cost_usd=total_cost(responses),
            )
        )
    return tuple(sorted(records, key=lambda record: record.as_of))


def load(
    corpus_id: str, *, workspace: Path | None = None
) -> tuple[CorpusMeta, tuple[CycleRecord, ...]]:
    """Re-open a built corpus and read its cycles. `open_database` never migrates."""
    directory = corpus_dir(corpus_id, workspace=workspace)
    meta_path = directory / CORPUS_META
    if not meta_path.is_file():
        raise ConfigError(
            f"no corpus {corpus_id!r} in {directory.parent}. Build one with "
            "`python -m decision_lab corpus build --data … --every …`"
        )
    meta = CorpusMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    engine = open_database(directory / CORPUS_DB)
    # `EventStore` takes a writer because it *can* write; this call only reads, and slice A's
    # `corpus.load` constructs it the same way. The writer still owns a thread, so it is closed.
    writer = SingleWriter(engine)
    try:
        return meta, records_from_store(EventStore(engine, writer))
    finally:
        writer.close()
        engine.dispose()
