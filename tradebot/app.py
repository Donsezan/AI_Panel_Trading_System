"""Composition root. The only module that names concrete classes.

Modes differ **only** in what is wired here. Same runner, same risk code, same persistence,
same event log — which is what makes a paper result predictive of live behaviour rather than a
result from a parallel implementation (DESIGN §5).

Mode safety (PLAN §2.4) is enforced at construction:

* mode is a required argument with no default;
* each mode gets its own database file, so a paper ledger can never be read as a live one;
* live additionally requires a typed confirmation phrase, an armed config flag, and a notional
  cap. Any one missing and the process refuses to start. Live wiring is deliberately absent in
  this phase — `build_live` does not exist, so there is no code path to reach a real venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import blake2s
from pathlib import Path

from tradebot.control.basket_runner import BasketRunner
from tradebot.control.context_builder import ContextBuilder
from tradebot.core.clock import Clock, SystemClock
from tradebot.core.config import Basket, PanelConfig, RiskPolicy, SeatConfig
from tradebot.core.enums import AssetClass, Mode
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import timeframe_interval
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.protocols import SingleRoundProtocol
from tradebot.decision.providers import StubLLMProvider
from tradebot.decision.seat import SeatRunner
from tradebot.execution.service import ExecutionService
from tradebot.execution.sim_broker import SimBroker
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore
from tradebot.risk.tier1 import Tier1RiskEngine

LIVE_CONFIRMATION_PHRASE = "I ACCEPT REAL MONEY RISK"


@dataclass(slots=True)
class Application:
    """A wired system for one mode. Owns the resources it created."""

    mode: Mode
    store: EventStore
    ledger: Ledger
    runners: tuple[BasketRunner, ...]
    _writer: SingleWriter

    def close(self) -> None:
        self._writer.close()


def _seed_for(instrument_key: str) -> int:
    """Stable across processes — `hash()` is randomized per run, which would make the
    simulation irreproducible."""
    return blake2s(instrument_key.encode(), digest_size=2).digest()[0] + 1


def database_path(mode: Mode, root: Path = Path("data")) -> Path:
    """One database per mode. Never shared, never inferred (PLAN §2.4)."""
    return root / f"{mode.value}.db"


def demo_basket() -> Basket:
    """The single-instrument basket the simulation runs out of the box.

    A stand-in for the ConfigStore that Phase 6 replaces it with; the shapes are identical, so
    the runner does not change when configuration moves into the database.
    """
    instrument = Instrument(
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
    return Basket(
        basket_id="demo",
        name="Demo crypto basket",
        instruments=(instrument,),
        panel=PanelConfig(
            panel_id="demo-panel",
            seats=(
                SeatConfig(
                    seat_id="technical",
                    role="Technical Analyst",
                    provider_id="stub",
                    model="stub-technical",
                    evidence=("indicators", "position"),
                ),
            ),
        ),
        risk_policy=RiskPolicy(),
    )


def build_sim(
    *,
    clock: Clock | None = None,
    db_path: Path | None = None,
    basket: Basket | None = None,
    start_equity: Decimal = Decimal(10_000),
) -> Application:
    """Wire the simulation stack: replayed data, a scripted panel, and `SimBroker`."""
    clock = clock or SystemClock()
    basket = basket or demo_basket()

    engine = create_database(db_path)
    writer = SingleWriter(engine)
    store = EventStore(engine, writer)

    quote_currency = basket.instruments[0].quote_currency
    ledger = Ledger(clock, venue="sim", balances={quote_currency: start_equity})

    # Generate up to *now*, so the staleness policy is exercised for real rather than being
    # permanently tripped (or permanently disabled) by data from a fixed date in the past.
    bars = 240
    market_data = ReplayMarketData(
        {
            (instrument.key, timeframe): synthetic_candles(
                start=clock.now() - timeframe_interval(timeframe) * bars,
                timeframe=timeframe,
                count=bars,
                open_price=Decimal("50000"),
                step=Decimal("25"),
                seed=_seed_for(instrument.key),
            )
            for instrument in basket.instruments
            for timeframe in ("1h", "4h", "1d")
        },
        clock,
    )

    broker = SimBroker(clock, balances={quote_currency: start_equity})
    seat_runner = SeatRunner({"stub": StubLLMProvider()}, clock)

    runner = BasketRunner(
        basket,
        mode=Mode.SIM,
        context_builder=ContextBuilder(
            market_data,
            ledger,
            clock,
            protective_orders_supported=broker.capabilities().protective_orders,
        ),
        decision_engine=DecisionEngine(SingleRoundProtocol(seat_runner)),
        risk_engine=Tier1RiskEngine(clock),
        execution=ExecutionService(broker, store, ledger, clock),
        ledger=ledger,
        store=store,
        clock=clock,
        quote_currency=quote_currency,
    )
    return Application(mode=Mode.SIM, store=store, ledger=ledger, runners=(runner,), _writer=writer)


def build(mode: Mode, *, confirmation: str | None = None, **kwargs: object) -> Application:
    """Wire the stack for `mode`, refusing anything that could reach a real venue.

    Paper and live wiring arrive with their adapters (Phases 5 and 8). Until then this raises
    rather than silently degrading to simulation — a mode that quietly does something other
    than what was asked is exactly the mode confusion PLAN §2.4 treats as catastrophic.
    """
    if mode is Mode.SIM:
        return build_sim(**kwargs)  # type: ignore[arg-type]
    if mode is Mode.LIVE and confirmation != LIVE_CONFIRMATION_PHRASE:
        raise ConfigError(
            "live mode requires the typed confirmation phrase, an armed config row and a "
            "notional cap; none of which exist yet — live wiring ships disabled (PLAN Phase 8)"
        )
    raise ConfigError(
        f"mode {mode.value!r} has no wiring in this build; only 'sim' is implemented so far"
    )
