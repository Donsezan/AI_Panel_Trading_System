"""The corpus: one reference pass, and the frozen contexts it produced (spec §5).

An ordered collection of `ContextSnapshot`s — everything the panel is given for one instrument
set at one instant. **Read out of the event log**, because every cycle already appends
`SNAPSHOT_FROZEN` carrying the whole snapshot body: no new persistence format, no second
rendering path, and the corpus is byte-identical to what the panel deliberated on.

Why a reference pass rather than a flat book of contexts (§5.2): positions. A corpus built
against an empty ledger makes SELL and HOLD unreachable, so the panel only ever chooses between
BUY and WAIT and half the action space goes unmeasured. Which configuration supplied those
positions is a property of the experiment, so it is recorded in the meta and printed on every
report.

Separating the corpus build from the sweep is the design's load-bearing decision (§3), and it is
ADR 0018's principle generalised from one challenger to N: every candidate is judged on the same
frozen evidence, so a difference in score is a difference in reasoning rather than a difference in
luck. Two candidates run through their own full loops would hold different positions from cycle
two onward and be compared across two different markets.

`BacktestHarness` is used **unchanged**, in a workspace database. Nothing here writes to a bot
database, constructs a venue broker, or has a code path from a `Decision` to a live order.

Failure semantics: an unverified dataset refuses (§4.4). A `SNAPSHOT_FROZEN` whose body has been
compacted away refuses by name rather than yielding an empty context — a corpus of blanks scores
perfectly and means nothing. A build whose identity already exists is **reused**, never appended
to; one whose window differs under that same identity refuses, because §5.4 deliberately leaves
the window out of `corpus_id` and two windows must not collide on one. A build interrupted before
it wrote its meta also refuses, and keeps the database it left: that log is the record of why the
pass failed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from decision_lab.dataset import require_verified
from decision_lab.params import CORPUS_META, workspace_root
from tradebot.app import build_sim, dataset_basket, dataset_catalogue, select_panel
from tradebot.core.clock import Clock, ManualClock, SystemClock
from tradebot.core.config import Basket
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel, Money, UtcDatetime, canonical_json
from tradebot.core.snapshot import ContextSnapshot
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore
from tradebot.validation.backtest import BacktestHarness

logger = get_logger("decision_lab.corpus")

#: The marker `maintenance/compaction` leaves behind when it drops a payload's heavy body.
COMPACTION_MARKER = "compacted"

#: The workspace database one reference pass writes into. Never a bot database (§2.1).
CORPUS_DB = "corpus.db"


class CorpusMeta(DomainModel):
    """Everything needed to explain, reproduce and identify one corpus."""

    corpus_id: str
    built_at: UtcDatetime
    dataset_directory: str
    dataset_digest: str
    reference_panel_id: str
    #: The whole basket, not just the panel id: slice B's §9.7 swing rate replays
    #: `reach_consensus` over the recorded votes and needs the `PanelConfig` that produced them.
    reference_basket: Basket
    reference_config_digest: str
    cadence_seconds: int
    #: `""` until slice E. It feeds `corpus_id`, so re-summarising an archive with a different
    #: model yields a different corpus rather than silently mixing two experiments (§6.6).
    archive_digest: str = ""
    news_blind: bool = True
    start_equity: Money
    #: What was asked for, and where cycling actually began once the indicators had the history
    #: they need. Both, like `BacktestReport`: a corpus whose window silently differs from the
    #: requested one is a corpus about a different experiment — and `requested_start` is what a
    #: rebuild is checked against, since §5.4 keeps the window out of `corpus_id`.
    requested_start: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    warmup_seconds: int
    planned_cycles: int
    ran_cycles: int


class CorpusEntry(DomainModel):
    """One frozen decision context, with its place in the log."""

    seq: int
    cycle_id: str
    basket_id: str
    as_of: UtcDatetime
    snapshot: ContextSnapshot

    @property
    def day(self) -> date:
        return self.as_of.date()


@dataclass(frozen=True, slots=True)
class Corpus:
    """A built corpus: its identity and its entries, in log order."""

    meta: CorpusMeta
    entries: tuple[CorpusEntry, ...]

    def for_day(self, day: date) -> tuple[CorpusEntry, ...]:
        return tuple(entry for entry in self.entries if entry.day == day)

    def for_days(self, days: Sequence[date]) -> tuple[CorpusEntry, ...]:
        wanted = set(days)
        return tuple(entry for entry in self.entries if entry.day in wanted)


def config_digest(basket: Basket) -> str:
    """Identity of the reference configuration — the whole document, panel included (ADR 0013)."""
    return hashlib.blake2s(canonical_json(basket).encode("utf-8"), digest_size=16).hexdigest()


def corpus_identity(
    *, dataset_digest: str, reference_config_digest: str, cadence_seconds: int, archive_digest: str
) -> str:
    """§5.4. Changing the cadence, the reference panel, or the news archive is a *different*
    corpus rather than a silent mixing of two experiments."""
    payload = f"{dataset_digest}|{reference_config_digest}|{cadence_seconds}|{archive_digest}"
    return hashlib.blake2s(payload.encode("utf-8"), digest_size=16).hexdigest()


def corpus_dir(corpus_id: str, *, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / corpus_id


def entry_from_payload(
    *, seq: int, cycle_id: str, basket_id: str, payload: dict[str, Any]
) -> CorpusEntry:
    """Rebuild one entry from a `SNAPSHOT_FROZEN` payload, refusing a compacted one."""
    body = payload.get("snapshot")
    if body is None:
        marker = payload.get(COMPACTION_MARKER)
        raise ConfigError(
            f"snapshot {payload.get('snapshot_id')} has been compacted away"
            + (f" into {marker}" if marker else "")
            + ". A corpus of empty contexts scores perfectly and means nothing; rebuild the "
            "corpus from the dataset rather than reading a compacted database"
        )
    snapshot = ContextSnapshot.model_validate(body)
    return CorpusEntry(
        seq=seq,
        cycle_id=cycle_id,
        basket_id=basket_id,
        as_of=snapshot.as_of,
        snapshot=snapshot,
    )


def entries_from_store(store: EventStore) -> tuple[CorpusEntry, ...]:
    """`store.read_types(SNAPSHOT_FROZEN)` plus an index. That is the whole of §5.1."""
    return tuple(
        entry_from_payload(
            seq=event.seq or 0,
            cycle_id=event.cycle_id or "",
            basket_id=event.basket_id or "",
            payload=event.payload,
        )
        for event in store.read_types(EventType.SNAPSHOT_FROZEN)
    )


async def build(
    *,
    data_dir: Path,
    reference_panel: str,
    cadence_seconds: int,
    start_equity: Decimal,
    workspace: Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    archive_digest: str = "",
    wall_clock: Clock | None = None,
) -> Corpus:
    """One reference pass through the unmodified `BacktestHarness`, into a workspace database.

    `wall_clock` stamps `built_at` only. It is deliberately *not* the replay clock: that one is a
    `ManualClock` sitting in 2024 by the end of the pass, and a corpus built today carrying a
    2024 provenance date is a provenance date that says nothing.
    """
    audit = require_verified(data_dir)

    # The clock is set by the harness before the first cycle; this initial value only has to be
    # inside the dataset so `ReplayDataset.load` and the wiring have a coherent "now".
    probe = ReplayDataset.load(data_dir, ManualClock(audit.audited_at))
    requested_start, window_end = probe.window(since, until)
    clock = ManualClock(requested_start)
    dataset = ReplayDataset.load(data_dir, clock)

    basket = dataset_basket(
        dataset,
        select_panel(reference_panel),
        basket_id="reference",
        every_seconds=cadence_seconds,
    )
    identity = corpus_identity(
        dataset_digest=audit.dataset_digest,
        reference_config_digest=config_digest(basket),
        cadence_seconds=cadence_seconds,
        archive_digest=archive_digest,
    )
    directory = corpus_dir(identity, workspace=workspace)
    built = _existing(directory, identity, requested_start, window_end, workspace=workspace)
    if built is not None:
        return built

    directory.mkdir(parents=True, exist_ok=True)
    _refuse_interrupted(directory / CORPUS_DB)
    application = await build_sim(
        clock=clock,
        db_path=directory / CORPUS_DB,
        baskets=(basket,),
        start_equity=start_equity,
        market_data=dataset.market_data,
        catalogue=dataset_catalogue(dataset),
        # News stays off until slice E. §6.9: the snapshot records "no sources configured" rather
        # than leaving the panel to read an empty list as a quiet market.
        news_sources=(),
    )
    try:
        report = await BacktestHarness(
            application,
            clock,
            start=requested_start,
            end=window_end,
            data_source=str(data_dir),
        ).run()
        entries = entries_from_store(application.store)
    finally:
        await application.shutdown()

    meta = CorpusMeta(
        corpus_id=identity,
        built_at=(wall_clock or SystemClock()).now(),
        dataset_directory=str(data_dir),
        dataset_digest=audit.dataset_digest,
        reference_panel_id=reference_panel,
        reference_basket=basket,
        reference_config_digest=config_digest(basket),
        cadence_seconds=cadence_seconds,
        archive_digest=archive_digest,
        news_blind=not archive_digest,
        start_equity=start_equity,
        requested_start=report.requested_start,
        window_start=report.window_start,
        window_end=report.window_end,
        warmup_seconds=int(report.warmup // timedelta(seconds=1)),
        planned_cycles=report.planned_cycles,
        ran_cycles=report.ran_cycles,
    )
    (directory / CORPUS_META).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "corpus built",
        extra={"corpus_id": identity, "entries": len(entries), "cycles": report.ran_cycles},
    )
    return Corpus(meta=meta, entries=entries)


def _refuse_interrupted(database: Path) -> None:
    """Refuse to build on top of the database a previous pass left behind.

    Reached only when `_existing` found no `corpus.json`, so this file belongs to a pass that died
    before it could write one — a reference pass that raised, or a process that was killed.
    Building onto it would append a *second* pass into the same log and double every entry, which
    is the corruption `_existing` refuses for a completed corpus, arriving by the other door.

    Refusing rather than deleting, for two reasons. That database is the **record of why the pass
    failed**: its event log is the only account of the cycle that raised, and deleting it to make
    room for a retry destroys the evidence at exactly the moment somebody needs it. And on Windows
    it cannot be deleted from this process anyway — `Application.shutdown` closes the writer but
    never disposes the engine, so the pooled SQLite connection holds the file until exit.
    """
    if not database.is_file():
        return
    raise ConfigError(
        f"{database} already exists but its corpus has no {CORPUS_META}, so a previous build was "
        "interrupted. Its event log is the record of why that pass failed — read it before you "
        "discard it. Building over it would append a second reference pass into the same log and "
        "double every entry; remove the directory once you are done with it to build again"
    )


def _existing(
    directory: Path,
    corpus_id: str,
    requested_start: datetime,
    window_end: datetime,
    *,
    workspace: Path | None,
) -> Corpus | None:
    """The corpus already at this identity, or `None` when there is none.

    Reuse rather than rebuild, because §11's premise is that identical parameters are one
    experiment: a second pass into the same log would double every entry and leave the corpus
    describing two passes as if they were one.

    A *different window* under the same identity is the one case that refuses. §5.4 deliberately
    leaves the window out of `corpus_id`, so returning the stored corpus would answer a question
    about March with six months of evidence, and overwriting it would do the reverse to whoever
    built it first.
    """
    path = directory / CORPUS_META
    if not path.is_file():
        return None
    meta = CorpusMeta.model_validate_json(path.read_text(encoding="utf-8"))
    if (meta.requested_start, meta.window_end) != (requested_start, window_end):
        raise ConfigError(
            f"corpus {corpus_id} already exists over the window "
            f"{meta.requested_start.isoformat()}..{meta.window_end.isoformat()}, but this build "
            f"asks for {requested_start.isoformat()}..{window_end.isoformat()}. The window is not "
            "part of corpus identity (§5.4), so the two would collide: build the narrower window "
            "from a dataset recorded over it, or remove the existing corpus directory"
        )
    logger.info("corpus already built; reusing it", extra={"corpus_id": corpus_id})
    return load(corpus_id, workspace=workspace)


def load(corpus_id: str, *, workspace: Path | None = None) -> Corpus:
    """Re-open a built corpus. `open_database` never migrates — this is a read of a finished run."""
    directory = corpus_dir(corpus_id, workspace=workspace)
    meta_path = directory / CORPUS_META
    if not meta_path.is_file():
        raise ConfigError(
            f"no corpus {corpus_id!r} in {directory.parent}. Build one with "
            "`python -m decision_lab corpus build --data … --every …`"
        )
    meta = CorpusMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    engine = open_database(directory / CORPUS_DB)
    # `EventStore` takes a writer because it *can* write; this call only reads, and the corpus
    # database has no other process on it. The writer still owns a thread, so it is closed here.
    writer = SingleWriter(engine)
    try:
        return Corpus(meta=meta, entries=entries_from_store(EventStore(engine, writer)))
    finally:
        writer.close()
        engine.dispose()
