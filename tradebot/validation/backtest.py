"""The backtest harness: the real loop, over recorded history, with the clock in our hands.

Rung 4 of DESIGN §9, and its purpose is narrow and stated everywhere it can be: **this validates
plumbing and risk behaviour over long horizons, and it is never evidence of alpha.** Every report
carries the banner. The reason is [L12] — the models memorized this period, and no amount of
point-in-time correctness in our code reaches inside their weights (`cutoffs.py`).

What it does exercise, and what nothing else does at this length: hundreds of consecutive cycles
against real prices, so cooldowns, daily caps, loss streaks, TTL expiry, protective legs arming
on a bar that trades through them, and the reconciler running against a venue that has moved all
get thousands of chances to be wrong.

The harness owns time. It steps a `ManualClock` from tick to tick rather than sleeping, so a
year of hourly cycles runs in seconds and lands on exactly the instants the schedule names. Two
things follow, and both are deliberate:

* **the scheduler is bypassed, the worker is not.** Cycles are run through the supervisor's own
  `BasketWorker`, so config re-reads, halts and auto-pause behave exactly as they do in a live
  process. Only the waiting is ours.
* **the monitor is polled between cycles.** In a running process a background poller books fills
  and expires orders; here nothing is running, so the harness drives one poll per step. Without
  it a stop armed by the bar just read would never fill and the backtest would report protective
  orders as if they had never triggered.

Failure semantics: a backtest that cannot start refuses (`FailClosedError`) rather than reporting
an empty run — an empty report reads like a system that did nothing wrong. Once running, a failed
cycle is recorded and counted like any other; the report shows it and the promotion gates would
fail on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from tradebot.app import Application
from tradebot.control.context_builder import DEFAULT_TIMEFRAMES
from tradebot.core.clock import ManualClock, ensure_utc
from tradebot.core.config import Basket
from tradebot.core.enums import ProviderKind
from tradebot.core.errors import ConfigError, FailClosedError
from tradebot.core.logging import get_logger
from tradebot.core.market import timeframe_interval
from tradebot.indicators.library import DEFAULT_INDICATORS, required_history
from tradebot.validation.cutoffs import Contamination, classify_all
from tradebot.validation.evidence import Evidence

logger = get_logger(__name__)

#: Printed at the top of every backtest report, and repeated in the CLI's output. R8 is that
#: someone eventually quotes a backtest as a result; the banner is the cheap defence.
BANNER = (
    "PLUMBING VALIDATION ONLY — NOT ALPHA EVIDENCE. This replays recorded prices through the "
    "real decision, risk and execution path to exercise them over a long horizon. The panel's "
    "models were trained on data from this period, so any PnL here is contaminated by "
    "memorization and means nothing about future performance (DESIGN §9 rung 4, [L12])."
)

#: How many cycles one run may plan before it is treated as a mistake. A year of one-minute
#: cycles is half a million panel runs, which is not a backtest anybody meant to start.
DEFAULT_MAX_CYCLES = 20_000


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """One replayed run: what it covered, what it did, and what it is not evidence of."""

    #: What the operator asked for, and where cycling actually began once the indicators had
    #: the history they need. Both are shown: a report whose window silently differs from the
    #: one that was requested is a report about a different experiment.
    requested_start: datetime
    warmup: timedelta
    window_start: datetime
    window_end: datetime
    finished_at: datetime
    data_source: str
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    panel_models: tuple[str, ...]
    contamination: tuple[Contamination, ...]
    evidence: Evidence
    planned_cycles: int
    banner: str = BANNER

    @property
    def ran_cycles(self) -> int:
        return len(self.evidence.cycles)

    @property
    def skipped_cycles(self) -> int:
        """Scheduled ticks that never ran, because every basket had stopped cycling."""
        return self.planned_cycles - self.ran_cycles


def panel_models(basket: Basket) -> tuple[str, ...]:
    """Every model this basket's panel could reach, offline stubs excluded.

    Fallback bindings count: a seat that spent the run on its backup was answered by that model,
    and a contamination verdict about a model that never ran would be the wrong verdict.
    """
    kinds = {provider.provider_id: provider.kind for provider in basket.panel.providers}
    return tuple(
        binding.model
        for seat in basket.panel.seats
        for binding in seat.bindings
        # An undeclared provider is treated as real: assuming "probably a stub" would drop a
        # model out of the contamination analysis on the strength of a configuration gap.
        if kinds.get(binding.provider_id, ProviderKind.OPENAI_COMPAT) is not ProviderKind.STUB
    )


def warmup_for(baskets: tuple[Basket, ...]) -> timedelta:
    """History an indicator needs behind the first cycle before it can produce a reading.

    Without this the opening cycles of a replay abort as `DATA_STALE` — correctly, since MACD
    genuinely cannot be computed from four bars — and a backtest that starts at the first bar of
    its dataset reports a run of failures that says nothing about the system. The window is
    therefore moved forward by the longest indicator's requirement on the longest timeframe, and
    the report states that it was.
    """
    spans = [
        timeframe_interval(max(basket.timeframes or DEFAULT_TIMEFRAMES, key=timeframe_interval))
        * (required_history(basket.indicators or DEFAULT_INDICATORS) + 1)
        for basket in baskets
    ]
    return max(spans, default=timedelta(0))


def plan_ticks(
    baskets: tuple[Basket, ...], *, start: datetime, end: datetime, limit: int
) -> tuple[tuple[datetime, str], ...]:
    """Every (instant, basket) the schedules name inside the window, in time order.

    Computed from `Schedule.next_tick`, the same function a live scheduler fires on, so a
    backtest cycles on the grid the basket is actually configured for rather than on a cadence
    invented for replay.
    """
    ticks: list[tuple[datetime, str]] = []
    for basket in baskets:
        moment = basket.schedule.next_tick(start)
        while moment <= end:
            ticks.append((moment, basket.basket_id))
            if len(ticks) > limit:
                raise ConfigError(
                    f"this window plans more than {limit} cycles; narrow it or lengthen the "
                    "basket's schedule. A backtest is a plumbing test, not a search"
                )
            moment = basket.schedule.next_tick(moment)
    return tuple(sorted(ticks))


class BacktestHarness:
    """Drives a wired sim application over a historical window."""

    def __init__(
        self,
        application: Application,
        clock: ManualClock,
        *,
        start: datetime,
        end: datetime,
        data_source: str = "",
        warmup: timedelta | None = None,
        max_cycles: int = DEFAULT_MAX_CYCLES,
    ) -> None:
        self._application = application
        self._clock = clock
        self._requested_start = ensure_utc(start)
        self._end = ensure_utc(end)
        self._data_source = data_source
        self._warmup = warmup
        self._max_cycles = max_cycles
        if self._requested_start >= self._end:
            raise ConfigError(
                f"empty window: {self._requested_start.isoformat()} is not before {self._end}"
            )

    async def run(self) -> BacktestReport:
        """Recover, replay every scheduled tick, and report what the log says happened."""
        baskets = self._application.baskets
        warmup = self._warmup if self._warmup is not None else warmup_for(baskets)
        start = self._requested_start + warmup
        if start >= self._end:
            raise ConfigError(
                f"the window is shorter than the {warmup} of history the indicators need before "
                "the first cycle; widen it or record more data"
            )
        ticks = plan_ticks(baskets, start=start, end=self._end, limit=self._max_cycles)

        # The clock starts *at* the window, before recovery: reconciliation and the risk
        # baselines must be established as of the replayed period, not as of today.
        self._clock.set(start)
        recovery = await self._application.recover()
        if recovery.halted:
            raise FailClosedError(
                f"startup recovery halted before the backtest began: {recovery.state.reason}; "
                f"failures: {', '.join(recovery.failures) or 'none recorded'}"
            )

        retired = await self._replay(ticks, len(baskets))
        logger.info("backtest complete", extra={"ticks": len(ticks), "retired": sorted(retired)})

        return self._report(baskets, start=start, warmup=warmup, planned=len(ticks))

    async def _replay(self, ticks: tuple[tuple[datetime, str], ...], baskets: int) -> set[str]:
        """Cycle each tick, and stop once no basket is left that may cycle.

        A basket that auto-paused on a loss streak, or halted after repeated failures, needs a
        human to clear it — and there is no human inside a replay. Continuing to step time would
        add nothing to the log but hours of "not cycling", so the run ends and the report shows
        how much of the window went unused.
        """
        retired: set[str] = set()
        for moment, basket_id in ticks:
            if basket_id in retired:
                continue
            self._clock.set(moment)
            worker = self._application.supervisor.worker_for(basket_id)
            await worker.cycle()
            await self._application.monitor.poll()
            if worker.stopped:
                logger.warning(
                    "basket stopped cycling during the replay",
                    extra={"basket_id": basket_id, "at": moment.isoformat()},
                )
                retired.add(basket_id)
                if len(retired) == baskets:
                    break
        return retired

    def _report(
        self, baskets: tuple[Basket, ...], *, start: datetime, warmup: timedelta, planned: int
    ) -> BacktestReport:
        models = tuple(dict.fromkeys(model for b in baskets for model in panel_models(b)))
        return BacktestReport(
            requested_start=self._requested_start,
            warmup=warmup,
            window_start=start,
            window_end=self._end,
            finished_at=self._clock.now(),
            data_source=self._data_source,
            instruments=tuple(
                dict.fromkeys(i.key for basket in baskets for i in basket.instruments)
            ),
            timeframes=tuple(dict.fromkeys(tf for basket in baskets for tf in basket.timeframes)),
            panel_models=models,
            contamination=classify_all(models, start=start, end=self._end),
            evidence=Evidence.gather(self._application.store),
            planned_cycles=planned,
        )
