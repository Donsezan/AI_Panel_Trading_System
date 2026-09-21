"""Rung 3: a venue-held stop against a position that moved under it.

Two gaps live in this seam and neither is reachable from an ordinary scenario, so both are
scripted here off the same crashing series.

**KNOWN_GAPS §4** survived hundreds of backtests and the whole scenario suite because the `stub`
panel never takes a partial exit — every prior scenario either held or closed in full. The first
test scripts the sequence that reaches it.

**KNOWN_GAPS §5** is the other half of the same fact: a venue-held stop fires *between* cycles by
construction (ADR 0004), and until the supervisor's sweep polled, the only thing that ever polled
was a cycle which had itself just placed an order. The second test runs the real supervision loop
over a basket that is not cycling at all.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from tests.conftest import SERIES_START, TIMEFRAMES
from tests.scenario.harness import Harness

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.scheduler import Scheduler
from tradebot.control.supervisor import Supervisor
from tradebot.core.clock import ManualClock
from tradebot.core.config import Basket, GlobalRiskPolicy
from tradebot.core.enums import CycleOutcome, OrderRole, OrderState
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, timeframe_interval
from tradebot.core.money import ZERO
from tradebot.decision.providers import DEFAULT_RESPONSE
from tradebot.execution.brokers.calendars import ContinuousCalendar
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles

pytestmark = pytest.mark.scenario

PARTIAL_SELL = """{
  "action": "SELL", "conviction": 5, "size_hint": "quarter",
  "thesis": "Take a quarter off.", "key_risks": [], "invalidation": "n/a"
}"""
HOLD_RESPONSE = """{
  "action": "HOLD", "conviction": 3, "size_hint": "none",
  "thesis": "Nothing to do.", "key_risks": [], "invalidation": "n/a"
}"""

#: How much of each timeframe's series is generated before the crash bar. `TIMEFRAMES` spans
#: three very different bar durations off the same `SERIES_START` — 200 daily bars (the shared
#: `market_data` fixture's count) need 200 days, by which point 200 hourly bars are eight
#: months stale and would already include whatever came after them. A single elapsed-time
#: cutoff cannot show "the rising walk, not yet the crash" on all three timeframes at once
#: unless each one is given just enough history to clear EMA50's warm-up (50 bars) — so all
#: three run out of rising data at the *same instant*, and the crash bar appended after each one
#: stays hidden until the clock is deliberately walked into it.
HISTORY_DAYS = 55

#: Far past the low-hundreds stop distance a 2×ATR(14) stop sits at on this series — the point
#: is to cross the trigger by a wide, unambiguous margin, not to find the exact edge.
CRASH_DEPTH = Decimal("6000")


def _bar_count(timeframe: str, days: int) -> int:
    """How many bars of `timeframe` fit in `days` — the walk length that clears the warm-up."""
    return timedelta(days=days) // timeframe_interval(timeframe)


@pytest.fixture
def crashing_market(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    """A rising series long enough to warm up every timeframe, then one bar that falls through
    the entry's stop on every timeframe at once.

    The shared `market_data` fixture only ever rises, which is why no scenario has fired a stop
    against a *reduced* position before. The clock is pinned to the instant every timeframe's
    rising walk ends, so cycles run against exactly that data until the test advances it onto
    the appended crash bar (see `HISTORY_DAYS` above for why the walk is sized the way it is).
    """
    clock.set(SERIES_START + timedelta(days=HISTORY_DAYS))
    series: dict[tuple[str, str], tuple[Candle, ...]] = {}
    for timeframe in TIMEFRAMES:
        rising = synthetic_candles(
            start=SERIES_START,
            timeframe=timeframe,
            count=_bar_count(timeframe, HISTORY_DAYS),
            open_price=Decimal("50000"),
            step=Decimal("25"),
        )
        last = rising[-1]
        duration = last.close_time - last.open_time
        series[(instrument.key, timeframe)] = (
            *rising,
            Candle(
                open_time=last.close_time,
                close_time=last.close_time + duration,
                open=last.close,
                high=last.close,
                low=last.close - CRASH_DEPTH,
                close=last.close - (CRASH_DEPTH - Decimal(500)),
                volume=Decimal(1),
            ),
        )
    return ReplayMarketData(series, clock)


async def test_a_partial_exit_resizes_the_legs_before_the_stop_fires(
    basket: Basket, clock: ManualClock, crashing_market: ReplayMarketData
) -> None:
    """KNOWN_GAPS §4 end to end, through the real loop.

    Before the fix, the stop's own poll — the one that pulls a venue-held fill into the ledger —
    raises out of `Ledger._apply_sell`: "sell of 0.00498 exceeds holding 0.00374 on
    sim:BTC/USDT", because the stop still rests at the size the entry filled rather than what
    survived the partial exit.
    """
    # The cooldown would veto the second cycle's SELL outright, which tests nothing about this
    # defect — `test_full_cycle.py` already owns the cooldown itself.
    eager = basket.model_copy(
        update={"risk_policy": basket.risk_policy.model_copy(update={"cooldown_cycles": 0})}
    )
    harness = Harness(
        eager,
        clock,
        crashing_market,
        [DEFAULT_RESPONSE, PARTIAL_SELL, HOLD_RESPONSE],
        # A running process's supervisor resync sweep keeps marks fresh between cycles; this
        # harness drives cycles directly, so nothing refreshes them while the clock is walked
        # forward onto the crash bar. Widened rather than removed, so a genuinely stale feed
        # would still freeze the portfolio — that freeze is not the fault under test here.
        policy=GlobalRiskPolicy(mark_staleness_seconds=int(timedelta(days=3).total_seconds())),
    )
    await harness.start()
    key = basket.instruments[0].key
    try:
        await harness.runner.run_once()
        opened = harness.ledger.position(key).qty
        assert opened > ZERO, "the panel bought and the entry filled"

        await harness.runner.run_once()
        reduced = harness.ledger.position(key).qty
        assert ZERO < reduced < opened, "the panel took part of the position off"

        # Whether the stop was actually resized to `reduced` here — KNOWN_GAPS §4's fix — is not
        # asserted directly: pre-fix it visibly is not (the leg still rests at `opened`), but that
        # is a symptom, not the failure this test exists to catch. Asserting it here would stop
        # the pre-fix run on a plain `AssertionError` before it ever reaches the venue-held stop
        # actually firing oversized, which is the ledger-corrupting failure below.

        # What the stop actually rests at, read now because the crash bar is about to consume
        # it: once the leg fills it leaves `is_open` and there is nothing left to measure. The
        # *assertion* stays at the end so the inverted gate is undisturbed — checking it here
        # would stop a pre-fix run on a plain `AssertionError` before it ever reached the
        # oversized stop firing, which is the ledger-corrupting failure this test exists for.
        guarded = max(
            (
                order.remaining_qty
                for order in harness.monitor.tracked
                if order.role is OrderRole.STOP_LOSS and order.state.is_open
            ),
            default=ZERO,
        )

        # Two days clears the longest (1d) bar duration on every timeframe, so the crash bar
        # appended in `crashing_market` becomes the latest closed bar everywhere at once.
        clock.advance(timedelta(days=2).total_seconds())
        result = await harness.runner.run_once()
        assert result.outcome is CycleOutcome.NO_ACTION, "HOLD decides nothing this cycle"

        # A venue-held stop fires between cycles, not inside one (DESIGN §6.7) — the cycle above
        # only reveals the crash bar to the venue; the monitor's own poll is what pulls the fill
        # into the ledger, exactly as a background poller would between two scheduled cycles.
        await harness.monitor.poll()

        filled = [
            order
            for order in harness.monitor.tracked
            if order.role is OrderRole.STOP_LOSS and order.state is OrderState.FILLED
        ]
        assert filled, "the crash bar crossed the trigger"
        assert filled[0].filled_qty <= reduced, "no exit sells more than was held"
        assert harness.ledger.position(key).is_flat
        # The resize itself, not merely its absence of harm: `filled_qty <= reduced` above is
        # satisfied by any undersized leg, and `is_flat` only forces equality once the stop has
        # already sold whatever it rested at. This is the number KNOWN_GAPS §4 got wrong.
        assert guarded == reduced, "the leg was resized to the surviving position, exactly"
    finally:
        harness.close()


class NothingToRun:
    """A `RunnerFactory` that refuses to build: no basket is in service in the test below.

    Asserting rather than returning a stub is the point — the property under test is that the
    *sweep* reached the venue, and a runner quietly cycling would be the other explanation.
    """

    async def build(self, record: ConfigRecord[Basket]) -> None:
        raise AssertionError("no basket is in service; nothing may cycle")

    async def release(self, basket_id: str) -> None:
        return None

    def calendar_for(self, basket: Basket) -> ContinuousCalendar:
        return ContinuousCalendar("sim")


async def test_a_stop_that_fills_between_cycles_is_booked_by_the_supervision_sweep(
    basket: Basket, clock: ManualClock, crashing_market: ReplayMarketData
) -> None:
    """KNOWN_GAPS §5 end to end, through the loop a running process actually has.

    The bot buys and leaves a stop at the venue. The price falls through it overnight and the
    venue sells — while no basket cycles, which is the ordinary case: in `data/sim.db` 41 orders
    span 196 cycles, so the great majority of cycles placed nothing and polled nothing.

    Before the fix the ledger went on reporting a holding the venue had already sold, for as long
    as it took some basket to trade: `aggregate` valued it, `_size_sell` sized a reduce-only order
    from it, and `Ledger._apply_sell` then refused that order as ledger corruption.
    """
    harness = Harness(basket, clock, crashing_market, [DEFAULT_RESPONSE])
    await harness.start()
    instrument = basket.instruments[0]
    supervisor = Supervisor(
        NothingToRun(),  # type: ignore[arg-type]
        ConfigStore(harness.engine, harness.writer, harness.store, clock),
        Scheduler(clock),
        harness.watchdog,
        harness.states,
        clock,
        monitor=harness.monitor,
    )
    try:
        await harness.runner.run_once()
        assert harness.ledger.position(instrument.key).qty > ZERO, "the panel bought"

        # Two days clears the longest bar duration, so the crash bar appended in
        # `crashing_market` is the latest closed bar when the venue next looks.
        clock.advance(timedelta(days=2).total_seconds())
        # What a running process does every time anything reads prices: the sim stack's bridge
        # hands the bar to the venue, which matches the resting stop against its low. No cycle
        # runs — this is the venue acting on its own, which is what "venue-held" means.
        await harness.market_data.get_candles(instrument, TIMEFRAMES[0], limit=10)
        assert harness.ledger.position(instrument.key).qty > ZERO, (
            "the venue has sold it and nothing has looked yet — this is the state §5 is about"
        )

        serving = asyncio.create_task(supervisor.serve(resync_seconds=60.0))
        await asyncio.sleep(0)
        await supervisor.stop()
        await asyncio.wait_for(serving, timeout=5)

        filled = [
            order
            for order in harness.monitor.tracked
            if order.role is OrderRole.STOP_LOSS and order.state is OrderState.FILLED
        ]
        assert filled, "the sweep read the stop the venue had already filled"
        assert harness.ledger.position(instrument.key).is_flat, "and booked it against the ledger"
    finally:
        harness.close()
