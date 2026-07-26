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
from tradebot.control.startup import Recovery, StartupSequence
from tradebot.core.clock import Clock, SystemClock
from tradebot.core.config import Basket, GlobalRiskPolicy, PanelConfig, RiskPolicy, SeatConfig
from tradebot.core.enums import AssetClass, Mode
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.market import timeframe_interval
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.protocols import SingleRoundProtocol
from tradebot.decision.providers import StubLLMProvider
from tradebot.decision.seat import SeatRunner
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.execution.sim_broker import SimBroker, SimulatedMarket
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import Reconciler
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.tier1 import Tier1RiskEngine
from tradebot.risk.tier2 import Tier2RiskEngine
from tradebot.risk.watchdog import Watchdog

LIVE_CONFIRMATION_PHRASE = "I ACCEPT REAL MONEY RISK"


@dataclass(slots=True)
class Application:
    """A wired system for one mode. Owns the resources it created."""

    mode: Mode
    store: EventStore
    ledger: Ledger
    runners: tuple[BasketRunner, ...]
    startup: StartupSequence
    watchdog: Watchdog
    states: RiskStateStore
    quote_currency: str
    _writer: SingleWriter

    async def recover(self) -> Recovery:
        """Run DESIGN §8.2 before anything trades. Nothing else may be called first."""
        return await self.startup.recover()

    def equity(self) -> Decimal:
        return self.ledger.equity(
            {p.instrument_key: p.avg_entry for p in self.ledger.positions()},
            quote_currency=self.quote_currency,
        )

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
    """The two-instrument basket the simulation runs out of the box.

    A stand-in for the ConfigStore that Phase 6 replaces it with; the shapes are identical, so
    the runner does not change when configuration moves into the database. Two correlated
    instruments rather than one, so the Tier-2 cluster limit is exercised by the demo instead of
    only by its tests.
    """
    instruments = tuple(
        Instrument(
            symbol=symbol,
            venue="sim",
            asset_class=AssetClass.CRYPTO,
            base_currency=symbol.split("/")[0],
            quote_currency="USDT",
            lot_size=lot,
            tick_size=Decimal("0.01"),
            min_qty=lot,
            min_notional=Decimal("10"),
        )
        for symbol, lot in (("BTC/USDT", Decimal("0.00001")), ("ETH/USDT", Decimal("0.0001")))
    )
    return Basket(
        basket_id="demo",
        name="Demo crypto basket",
        instruments=instruments,
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


def _synthetic_market(basket: Basket, clock: Clock, opens: dict[str, Decimal]) -> ReplayMarketData:
    """Series ending at *now*, so the staleness policy is exercised rather than tripped."""
    bars = 240
    return ReplayMarketData(
        {
            (instrument.key, timeframe): synthetic_candles(
                start=clock.now() - timeframe_interval(timeframe) * bars,
                timeframe=timeframe,
                count=bars,
                open_price=opens.get(instrument.key, Decimal("50000")),
                step=Decimal("25"),
                seed=_seed_for(instrument.key),
            )
            for instrument in basket.instruments
            for timeframe in ("1h", "4h", "1d")
        },
        clock,
    )


def build_sim(
    *,
    clock: Clock | None = None,
    db_path: Path | None = None,
    basket: Basket | None = None,
    start_equity: Decimal = Decimal(10_000),
    global_policy: GlobalRiskPolicy | None = None,
) -> Application:
    """Wire the simulation stack: replayed data, a scripted panel, and `SimBroker`."""
    clock = clock or SystemClock()
    basket = basket or demo_basket()
    policy = global_policy or GlobalRiskPolicy()

    engine = create_database(db_path)
    writer = SingleWriter(engine)
    store = EventStore(engine, writer)

    quote_currency = basket.instruments[0].quote_currency
    ledger = Ledger(clock, venue="sim", balances={quote_currency: start_equity})
    broker = SimBroker(
        clock, balances={quote_currency: start_equity}, default_quote_currency=quote_currency
    )
    market_data = SimulatedMarket(
        _synthetic_market(
            basket,
            clock,
            {
                i.key: Decimal("50000") if i.base_currency == "BTC" else Decimal("3000")
                for i in basket.instruments
            },
        ),
        broker,
    )

    history = HistoryReader(engine, clock)
    execution = ExecutionService(broker, store, ledger, clock)
    monitor = ExecutionMonitor(broker, execution, store, clock)
    states = RiskStateStore(engine, writer, clock)
    watchdog = Watchdog(policy, states, store, clock)
    reconciler = Reconciler(
        broker, ledger, store, clock, mode=Mode.SIM, instruments=basket.instruments
    )

    runner = BasketRunner(
        basket,
        mode=Mode.SIM,
        context_builder=ContextBuilder(
            market_data,
            ledger,
            clock,
            protective_orders_supported=broker.capabilities().protective_orders,
            trading_history=history,
        ),
        decision_engine=DecisionEngine(
            SingleRoundProtocol(SeatRunner({"stub": StubLLMProvider()}, clock))
        ),
        risk_engine=Tier1RiskEngine(clock),
        tier2=Tier2RiskEngine(policy),
        watchdog=watchdog,
        history=history,
        execution=execution,
        monitor=monitor,
        ledger=ledger,
        store=store,
        clock=clock,
        global_policy=policy,
        quote_currency=quote_currency,
    )
    return Application(
        mode=Mode.SIM,
        store=store,
        ledger=ledger,
        runners=(runner,),
        startup=StartupSequence(
            store,
            ledger,
            reconciler,
            execution,
            monitor,
            states,
            watchdog,
            clock,
            instruments=basket.instruments,
            quote_currency=quote_currency,
            # The simulated venue's books die with the process; without this an ordinary
            # restart is indistinguishable from a testnet wipe.
            venue_restore=broker,
        ),
        watchdog=watchdog,
        states=states,
        quote_currency=quote_currency,
        _writer=writer,
    )


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
