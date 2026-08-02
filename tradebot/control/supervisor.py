"""The supervisor: one task per configured basket, and what happens when one keeps failing.

Each basket gets a `BasketWorker` that owns its loop — wait until due, run one cycle, account for
the result — and nothing else runs that basket's cycles. That is what makes "no position is
mutated from two code paths" (PLAN §2.6) structural rather than a convention: a cycle cannot
overlap itself, because the only thing that could start the next one is the task still running
the last one.

Three responsibilities, all from DESIGN §6.1:

* **Start and stop with configuration.** A worker re-reads its basket from the `ConfigStore` at
  every cycle boundary, so a limit edited in the dashboard takes effect on the next cycle and
  never mid-cycle. A basket that is paused, retired, or halted stops being cycled.
* **Back off after a failure.** Failures are retried on a deterministic exponential delay. No
  jitter here on purpose: the venue-facing retries that need jitter already have it in the
  transports, and a supervisor whose timing is reproducible is one whose behaviour under failure
  can be tested.
* **Auto-halt after N consecutive failures.** A repeatedly failing basket is halted for human
  review and stops cycling. Only a human clears it (`tradebot risk unhalt`), because a bot that
  un-halts itself is a bot that crash-loops into the same broken market all night.

Failure semantics: a worker never propagates a cycle's failure. `BasketRunner.run_once` already
converts every classified error into a recorded outcome; anything that still escapes is an
unclassified defect, and it is counted, logged and retried under the same backoff rather than
being allowed to kill the process — a dead supervisor leaves working orders with nobody polling
them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from tradebot.control.basket_runner import BasketRunner, CycleResult
from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.scheduler import Scheduler
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.enums import ConfigKind, CycleOutcome
from tradebot.core.errors import TradebotError
from tradebot.core.logging import correlate, get_logger
from tradebot.interfaces.broker import TradingCalendar
from tradebot.risk.state import RiskStateStore
from tradebot.risk.watchdog import Watchdog

logger = get_logger(__name__)

DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
#: How often `serve` looks for baskets that have been created since it started. Existing baskets'
#: *edits* need no sweep — their own worker re-reads them at its next cycle boundary.
DEFAULT_RESYNC_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Backoff:
    """Deterministic exponential delay between failed cycles."""

    base_seconds: float = 30.0
    factor: float = 2.0
    max_seconds: float = 900.0

    def delay(self, failures: int) -> float:
        """Seconds to wait after `failures` consecutive failures. Zero when there are none."""
        if failures <= 0:
            return 0.0
        return min(self.base_seconds * self.factor ** (failures - 1), self.max_seconds)


#: The default retry ladder: 30s, 60s, 120s, ... capped at 15 minutes.
DEFAULT_BACKOFF = Backoff()


class RunnerFactory(Protocol):
    """Builds the runner for one basket, and releases what that basket alone owns.

    Implemented by the composition root, the only place allowed to name concrete adapters. The
    supervisor needs a runner per basket *version*; it must not have to know that building one
    means opening HTTP connections to a panel's providers.
    """

    async def build(self, record: ConfigRecord[Basket]) -> BasketRunner: ...

    async def release(self, basket_id: str) -> None:
        """Free whatever `build` opened for this basket. A no-op when nothing was built."""
        ...

    def calendar_for(self, basket: Basket) -> TradingCalendar:
        """The calendar governing when this basket may cycle."""
        ...


class BasketWorker:
    """Runs one basket's cycles, in sequence, for as long as it is allowed to."""

    def __init__(
        self,
        basket_id: str,
        *,
        factory: RunnerFactory,
        configs: ConfigStore,
        scheduler: Scheduler,
        watchdog: Watchdog,
        states: RiskStateStore,
        clock: Clock,
        backoff: Backoff = DEFAULT_BACKOFF,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self.basket_id = basket_id
        self._factory = factory
        self._configs = configs
        self._scheduler = scheduler
        self._watchdog = watchdog
        self._states = states
        self._clock = clock
        self._backoff = backoff
        self._max_failures = max_consecutive_failures
        self._runner: BasketRunner | None = None
        self._version = 0
        self._failures = 0
        self._stopped = False
        self._running = False

    @property
    def failures(self) -> int:
        """Consecutive failed cycles. Reset by any cycle that completes."""
        return self._failures

    @property
    def stopped(self) -> bool:
        """Whether this worker has retired itself — halted, paused, or no longer configured."""
        return self._stopped

    async def cycle(self) -> CycleResult | None:
        """Run one cycle if the basket is runnable right now. `None` means it was not.

        A tick that arrives while the previous cycle is still running is **skipped, not queued**
        (DESIGN §6.1). Queueing would let a slow cycle build a backlog that then trades on stale
        decisions, and two overlapping cycles would mutate one basket's orders from two paths.
        """
        if self._running:
            logger.info("previous cycle still running; skipping", extra={"basket": self.basket_id})
            return None
        record = self._runnable()
        if record is None:
            return None
        self._running = True
        try:
            return await self._cycle(record)
        finally:
            self._running = False

    async def _cycle(self, record: ConfigRecord[Basket]) -> CycleResult | None:
        with correlate(basket_id=self.basket_id):
            try:
                # Inside the guard, because building is as fallible as cycling — a panel that
                # cannot be wired or an unknown indicator would otherwise escape as a dead task,
                # which `serve` silently recreates every resync: a crash loop with no backoff,
                # no failure count, and never the auto-halt this exists to reach.
                runner = await self._runner_for(record)
                result = await runner.run_once()
            # An unclassified defect must not kill the loop: a dead supervisor leaves
            # working orders with nobody polling them.
            except Exception as exc:
                logger.exception("cycle raised", extra={"basket_id": self.basket_id})
                await self._failed(f"{type(exc).__name__}: {exc}")
                return None
        if result.outcome is CycleOutcome.FAILED:
            await self._failed(result.detail)
        else:
            self._failures = 0
        return result

    async def run(self) -> None:
        """Cycle on schedule until the basket stops being runnable, or the task is cancelled."""
        while not self._stopped:
            await self._wait_until_due()
            if self._stopped:
                return
            await self.cycle()
            await self._clock.sleep(self._backoff.delay(self._failures))

    async def stop(self) -> None:
        """Retire this worker and release what its basket owned."""
        self._stopped = True
        self._runner = None
        await self._factory.release(self.basket_id)

    async def _wait_until_due(self) -> None:
        """Sleep until this basket is next due, on its own venue's calendar."""
        record = self._configs.latest(ConfigKind.BASKET, self.basket_id)
        if record is None or not record.usable:
            await self.stop()
            return
        basket: Basket = record.document
        try:
            due = await self._scheduler.next_fire(
                basket.schedule, self._factory.calendar_for(basket), after=self._clock.now()
            )
        except TradebotError as exc:
            await self._halt(f"schedule cannot be satisfied: {exc}")
            return
        await self._scheduler.wait_until(due)

    def _runnable(self) -> ConfigRecord[Basket] | None:
        """This basket's current version, if it may cycle right now.

        Read fresh every cycle: this is where a dashboard edit takes effect, and where a basket
        paused or halted between cycles stops being run (DESIGN §6.10). The persisted halt is
        checked separately from the configured status because only one of them is the operator's
        intent — the other is the system protecting itself.
        """
        record = self._configs.latest(ConfigKind.BASKET, self.basket_id)
        if record is None or not record.usable:
            self._stopped = True
            return None
        basket: Basket = record.document
        if not basket.status.may_trade:
            logger.info(
                "basket is not active; not cycling",
                extra={"basket_id": self.basket_id, "status": basket.status.value},
            )
            self._stopped = True
            return None
        if not self._states.status_of(self.basket_id).may_trade:
            logger.warning("basket is halted; not cycling", extra={"basket_id": self.basket_id})
            self._stopped = True
            return None
        return record

    async def _runner_for(self, record: ConfigRecord[Basket]) -> BasketRunner:
        """The runner for this basket version, rebuilt when the version has moved on."""
        if self._runner is not None and self._version == record.ref.version:
            return self._runner
        if self._runner is not None:
            await self._factory.release(self.basket_id)
        self._runner = await self._factory.build(record)
        self._version = record.ref.version
        logger.info(
            "runner built",
            extra={"basket_id": self.basket_id, "config_version": record.ref.version},
        )
        return self._runner

    async def _failed(self, detail: str) -> None:
        """Count a failure, and halt the basket once it has failed too many times running."""
        self._failures += 1
        logger.warning(
            "cycle failed",
            extra={
                "basket_id": self.basket_id,
                "failures": self._failures,
                "limit": self._max_failures,
                "detail": detail,
            },
        )
        if self._failures >= self._max_failures:
            await self._halt(f"{self._failures} consecutive failed cycles: {detail}")

    async def _halt(self, reason: str) -> None:
        await self._watchdog.halt_basket(self.basket_id, reason)
        await self.stop()


class Supervisor:
    """Keeps one worker per configured basket, and is how the process runs cycles at all."""

    def __init__(
        self,
        factory: RunnerFactory,
        configs: ConfigStore,
        scheduler: Scheduler,
        watchdog: Watchdog,
        states: RiskStateStore,
        clock: Clock,
        *,
        backoff: Backoff = DEFAULT_BACKOFF,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self._factory = factory
        self._configs = configs
        self._scheduler = scheduler
        self._watchdog = watchdog
        self._states = states
        self._clock = clock
        self._backoff = backoff
        self._max_failures = max_consecutive_failures
        self._workers: dict[str, BasketWorker] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._serving = False

    @property
    def workers(self) -> tuple[BasketWorker, ...]:
        return tuple(self._workers.values())

    def baskets(self) -> tuple[ConfigRecord[Basket], ...]:
        """Baskets in service, newest version of each."""
        return self._configs.baskets()

    def worker_for(self, basket_id: str) -> BasketWorker:
        """The worker owning `basket_id`.

        A retired worker is replaced rather than reused, so a human un-halting a basket brings it
        back without a restart — the halt is cleared in the database, and the next sweep builds a
        worker that reads that.
        """
        worker = self._workers.get(basket_id)
        if worker is None or (worker.stopped and self._idle(basket_id)):
            worker = BasketWorker(
                basket_id,
                factory=self._factory,
                configs=self._configs,
                scheduler=self._scheduler,
                watchdog=self._watchdog,
                states=self._states,
                clock=self._clock,
                backoff=self._backoff,
                max_consecutive_failures=self._max_failures,
            )
            self._workers[basket_id] = worker
        return worker

    async def run_once(self) -> tuple[CycleResult, ...]:
        """One cycle for every runnable basket, in sequence.

        What `--once` runs and what the scenario suite drives. Sequential rather than concurrent so
        a single-shot run is a determined fact: two baskets racing for the same Tier-2 headroom
        would make the outcome depend on task scheduling.
        """
        results = []
        for record in self.baskets():
            result = await self.worker_for(record.ref.config_id).cycle()
            if result is not None:
                results.append(result)
        return tuple(results)

    async def serve(self, *, resync_seconds: float = DEFAULT_RESYNC_SECONDS) -> None:
        """Run every basket on its own schedule until stopped or cancelled."""
        self._serving = True
        try:
            while self._serving:
                self.sync()
                await self._clock.sleep(resync_seconds)
        finally:
            await self.stop()

    def sync(self) -> None:
        """Start a task for every basket in service; forget the tasks that have finished."""
        for basket_id in [key for key, task in self._tasks.items() if task.done()]:
            del self._tasks[basket_id]
        for record in self.baskets():
            basket_id = record.ref.config_id
            if basket_id not in self._tasks:
                self._tasks[basket_id] = asyncio.create_task(
                    self.worker_for(basket_id).run(), name=f"basket-{basket_id}"
                )

    async def stop(self) -> None:
        """Cancel every worker task and release what the workers held. Idempotent."""
        self._serving = False
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        for worker in self.workers:
            await worker.stop()

    def _idle(self, basket_id: str) -> bool:
        task = self._tasks.get(basket_id)
        return task is None or task.done()
