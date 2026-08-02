"""`SupervisionController`: what starts trading, what stops it, and what refuses (ADR 0021).

The object exists so that "is anything cycling right now" has one answer. These assert the three
properties that make that answer trustworthy: a start is refused unless every precondition holds
*at that moment*, a stop actually tears the supervisor down rather than leaving a second loop
alive, and neither ever raises into a caller that is a web request handler.

The supervisor's own work is substituted throughout. What is under test is the task's lifetime,
not the cycling inside it — and a `ManualClock`, whose `sleep` returns immediately, would turn a
real `Supervisor.serve()` into an unbounded loop underneath every assertion here.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tradebot.app import Application
from tradebot.control.startup import Recovery
from tradebot.control.supervision import SupervisionController
from tradebot.core.enums import KillSwitchState


async def idle() -> None:
    await asyncio.Event().wait()


@pytest.fixture
async def recovered(sim_application: Application) -> Application:
    """A system that has been through DESIGN §8.2 — the only state supervision may start from."""
    await sim_application.recover()
    return sim_application


@pytest.fixture
async def controller(recovered: Application) -> SupervisionController:
    return SupervisionController(recovered, serve=idle)


class TestStartAndStop:
    async def test_a_fresh_controller_is_not_running(
        self, controller: SupervisionController
    ) -> None:
        """Nothing cycles until someone asks for it, in every mode."""
        assert not controller.running

    async def test_start_then_stop(self, controller: SupervisionController) -> None:
        assert await controller.start() == ()
        assert controller.running

        await controller.stop()

        assert not controller.running

    async def test_starting_twice_leaves_one_task(self, controller: SupervisionController) -> None:
        """Two loops against one `Supervisor` would cycle a basket from two code paths."""
        await controller.start()
        first = asyncio.all_tasks()

        assert await controller.start() == ()
        assert len([task for task in asyncio.all_tasks() if task.get_name() == "supervisor"]) == 1
        assert first & asyncio.all_tasks()
        await controller.stop()

    async def test_stopping_when_stopped_is_a_no_op(
        self, controller: SupervisionController
    ) -> None:
        """An operator reaching for Stop during an incident is never argued with."""
        await controller.stop()

        assert not controller.running

    async def test_a_restart_is_a_new_task(self, controller: SupervisionController) -> None:
        await controller.start()
        await controller.stop()

        assert await controller.start() == ()
        assert controller.running
        await controller.stop()

    async def test_stop_cancels_the_real_supervisor(self, recovered: Application) -> None:
        """Cancellation is what routes through `Supervisor.serve`'s own `finally`, which is what
        releases the runners — calling `stop` from outside it would not."""
        controller = SupervisionController(recovered)

        await controller.start()
        await asyncio.sleep(0)
        await controller.stop()

        assert not controller.running
        assert all(worker.stopped for worker in recovered.supervisor.workers)


class TestRefusals:
    async def test_an_unrecovered_process_may_not_start(self, sim_application: Application) -> None:
        """A database that has not been through DESIGN §8.2 reads as never-armed, and stays that
        way: supervision may never precede recovery."""
        controller = SupervisionController(sim_application, serve=idle)

        unmet = await controller.start()

        assert not controller.running
        assert any("kill switch" in reason for reason in unmet)

    async def test_a_halted_recovery_blocks_every_start(self, recovered: Application) -> None:
        """DESIGN §8.2 step 5: the process stays up, says why, and nothing trades."""
        controller = SupervisionController(
            recovered,
            recovery=Recovery(
                state=recovered.states.load(), failures=("reconciliation failed: dust",)
            ),
            serve=idle,
        )

        unmet = await controller.start()

        assert not controller.running
        assert unmet == ("startup recovery: reconciliation failed: dust",)

    async def test_a_tripped_kill_switch_blocks_a_start(
        self, controller: SupervisionController, recovered: Application
    ) -> None:
        """The switch stops the bot trading; only a typed re-arm restores that, never a Start."""
        await recovered.watchdog.trip("manual", "tripped by hand")

        unmet = await controller.start()

        assert not controller.running
        assert any(KillSwitchState.TRIPPED.value in reason for reason in unmet)

    async def test_re_arming_clears_the_refusal(
        self, controller: SupervisionController, recovered: Application
    ) -> None:
        await recovered.watchdog.trip("manual", "tripped by hand")
        await recovered.watchdog.rearm(Decimal(10_000), actor="test")

        assert await controller.start() == ()
        await controller.stop()

    async def test_blockers_are_reported_before_anyone_clicks_start(
        self, sim_application: Application
    ) -> None:
        """The Control page renders these, so an operator fixes the list rather than guessing."""
        controller = SupervisionController(sim_application, serve=idle)

        assert controller.blockers()
        assert not controller.running


class TestLimitsAreRecorded:
    async def test_starting_records_the_limits_in_force(self, recovered: Application) -> None:
        """Sim clamps nothing, so nothing is written — an event per start that always says the
        same thing is an event nobody reads. The live case is in `test_live_wiring.py`."""
        controller = SupervisionController(recovered, serve=idle)
        before = len(list(recovered.store.read_all()))

        await controller.start()

        assert len(list(recovered.store.read_all())) == before
        await controller.stop()
