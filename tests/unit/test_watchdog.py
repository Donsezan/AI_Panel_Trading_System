"""The kill switch, the halt, and the baselines they measure against.

Three properties are load-bearing and each is tested directly:

* **A restart never un-halts.** State is persisted, and an unreadable one reads as tripped.
* **Re-arming is a human act.** The typed phrase is required; nothing automatic can supply it.
* **External flows are not PnL.** A withdrawal must never read as a drawdown (R16).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import BasketStatus, KillSwitchState
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.persistence.store import EventStore
from tradebot.risk.state import REARM_PHRASE, RiskState, RiskStateStore, assert_rearm_phrase
from tradebot.risk.watchdog import DAILY_LOSS_RULE, DRAWDOWN_RULE, Watchdog

START_EQUITY = Decimal(10_000)


@pytest.fixture
def states(store: EventStore, clock: ManualClock) -> RiskStateStore:
    return RiskStateStore(store.engine, store._writer, clock)


@pytest.fixture
def watchdog(store: EventStore, states: RiskStateStore, clock: ManualClock) -> Watchdog:
    return Watchdog(GlobalRiskPolicy(), states, store, clock)


async def armed(watchdog: Watchdog, equity: Decimal = START_EQUITY) -> RiskState:
    return await watchdog.rearm(equity, actor="test")


class TestPersistence:
    def test_an_uninitialised_system_reads_as_tripped(self, states: RiskStateStore) -> None:
        """Fail closed: an absent row is 'we do not know', which is never 'go ahead'."""
        state = states.load()

        assert state.kill_switch is KillSwitchState.TRIPPED
        assert not state.may_trade

    async def test_a_trip_survives_a_new_reader(
        self, watchdog: Watchdog, states: RiskStateStore
    ) -> None:
        await armed(watchdog)
        await watchdog.trip("manual", "operator pressed the button")

        assert states.load().kill_switch is KillSwitchState.TRIPPED

    async def test_a_halted_basket_survives_a_new_reader(
        self, watchdog: Watchdog, states: RiskStateStore
    ) -> None:
        await watchdog.halt_basket("b1", "reconciliation mismatch")

        assert states.halted_baskets() == {"b1": "reconciliation mismatch"}
        assert states.status_of("b1") is BasketStatus.HALTED

    async def test_an_unlisted_basket_is_active(self, states: RiskStateStore) -> None:
        assert states.status_of("never-seen") is BasketStatus.ACTIVE


class TestTriggers:
    async def test_a_drawdown_past_the_limit_trips_the_switch(self, watchdog: Watchdog) -> None:
        await armed(watchdog)

        verdict = await watchdog.check(Decimal("8500"))  # 15% below the mark, limit 10%

        assert verdict.tripped
        assert not verdict.may_trade
        assert DRAWDOWN_RULE in verdict.state.reason

    async def test_a_drawdown_inside_the_limit_does_not(self, watchdog: Watchdog) -> None:
        await armed(watchdog)

        # Inside both baselines: 2.5% below the mark (limit 10%) and below day start (limit 3%).
        verdict = await watchdog.check(Decimal("9750"))

        assert not verdict.tripped
        assert verdict.may_trade

    async def test_a_daily_loss_halts_orders_without_tripping_the_switch(
        self, watchdog: Watchdog, store: EventStore
    ) -> None:
        """A lesser response on purpose: existing protective legs keep doing their job."""
        await armed(watchdog)

        verdict = await watchdog.check(Decimal("9600"))  # 4% below day start, limit 3%

        assert verdict.day_halted
        assert not verdict.tripped
        assert not verdict.may_trade
        assert verdict.state.kill_switch is KillSwitchState.ARMED
        rules = [e.payload["rule"] for e in store.read_all() if e.type is EventType.RISK_EVENT]
        assert DAILY_LOSS_RULE in rules

    async def test_a_manual_trip_stops_everything(self, watchdog: Watchdog) -> None:
        await armed(watchdog)

        state = await watchdog.trip("manual", "operator pressed the button")

        assert state.kill_switch is KillSwitchState.TRIPPED

    async def test_tripping_twice_is_a_no_op(self, watchdog: Watchdog, store: EventStore) -> None:
        """The first reason is the true one; a second trip must not overwrite it."""
        await armed(watchdog)
        await watchdog.trip("first", "the real reason")
        await watchdog.trip("second", "a later symptom")

        changes = [e for e in store.read_all() if e.type is EventType.KILL_SWITCH_CHANGED]
        assert sum(1 for e in changes if e.payload["state"] == "tripped") == 1

    async def test_a_tripped_switch_short_circuits_the_next_check(self, watchdog: Watchdog) -> None:
        await armed(watchdog)
        await watchdog.trip("manual", "stopped")

        verdict = await watchdog.check(Decimal("20000"))

        assert not verdict.may_trade


class TestHighWaterMark:
    async def test_the_mark_rises_with_equity(self, watchdog: Watchdog) -> None:
        await armed(watchdog)

        verdict = await watchdog.check(Decimal("12000"))

        assert verdict.state.high_water_mark == Decimal("12000")

    async def test_the_mark_never_falls(self, watchdog: Watchdog) -> None:
        """A mark that follows equity down would make drawdown unmeasurable."""
        await armed(watchdog)
        await watchdog.check(Decimal("12000"))

        verdict = await watchdog.check(Decimal("11500"))

        assert verdict.state.high_water_mark == Decimal("12000")

    async def test_a_gain_after_a_dip_still_measures_from_the_peak(
        self, watchdog: Watchdog
    ) -> None:
        await armed(watchdog)
        await watchdog.check(Decimal("12000"))

        verdict = await watchdog.check(Decimal("10700"))  # ~10.8% below the peak

        assert verdict.tripped


class TestExternalFlows:
    async def test_a_withdrawal_lowers_both_baselines(self, watchdog: Watchdog) -> None:
        """Otherwise withdrawing your own money reads as a drawdown and stops trading (R16)."""
        await armed(watchdog)

        await watchdog.record_flow(Decimal("-3000"), "withdrawal")
        verdict = await watchdog.check(Decimal("7000"))

        assert not verdict.tripped
        assert verdict.state.high_water_mark == Decimal("7000")

    async def test_a_deposit_raises_both_baselines(self, watchdog: Watchdog) -> None:
        """Otherwise a deposit masks a real loss."""
        await armed(watchdog)

        await watchdog.record_flow(Decimal("5000"), "deposit")
        verdict = await watchdog.check(Decimal("13500"))  # 10% below the adjusted 15000

        assert verdict.state.day_start_equity == Decimal("15000")
        assert verdict.day_halted

    async def test_a_flow_larger_than_the_baseline_does_not_go_negative(
        self, watchdog: Watchdog
    ) -> None:
        await armed(watchdog)

        state = await watchdog.record_flow(Decimal("-99999"), "everything withdrawn")

        assert state.high_water_mark == Decimal(0)


class TestDayBoundary:
    async def test_the_daily_baseline_resets_on_a_new_utc_day(
        self, watchdog: Watchdog, clock: ManualClock
    ) -> None:
        await armed(watchdog)
        await watchdog.check(Decimal("9800"))

        clock.advance(60 * 60 * 24)
        verdict = await watchdog.check(Decimal("9800"))

        assert verdict.state.day_start_equity == Decimal("9800")
        assert not verdict.day_halted


class TestRearming:
    def test_the_phrase_is_required(self) -> None:
        with pytest.raises(ConfigError, match=REARM_PHRASE):
            assert_rearm_phrase("please")

    def test_the_exact_phrase_is_accepted(self) -> None:
        assert_rearm_phrase(REARM_PHRASE)

    async def test_rearming_restarts_the_baselines_from_current_equity(
        self, watchdog: Watchdog
    ) -> None:
        """Otherwise the next sweep re-trips on the same drawdown the operator just reviewed."""
        await armed(watchdog)
        await watchdog.check(Decimal("8000"))

        state = await watchdog.rearm(Decimal("8000"), actor="cli")

        assert state.kill_switch is KillSwitchState.ARMED
        assert state.high_water_mark == Decimal("8000")

    async def test_rearming_is_recorded_with_its_actor(
        self, watchdog: Watchdog, store: EventStore
    ) -> None:
        await watchdog.rearm(Decimal("8000"), actor="cli")

        event = next(e for e in store.read_all() if e.type is EventType.KILL_SWITCH_CHANGED)
        assert event.payload["actor"] == "cli"
        assert event.payload["state"] == "armed"

    async def test_a_halted_basket_can_be_resumed_by_a_human(
        self, watchdog: Watchdog, states: RiskStateStore
    ) -> None:
        await watchdog.halt_basket("b1", "mismatch")

        await watchdog.resume_basket("b1", actor="cli")

        assert states.halted_baskets() == {}


class TestBaselineEdges:
    def test_drawdown_is_zero_before_a_mark_exists(self, clock: ManualClock) -> None:
        """A percentage of nothing is not a breach; it is an unarmed system."""
        state = RiskState(updated_at=clock.now())

        assert state.drawdown_pct(Decimal("5000")) == Decimal(0)
        assert state.daily_loss_pct(Decimal("5000")) == Decimal(0)

    def test_a_gain_reads_as_zero_loss_not_a_negative_one(self, clock: ManualClock) -> None:
        state = RiskState(
            high_water_mark=Decimal("10000"),
            day_start_equity=Decimal("10000"),
            updated_at=clock.now(),
        )

        assert state.drawdown_pct(Decimal("12000")) == Decimal(0)
