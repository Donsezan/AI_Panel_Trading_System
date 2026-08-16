"""The kill switch, the halt, and the baselines they measure against.

Three properties are load-bearing and each is tested directly:

* **A restart never un-halts.** State is persisted, and an unreadable one reads as tripped.
* **Re-arming is a human act.** The typed phrase is required; nothing automatic can supply it.
* **External flows are not PnL.** A withdrawal must never read as a drawdown (R16).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import BasketStatus, KillSwitchState
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventType
from tradebot.core.money import ZERO
from tradebot.ledger.portfolio import ExternalFlow
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import PortfolioAggregate
from tradebot.risk.state import REARM_PHRASE, RiskState, RiskStateStore, assert_rearm_phrase
from tradebot.risk.watchdog import DAILY_LOSS_RULE, DRAWDOWN_RULE, Watchdog

START_EQUITY = Decimal(10_000)
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def valued(equity: str, *, frozen: str = "") -> PortfolioAggregate:
    """The aggregate the watchdog now reads, rather than a bare number.

    The seam this phase fixed is *what the caller passes*, not the arithmetic here — so these
    tests state an equity directly and `tests/scenario/` asserts that a real cycle computes it
    mark-to-market (PHASE_12 §1.5).
    """
    return PortfolioAggregate(
        equity=Decimal(equity),
        cash=Decimal(equity),
        gross_exposure=ZERO,
        frozen_reason=frozen,
        as_of=NOW,
    )


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

        verdict = await watchdog.check(valued("8500"))  # 15% below the mark, limit 10%

        assert verdict.tripped
        assert not verdict.may_trade
        assert DRAWDOWN_RULE in verdict.state.reason

    async def test_a_drawdown_inside_the_limit_does_not(self, watchdog: Watchdog) -> None:
        await armed(watchdog)

        # Inside both baselines: 2.5% below the mark (limit 10%) and below day start (limit 3%).
        verdict = await watchdog.check(valued("9750"))

        assert not verdict.tripped
        assert verdict.may_trade

    async def test_a_daily_loss_halts_orders_without_tripping_the_switch(
        self, watchdog: Watchdog, store: EventStore
    ) -> None:
        """A lesser response on purpose: existing protective legs keep doing their job."""
        await armed(watchdog)

        verdict = await watchdog.check(valued("9600"))  # 4% below day start, limit 3%

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

        verdict = await watchdog.check(valued("20000"))

        assert not verdict.may_trade


class TestHighWaterMark:
    async def test_the_mark_rises_with_equity(self, watchdog: Watchdog) -> None:
        await armed(watchdog)

        verdict = await watchdog.check(valued("12000"))

        assert verdict.state.high_water_mark == Decimal("12000")

    async def test_the_mark_never_falls(self, watchdog: Watchdog) -> None:
        """A mark that follows equity down would make drawdown unmeasurable."""
        await armed(watchdog)
        await watchdog.check(valued("12000"))

        verdict = await watchdog.check(valued("11500"))

        assert verdict.state.high_water_mark == Decimal("12000")

    async def test_a_gain_after_a_dip_still_measures_from_the_peak(
        self, watchdog: Watchdog
    ) -> None:
        await armed(watchdog)
        await watchdog.check(valued("12000"))

        verdict = await watchdog.check(valued("10700"))  # ~10.8% below the peak

        assert verdict.tripped


class TestExternalFlows:
    async def test_a_withdrawal_lowers_both_baselines(self, watchdog: Watchdog) -> None:
        """Otherwise withdrawing your own money reads as a drawdown and stops trading (R16)."""
        await armed(watchdog)

        await watchdog.record_flow(
            ExternalFlow(currency="USDT", amount=Decimal("-3000"), reason="withdrawal")
        )
        verdict = await watchdog.check(valued("7000"))

        assert not verdict.tripped
        assert verdict.state.high_water_mark == Decimal("7000")

    async def test_a_deposit_raises_both_baselines(self, watchdog: Watchdog) -> None:
        """Otherwise a deposit masks a real loss."""
        await armed(watchdog)

        await watchdog.record_flow(
            ExternalFlow(currency="USDT", amount=Decimal("5000"), reason="deposit")
        )
        verdict = await watchdog.check(valued("13500"))  # 10% below the adjusted 15000

        assert verdict.state.day_start_equity == Decimal("15000")
        assert verdict.day_halted

    async def test_a_flow_larger_than_the_baseline_does_not_go_negative(
        self, watchdog: Watchdog
    ) -> None:
        await armed(watchdog)

        state = await watchdog.record_flow(
            ExternalFlow(currency="USDT", amount=Decimal("-99999"), reason="everything withdrawn")
        )

        assert state.high_water_mark == Decimal(0)


class TestDayBoundary:
    async def test_the_daily_baseline_resets_on_a_new_utc_day(
        self, watchdog: Watchdog, clock: ManualClock
    ) -> None:
        await armed(watchdog)
        await watchdog.check(valued("9800"))

        clock.advance(60 * 60 * 24)
        verdict = await watchdog.check(valued("9800"))

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
        await watchdog.check(valued("8000"))

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


class TestConcurrency:
    async def test_concurrent_checks_do_not_lose_a_high_water_raise(
        self, watchdog: Watchdog, states: RiskStateStore
    ) -> None:
        """Load-compare-save from N basket tasks plus the sweep.

        `SingleWriter` serializes the *write*; it does not make read-compare-write atomic, so two
        cycles raising the mark could each read the old value and the higher one be lost. The
        sweep added an N+1th caller on a fixed cadence and made the interleaving routine rather
        than incidental — and the row this guards is the kill switch (PHASE_12 §3.8).
        """
        await armed(watchdog)

        # Descending, deliberately. Ascending passes even unlocked: each caller reads the stale
        # mark, but the highest happens to write last, so the right answer survives by luck of
        # scheduling. Descending makes the *lowest* write last, which is exactly the lost update.
        await asyncio.gather(*(watchdog.check(valued(str(10_000 + n))) for n in range(20, 0, -1)))

        assert states.load().high_water_mark == Decimal(10_020)
