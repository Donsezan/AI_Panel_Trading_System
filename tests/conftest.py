"""Shared fixtures. Every test runs on a `ManualClock` and an in-memory database.

Nothing here touches the network, the wall clock, or the filesystem, so the suite is
deterministic and free — which is what lets it run on every commit (PLAN §7).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from tradebot.app import Application, build_sim
from tradebot.control.supervision import SupervisionController
from tradebot.core.clock import ManualClock
from tradebot.core.config import (
    Basket,
    PanelConfig,
    ProviderSettings,
    RiskPolicy,
    SeatConfig,
)
from tradebot.core.enums import AssetClass, ProviderKind
from tradebot.core.instrument import Instrument
from tradebot.core.market import Quote
from tradebot.core.snapshot import (
    BasketState,
    ContextSnapshot,
    IndicatorReading,
    InstrumentContext,
)
from tradebot.dashboard.app import create_dashboard
from tradebot.interfaces.debate import PanelRequest
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
SERIES_START = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAMES = ("1h", "4h", "1d")

#: Passed to `create_dashboard` explicitly so no test reads — or needs — the environment.
DASHBOARD_TOKEN = "a-token-long-enough-to-pass"


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def instrument() -> Instrument:
    return Instrument(
        symbol="BTC/USDT",
        venue="sim",
        asset_class=AssetClass.CRYPTO,
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=Decimal("0.00001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.00001"),
        min_notional=Decimal("10"),
    )


@pytest.fixture
def second_instrument() -> Instrument:
    """A sibling instrument, so basket-mode deliberation has something to be about."""
    return Instrument(
        symbol="ETH/USDT",
        venue="sim",
        asset_class=AssetClass.CRYPTO,
        base_currency="ETH",
        quote_currency="USDT",
        lot_size=Decimal("0.0001"),
        tick_size=Decimal("0.01"),
        min_qty=Decimal("0.0001"),
        min_notional=Decimal("10"),
    )


@pytest.fixture
def seat() -> SeatConfig:
    return SeatConfig(
        seat_id="technical",
        role="Technical Analyst",
        provider_id="stub",
        model="stub-technical",
        evidence=("indicators", "news", "position"),
    )


@pytest.fixture
def panel(seat: SeatConfig) -> PanelConfig:
    return PanelConfig(panel_id="p1", seats=(seat,))


#: A panel that needs a key, unlike the offline stub everything else here runs on. Shared, because
#: what an absent key does differs by mode — a warning in sim and paper, a refusal in live (ADR
#: 0023) — and both halves must be asserted against the same configuration.
KEYED_PANEL = PanelConfig(
    panel_id="keyed",
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


async def publish_keyed_panel(application: Application) -> None:
    """Give every basket in service a panel that needs a key it may or may not have."""
    for record in application.configs.baskets():
        await application.configs.put(
            record.ref.config_id,
            record.document.model_copy(update={"panel": KEYED_PANEL}),
            actor="test",
        )


@pytest.fixture
def basket(instrument: Instrument, panel: PanelConfig) -> Basket:
    return Basket(
        basket_id="b1",
        name="test basket",
        instruments=(instrument,),
        panel=panel,
        risk_policy=RiskPolicy(),
    )


@pytest.fixture
def request_for(instrument: Instrument) -> PanelRequest:
    """The per-asset panel request the single-instrument fixtures imply."""
    return PanelRequest.for_instrument(instrument.key)


@pytest.fixture
def market_data(instrument: Instrument, clock: ManualClock) -> ReplayMarketData:
    return ReplayMarketData(
        {
            (instrument.key, timeframe): synthetic_candles(
                start=SERIES_START,
                timeframe=timeframe,
                count=200,
                open_price=Decimal("50000"),
                step=Decimal("25"),
            )
            for timeframe in TIMEFRAMES
        },
        clock,
    )


@pytest.fixture
def ledger(clock: ManualClock) -> Ledger:
    return Ledger(clock, venue="sim", balances={"USDT": Decimal(10_000)})


@pytest.fixture
def database() -> Iterator[tuple[Engine, SingleWriter]]:
    """An in-memory database and its single writer, for stores that own their own tables."""
    engine = create_database(None)
    writer = SingleWriter(engine)
    yield engine, writer
    writer.close()


@pytest.fixture
def store() -> Iterator[EventStore]:
    engine = create_database(None)
    writer = SingleWriter(engine)
    yield EventStore(engine, writer)
    writer.close()


@pytest.fixture
async def sim_application(clock: ManualClock) -> AsyncIterator[Application]:
    """A fully wired sim stack over an in-memory database — what the dashboard is given.

    The real composition root, not a stand-in: the dashboard's whole contract is that it takes an
    `Application` someone else wired, so testing it against a hand-built double would test
    something the process never runs.
    """
    application = await build_sim(clock=clock)
    yield application
    await application.shutdown()


async def idle_supervision() -> None:
    """Stands in for `Supervisor.serve()` so a test dashboard can be *running* without cycling.

    The controller's job is task lifetime, not the work inside the task; substituting the work is
    what keeps `ManualClock` — whose `sleep` returns immediately — from turning a started
    supervisor into an unbounded cycling loop underneath a dashboard test.
    """
    await asyncio.Event().wait()


@pytest.fixture
async def supervision(sim_application: Application) -> AsyncIterator[SupervisionController]:
    """A started controller: the ordinary case, where the dashboard is watching a running bot.

    Recovery first, because supervision can never precede it: a database that has not been through
    DESIGN §8.2 reads as "never armed", and the controller refuses to start against one.
    """
    await sim_application.recover()
    controller = SupervisionController(sim_application, serve=idle_supervision)
    assert await controller.start() == ()
    yield controller
    await controller.stop()


@pytest.fixture
def dashboard(sim_application: Application, supervision: SupervisionController) -> FastAPI:
    return create_dashboard(sim_application, token=DASHBOARD_TOKEN, controller=supervision)


@pytest.fixture
async def http(dashboard: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Real HTTP over the ASGI app, in-process. No socket, so the suite stays offline.

    `httpx.ASGITransport` rather than Starlette's `TestClient`: the latter now wants httpx 2,
    and this drives the same application through the client the project already depends on.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=dashboard), base_url="http://dashboard"
    ) as client:
        yield client


@pytest.fixture
async def client(http: httpx.AsyncClient) -> httpx.AsyncClient:
    """A signed-in client. Every dashboard test that is not about auth starts here."""
    await http.post("/login", data={"token": DASHBOARD_TOKEN})
    return http


@pytest.fixture
async def dashboard_observing(sim_application: Application) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in client against a dashboard whose supervision has never been started."""
    observing = create_dashboard(sim_application, token=DASHBOARD_TOKEN)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observing), base_url="http://dashboard"
    ) as connected:
        await connected.post("/login", data={"token": DASHBOARD_TOKEN})
        yield connected


@pytest.fixture
def quote(instrument: Instrument) -> Quote:
    return Quote(
        instrument_key=instrument.key,
        bid=Decimal("49990"),
        ask=Decimal("50010"),
        last=Decimal("50000"),
        observed_at=START,
    )


def instrument_context(instrument: Instrument, last: Decimal) -> InstrumentContext:
    return InstrumentContext(
        instrument=instrument,
        quote=Quote(
            instrument_key=instrument.key,
            bid=last - Decimal(10),
            ask=last + Decimal(10),
            last=last,
            observed_at=START,
        ),
        candle_summaries=(("1h", f"200 bars; last close {last}"),),
        indicators=(
            IndicatorReading(
                name="RSI",
                timeframe="1h",
                value=Decimal("62"),
                text="RSI(14)=62.00 — firm momentum",
            ),
            IndicatorReading(
                name="ATR",
                timeframe="1h",
                value=Decimal("500"),
                text="ATR(14)=500.00 — absolute volatility per unit, in quote currency",
            ),
        ),
    )


@pytest.fixture
def snapshot(instrument: Instrument, quote: Quote) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="snap-1",
        basket_id="b1",
        as_of=START,
        instruments=(instrument_context(instrument, quote.last),),
        basket_state=BasketState(),
    )


@pytest.fixture
def two_instrument_snapshot(
    instrument: Instrument, second_instrument: Instrument
) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="snap-2",
        basket_id="b1",
        as_of=START,
        instruments=(
            instrument_context(instrument, Decimal("50000")),
            instrument_context(second_instrument, Decimal("3000")),
        ),
        basket_state=BasketState(),
    )
