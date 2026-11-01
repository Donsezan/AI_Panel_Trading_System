"""One basket through a chosen calendar month, quarter or year, read as UTC (period spec).

The unit an operator names ("2025-Q2") and the unit the tool replays are the same thing: a
half-open `[start, end)` span that decomposes into whole months, so a quarter's result is the
three months it holds rather than a separate measurement.

A run is one workspace database, and a period's months are folded out of that one log by time, so
money and open positions carry because the ledger and `SimBroker` simply continue (§5.1). Its
orders reach `SimBroker` only; nothing here writes to a bot database or constructs a venue broker.

Failure semantics: parsing touches neither the network nor disk. Text that is not `YYYY`, `YYYY-MM`
or `YYYY-Qn` refuses with `ConfigError` naming what was typed; a period the recorded history does
not fully cover refuses naming the span that exists. Fail closed: a partly covered period is never
offered, because a result over a subset the tool chose would be reported as the whole. A halted
recovery raises `FailClosedError`. A period in which no cycle could run refuses rather than
reporting an empty result, and a database left without a finished meta refuses, naming it. A frozen
portfolio at a month's end is `unvaluable` with no figure: its equity reads zero and means nothing,
as does the start of the month after it, exactly as `longrun.Profit` does (§10.4).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final

from decision_lab import registry
from decision_lab.candidates import Candidate
from decision_lab.corpus import config_digest
from decision_lab.dataset import require_verified
from decision_lab.longrun import Profit, VetoRow, profit_of, veto_breakdown
from decision_lab.newsfeed import ArchiveNewsFeed
from decision_lab.params import workspace_root
from tradebot.app import Application, build_sim, dataset_catalogue
from tradebot.core.clock import Clock, ManualClock
from tradebot.core.config import Basket, Schedule
from tradebot.core.errors import ConfigError, FailClosedError
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.risk.aggregate import PortfolioAggregate
from tradebot.validation.backtest import DEFAULT_MAX_CYCLES, plan_ticks, warmup_for
from tradebot.validation.evidence import Evidence

_PATTERN: Final = re.compile(r"^(\d{4})(?:-(?:(0[1-9]|1[0-2])|Q([1-4])))?$")
_BAR: Final = timedelta(hours=1)
#: The one instrument a period run trades, by the operator's decision (§4). It reaches
#: `config_digest` through the basket, so widening this later mints new runs rather than colliding.
PERIOD_SYMBOL: Final = "BTC/USDT"
PERIOD_DB: Final = "period.db"
PERIOD_META: Final = "period.json"

logger = get_logger("decision_lab.period")


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def _next_month(year: int, month: int) -> datetime:
    return datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)


class Period(DomainModel):
    """A labelled half-open span; `end` is exclusive."""

    label: str
    start: UtcDatetime
    end: UtcDatetime

    @property
    def months(self) -> tuple[Period, ...]:
        out: list[Period] = []
        year, month = self.start.year, self.start.month
        while (here := _month_start(year, month)) < self.end:
            out.append(
                Period(label=f"{year:04d}-{month:02d}", start=here, end=_next_month(year, month))
            )
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return tuple(out)


def _span(label: str, year: int, first_month: int, last_month: int) -> Period:
    return Period(
        label=label,
        start=_month_start(year, first_month),
        end=_next_month(year, last_month),
    )


def parse_period(text: str) -> Period:
    match = _PATTERN.match(text)
    if match is None:
        raise ConfigError(
            f"unreadable period {text!r}: expected YYYY, YYYY-MM or YYYY-Qn (for example 2025-Q2)"
        )
    year = int(match.group(1))
    if match.group(2):
        month = int(match.group(2))
        return _span(text, year, month, month)
    if match.group(3):
        first = (int(match.group(3)) - 1) * 3 + 1
        return _span(text, year, first, first + 2)
    return _span(text, year, 1, 12)


def _is_covered(period: Period, first: datetime, last: datetime) -> bool:
    # `last` is the final bar's open time: the period is whole when that bar is its last hour.
    return first <= period.start and last >= period.end - _BAR


def covered_periods(first: datetime, last: datetime) -> dict[str, tuple[str, ...]]:
    """Every month, quarter and year the history `[first, last]` covers in full."""
    offered: dict[str, list[str]] = {"month": [], "quarter": [], "year": []}
    for year in range(first.year, last.year + 1):
        if _is_covered(_span(str(year), year, 1, 12), first, last):
            offered["year"].append(str(year))
        for quarter in range(1, 5):
            label = f"{year}-Q{quarter}"
            if _is_covered(parse_period(label), first, last):
                offered["quarter"].append(label)
        for month in range(1, 13):
            label = f"{year}-{month:02d}"
            if _is_covered(parse_period(label), first, last):
                offered["month"].append(label)
    return {kind: tuple(labels) for kind, labels in offered.items()}


def require_covered(period: Period, first: datetime, last: datetime) -> None:
    if not _is_covered(period, first, last):
        raise ConfigError(
            f"period {period.label} is not fully covered: recorded history runs "
            f"{first.date().isoformat()} to {last.date().isoformat()}"
        )


class RunEnding(StrEnum):
    """How a run stands. `RUNNING` on disk means a process died, never that one is alive (§5.7)."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    RULE_ENDED = "RULE_ENDED"


class MonthResult(DomainModel):
    """One month, folded out of the run's log by time."""

    label: str
    start_equity: Money
    #: Zero and meaningless when `unvaluable`.
    end_equity: Money
    unvaluable: bool = False
    planned_cycles: int
    ran_cycles: int
    paused_at: UtcDatetime | None = None
    cost_usd: Money


class PauseLift(DomainModel):
    """One lifted loss-streak pause (§5.3). A result produced with one must say so."""

    at: UtcDatetime
    streak: int
    new_limit: int


class PeriodMeta(DomainModel):
    """One period run: its identity, its months so far, and what it produced."""

    run_id: str
    #: Stamped by the wall clock: the replay clock is sitting inside the period by the end.
    built_at: UtcDatetime
    period: Period
    dataset_directory: str
    dataset_digest: str
    candidate_id: str
    basket_digest: str
    panel_digest: str
    archive_digest: str
    cadence_seconds: int
    start_equity: Money
    budget: Money
    #: Later than the period's start when the history cannot warm the indicators first (§5.2).
    first_cycle_at: UtcDatetime
    months: tuple[MonthResult, ...] = ()
    lifts: tuple[PauseLift, ...] = ()
    profit: Profit = Profit()
    vetoes: tuple[VetoRow, ...] = ()
    incidents: int = 0
    ending: RunEnding = RunEnding.RUNNING
    ending_reason: str = ""
    gate_skipped: bool = False
    plumbing_check: bool = False
    news_blind: bool = True


def period_dir(run_id: str, *, workspace: Path | None = None) -> Path:
    return (workspace or workspace_root()) / f"period-{run_id}"


def period_basket(candidate: Candidate, dataset: ReplayDataset, cadence_seconds: int) -> Basket:
    """The candidate's basket over BTC/USDT alone, on the dataset's own timeframes (§4)."""
    instruments = tuple(one for one in dataset.instruments if one.symbol == PERIOD_SYMBOL)
    if not instruments:
        held = ", ".join(one.symbol for one in dataset.instruments)
        raise ConfigError(f"this dataset holds no {PERIOD_SYMBOL}; it holds {held}")
    return candidate.basket.model_copy(
        update={
            "basket_id": "period",
            "instruments": instruments,
            "timeframes": dataset.timeframes,
            "schedule": Schedule(every_seconds=cadence_seconds),
        }
    )


def _planned_ticks(basket: Basket, *, first: datetime, end: datetime) -> tuple[datetime, ...]:
    """The grid instants in `[first, end)`, over the whole period at once.

    `plan_ticks` reads `(start, end]`, because `next_tick` is strict. Asked for a month as it
    stands it would skip the tick on the month's first instant and hand back the one on its last,
    which belongs to the next month, so a boundary tick would run nowhere. One second back admits
    the first (the grid is whole seconds); the filter removes the last.

    Planned once rather than per month so `DEFAULT_MAX_CYCLES` bounds the run the operator asked
    for: a year of quarter-hours is 35 040 panel runs and no single month of it would say so.
    """
    ticks = plan_ticks(
        (basket,), start=first - timedelta(seconds=1), end=end, limit=DEFAULT_MAX_CYCLES
    )
    return tuple(moment for moment, _ in ticks if moment < end)


async def _drive(
    application: Application, clock: ManualClock, basket_id: str, ticks: tuple[datetime, ...]
) -> datetime | None:
    """Cycle each tick in order. Returns when the basket stopped, or `None` if it never did.

    The same calls `BacktestHarness._replay` makes, in the same order (§5.4), because this loop
    must not drift from it. `test_the_driver_matches_the_harness_on_one_window` holds it to that.
    """
    for moment in ticks:
        clock.set(moment)
        worker = application.supervisor.worker_for(basket_id)
        await worker.cycle()
        await application.monitor.poll()
        if worker.stopped:
            logger.warning(
                "basket stopped cycling during the period",
                extra={"basket_id": basket_id, "at": moment.isoformat()},
            )
            return moment
    return None


def _month_result(
    month: Period,
    *,
    start_equity: Decimal,
    planned: int,
    evidence: Evidence,
    valuation: PortfolioAggregate,
) -> MonthResult:
    """A month's row. Cycles are folded from the log by time: it alone says what ran (ADR 0016)."""
    cycles = [c for c in evidence.cycles if month.start <= c.started_at < month.end]
    return MonthResult(
        label=month.label,
        start_equity=start_equity,
        end_equity=ZERO if valuation.frozen else valuation.equity,
        unvaluable=valuation.frozen,
        planned_cycles=planned,
        ran_cycles=len(cycles),
        cost_usd=sum((cycle.cost_usd for cycle in cycles), start=ZERO),
    )


def _write_meta(path: Path, meta: PeriodMeta) -> None:
    """Atomically, because a truncated meta would make the run unreadable rather than resumable."""
    scratch = path.with_suffix(".tmp")
    scratch.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    scratch.replace(path)


async def run(
    *,
    data_dir: Path,
    candidate: Candidate,
    period: Period,
    cadence_seconds: int,
    start_equity: Decimal,
    budget: Decimal,
    wall_clock: Clock,
    news_feed: ArchiveNewsFeed | None = None,
    archive_digest: str = "",
    gate_skipped: bool = False,
    plumbing_check: bool = False,
    workspace: Path | None = None,
) -> PeriodMeta:
    """One candidate's path through one period, in its own database, month after month.

    Mirrors `longrun.run`'s shape (identity, reuse-if-finished, refuse-if-interrupted, run) for its
    reasons: a second pass appended into one log doubles every entry, and the database a failed
    pass left behind is the only record of why it failed. Not `BacktestHarness`, which recovers on
    every call and retires a stopped basket for good, so it cannot express a month boundary (§5.4).
    """
    # §6.6's rule, as in `corpus.build`: two arguments that must agree is one that can disagree.
    if news_feed is not None:
        if archive_digest:
            raise ConfigError("pass either a news feed or an archive_digest, never both")
        archive_digest = news_feed.digest
    audit = require_verified(data_dir)
    probe = ReplayDataset.load(data_dir, ManualClock(audit.audited_at))
    covered_start, covered_end = probe.coverage
    # `coverage` ends at the last bar's close; `require_covered` takes the last hour's open.
    require_covered(period, covered_start, covered_end - _BAR)

    clock = ManualClock(period.start)
    dataset = ReplayDataset.load(data_dir, clock)
    basket = period_basket(candidate, dataset, cadence_seconds)
    basket_digest = config_digest(basket)

    # The whole basket rather than `panel_digest`, so a changed risk limit cannot reuse old profit.
    # Budget is out: it bounds spend and changes nothing the run produces (§5.1).
    identity = registry.run_id(
        scenario="evaluate-period",
        dataset_digest=audit.dataset_digest,
        basket_digest=basket_digest,
        cadence=str(cadence_seconds),
        start_equity=str(start_equity),
        window=period.label,
        archive_digest=archive_digest,
    )
    directory = period_dir(identity, workspace=workspace)
    meta_path = directory / PERIOD_META
    database = directory / PERIOD_DB
    if meta_path.is_file():
        found = PeriodMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        # The meta is written after every month, so RUNNING is part of a period, not a result.
        if found.ending is not RunEnding.RUNNING:
            return found
    if database.is_file():
        raise ConfigError(
            f"{database} already exists but its run never finished, so a previous pass was "
            "interrupted. Its event log is the record of why that pass failed — read it before "
            "you discard it. Remove the directory once you are done with it to run again"
        )

    # §5.2: the bars before the period are read for indicators only, so a period whose history can
    # warm them starts on its own first instant. One that cannot starts late and says so.
    warmup = warmup_for((basket,))
    first = max(period.start, covered_start + warmup)
    ticks = _planned_ticks(basket, first=first, end=period.end)
    if not ticks:
        raise ConfigError(
            f"no cycle of {period.label} can run: the indicators' warm-up of {warmup} reaches "
            "past its end. Record more history before it"
        )

    directory.mkdir(parents=True, exist_ok=True)
    meta = PeriodMeta(
        run_id=identity,
        built_at=wall_clock.now(),
        period=period,
        dataset_directory=str(data_dir),
        dataset_digest=audit.dataset_digest,
        candidate_id=candidate.candidate_id,
        basket_digest=basket_digest,
        panel_digest=candidate.panel_digest,
        archive_digest=archive_digest,
        cadence_seconds=cadence_seconds,
        start_equity=start_equity,
        budget=budget,
        first_cycle_at=ticks[0],
        gate_skipped=gate_skipped,
        plumbing_check=plumbing_check,
        news_blind=not archive_digest,
    )
    # Before the wiring and recovery: the baselines are established as of the replayed period.
    clock.set(first)
    application = await build_sim(
        clock=clock,
        db_path=database,
        baskets=(basket,),
        start_equity=start_equity,
        market_data=dataset.market_data,
        catalogue=dataset_catalogue(dataset),
        news_sources=(),
        news_feed=news_feed,
    )
    try:
        recovery = await application.recover()
        if recovery.halted:
            raise FailClosedError(
                f"startup recovery halted before the period began: {recovery.state.reason}; "
                f"failures: {', '.join(recovery.failures) or 'none recorded'}"
            )
        carried = start_equity
        stopped_at: datetime | None = None
        for month in period.months:
            due = tuple(moment for moment in ticks if month.start <= moment < month.end)
            # A stopped basket stays stopped: `worker_for` would replace the retired worker, and
            # the clock and the poll would step over ticks nothing can cycle. §5.3 branches here.
            if stopped_at is None:
                stopped_at = await _drive(application, clock, basket.basket_id, due)
            evidence = Evidence.gather(application.store)
            # Through `Application.valuation`, never a price map built here (ADR 0027).
            valuation = application.valuation()
            result = _month_result(
                month,
                start_equity=carried,
                planned=len(due),
                evidence=evidence,
                valuation=valuation,
            )
            carried = result.end_equity
            meta = meta.model_copy(
                update={
                    "months": (*meta.months, result),
                    "profit": profit_of(valuation, evidence, start_equity=start_equity),
                    "vetoes": veto_breakdown(evidence),
                    "incidents": len(evidence.incidents),
                }
            )
            _write_meta(meta_path, meta)
    finally:
        await application.shutdown()

    meta = meta.model_copy(update={"ending": RunEnding.COMPLETED})
    _write_meta(meta_path, meta)
    logger.info(
        "period run complete",
        extra={
            "run_id": identity,
            "period": period.label,
            "cycles": sum(month.ran_cycles for month in meta.months),
            "net": str(meta.profit.net),
            "unvaluable": meta.profit.unvaluable,
        },
    )
    return meta
