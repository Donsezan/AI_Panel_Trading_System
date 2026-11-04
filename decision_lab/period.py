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
exactly as `longrun.Profit` does (§10.4), and the month after it has no `start_equity` at all.

A loss-streak pause is the one stop that does not end cycling (§5.3, §5.4): the rest of its month
runs quarantined, and the next month, if there is one, lifts it. A win then hands the limit back, so
a lift restores an allowance rather than loosening the rule. All of it is written to this run's own
workspace database, through the same public stores a bot edit uses.

Every other stop ends the run, and §5.7 splits them by what they mean. A tripped kill switch, or
no position with too little cash for any order to exist, is `RULE_ENDED`: a verdict, final and
reused at its identity. A basket halted for any other cause, spend past the budget at a month
boundary (with a month still to run), or an unexpected exception is `INTERRUPTED`, because an error
says nothing about the model. The exception is written and re-raised for the CLI's exit code. Months
after an ending were not run, and `not_run_months` is how a reader knows: an absent month is never a
quiet one.

An interrupted run continues from its last month end (§5.7), by re-executing rather than restoring:
a fresh database from the period's start, the panel's answers replayed from the previous log up to
that month end (`replay.py`), and live calls after it. A checkpoint is only the month's meta,
durable on disk, marking how far those answers can be trusted. One that cannot be written raises
`OSError` and interrupts the run: going on would leave more to redo than the page promises. Continue
never overwrites anything: the database it abandons stays as `period.db.abandoned-<stamp>`, and what
that work spent past the month end still counts against the budget. A Continue whose budget is
already spent refuses with `BudgetReached` before moving anything; with no checkpoint it starts the
period again; with a checkpoint whose log no longer exists it refuses with `ConfigError`. A replay
that differs from what it replays raises `ConfigError` and interrupts the run, every earlier
database intact.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum, StrEnum
from pathlib import Path
from typing import Final

from sqlalchemy import inspect

from decision_lab import registry
from decision_lab.candidates import Candidate
from decision_lab.corpus import config_digest
from decision_lab.dataset import CoverageAudit, require_verified
from decision_lab.gate import GateRecord
from decision_lab.longrun import Profit, ProfitEvidence, VetoRow, profit_of, veto_breakdown
from decision_lab.newsfeed import ArchiveNewsFeed
from decision_lab.params import workspace_root
from decision_lab.records import CycleRecord
from decision_lab.replay import Replayer, recorded_before
from tradebot.app import Application, build_sim, dataset_catalogue
from tradebot.control.supervisor import BasketWorker
from tradebot.core.clock import Clock, ManualClock
from tradebot.core.config import Basket, Schedule
from tradebot.core.decision import SeatResponse, total_cost
from tradebot.core.enums import BasketStatus, ConfigKind, RiskTier
from tradebot.core.errors import ConfigError, FailClosedError
from tradebot.core.events import EventFactory, EventType
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO, to_decimal
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.execution.monitor import TRACKED_GROUP_RULE
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.persistence.database import SingleWriter, open_database
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import PortfolioAggregate
from tradebot.risk.rules import AUTO_PAUSE_RULE
from tradebot.validation.backtest import DEFAULT_MAX_CYCLES, plan_ticks, warmup_for
from tradebot.validation.evidence import Evidence

#: Matched whole, over ASCII digits only. The label is part of a run's identity, so `$` (which admits
#: a trailing newline) or `\d` (which admits full-width digits) would give one span a second name and
#: a second paid run.
_PATTERN: Final = re.compile(r"([0-9]{4})(?:-(?:(0[1-9]|1[0-2])|Q([1-4])))?")
_BAR: Final = timedelta(hours=1)
#: The one instrument a period run trades, by the operator's decision (§4). It reaches
#: `config_digest` through the basket, so widening this later mints new runs rather than colliding.
PERIOD_SYMBOL: Final = "BTC/USDT"
PERIOD_DB: Final = "period.db"
PERIOD_META: Final = "period.json"
CHECKPOINT_DIR: Final = "checkpoints"
_ABANDONED: Final = re.compile(
    rf"^{re.escape(PERIOD_DB)}\.abandoned-(\d{{8}}T\d{{6}}Z)(?:-(\d+))?$"
)
#: Who published a pause's two configuration versions, so the audit trail names this tool.
ACTOR: Final = "decision_lab.period"
#: The `RISK_EVENT` a lift appends. Namespaced because no rule of the bot's ever did this.
PAUSE_LIFTED_RULE: Final = "decision_lab.pause_lifted"
_BASKET_PAUSED: Final = "basket_paused"
#: `RISK_EVENT`s that are never a refusal on any run, by rule and by the actions each may take. A
#: new action under either rule stays on the table until someone says what it is.
#:
#: `valuation_basis` announces at a restart how equity compares with the high-water mark, and
#: changes nothing: nothing was proposed, so nothing was refused. `tracked_group` is the monitor
#: releasing a group it has finished with: retired, closed or lapsed, and no order is involved.
_NOT_REFUSALS: Final[dict[str, frozenset[str]]] = {
    "valuation_basis": frozenset({"recorded"}),
    TRACKED_GROUP_RULE: frozenset({"group_retired", "group_closed", "group_lapsed"}),
    # This tool's own act, appended so the log shows each lift (§5.3). Nothing was proposed.
    PAUSE_LIFTED_RULE: frozenset({"pause_lifted"}),
}

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
    match = _PATTERN.fullmatch(text)
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

    @property
    def is_final(self) -> bool:
        """Whether this is the run's result, reused at its identity without spend (§5.1)."""
        return self in {RunEnding.COMPLETED, RunEnding.RULE_ENDED}


class BudgetReached(FailClosedError):  # noqa: N818 - the plan's name: reaching it is no defect
    """Spend passed the budget at a month boundary (§5.5). Ends the run as an interruption."""


class MonthResult(DomainModel):
    """One month, folded out of the run's log by time."""

    label: str
    #: `None` after an unvaluable month. Carrying that month's zero would make this one's whole
    #: closing equity read as profit on a row that is itself valuable (ADR 0027).
    start_equity: Money | None
    #: Zero and meaningless when `unvaluable`.
    end_equity: Money
    unvaluable: bool = False
    #: The month's whole grid, whatever the history allowed, so coverage counts the cycles a late
    #: start lost (§5.2). `ran_cycles` is what actually ran.
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
    #: What work thrown away by a Continue spent. Its cycles left this run's log, but the money left
    #: the account, so budget checks and `profit.net` both count it (§5.7).
    abandoned_cost_usd: Money = ZERO
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
    #: The gate's key parts, as of the run's first build. Provenance read back by whoever files this
    #: run, so a reused or continued run is described by how it was made, never by the invocation
    #: that happened to find it.
    matrix_digest: str = ""
    dayset_digest: str = ""
    #: What the calibrated cost per cycle projected at the first build. `None` is "no projection was
    #: available" (§5.5), which is a different statement from a projection of zero.
    projected_usd: Money | None = None
    #: The seat-set file the run was started from, and the news sources it read, as of the first
    #: build. Where a reader finds the panel and the archive to read this run with; both are checked
    #: against the digests above, so a moved or edited file is refused rather than trusted. Empty on
    #: a run made before they were recorded, which a reader then looks up by digest.
    matrix_source: str = ""
    news_sources: tuple[str, ...] = ()
    #: The ending is `INTERRUPTED` because spend passed the budget. A field and not a reading of
    #: `ending_reason`, which is prose for a human (§5.7).
    budget_stopped: bool = False

    @property
    def spent_usd(self) -> Decimal:
        """What the panel cost, known whatever the valuation: a frozen portfolio has no profit
        figure, but the model calls were still paid for (§10.4)."""
        return sum((month.cost_usd for month in self.months), start=ZERO) + self.abandoned_cost_usd


def not_run_months(meta: PeriodMeta) -> tuple[str, ...]:
    """The period's months with no row on the meta, derived so it cannot disagree with `months`.

    Not quite "no cycle was attempted": a month an exception cut short has cycled and has no row,
    because only a month's end writes one. A Continue interrupted while replaying likewise shows
    fewer rows than its checkpoint, until the replay reaches them again.

    A row per month carrying `not run` was the alternative, and would give a reader a zero-cycle
    row to add up as though it were a measurement (§5.3).
    """
    reached = {month.label for month in meta.months}
    return tuple(month.label for month in meta.period.months if month.label not in reached)


def projected_cost(
    record: GateRecord | None, candidate_id: str, period: Period, cadence_seconds: int
) -> Decimal | None:
    """The gate's measured cost per cycle times the cycles the period plans (§5.5).

    `None` when there is nothing to project from, which a page says rather than showing a zero
    that reads as free. Shared by the CLI and the dashboard so the two quote one number.
    """
    if record is None or record.normal is None:
        return None
    found = next(
        (one for one in record.normal.candidates if one.candidate_id == candidate_id), None
    )
    if found is None:
        return None
    planned = (period.end - period.start) // timedelta(seconds=cadence_seconds)
    return found.cost_per_cycle * planned


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
            # As `candidates._basket_for` does, and here for one more reason: a challenger's
            # answers are not replayed, so a Continue would pay for them all again, unread.
            "shadow_panel": None,
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


async def stopped_on_loss_streak(application: Application, basket_id: str) -> bool:
    """Whether this basket stands halted by `AUTO_PAUSE_RULE`, with the kill switch armed (§5.3).

    Asks of the *newest halt*, not merely whether a pause exists: the log keeps January's pause
    when February's halt arrives for some other reason, and that one is not ours to lift.
    `_on_tier1_veto` appends the pause and then halts with its detail, so the two must agree.
    """
    states = application.states
    if states.status_of(basket_id).may_trade or not states.load().may_trade:
        return False
    # The watchdog stamps its own events `global`, so a halt names its basket in the payload alone.
    halt = next(
        (
            event
            for event in reversed(application.store.read_types(EventType.BASKET_STATUS_CHANGED))
            if event.payload.get("basket_id") == basket_id
            and event.payload.get("status") == BasketStatus.HALTED.value
        ),
        None,
    )
    pause = next(
        (
            event
            for event in reversed(application.store.read_types(EventType.RISK_EVENT))
            if event.basket_id == basket_id
            and event.payload.get("rule") == AUTO_PAUSE_RULE
            and event.payload.get("action_taken") == _BASKET_PAUSED
        ),
        None,
    )
    if halt is None or pause is None:
        return False
    agree = halt.payload.get("reason") == pause.payload.get("detail")
    return agree and (pause.seq or 0) < (halt.seq or 0)


def _current_basket(application: Application, basket_id: str) -> Basket:
    record = application.configs.latest(ConfigKind.BASKET, basket_id)
    if record is None:
        raise FailClosedError(f"basket {basket_id!r} has no stored configuration to pause or lift")
    document: Basket = record.document
    return document


def _with_policy(basket: Basket, **changes: object) -> Basket:
    return basket.model_copy(update={"risk_policy": basket.risk_policy.model_copy(update=changes)})


async def quarantine_stretch(application: Application, basket: Basket) -> None:
    """Keep time moving through a pause without the panel (§5.4).

    A quarantined cycle still takes its snapshot, which is what feeds `SimBroker` a tick (so a
    resting stop can fire) and refreshes the marks (so the month end can be valued). Published
    first and cleared second, so no instant exists at which the basket is both free and unbarred.
    """
    current = _current_basket(application, basket.basket_id)
    await application.configs.put(
        basket.basket_id,
        _with_policy(current, quarantined=True),
        actor=ACTOR,
        note="loss-streak pause: the rest of this month runs quarantined (§5.4)",
    )
    # `resume_basket` rather than the bare status write, so the clearing is in the log as well.
    await application.watchdog.resume_basket(basket.basket_id, actor=ACTOR)


def _consecutive_losses(store: EventStore, basket_id: str) -> int:
    """The streak `ConsecutiveLossRule` read, derived the way `ledger/history.py:96` derives it.

    From the log, since `Application` exposes no read of `round_trips`: losing round trips at the
    head of the basket's history, where anything but a loss ends the count. Narrowed to that one
    event type because a lifted limit asks this after every cycle until a win.
    """
    streak = 0
    for event in reversed(store.read_types(EventType.ROUND_TRIP_CLOSED)):
        if event.basket_id != basket_id:
            continue
        if to_decimal(event.payload["round_trip"]["realized_pnl"]) >= ZERO:
            break
        streak += 1
    return streak


async def restore_limit(application: Application, basket: Basket) -> None:
    """Hand back the configured limit once a win has reset the streak (§5.3).

    A lift adds to the limit only so the streak it inherited cannot veto at once. With that streak
    gone the addition would be a looser rule than the candidate declared, and every later figure
    would be measured under a limit nobody configured.
    """
    configured = basket.risk_policy.max_consecutive_losses
    current = _current_basket(application, basket.basket_id)
    if current.risk_policy.max_consecutive_losses == configured:
        return
    if _consecutive_losses(application.store, basket.basket_id) != 0:
        return
    await application.configs.put(
        basket.basket_id,
        _with_policy(current, max_consecutive_losses=configured),
        actor=ACTOR,
        note=f"a win reset the streak: limit {configured} again (§5.3)",
    )


async def lift_pause(
    application: Application, basket: Basket, *, configured_limit: int, at: datetime
) -> PauseLift:
    """Restore the basket's whole allowance at a month boundary (§5.3).

    Clearing the status alone would achieve nothing: the streak is derived rather than stored, and
    only a win resets it, so the next proposal would veto and halt again. `configured_limit` is the
    candidate's, never the current version's, or each lift would add to the last one's allowance.
    """
    streak = _consecutive_losses(application.store, basket.basket_id)
    new_limit = streak + configured_limit
    current = _current_basket(application, basket.basket_id)
    await application.configs.put(
        basket.basket_id,
        _with_policy(current, quarantined=False, max_consecutive_losses=new_limit),
        actor=ACTOR,
        note=f"loss-streak pause lifted at a month boundary: limit {new_limit} (§5.3)",
    )
    events = EventFactory(clock=application.clock, basket_id=basket.basket_id, cycle_id="period")
    await application.store.append(
        events.risk_event(
            tier=RiskTier.TIER1,
            rule=PAUSE_LIFTED_RULE,
            scope=basket.basket_id,
            action="pause_lifted",
            detail=f"streak {streak} plus the configured {configured_limit}: limit {new_limit}",
        )
    )
    return PauseLift(at=at, streak=streak, new_limit=new_limit)


def out_of_money(application: Application, basket: Basket) -> bool:
    """Flat, with too little cash for any order of this basket to exist again (§5.7).

    `balance` is total funds rather than free, deliberately: this is a final verdict, and cash a
    resting entry has locked is still an order that can fill. A position only appears at a fill.
    """
    if any(not position.is_flat for position in application.ledger.positions()):
        return False
    cash = application.ledger.balance(application.quote_currency)
    return cash < min(instrument.min_notional for instrument in basket.instruments)


class _Cause(Enum):
    """Why cycling stopped. A loss-streak pause is not one: it does not stop cycling."""

    KILL_SWITCH = "kill switch"
    OUT_OF_MONEY = "out of money"
    HALTED = "basket stopped"


#: §5.7's table. A rule ending the run is a result; an error continues from the last good point.
_ENDING_OF: Final[dict[_Cause, RunEnding]] = {
    _Cause.KILL_SWITCH: RunEnding.RULE_ENDED,
    _Cause.OUT_OF_MONEY: RunEnding.RULE_ENDED,
    _Cause.HALTED: RunEnding.INTERRUPTED,
}


@dataclass(frozen=True, slots=True)
class _Stop:
    cause: _Cause
    detail: str
    at: datetime

    @property
    def reason(self) -> str:
        return f"{self.cause.value} on {self.at.date().isoformat()}: {self.detail}"


def _stop_after_cycle(
    application: Application, worker: BasketWorker, basket: Basket, at: datetime
) -> _Stop | None:
    """What ended cycling at this tick, asked of the database rather than of the worker.

    `_runnable` never reads the kill switch, so a tripped run cycles on as BLOCKED and QUARANTINED
    and would report every later month fully run. The switch is read first: it outranks whatever
    else the same cycle did, and its verdict is the one that must not be lost to a lesser reason.
    """
    states = application.states
    state = states.load()
    if not state.may_trade:
        return _Stop(_Cause.KILL_SWITCH, state.reason, at)
    if worker.stopped or not states.status_of(basket.basket_id).may_trade:
        detail = states.halted_baskets().get(basket.basket_id) or "it is no longer runnable"
        return _Stop(_Cause.HALTED, detail, at)
    if out_of_money(application, basket):
        cash = application.ledger.balance(application.quote_currency)
        return _Stop(_Cause.OUT_OF_MONEY, f"flat with {cash} {application.quote_currency} left", at)
    return None


@dataclass(frozen=True, slots=True)
class _Driven:
    """What one month's ticks did. `stop` is the run's end; `paused_at` is not."""

    paused_at: datetime | None = None
    stop: _Stop | None = None


async def _drive(
    application: Application,
    clock: ManualClock,
    basket: Basket,
    ticks: tuple[datetime, ...],
    *,
    after_cycle: Callable[[], None] = lambda: None,
) -> _Driven:
    """Cycle each tick in order, through a pause, until the ticks end or something ends the run.

    The same calls `BacktestHarness._replay` makes, in the same order (§5.4), because this loop
    must not drift from it. `test_the_driver_matches_the_harness_on_one_window` holds it to that.

    The pause is asked of the database after each cycle, not of `worker.stopped`: a worker only
    learns of a halt at its *next* `_runnable`, so a pause on a month's last tick would otherwise
    surface in the following month, with no month boundary left to lift it at.

    `after_cycle` may raise, ending the run: `BasketWorker` absorbs what a cycle itself raises.
    """
    paused_at: datetime | None = None
    for moment in ticks:
        clock.set(moment)
        worker = application.supervisor.worker_for(basket.basket_id)
        await worker.cycle()
        after_cycle()
        await application.monitor.poll()
        if paused_at is None and await stopped_on_loss_streak(application, basket.basket_id):
            logger.warning(
                "loss-streak pause: quarantined for the rest of the month",
                extra={"basket_id": basket.basket_id, "at": moment.isoformat()},
            )
            await quarantine_stretch(application, basket)
            paused_at = moment
        if (stop := _stop_after_cycle(application, worker, basket, moment)) is not None:
            logger.warning(
                "the period run ended early",
                extra={"basket_id": basket.basket_id, "reason": stop.reason},
            )
            return _Driven(paused_at=paused_at, stop=stop)
        await restore_limit(application, basket)
    return _Driven(paused_at=paused_at)


def _month_result(
    month: Period,
    *,
    start_equity: Decimal | None,
    planned: int,
    evidence: Evidence,
    valuation: PortfolioAggregate,
    paused_at: datetime | None = None,
) -> MonthResult:
    """A month's row. Cycles are folded from the log by time: it alone says what ran (ADR 0016).

    Everything after a pause is its stretch, whatever each cycle's outcome was, so a stretch cycle
    the daily-loss gate blocked still does not read as a cycle the panel ran (§5.4).
    """
    last = month.end if paused_at is None else paused_at + timedelta(seconds=1)
    cycles = [c for c in evidence.cycles if month.start <= c.started_at < last]
    return MonthResult(
        label=month.label,
        start_equity=start_equity,
        end_equity=ZERO if valuation.frozen else valuation.equity,
        unvaluable=valuation.frozen,
        planned_cycles=planned,
        ran_cycles=len(cycles),
        paused_at=paused_at,
        cost_usd=sum((cycle.cost_usd for cycle in cycles), start=ZERO),
    )


def _vetoes(evidence: ProfitEvidence) -> tuple[VetoRow, ...]:
    return tuple(
        row
        for row in veto_breakdown(evidence)
        if row.action_taken not in _NOT_REFUSALS.get(row.rule, frozenset())
    )


def _write_meta(path: Path, meta: PeriodMeta) -> None:
    """Atomically, because a truncated meta would make the run unreadable rather than resumable."""
    scratch = path.with_suffix(".tmp")
    scratch.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    scratch.replace(path)


def _sidecars(database: Path) -> tuple[Path, ...]:
    return tuple(database.with_name(database.name + suffix) for suffix in ("-wal", "-shm"))


def write_checkpoint(directory: Path, month_label: str, *, meta: PeriodMeta) -> Path:
    """Put the month's meta on disk, atomically and durably: it marks the replay boundary (§5.7).

    No copy of the database. Re-execution replays the log and restores nothing from a copy, and a
    year of them would be twelve growing files. The log it vouches for is durable already, because
    the schema commits with `synchronous=FULL`, so the fsync here is what makes the two agree.
    """
    root = directory / CHECKPOINT_DIR
    root.mkdir(exist_ok=True)
    target = root / f"{month_label}.json"
    scratch = target.with_suffix(".tmp")
    with scratch.open("w", encoding="utf-8") as out:
        out.write(meta.model_dump_json(indent=2))
        out.flush()
        os.fsync(out.fileno())
    scratch.replace(target)
    return target


def _latest_checkpoint(directory: Path) -> PeriodMeta | None:
    """The newest whole checkpoint. `YYYY-MM` sorts by time; a `.tmp` is passed over."""
    written = sorted((directory / CHECKPOINT_DIR).glob("*.json"), key=lambda one: one.name)
    if not written:
        return None
    return PeriodMeta.model_validate_json(written[-1].read_text(encoding="utf-8"))


@contextmanager
def _log_of(database: Path) -> Iterator[EventStore | None]:
    """A database's log, or `None` for one killed before its schema existed."""
    engine = open_database(database)
    writer = SingleWriter(engine)
    try:
        yield EventStore(engine, writer) if inspect(engine).has_table("events") else None
    finally:
        writer.close()
        engine.dispose()


def _spend_around(database: Path, boundary: datetime | None) -> tuple[Decimal, Decimal]:
    """What a database's log spent before `boundary`, and from it on.

    Off the responses rather than `Evidence.cost_usd`: a cycle that failed, or died part way,
    records no cost of its own, and the provider was still paid.
    """
    if not database.exists():
        return ZERO, ZERO
    with _log_of(database) as store:
        if store is None:
            return ZERO, ZERO
        events = store.read_types(EventType.SEAT_RESPONDED)
    responses = [(e.ts, SeatResponse.model_validate(e.payload["response"])) for e in events]
    before = [one for at, one in responses if boundary is not None and at < boundary]
    after = [one for at, one in responses if boundary is None or at >= boundary]
    return total_cost(before), total_cost(after)


def _abandoned_newest_first(directory: Path) -> list[Path]:
    """Ordered by what the name says, numerically: `-10` follows `-9`. Sidecars do not match."""
    found = []
    for path in directory.iterdir():
        match = _ABANDONED.match(path.name)
        if match is not None:
            found.append(((match.group(1), int(match.group(2) or 1)), path))
    return [path for _, path in sorted(found, key=lambda one: one[0], reverse=True)]


def _log_reaching(directory: Path, last_tick: datetime) -> Path | None:
    """The newest database that finished the checkpointed month's last cycle.

    Not simply the newest: a Continue that died mid-replay leaves a shorter log than the one it was
    replaying, and that log is now the only one that can reach the boundary. An older one reaching
    it may hold answers later overwritten, so it is the newest of those that qualify.
    """
    for candidate in (directory / PERIOD_DB, *_abandoned_newest_first(directory)):
        if not candidate.exists():
            continue
        with _log_of(candidate) as store:
            done = store.read_types(EventType.CYCLE_COMPLETED) if store is not None else ()
        if done and done[-1].ts >= last_tick:
            return candidate
    return None


@dataclass(frozen=True, slots=True)
class _Replay:
    """What a Continue re-executes: the checkpoint's month rows and the answers behind them."""

    months: tuple[MonthResult, ...]
    boundary: datetime
    recorded: tuple[CycleRecord, ...]


def _replay_from(
    directory: Path, checkpoint: PeriodMeta, ticks: tuple[datetime, ...]
) -> tuple[_Replay, Decimal]:
    """The replay a checkpoint calls for, and what the log it reads spent up to the boundary."""
    boundary = checkpoint.period.months[len(checkpoint.months) - 1].end
    last_tick = max((moment for moment in ticks if moment < boundary), default=None)
    if last_tick is None:
        return _Replay(checkpoint.months, boundary, ()), ZERO
    source = _log_reaching(directory, last_tick)
    if source is None:
        raise ConfigError(
            f"{directory} has a checkpoint at {checkpoint.months[-1].label}, but no database there "
            "holds the log it vouches for, so nothing can be replayed. Delete the directory to "
            "start the period again"
        )
    kept, _ = _spend_around(source, boundary)
    return _Replay(checkpoint.months, boundary, recorded_before(source, boundary)), kept


def _abandon(database: Path, clock: Clock) -> None:
    """Move the interrupted database aside, kept, with its sidecars (§5.7).

    The sidecars move with it: a process that died leaves a `-wal` holding committed rows, and left
    behind it would be read as belonging to the restored file.
    """
    if not database.exists():
        return
    stem = f"{database.name}.abandoned-{clock.now().strftime('%Y%m%dT%H%M%SZ')}"
    target = database.with_name(stem)
    attempt = 1
    # A second Continue within one second must not overwrite the first's record.
    while target.exists():
        attempt += 1
        target = database.with_name(f"{stem}-{attempt}")
    sidecars = _sidecars(database)
    database.replace(target)
    for sidecar, moved in zip(sidecars, _sidecars(target), strict=True):
        if sidecar.exists():
            sidecar.replace(moved)


def _first_build(found: PeriodMeta | None) -> dict[str, object]:
    """The provenance a run keeps from the invocation that began it (§5.7)."""
    if found is None:
        return {}
    # Each kept only when recorded: a run begun before a field existed takes this invocation's.
    kept: dict[str, object] = {}
    if found.matrix_digest:
        kept.update(
            matrix_digest=found.matrix_digest,
            dayset_digest=found.dayset_digest,
            projected_usd=found.projected_usd,
        )
    if found.matrix_source:
        kept["matrix_source"] = found.matrix_source
    if found.news_sources:
        kept["news_sources"] = found.news_sources
    return kept


def _continue_from(
    directory: Path,
    *,
    fresh: PeriodMeta,
    found: PeriodMeta | None,
    ticks: tuple[datetime, ...],
    budget: Decimal,
    clock: Clock,
) -> tuple[PeriodMeta, _Replay | None]:
    """Clear the way for a fresh database, and say what it will replay (§5.7).

    Spend past the boundary in the database being moved joins what earlier Continues abandoned.
    Spend before it is carried into the new log by the replay, so it is not abandoned, and a
    database that died mid-replay holds nothing else. Written down *before* the database moves: a
    crash between the two counts that work twice, which errs towards stopping, where the other order
    would forget it.
    """
    database = directory / PERIOD_DB
    checkpoint = _latest_checkpoint(directory)
    replay, kept = _replay_from(directory, checkpoint, ticks) if checkpoint else (None, ZERO)
    _, beyond = _spend_around(database, replay.boundary if replay else None)
    abandoned = (found.abandoned_cost_usd if found is not None else ZERO) + beyond
    months_left = checkpoint is None or len(checkpoint.months) < len(fresh.period.months)
    if months_left and kept + abandoned > budget:
        raise BudgetReached(
            f"this run has spent {kept + abandoned}, past the budget of {budget}: continuing it "
            "needs a larger budget"
        )
    meta = fresh.model_copy(
        update={
            "abandoned_cost_usd": abandoned,
            "built_at": found.built_at if found is not None else fresh.built_at,
            # A stamp once earned stays: part of this run's money was made without the gate.
            "gate_skipped": fresh.gate_skipped or (found is not None and found.gate_skipped),
            # As first built: this invocation may hold a different gate, key or projection.
            **_first_build(found),
        }
    )
    _write_meta(directory / PERIOD_META, meta)
    _abandon(database, clock)
    logger.warning(
        "continuing an interrupted period run",
        extra={
            "run_id": fresh.run_id,
            "checkpoint": checkpoint.months[-1].label if checkpoint else "",
            "abandoned_cost_usd": str(abandoned),
        },
    )
    return meta, replay


def _require_same_month(recorded: MonthResult, replayed: MonthResult) -> None:
    """A replayed month must come out as it did: its last poll is in no later snapshot."""
    if replayed != recorded:
        raise ConfigError(
            f"the replay differs from the recorded run at the end of {recorded.label}: "
            f"{replayed.model_dump_json()} against {recorded.model_dump_json()}"
        )


def _profit(valuation: PortfolioAggregate, evidence: Evidence, *, meta: PeriodMeta) -> Profit:
    """`profit_of`, plus what abandoned work cost. A frozen portfolio still has no figure at all."""
    profit = profit_of(valuation, evidence, start_equity=meta.start_equity)
    if profit.unvaluable:
        return profit
    return profit.model_copy(
        update={
            "cost_usd": profit.cost_usd + meta.abandoned_cost_usd,
            "net": profit.net - meta.abandoned_cost_usd,
        }
    )


@dataclass(frozen=True, slots=True)
class _Identity:
    """Everything that names a run, computed without touching its directory."""

    audit: CoverageAudit
    coverage_start: datetime
    archive_digest: str
    dataset: ReplayDataset
    clock: ManualClock
    basket: Basket
    basket_digest: str
    run_id: str


def _identify(
    *,
    data_dir: Path,
    candidate: Candidate,
    period: Period,
    cadence_seconds: int,
    start_equity: Decimal,
    news_feed: ArchiveNewsFeed | None,
    archive_digest: str,
) -> _Identity:
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
    return _Identity(
        audit, covered_start, archive_digest, dataset, clock, basket, basket_digest, identity
    )


def finished_run(
    *,
    data_dir: Path,
    candidate: Candidate,
    period: Period,
    cadence_seconds: int,
    start_equity: Decimal,
    news_feed: ArchiveNewsFeed | None = None,
    archive_digest: str = "",
    workspace: Path | None = None,
) -> PeriodMeta | None:
    """The final result already filed at this identity, or `None`. Read-only (§5.1).

    A finished run is reused at no spend, so whoever is about to ask for a gate, a provider or a
    projection asks this first: those are consulted before spend, never before reuse. Raises what
    `run` raises for a dataset that is unverified or does not cover the period.
    """
    who = _identify(
        data_dir=data_dir,
        candidate=candidate,
        period=period,
        cadence_seconds=cadence_seconds,
        start_equity=start_equity,
        news_feed=news_feed,
        archive_digest=archive_digest,
    )
    meta_path = period_dir(who.run_id, workspace=workspace) / PERIOD_META
    if not meta_path.is_file():
        return None
    found = PeriodMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    return found if found.ending.is_final else None


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
    matrix_digest: str = "",
    dayset_digest: str = "",
    projected_usd: Decimal | None = None,
    workspace: Path | None = None,
    resume: bool = False,
    matrix_source: str = "",
    news_sources: tuple[str, ...] = (),
) -> PeriodMeta:
    """One candidate's path through one period, in its own database, month after month.

    Mirrors `longrun.run`'s shape (identity, reuse-if-finished, refuse-if-interrupted, run) for its
    reasons: a second pass appended into one log doubles every entry, and the database a failed
    pass left behind is the only record of why it failed. Not `BacktestHarness`, which recovers on
    every call and retires a stopped basket for good, so it cannot express a month boundary (§5.4).

    An unexpected exception out of the month loop writes `INTERRUPTED` and is raised again.

    `resume` is Continue (§5.7): an unfinished run is re-executed in a fresh database, replaying
    answers up to its last month end, under this `budget` rather than the stored one, since a budget
    stop is exactly what needs a larger one. A finished run is reused as ever, except that a rule's
    verdict refuses, because being asked to continue one means someone read it as an error. Identity
    is unchanged, so seats changed since the run began are a different directory, which has
    nothing to continue, and the old one is not touched. Continue with nothing unfinished at the
    identity refuses with `ConfigError`: it never starts a run. A run that used a `news_feed` must
    be given the same one: its digest
    is in the identity, and each replayed snapshot compares the news it was served.
    """
    who = _identify(
        data_dir=data_dir,
        candidate=candidate,
        period=period,
        cadence_seconds=cadence_seconds,
        start_equity=start_equity,
        news_feed=news_feed,
        archive_digest=archive_digest,
    )
    audit, clock, dataset, basket = who.audit, who.clock, who.dataset, who.basket
    archive_digest, basket_digest, identity = who.archive_digest, who.basket_digest, who.run_id
    covered_start = who.coverage_start
    directory = period_dir(identity, workspace=workspace)
    meta_path = directory / PERIOD_META
    database = directory / PERIOD_DB
    found: PeriodMeta | None = None
    why = ""
    if meta_path.is_file():
        found = PeriodMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
        if resume and found.ending is RunEnding.RULE_ENDED:
            raise ConfigError(
                "this run was ended by a rule, which is its result and has nothing to continue: "
                f"{found.ending_reason}"
            )
        # The meta is written after every month, so RUNNING is part of a period, not a result.
        if found.ending.is_final:
            return found
        why = f" ({found.ending_reason})" if found.ending_reason else ""
    # An unfinished run is never run over: a second pass appended to this log would double every
    # entry in it. A meta with no database is a Continue that died between its two moves.
    unfinished = found is not None or database.is_file()
    if resume and not unfinished:
        # Continue means continue. Starting fresh here would let a typo in the cadence, the equity
        # or the period mint a new identity and spend past the projection the command applies only
        # where there is something to continue (§5.5, §5.7).
        raise ConfigError(
            f"there is no unfinished run to continue at this identity ({directory.name}): check "
            "the data, seat set, period, cadence, start equity and news archive against the run "
            "you meant. Starting a new run is the same command without --continue"
        )
    if unfinished and not resume:
        raise ConfigError(
            f"{directory} holds a run that never finished, so a previous pass was "
            f"interrupted{why}. Continue it to carry on from its last month end. Its event log is "
            "the record of why that pass failed — read it before you discard it"
        )

    # §5.2: the bars before the period are read for indicators only, so a period whose history can
    # warm them starts on its own first instant. One that cannot starts late and says so.
    warmup = warmup_for((basket,))
    first = max(period.start, covered_start + warmup)
    grid = _planned_ticks(basket, first=period.start, end=period.end)
    ticks = tuple(moment for moment in grid if moment >= first)
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
        matrix_digest=matrix_digest,
        dayset_digest=dayset_digest,
        projected_usd=projected_usd,
        matrix_source=matrix_source,
        news_sources=news_sources,
    )
    replay: _Replay | None = None
    if unfinished:
        meta, replay = _continue_from(
            directory, fresh=meta, found=found, ticks=ticks, budget=budget, clock=wall_clock
        )
    replayer = (
        Replayer(replay.recorded, boundary=replay.boundary, clock=clock)
        if replay is not None
        else None
    )
    replayed_months = replay.months if replay is not None else ()
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
        deliberate=replayer,
    )
    try:
        recovery = await application.recover()
        if recovery.halted:
            raise FailClosedError(
                f"startup recovery halted before the period began: {recovery.state.reason}; "
                f"failures: {', '.join(recovery.failures) or 'none recorded'}"
            )
        carried: Decimal | None = start_equity
        needs_lift = False
        try:
            for index, month in enumerate(period.months):
                due = tuple(moment for moment in ticks if month.start <= moment < month.end)
                planned = sum(1 for moment in grid if month.start <= moment < month.end)
                # Only a stretch that reached its month's end leaves this set, and `_drive` read
                # the switch and the basket's status after its every cycle, so no halt is here to
                # wave through: §5.3 gives this tool authority over the streak alone.
                if needs_lift:
                    clock.set(month.start)
                    lift = await lift_pause(
                        application,
                        basket,
                        configured_limit=basket.risk_policy.max_consecutive_losses,
                        at=month.start,
                    )
                    meta = meta.model_copy(update={"lifts": (*meta.lifts, lift)})
                    needs_lift = False
                driven = await _drive(
                    application,
                    clock,
                    basket,
                    due,
                    after_cycle=replayer.raise_if_diverged if replayer else lambda: None,
                )
                needs_lift = driven.paused_at is not None
                # A month that was checkpointed ran whole, so a stop in it now is no verdict.
                if index < len(replayed_months) and driven.stop is not None:
                    raise ConfigError(
                        f"the replay differs from the recorded run: {driven.stop.reason}, in "
                        f"{month.label}, which the recorded run finished"
                    )
                evidence = Evidence.gather(application.store)
                # Through `Application.valuation`, never a price map built here (ADR 0027).
                valuation = application.valuation()
                result = _month_result(
                    month,
                    start_equity=carried,
                    planned=planned,
                    evidence=evidence,
                    valuation=valuation,
                    paused_at=driven.paused_at,
                )
                if index < len(replayed_months):
                    _require_same_month(replayed_months[index], result)
                if replayer is not None and index == len(replayed_months) - 1:
                    replayer.require_consumed()
                carried = None if result.unvaluable else result.end_equity
                meta = meta.model_copy(
                    update={
                        "months": (*meta.months, result),
                        "profit": _profit(valuation, evidence, meta=meta),
                        "vetoes": _vetoes(evidence),
                        "incidents": len(evidence.incidents),
                    }
                )
                _write_meta(meta_path, meta)
                if driven.stop is not None:
                    meta = meta.model_copy(
                        update={
                            "ending": _ENDING_OF[driven.stop.cause],
                            "ending_reason": driven.stop.reason,
                        }
                    )
                    # Now, not after the shutdown below: a verdict must survive one that raises.
                    # And no checkpoint, because a month cut short is no last good point.
                    _write_meta(meta_path, meta)
                    break
                write_checkpoint(directory, month.label, meta=meta)
                # After the last month nothing is left to refuse, and so nothing to continue (§5.5).
                # A replayed month spends nothing, and `_continue_from` has already judged them.
                spent = evidence.cost_usd + meta.abandoned_cost_usd
                is_live = index >= len(replayed_months)
                if is_live and index + 1 < len(period.months) and spent > budget:
                    raise BudgetReached(
                        f"budget of {budget} passed at the end of {month.label}: {spent} spent"
                    )
        except BudgetReached as exc:
            meta = meta.model_copy(
                update={
                    "ending": RunEnding.INTERRUPTED,
                    "ending_reason": str(exc),
                    "budget_stopped": True,
                }
            )
            _write_meta(meta_path, meta)
        except Exception as exc:
            _write_meta(
                meta_path,
                meta.model_copy(
                    update={
                        "ending": RunEnding.INTERRUPTED,
                        "ending_reason": f"{type(exc).__name__}: {exc}",
                    }
                ),
            )
            raise
    finally:
        # Nested so a replayer that fails to close cannot leave the wiring open behind it.
        try:
            if replayer is not None:
                await replayer.aclose()
        finally:
            await application.shutdown()

    if meta.ending is RunEnding.RUNNING:
        meta = meta.model_copy(update={"ending": RunEnding.COMPLETED})
    _write_meta(meta_path, meta)
    logger.info(
        "period run finished",
        extra={
            "run_id": identity,
            "period": period.label,
            "ending": meta.ending.value,
            "cycles": sum(month.ran_cycles for month in meta.months),
            "net": str(meta.profit.net),
            "unvaluable": meta.profit.unvaluable,
        },
    )
    return meta
