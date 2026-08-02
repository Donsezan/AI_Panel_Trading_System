"""Supervision: what keeps cycling, what stops, and what a repeated failure does.

The properties under test are the ones that decide whether a bot left running overnight is safe:

* a **configuration edit** takes effect at the next cycle boundary and never mid-cycle;
* a **paused, retired or halted** basket stops being cycled, and only a human clears a halt;
* a **failing** basket backs off and is halted after N failures rather than crash-looping;
* a cycle that raises **never kills the loop** — a dead supervisor leaves working orders with
  nobody polling them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest

from tradebot.control.basket_runner import CycleResult
from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.scheduler import Scheduler
from tradebot.control.supervisor import Backoff, BasketWorker, Supervisor
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy, Schedule
from tradebot.core.enums import BasketStatus, ConfigKind, CycleOutcome
from tradebot.core.errors import ConfigError, FailClosedError
from tradebot.execution.brokers.calendars import ContinuousCalendar
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.watchdog import Watchdog

ACTOR = "test"

CycleHook = Callable[["ScriptedRunner"], Awaitable[None]]


class ScriptedRunner:
    """A `BasketRunner` stand-in whose every cycle outcome the test dictates."""

    def __init__(self, basket: Basket, on_cycle: CycleHook | None = None) -> None:
        self.basket = basket
        self.on_cycle = on_cycle
        self.calls = 0
        self.concurrent = 0
        self.peak_concurrent = 0
        self.outcomes: list[CycleOutcome | Exception] = []

    async def run_once(self) -> CycleResult:
        self.calls += 1
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        await asyncio.sleep(0)
        self.concurrent -= 1
        if self.on_cycle is not None:
            await self.on_cycle(self)
        outcome = self.outcomes.pop(0) if self.outcomes else CycleOutcome.NO_ACTION
        if isinstance(outcome, Exception):
            raise outcome
        return CycleResult(
            cycle_id=f"c{self.calls}",
            basket_id=self.basket.basket_id,
            outcome=outcome,
            detail="scripted",
        )


class ScriptedFactory:
    """A `RunnerFactory` that hands out `ScriptedRunner`s and records its lifecycle calls."""

    def __init__(self) -> None:
        self.built: list[int] = []
        self.released: list[str] = []
        self.runners: dict[str, ScriptedRunner] = {}
        self.calendar = ContinuousCalendar("sim")
        #: Awaited at the end of every scripted cycle, so a test can change the world mid-loop
        #: without polling for the loop to get there.
        self.on_cycle: CycleHook | None = None
        #: Raised instead of building, standing in for a panel that cannot be wired.
        self.build_error: Exception | None = None

    async def build(self, record: ConfigRecord[Basket]) -> ScriptedRunner:
        if self.build_error is not None:
            raise self.build_error
        self.built.append(record.ref.version)
        runner = ScriptedRunner(record.document, self.on_cycle)
        self.runners[record.ref.config_id] = runner
        return runner

    async def release(self, basket_id: str) -> None:
        self.released.append(basket_id)

    def calendar_for(self, basket: Basket) -> ContinuousCalendar:
        return self.calendar


@pytest.fixture
def states(store: EventStore, clock: ManualClock) -> RiskStateStore:
    return RiskStateStore(store.engine, store._writer, clock)


@pytest.fixture
def watchdog(store: EventStore, states: RiskStateStore, clock: ManualClock) -> Watchdog:
    return Watchdog(GlobalRiskPolicy(), states, store, clock)


@pytest.fixture
def configs(store: EventStore, clock: ManualClock) -> ConfigStore:
    return ConfigStore(store.engine, store._writer, store, clock)


@pytest.fixture
def factory() -> ScriptedFactory:
    return ScriptedFactory()


@pytest.fixture
def supervisor(
    factory: ScriptedFactory,
    configs: ConfigStore,
    clock: ManualClock,
    watchdog: Watchdog,
    states: RiskStateStore,
) -> Supervisor:
    return Supervisor(factory, configs, Scheduler(clock), watchdog, states, clock)


@pytest.fixture
def worker(
    factory: ScriptedFactory,
    configs: ConfigStore,
    clock: ManualClock,
    watchdog: Watchdog,
    states: RiskStateStore,
) -> BasketWorker:
    return BasketWorker(
        "b1",
        factory=factory,
        configs=configs,
        scheduler=Scheduler(clock),
        watchdog=watchdog,
        states=states,
        clock=clock,
        backoff=Backoff(base_seconds=1.0),
        max_consecutive_failures=2,
    )


async def publish(configs: ConfigStore, basket: Basket) -> ConfigRecord[Basket]:
    return await configs.put(basket.basket_id, basket, actor=ACTOR)


class TestBackoff:
    def test_no_failures_means_no_delay(self) -> None:
        assert Backoff().delay(0) == 0.0

    def test_the_delay_doubles_with_each_failure(self) -> None:
        backoff = Backoff(base_seconds=30.0)
        assert [backoff.delay(n) for n in (1, 2, 3)] == [30.0, 60.0, 120.0]

    def test_the_delay_is_capped(self) -> None:
        assert Backoff(base_seconds=30.0, max_seconds=100.0).delay(10) == 100.0


class TestCycling:
    async def test_a_configured_basket_cycles(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)

        result = await worker.cycle()

        assert result is not None
        assert result.outcome is CycleOutcome.NO_ACTION

    async def test_a_basket_that_is_not_configured_does_not_cycle(
        self, worker: BasketWorker
    ) -> None:
        assert await worker.cycle() is None
        assert worker.stopped

    async def test_a_paused_basket_does_not_cycle(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket.model_copy(update={"status": BasketStatus.PAUSED}))

        assert await worker.cycle() is None
        assert worker.stopped

    async def test_a_retired_basket_does_not_cycle(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)
        await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR)

        assert await worker.cycle() is None

    async def test_a_halted_basket_does_not_cycle(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, watchdog: Watchdog
    ) -> None:
        """A halt is the system protecting itself; only a human in the GUI clears it."""
        await publish(configs, basket)
        await watchdog.halt_basket("b1", "recon mismatch")

        assert await worker.cycle() is None

    async def test_an_un_halted_basket_cycles_again(
        self,
        supervisor: Supervisor,
        configs: ConfigStore,
        basket: Basket,
        watchdog: Watchdog,
    ) -> None:
        """Clearing a halt in the database must bring the basket back without a restart."""
        await publish(configs, basket)
        await watchdog.halt_basket("b1", "recon mismatch")
        assert await supervisor.run_once() == ()

        await watchdog.resume_basket("b1", actor="human")

        assert len(await supervisor.run_once()) == 1


class TestConfigurationChanges:
    async def test_a_runner_is_reused_while_the_version_is_unchanged(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        await publish(configs, basket)

        await worker.cycle()
        await worker.cycle()

        assert factory.built == [1]

    async def test_a_new_version_rebuilds_the_runner_at_the_cycle_boundary(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        """An edit takes effect on the next cycle, never inside the one already running."""
        await publish(configs, basket)
        await worker.cycle()

        await publish(configs, basket.model_copy(update={"name": "renamed"}))
        await worker.cycle()

        assert factory.built == [1, 2]
        assert factory.released == ["b1"]

    async def test_stopping_releases_what_the_basket_owned(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        await publish(configs, basket)
        await worker.cycle()

        await worker.stop()

        assert factory.released == ["b1"]


class TestFailureHandling:
    async def test_a_failed_outcome_counts_as_a_failure(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        await publish(configs, basket)
        await worker.cycle()
        factory.runners["b1"].outcomes = [CycleOutcome.FAILED]

        await worker.cycle()

        assert worker.failures == 1

    async def test_a_cycle_that_raises_does_not_kill_the_loop(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        """An unclassified defect is counted and retried; the process keeps polling its orders."""
        await publish(configs, basket)
        await worker.cycle()
        factory.runners["b1"].outcomes = [FailClosedError("boom")]

        assert await worker.cycle() is None
        assert worker.failures == 1
        assert not worker.stopped

    async def test_a_clean_cycle_resets_the_streak(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        await publish(configs, basket)
        await worker.cycle()
        factory.runners["b1"].outcomes = [CycleOutcome.FAILED, CycleOutcome.NO_ACTION]

        await worker.cycle()
        await worker.cycle()

        assert worker.failures == 0

    async def test_repeated_failures_halt_the_basket_for_a_human(
        self,
        worker: BasketWorker,
        configs: ConfigStore,
        basket: Basket,
        factory: ScriptedFactory,
        states: RiskStateStore,
    ) -> None:
        await publish(configs, basket)
        await worker.cycle()
        factory.runners["b1"].outcomes = [CycleOutcome.FAILED, CycleOutcome.FAILED]

        await worker.cycle()
        await worker.cycle()

        assert states.status_of("b1") is BasketStatus.HALTED
        assert worker.stopped

    async def test_a_runner_that_cannot_be_built_counts_as_a_failed_cycle(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        """Building is as fallible as cycling — an unwireable panel, an unknown indicator, an
        absent Tier-2 policy. Escaping here would kill the worker's task, which `serve` silently
        recreates every resync: a crash loop with no backoff and never the auto-halt below."""
        await publish(configs, basket)
        factory.build_error = ConfigError("panel cannot be wired")

        assert await worker.cycle() is None
        assert worker.failures == 1
        assert not worker.stopped

    async def test_repeated_build_failures_halt_the_basket_like_any_other(
        self,
        worker: BasketWorker,
        configs: ConfigStore,
        basket: Basket,
        factory: ScriptedFactory,
        states: RiskStateStore,
    ) -> None:
        await publish(configs, basket)
        factory.build_error = ConfigError("panel cannot be wired")

        await worker.cycle()
        await worker.cycle()

        assert states.status_of("b1") is BasketStatus.HALTED
        assert worker.stopped

    async def test_an_unsatisfiable_schedule_halts_rather_than_guessing(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, states: RiskStateStore
    ) -> None:
        class ShutForever:
            venue_id = "shut"

            async def is_open(self, at: object) -> bool:
                return False

            async def session_day(self, at: object) -> str:
                return "2026-07-30"

            async def next_open(self, after: object) -> None:
                return None

        await publish(configs, basket)
        worker._factory.calendar = ShutForever()  # type: ignore[attr-defined]

        await worker._wait_until_due()

        assert states.status_of("b1") is BasketStatus.HALTED
        assert worker.stopped


class TestSupervision:
    async def test_every_configured_basket_cycles_once(
        self, supervisor: Supervisor, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)
        await publish(configs, basket.model_copy(update={"basket_id": "b2"}))

        results = await supervisor.run_once()

        assert {result.basket_id for result in results} == {"b1", "b2"}

    async def test_a_basket_created_later_is_picked_up(
        self, supervisor: Supervisor, configs: ConfigStore, basket: Basket
    ) -> None:
        """A basket created in the dashboard starts cycling without a restart."""
        await publish(configs, basket)
        await supervisor.run_once()

        await publish(configs, basket.model_copy(update={"basket_id": "b2"}))

        assert len(await supervisor.run_once()) == 2

    async def test_syncing_starts_one_task_per_basket_and_stopping_ends_them(
        self, supervisor: Supervisor, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)
        await publish(configs, basket.model_copy(update={"basket_id": "b2"}))

        supervisor.sync()
        await asyncio.sleep(0)
        assert {worker.basket_id for worker in supervisor.workers} == {"b1", "b2"}

        await supervisor.stop()

        assert all(worker.stopped for worker in supervisor.workers)

    async def test_a_cycle_never_overlaps_itself(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        """One task per basket is what makes this structural rather than a check (PLAN §2.6).

        A tick arriving while a cycle is in flight is skipped rather than queued: a backlog
        would trade on decisions taken against a market that has since moved.
        """
        await publish(configs, basket)

        results = await asyncio.gather(worker.cycle(), worker.cycle())

        assert factory.runners["b1"].peak_concurrent == 1
        assert [result is None for result in results].count(True) == 1

    async def test_serving_supervises_until_stopped(
        self, supervisor: Supervisor, configs: ConfigStore, basket: Basket
    ) -> None:
        await publish(configs, basket)

        serving = asyncio.create_task(supervisor.serve(resync_seconds=60.0))
        await asyncio.sleep(0)
        assert [worker.basket_id for worker in supervisor.workers] == ["b1"]

        await supervisor.stop()

        await asyncio.wait_for(serving, timeout=5)


class TestScheduledLoop:
    async def test_the_loop_cycles_on_schedule_until_the_basket_goes_away(
        self,
        worker: BasketWorker,
        configs: ConfigStore,
        basket: Basket,
        factory: ScriptedFactory,
        clock: ManualClock,
    ) -> None:
        """The loop is what runs unattended; it must stop when its basket is retired."""
        await publish(configs, basket.model_copy(update={"schedule": Schedule(every_seconds=600)}))
        started = clock.now()

        async def retire_after_two(runner: ScriptedRunner) -> None:
            if runner.calls >= 2:
                await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR)

        factory.on_cycle = retire_after_two

        await worker.run()

        assert factory.runners["b1"].calls == 2
        assert clock.now() >= started + timedelta(seconds=1200)
        assert worker.stopped

    async def test_a_basket_retired_while_waiting_stops_its_worker(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, factory: ScriptedFactory
    ) -> None:
        await publish(configs, basket)
        await configs.retire(ConfigKind.BASKET, "b1", actor=ACTOR)

        await worker._wait_until_due()

        assert worker.stopped
        assert factory.runners == {}


class TestScheduling:
    async def test_a_worker_waits_for_its_next_tick_before_cycling(
        self, worker: BasketWorker, configs: ConfigStore, basket: Basket, clock: ManualClock
    ) -> None:
        await publish(configs, basket.model_copy(update={"schedule": Schedule(every_seconds=600)}))
        started = clock.now()

        await worker._wait_until_due()

        assert timedelta() < clock.now() - started <= timedelta(seconds=600)
        assert clock.now().second == 0
