"""Scenario 3 — six months of long exposure, with its own ledger (spec §10.4).

A different *kind* of instrument from scenarios 1 and 2, and §10.1 keeps them apart for a reason:
positions compound, so a candidate's cycle 400 happens in a market its own cycle 12 helped create.
Its numbers are not on the same scale as a snapshot-scored accuracy and must never be ranked
against one — §10.5 prints both rankings and names every position where they disagree.

`BacktestHarness` runs unchanged, in a workspace database, from a declared starting balance. No
`tradebot` seam is needed: `build_sim(start_equity=…)` already threads to `SimBroker.balances`.

Failure semantics: a frozen aggregate at the window's end reports `UNVALUABLE` and no figure at
all — freezing is ignorance, and a number produced in ignorance is worse than its absence. An
unknown `--window` refuses, naming the ones that exist. Nothing here writes to a bot database or
constructs a venue broker; its orders reach `SimBroker` only, in a workspace database.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from decision_lab import registry
from decision_lab.candidates import Candidate
from decision_lab.corpus import config_digest, corpus_identity
from decision_lab.dataset import require_verified
from decision_lab.params import WINDOW_DAYS, workspace_root
from tradebot.app import build_sim, dataset_catalogue
from tradebot.core.clock import Clock, ManualClock
from tradebot.core.config import Schedule
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.risk.aggregate import PortfolioAggregate
from tradebot.validation.backtest import BacktestHarness


class ProfitEvidence(Protocol):
    """What `profit_of` and `veto_breakdown` need of `validation.Evidence`.

    A Protocol rather than the class itself, so the arithmetic can be driven by a stand-in without
    building a whole event log — and so this module states exactly which three of `Evidence`'s
    properties it depends on.
    """

    @property
    def realized_pnl(self) -> Decimal: ...

    @property
    def cost_usd(self) -> Decimal: ...

    @property
    def risk_events(self) -> Mapping[str, int]: ...


class Profit(DomainModel):
    """§10.4's headline, with both halves and never just the total."""

    #: A frozen aggregate. Every figure below is zero and none of them means anything.
    unvaluable: bool = False
    freeze_reason: str = ""
    equity: Money = ZERO
    start_equity: Money = ZERO
    #: `equity − start_equity`. Mark-to-market, so an open position at the window's end counts.
    total: Money = ZERO
    realized: Money = ZERO
    unrealized: Money = ZERO
    cost_usd: Money = ZERO
    #: `total − cost_usd`. The ask is efficient, not merely profitable.
    net: Money = ZERO


class VetoRow(DomainModel):
    """One rule's refusals over the run.

    §10.4: a panel that is right often but only in ways the price collar, the cooldown or the
    daily cap refuse is not an improvement — and a corpus sweep can never discover that.
    """

    rule: str
    action_taken: str = ""
    count: int = 0


def profit_of(
    aggregate: PortfolioAggregate, evidence: ProfitEvidence, *, start_equity: Decimal
) -> Profit:
    """What the run made, decomposed. `UNVALUABLE` when the portfolio cannot be valued.

    The total is **not** `Evidence.realized_pnl`: that property sums closed round trips only, so a
    run ending with an open position would report a profit that ignores it (§10.4). Realized is
    reported beside the mark-to-market total and unrealized is the difference — both halves always
    printed, because "made $80" reads very differently when $80 of it is still at risk.
    """
    if aggregate.frozen:
        # Every figure stays zero. Quoting realized alone would be the cost-basis fallback ADR
        # 0027 forbids, arriving as a partial answer instead of a wrong one.
        return Profit(unvaluable=True, freeze_reason=aggregate.frozen_reason)
    total = aggregate.equity - start_equity
    realized = evidence.realized_pnl
    return Profit(
        equity=aggregate.equity,
        start_equity=start_equity,
        total=total,
        realized=realized,
        unrealized=total - realized,
        cost_usd=evidence.cost_usd,
        net=total - evidence.cost_usd,
    )


def veto_breakdown(evidence: ProfitEvidence) -> tuple[VetoRow, ...]:
    """Which rule refused what, most frequent first.

    `Evidence.risk_events` is already keyed `rule/action_taken`, so this splits rather than
    re-folds the log. A key with no separator is kept under an empty action: a refusal nobody can
    parse is still a refusal that happened, and dropping it would understate the breakdown.
    """
    rows = []
    for key, count in evidence.risk_events.items():
        rule, _, action = key.partition("/")
        rows.append(VetoRow(rule=rule, action_taken=action, count=count))
    return tuple(sorted(rows, key=lambda row: (-row.count, row.rule, row.action_taken)))


def window_bounds(dataset_end: datetime, window: str) -> tuple[datetime, datetime]:
    """The window to replay, ending at the dataset's last covered instant.

    Backwards from the end rather than forwards from the start: the most recent history is the
    most relevant, and it is also the half most likely to be complete after a §4.3 repair.
    """
    if window not in WINDOW_DAYS:
        raise ConfigError(
            f"{window!r} is not a window this tool measures; it has "
            f"{', '.join(sorted(WINDOW_DAYS))}"
        )
    return dataset_end - timedelta(days=WINDOW_DAYS[window]), dataset_end


LONG_DB: Final = "long.db"
LONG_META: Final = "long.json"

logger = get_logger("decision_lab.longrun")


class LongRunMeta(DomainModel):
    """One long run: its identity, its window, and what it produced."""

    run_id: str
    #: Derived through `corpus.corpus_identity` so a long run has "its own corpus_id" exactly as
    #: §5.5 defines one — provenance for the §11 row, never a shared directory. The two have
    #: different windows by construction, and putting them in one place would turn §5.4's
    #: window-mismatch refusal into a refusal of an unrelated command.
    corpus_id: str
    built_at: UtcDatetime
    dataset_directory: str
    dataset_digest: str
    candidate_id: str
    panel_digest: str
    cadence_seconds: int
    window: str
    start_equity: Money
    requested_start: UtcDatetime
    window_start: UtcDatetime
    window_end: UtcDatetime
    warmup_seconds: int = 0
    planned_cycles: int = 0
    ran_cycles: int = 0
    profit: Profit = Profit()
    vetoes: tuple[VetoRow, ...] = ()
    incidents: int = 0
    decisions: int = 0
    fills: int = 0


def long_dir(run_id: str, *, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / f"long-{run_id}"


async def run(
    *,
    data_dir: Path,
    candidate: Candidate,
    cadence_seconds: int,
    window: str,
    start_equity: Decimal,
    wall_clock: Clock,
    workspace: Path | None = None,
) -> LongRunMeta:
    """One candidate's own path through `BacktestHarness`, from a declared starting balance.

    Mirrors `corpus.build`'s shape — identity, reuse-if-built, refuse-if-interrupted, run — for
    the same reasons: a second pass appended into one log doubles every entry, and the database a
    failed pass left behind is the only record of why it failed.
    """
    audit = require_verified(data_dir)
    probe = ReplayDataset.load(data_dir, ManualClock(audit.audited_at))
    _, dataset_end = probe.window(None, None)
    start, end = window_bounds(dataset_end, window)

    clock = ManualClock(start)
    dataset = ReplayDataset.load(data_dir, clock)
    basket = candidate.basket.model_copy(
        update={
            "basket_id": "longrun",
            "instruments": dataset.instruments,
            "timeframes": dataset.timeframes,
            "schedule": Schedule(every_seconds=cadence_seconds),
        }
    )
    corpus_id = corpus_identity(
        dataset_digest=audit.dataset_digest,
        reference_config_digest=config_digest(basket),
        cadence_seconds=cadence_seconds,
        archive_digest="",
    )
    identity = registry.run_id(
        scenario="calibrate-long",
        dataset_digest=audit.dataset_digest,
        corpus_id=corpus_id,
        matrix_digest="",
        dayset_digest="",
        candidate_id=candidate.candidate_id,
        cadence=str(cadence_seconds),
        start_equity=str(start_equity),
        window=window,
        sample_seed="0",
    )
    directory = long_dir(identity, workspace=workspace)
    meta_path = directory / LONG_META
    if meta_path.is_file():
        # §5.4's rule one level over: identical parameters are one experiment. Re-running returns
        # the pass that already happened rather than appending a second into its log.
        return LongRunMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))

    directory.mkdir(parents=True, exist_ok=True)
    database = directory / LONG_DB
    if database.is_file():
        raise ConfigError(
            f"{database} already exists but its run has no {LONG_META}, so a previous pass was "
            "interrupted. Its event log is the record of why that pass failed — read it before "
            "you discard it. Remove the directory once you are done with it to run again"
        )

    application = await build_sim(
        clock=clock,
        db_path=database,
        baskets=(basket,),
        start_equity=start_equity,
        market_data=dataset.market_data,
        catalogue=dataset_catalogue(dataset),
        news_sources=(),
    )
    try:
        report = await BacktestHarness(
            application, clock, start=start, end=end, data_source=str(data_dir)
        ).run()
        # Taken *before* shutdown, and through `Application.valuation` rather than a price map
        # built here: six call sites each building their own out of `avg_entry` is how the
        # drawdown kill switch came to report 0% on a portfolio that had halved (ADR 0027).
        valuation = application.valuation()
    finally:
        await application.shutdown()

    evidence = report.evidence
    meta = LongRunMeta(
        run_id=identity,
        corpus_id=corpus_id,
        built_at=wall_clock.now(),
        dataset_directory=str(data_dir),
        dataset_digest=audit.dataset_digest,
        candidate_id=candidate.candidate_id,
        panel_digest=candidate.panel_digest,
        cadence_seconds=cadence_seconds,
        window=window,
        start_equity=start_equity,
        requested_start=report.requested_start,
        window_start=report.window_start,
        window_end=report.window_end,
        warmup_seconds=int(report.warmup // timedelta(seconds=1)),
        planned_cycles=report.planned_cycles,
        ran_cycles=report.ran_cycles,
        profit=profit_of(valuation, evidence, start_equity=start_equity),
        vetoes=veto_breakdown(evidence),
        incidents=len(evidence.incidents),
        decisions=sum(evidence.actions.values()),
        fills=evidence.fills,
    )
    meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "long run complete",
        extra={
            "run_id": identity,
            "cycles": report.ran_cycles,
            "net": str(meta.profit.net),
            "unvaluable": meta.profit.unvaluable,
        },
    )
    return meta
