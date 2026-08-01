"""Live readiness: the difference between being permitted to trade live and being able to.

`test_arming.py` covers permission — four facts a human puts in place. Every one of them can be
satisfied on a machine whose alerting was never configured, whose free model slot disappeared last
week, and whose feed has been returning a holed series since the last restart. These are the gates
that catch that (ADR 0020).

Contract, shared with `control/preflight.py`: `run` never raises, and every finding is returned
together — an operator fixing them one refusal per start is an operator who stops reading.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.readiness import LiveReadiness
from tradebot.core.clock import ManualClock
from tradebot.core.config import (
    Basket,
    PanelConfig,
    ProviderBinding,
    ProviderSettings,
    SeatConfig,
)
from tradebot.core.enums import BasketStatus, ProviderKind
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import Candle, timeframe_interval
from tradebot.decision.probe import PanelProbeResult
from tradebot.indicators.library import DEFAULT_INDICATORS, required_history
from tradebot.interfaces.alerts import Alert
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.store import EventStore

TIMEFRAMES = ("1h", "4h", "1d")
DEPTH = required_history(DEFAULT_INDICATORS)
VENUE = "sim"


class FakeSink:
    sink_id = "fake"

    async def send(self, alert: Alert) -> None: ...


class FakeFactory:
    """A `RunnerFactory` that records what it was asked to build, and can refuse to.

    The real one opens provider connections; readiness only cares that building *succeeds*, which
    is what makes the config gate free of its own validation rules — it restates nothing.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.built: list[str] = []

    async def build(self, record: ConfigRecord[Basket]) -> Any:
        if self.error is not None:
            raise self.error
        self.built.append(record.ref.config_id)
        return object()

    async def release(self, basket_id: str) -> None: ...

    def calendar_for(self, basket: Basket) -> Any:  # pragma: no cover — never reached here
        raise AssertionError("readiness does not schedule anything")


class FakeProbe:
    """Stands in for a real completion down each seat's chain."""

    def __init__(self, result: PanelProbeResult | None = None) -> None:
        self.result = result or PanelProbeResult()
        self.calls: list[str] = []

    async def __call__(self, record: ConfigRecord[Basket]) -> PanelProbeResult:
        self.calls.append(record.ref.config_id)
        return self.result


REAL_PANEL = PanelConfig(
    panel_id="real",
    providers=(
        ProviderSettings(
            provider_id="openrouter",
            kind=ProviderKind.OPENAI_COMPAT,
            base_url="https://openrouter.ai/api/v1",
            secret_ref="OPENROUTER_API_KEY",
        ),
    ),
    seats=(
        SeatConfig(
            seat_id="technical",
            role="Technical Analyst",
            provider_id="openrouter",
            model="a-real-model",
        ),
    ),
)

STUB_BACKED_PANEL = REAL_PANEL.model_copy(
    update={
        "providers": (
            *REAL_PANEL.providers,
            ProviderSettings(provider_id="offline", kind=ProviderKind.STUB),
        ),
        "seats": (
            SeatConfig(
                seat_id="technical",
                role="Technical Analyst",
                provider_id="openrouter",
                model="a-real-model",
                fallbacks=(ProviderBinding(provider_id="offline", model="stub-technical"),),
            ),
        ),
    }
)


def candles(
    clock: ManualClock, timeframe: str, *, count: int = 240, ends_ago: timedelta = timedelta(0)
) -> tuple[Candle, ...]:
    """A contiguous series whose last bar closes `ends_ago` before now."""
    interval = timeframe_interval(timeframe)
    end = clock.now() - ends_ago
    return synthetic_candles(
        start=end - interval * count,
        timeframe=timeframe,
        count=count,
        open_price=Decimal(50_000),
        step=Decimal(25),
    )


def market(
    clock: ManualClock, instrument: Instrument, *, holed: bool = False, **kwargs: Any
) -> ReplayMarketData:
    """Fresh, deep, contiguous series on every default timeframe — unless asked otherwise."""
    series = {
        (instrument.key, timeframe): candles(clock, timeframe, **kwargs) for timeframe in TIMEFRAMES
    }
    if holed:
        # Near the end on purpose: the builder fetches only as deep as the indicators need, so a
        # hole in the older bars is one the cycle would never have read either.
        bars = series[(instrument.key, "1h")]
        series[(instrument.key, "1h")] = bars[:-5] + bars[-4:]
    return ReplayMarketData(series, clock)


@pytest.fixture
def configs(clock: ManualClock, database: tuple[Engine, SingleWriter]) -> ConfigStore:
    engine, writer = database
    return ConfigStore(engine, writer, EventStore(engine, writer), clock)


@pytest.fixture
def live_basket(instrument: Instrument) -> Basket:
    """What a basket must look like to be allowed live: real providers, this venue's instruments."""
    return Basket(basket_id="live", name="live basket", instruments=(instrument,), panel=REAL_PANEL)


async def readiness(
    configs: ConfigStore,
    basket: Basket,
    clock: ManualClock,
    ledger: Ledger,
    *,
    market_data: ReplayMarketData,
    factory: FakeFactory | None = None,
    probe: FakeProbe | None = None,
    sinks: Sequence[FakeSink] | None = None,
    venue: str = VENUE,
) -> LiveReadiness:
    await configs.put(basket.basket_id, basket, actor="test")
    return LiveReadiness(
        configs=configs,
        factory=factory or FakeFactory(),  # type: ignore[arg-type]
        market_data=market_data,
        ledger=ledger,
        clock=clock,
        venue=venue,
        alert_sinks=(FakeSink(),) if sinks is None else sinks,
        panel_probe=probe or FakeProbe(),
    )


class TestReady:
    async def test_a_working_system_passes_every_gate(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        gates = await readiness(
            configs, live_basket, clock, ledger, market_data=market(clock, instrument)
        )
        assert await gates.run() == ()

    async def test_a_seat_on_its_fallback_is_a_warning_not_a_refusal(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """The chain exists so an outage is survivable; refusing over a healthy fallback would
        make the fallback pointless."""
        probe = FakeProbe(PanelProbeResult(substitutions=("technical on openrouter/backup",)))
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument),
            probe=probe,
        )
        assert await gates.run() == ()


class TestAlerting:
    async def test_no_destination_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """Every control ends in "halt and tell someone". Live starting unheard is live that
        halts into silence (DESIGN §8.3)."""
        gates = await readiness(
            configs, live_basket, clock, ledger, market_data=market(clock, instrument), sinks=()
        )
        failures = await gates.run()
        assert any("alert destination" in failure for failure in failures)


class TestPanel:
    async def test_a_stub_binding_anywhere_in_a_chain_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """A fallback to the stub is one outage away from a real order sized by canned JSON."""
        basket = live_basket.model_copy(update={"panel": STUB_BACKED_PANEL})
        gates = await readiness(
            configs, basket, clock, ledger, market_data=market(clock, instrument)
        )
        failures = await gates.run()
        assert any("stub provider" in failure for failure in failures)

    async def test_a_stub_panel_is_refused_before_it_is_probed(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """A probe of a stub would succeed — it answers offline — and hide the finding."""
        probe = FakeProbe()
        basket = live_basket.model_copy(update={"panel": STUB_BACKED_PANEL})
        gates = await readiness(
            configs, basket, clock, ledger, market_data=market(clock, instrument), probe=probe
        )
        await gates.run()
        assert probe.calls == []

    async def test_an_unreachable_seat_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        probe = FakeProbe(PanelProbeResult(failures=("seat 'technical' could not be reached",)))
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument),
            probe=probe,
        )
        assert await gates.run() == ("seat 'technical' could not be reached",)


class TestConfiguration:
    async def test_a_basket_that_does_not_build_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """A missing secret or an unknown indicator must refuse now, not at 03:00 mid-position."""
        factory = FakeFactory(ConfigError("OPENROUTER_API_KEY is not set"))
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument),
            factory=factory,
        )
        failures = await gates.run()
        assert any("does not build" in failure for failure in failures)

    async def test_an_instrument_on_another_venue_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """A fresh database seeds a demo basket on `sim`; wired to a real exchange that would be
        priced and quantized against another market's rules."""
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument),
            venue="binance",
        )
        failures = await gates.run()
        assert any("another venue" in failure for failure in failures)

    async def test_an_unknown_indicator_is_a_finding_not_a_raise(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """`run` never raises, whatever it finds — the caller turns findings into a halt."""
        basket = live_basket.model_copy(update={"indicators": ("no_such_indicator",)})
        gates = await readiness(
            configs, basket, clock, ledger, market_data=market(clock, instrument)
        )

        failures = await gates.run()

        assert any("cannot build a context" in failure for failure in failures)

    async def test_a_paused_basket_is_checked_but_not_probed_or_fetched(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """It cannot cycle, so spending provider calls and venue weight on it is waste — but its
        configuration is still checked, because it can be resumed without a restart."""
        probe = FakeProbe()
        factory = FakeFactory()
        paused = live_basket.model_copy(update={"status": BasketStatus.PAUSED})
        gates = await readiness(
            configs,
            paused,
            clock,
            ledger,
            market_data=market(clock, instrument, count=2),  # far too short to pass
            factory=factory,
            probe=probe,
        )
        assert await gates.run() == ()
        assert factory.built == ["live"]
        assert probe.calls == []


class TestMarketData:
    async def test_a_gap_in_the_tape_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """ATR sizes every position; an ATR across a hole is a stop distance from a bar the venue
        never published (DESIGN §6.2, §6.6)."""
        gates = await readiness(
            configs, live_basket, clock, ledger, market_data=market(clock, instrument, holed=True)
        )
        failures = await gates.run()
        assert any("gap(s) in the tape" in failure for failure in failures)

    async def test_a_series_too_short_for_the_indicators_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument, count=DEPTH - 1),
        )
        failures = await gates.run()
        assert len(failures) == len(TIMEFRAMES)
        assert all("usable bars" in failure for failure in failures)

    async def test_a_stale_series_refuses(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """The freshness policy is the ContextBuilder's own — readiness restates nothing."""
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument, ends_ago=timedelta(days=30)),
        )
        failures = await gates.run()
        assert failures and all("old, limit" in failure for failure in failures)

    async def test_a_missing_series_refuses_rather_than_raising(
        self, configs: ConfigStore, live_basket: Basket, clock: ManualClock, ledger: Ledger
    ) -> None:
        gates = await readiness(
            configs, live_basket, clock, ledger, market_data=ReplayMarketData({}, clock)
        )
        assert len(await gates.run()) == len(TIMEFRAMES)


class TestReporting:
    async def test_every_finding_is_returned_together(
        self,
        configs: ConfigStore,
        live_basket: Basket,
        clock: ManualClock,
        ledger: Ledger,
        instrument: Instrument,
    ) -> None:
        """One refusal per start would take four restarts to discover four problems."""
        gates = await readiness(
            configs,
            live_basket,
            clock,
            ledger,
            market_data=market(clock, instrument, holed=True),
            sinks=(),
        )
        failures = await gates.run()
        assert any("alert destination" in failure for failure in failures)
        assert any("gap(s) in the tape" in failure for failure in failures)
