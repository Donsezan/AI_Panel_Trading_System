"""Composition root. The only module that names concrete classes.

Modes differ **only** in what is wired here. Same runner, same risk code, same persistence,
same event log — which is what makes a paper result predictive of live behaviour rather than a
result from a parallel implementation (DESIGN §5).

Mode safety (PLAN §2.4) is enforced at construction:

* mode is a required argument with no default;
* each mode gets its own database file, so a paper ledger can never be read as a live one;
* each mode reads *differently named* credential variables, so a live key is unreachable from a
  paper run (`venues/credentials.py`);
* every venue transport asserts its resolved host against the mode before the first call;
* live additionally requires a typed confirmation phrase, an armed database row, a positive
  notional cap, and credentials — enumerated in `control/arming.py`. Any one missing and the
  process refuses to start. Live *wiring* remains deliberately absent (PLAN Phase 8): the
  preconditions are built and tested here, and there is still no code path that reaches a real
  exchange.

**Paper's primary shape is live market data plus `SimBroker`** (DESIGN §9 rung 5): real prices,
deterministic fills, no venue-side test artifacts. A real venue adapter is opt-in per venue and
runs as an *integration check*, not as the evidence base — Binance's spot testnet resets to a
blank state roughly monthly and its fills are unrealistically good.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import blake2s
from pathlib import Path

import httpx
from sqlalchemy import Engine

from tradebot.control.arming import (
    LIVE_CONFIRMATION_PHRASE,
    ArmingStore,
    assert_live_preconditions,
    capped,
)
from tradebot.control.basket_runner import BasketRunner
from tradebot.control.config_store import SINGLETON_ID, ConfigRecord, ConfigStore
from tradebot.control.context_builder import ContextBuilder
from tradebot.control.manual_close import ManualCloser
from tradebot.control.preflight import VenuePreflight
from tradebot.control.scheduler import Scheduler
from tradebot.control.startup import Recovery, StartupSequence
from tradebot.control.supervisor import Supervisor
from tradebot.core.clock import Clock, SystemClock
from tradebot.core.config import Basket, GlobalRiskPolicy, PanelConfig, RiskPolicy
from tradebot.core.enums import AssetClass, Mode
from tradebot.core.errors import ConfigError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.market import timeframe_interval
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.presets import PANELS, STUB_PANEL
from tradebot.decision.providers.registry import ProviderPool, build_providers
from tradebot.decision.seat import SeatRunner
from tradebot.execution.brokers.alpaca import AlpacaAnnouncements, AlpacaBroker, AlpacaCalendar
from tradebot.execution.brokers.binance import BinanceSpotBroker
from tradebot.execution.brokers.calendars import ContinuousCalendar
from tradebot.execution.brokers.sim import SimBroker, SimulatedMarket
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.broker import (
    BrokerAdapter,
    CorporateActionSource,
    RestorableVenue,
    TradingCalendar,
)
from tradebot.interfaces.llm import LLMProvider
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.interfaces.news import NewsFeed
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import Reconciler
from tradebot.marketdata.factory import binance_spot_market_data, live_binance_spot
from tradebot.marketdata.replay import ReplayMarketData, synthetic_candles
from tradebot.news.http import build_fetcher
from tradebot.news.hub import NewsHub
from tradebot.news.relevance import KeywordRelevanceFilter
from tradebot.news.rss import build_sources
from tradebot.news.store import NewsStore
from tradebot.news.vectorstore import SqliteVectorStore
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore
from tradebot.risk.state import RiskStateStore
from tradebot.risk.tier1 import Tier1RiskEngine
from tradebot.risk.tier2 import Tier2RiskEngine
from tradebot.risk.watchdog import Watchdog
from tradebot.venues.alpaca_transport import AlpacaTransport
from tradebot.venues.ccxt_transport import (
    MODE_SANDBOX,
    binance_spot_trading_transport,
    binance_spot_transport,
)
from tradebot.venues.credentials import credentials, has_credentials

logger = get_logger(__name__)

__all__ = ["LIVE_CONFIRMATION_PHRASE", "Application", "BrokerChoice", "build", "database_path"]

#: Who the composition root records as the author of the configuration it publishes. Distinct
#: from `cli` and from a dashboard user, so the audit trail says whether a human chose a limit.
ACTOR = "composition_root"


@dataclass(slots=True)
class Application:
    """A wired system for one mode. Owns the resources it created."""

    mode: Mode
    store: EventStore
    ledger: Ledger
    supervisor: Supervisor
    configs: ConfigStore
    startup: StartupSequence
    watchdog: Watchdog
    states: RiskStateStore
    #: The operator's "close this position" action, wired to the same Tier-1 → Tier-2 →
    #: execution path a cycle uses. Exposed here because the dashboard may not build one.
    manual_close: ManualCloser
    quote_currency: str
    _writer: SingleWriter
    #: Async resources that hold sockets — HTTP clients, exchange sessions. Closed by `shutdown`.
    _closers: tuple[Callable[[], Awaitable[None]], ...] = ()

    @property
    def baskets(self) -> tuple[Basket, ...]:
        """The baskets currently in service, read from the ConfigStore."""
        return tuple(record.document for record in self.configs.baskets())

    async def recover(self) -> Recovery:
        """Run DESIGN §8.2 before anything trades. Nothing else may be called first."""
        return await self.startup.recover()

    def equity(self) -> Decimal:
        return self.ledger.equity(
            {p.instrument_key: p.avg_entry for p in self.ledger.positions()},
            quote_currency=self.quote_currency,
        )

    async def shutdown(self) -> None:
        """Release every resource. A leaked HTTP session keeps the process alive after a cycle."""
        await self.supervisor.stop()
        for close in self._closers:
            try:
                await close()
            except Exception:
                logger.exception("failed to close a resource during shutdown")
        self.close()

    def close(self) -> None:
        self._writer.close()


def _seed_for(instrument_key: str) -> int:
    """Stable across processes — `hash()` is randomized per run, which would make the
    simulation irreproducible."""
    return blake2s(instrument_key.encode(), digest_size=2).digest()[0] + 1


def database_path(mode: Mode, root: Path = Path("data")) -> Path:
    """One database per mode. Never shared, never inferred (PLAN §2.4)."""
    return root / f"{mode.value}.db"


def demo_basket(panel: PanelConfig | None = None) -> Basket:
    """The two-instrument basket a fresh database is seeded with.

    Published into the ConfigStore as version 1 on first run and never consulted again: from then
    on the stored basket is the truth, and editing it in the dashboard creates version 2. Two
    correlated instruments rather than one, so the Tier-2 cluster limit is exercised by the demo
    instead of only by its tests.

    The panel defaults to the offline stub for the same reason news is off unless a source is
    named: a zero-configuration run that reaches the internet — and demands an API key to do it —
    is a surprise, not a default.
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
        panel=panel or STUB_PANEL,
        risk_policy=RiskPolicy(),
    )


def select_panel(panel_id: str) -> PanelConfig:
    """A seeded panel by name — what the demo basket is published with on a fresh database."""
    panel = PANELS.get(panel_id)
    if panel is None:
        raise ConfigError(
            f"unknown panel {panel_id!r}; available: {', '.join(sorted(PANELS))}. "
            "Panels are data — see tradebot/decision/presets.py"
        )
    return panel


def _synthetic_market(
    instruments: tuple[Instrument, ...], clock: Clock, opens: dict[str, Decimal]
) -> ReplayMarketData:
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
            for instrument in instruments
            for timeframe in ("1h", "4h", "1d")
        },
        clock,
    )


def build_news_hub(
    engine: Engine,
    writer: SingleWriter,
    clock: Clock,
    source_ids: tuple[str, ...],
) -> tuple[NewsFeed | None, tuple[Callable[[], Awaitable[None]], ...]]:
    """The RSS news pipeline, or nothing at all when no sources are configured.

    Off unless asked for, deliberately: a default that reaches out to the internet on the first
    simulated cycle is a surprise, and the snapshot states "no sources configured" rather than
    letting the panel read an empty news list as a quiet market.
    """
    if not source_ids:
        return None, ()
    fetcher = build_fetcher(clock)
    hub = NewsHub(
        build_sources(source_ids, fetcher, clock),
        NewsStore(engine, writer),
        SqliteVectorStore(engine, writer, clock),
        KeywordRelevanceFilter(),
        clock,
    )
    return hub, (fetcher.close,)


class BrokerChoice(StrEnum):
    """Which venue takes the orders. Never a default beyond `SIM` (PLAN §2.4).

    `SIM` is the primary paper venue: real market data, deterministic fills, nothing at a venue to
    reset or mis-fill. The other two are *adapter integration checks* and have to be asked for by
    name (DESIGN §9 rung 5).
    """

    SIM = "sim"
    BINANCE = "binance"
    ALPACA = "alpaca"

    @property
    def is_venue(self) -> bool:
        return self is not BrokerChoice.SIM


@dataclass(frozen=True, slots=True)
class VenueStack:
    """Everything one venue contributes, so `_assemble` never branches on which venue it is."""

    broker: BrokerAdapter
    prices: MarketDataProvider
    calendar: TradingCalendar
    announcements: CorporateActionSource | None = None
    preflight: VenuePreflight | None = None
    #: Only a *simulated* venue can be handed its books back after a restart (`RestorableVenue`).
    venue_restore: RestorableVenue | None = None
    closers: tuple[Callable[[], Awaitable[None]], ...] = ()


@dataclass(frozen=True, slots=True)
class StackRequest:
    """What every venue builder needs, so the three share one signature and one dispatch table.

    `instruments` is the union across every configured basket, not one basket's: a venue account
    is shared by all of them, so the broker, the reconciler and the market feed have to know about
    every instrument any basket might trade (DESIGN §4).
    """

    instruments: tuple[Instrument, ...]
    clock: Clock
    mode: Mode
    market_data: MarketDataProvider | None
    start_equity: Decimal
    quote_currency: str


def _sim_stack(request: StackRequest) -> VenueStack:
    """`SimBroker` over whatever prices it is given — synthetic, replayed, or live.

    Live prices plus simulated fills is DESIGN §9's primary paper mode, and it is this same code:
    only the market-data provider changes, so nothing about the venue path differs between the
    simulation and the soak.
    """
    broker = SimBroker(
        request.clock,
        balances={request.quote_currency: request.start_equity},
        default_quote_currency=request.quote_currency,
    )
    source = request.market_data or _synthetic_market(
        request.instruments,
        request.clock,
        {
            i.key: Decimal("50000") if i.base_currency == "BTC" else Decimal("3000")
            for i in request.instruments
        },
    )
    return VenueStack(
        broker=broker,
        prices=SimulatedMarket(source, broker),
        calendar=ContinuousCalendar(broker.venue_id),
        venue_restore=broker,
    )


def _binance_stack(request: StackRequest) -> VenueStack:
    """Binance spot: public reads and signed calls over **one** rate budget.

    The two transports share a limiter and a circuit breaker deliberately — a venue bans an IP and
    a key, not a code path, so a burst of candle reads and a burst of submits must spend the same
    budget (PLAN §3.1). Both resolve to the same host for the mode, which is asserted rather than
    assumed.
    """
    clock, mode = request.clock, request.mode
    data_transport = binance_spot_transport(clock, sandbox=MODE_SANDBOX[mode])
    trading_transport = binance_spot_trading_transport(
        clock, credentials("binance", mode), mode=mode, limiter=data_transport.limiter
    )
    broker = BinanceSpotBroker(trading_transport, clock, instruments=request.instruments)
    return VenueStack(
        broker=broker,
        prices=request.market_data or binance_spot_market_data(data_transport, clock),
        calendar=ContinuousCalendar(broker.venue_id),
        preflight=VenuePreflight(broker, clock, mode=mode),
        closers=(data_transport.close, trading_transport.close),
    )


def _alpaca_stack(request: StackRequest) -> VenueStack:
    """Alpaca equities: broker, exchange calendar, and the corporate-action feed.

    `market_data` is **required**, because there is no equity market-data provider yet: Phase 3
    built the Binance stack only. Refusing here is the honest answer — silently substituting crypto
    candles for equity ones would produce indicators, decisions and orders that all look valid.
    """
    if request.market_data is None:
        raise ConfigError(
            "the alpaca broker needs an equity market-data provider, and none is wired yet: "
            "Phase 3 delivered Binance spot data only. Pass `market_data=` explicitly, or run "
            "alpaca through the contract suite and the opt-in smoke test until AlpacaMarketData "
            "exists. Feeding it crypto candles would produce a decision that looks valid."
        )
    clock, mode = request.clock, request.mode
    key_id, secret_key = credentials("alpaca", mode)
    client = httpx.AsyncClient()
    transport = AlpacaTransport(client, clock, mode=mode, key_id=key_id, secret_key=secret_key)
    broker = AlpacaBroker(transport, clock, instruments=request.instruments)
    return VenueStack(
        broker=broker,
        prices=request.market_data,
        calendar=AlpacaCalendar(transport, clock),
        announcements=AlpacaAnnouncements(transport),
        preflight=VenuePreflight(broker, clock, mode=mode),
        closers=(client.aclose,),
    )


_STACKS: dict[BrokerChoice, Callable[[StackRequest], VenueStack]] = {
    BrokerChoice.SIM: _sim_stack,
    BrokerChoice.BINANCE: _binance_stack,
    BrokerChoice.ALPACA: _alpaca_stack,
}


class RunnerBuilder:
    """Turns a stored basket into a running `BasketRunner` (`supervisor.RunnerFactory`).

    A basket owns its panel, and therefore the provider connections that panel opens; everything
    else — the ledger, the execution service, the monitor, the watchdog, the event store — is
    **shared**, because positions belong to the venue portfolio and not to any basket (DESIGN §4).
    Giving each basket its own ledger would give two baskets two views of one account, which is
    precisely the concentration failure Tier-2 exists to prevent.

    Each build re-reads the Tier-2 policy from the store and re-applies the live notional cap, so
    a policy edited in the dashboard takes effect at the next cycle boundary and the arming row's
    cap can never be edited away from underneath it (PLAN §2.4).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        mode: Mode,
        configs: ConfigStore,
        stack: VenueStack,
        store: EventStore,
        ledger: Ledger,
        execution: ExecutionService,
        monitor: ExecutionMonitor,
        watchdog: Watchdog,
        history: HistoryReader,
        news_feed: NewsFeed | None,
        live_cap: Decimal,
        quote_currency: str,
    ) -> None:
        self._clock = clock
        self._mode = mode
        self._configs = configs
        self._stack = stack
        self._store = store
        self._ledger = ledger
        self._execution = execution
        self._monitor = monitor
        self._watchdog = watchdog
        self._history = history
        self._news_feed = news_feed
        self._live_cap = live_cap
        self._quote_currency = quote_currency
        self._pools: dict[str, ProviderPool] = {}

    def calendar_for(self, basket: Basket) -> TradingCalendar:  # noqa: ARG002 — one venue in v1
        """The venue calendar. One venue per process in v1, so every basket shares it."""
        return self._stack.calendar

    async def build(self, record: ConfigRecord[Basket]) -> BasketRunner:
        basket = record.document
        policy_record = self._configs.global_risk()
        if policy_record is None:
            raise ConfigError(
                "no Tier-2 policy is published; refusing to run a basket with no global limits"
            )
        policy = capped(policy_record.document, self._live_cap)
        self._watchdog.use_policy(policy)

        decision_engine = DecisionEngine(SeatRunner(self._providers(record), self._clock))
        decision_engine.validate(basket)
        return BasketRunner(
            basket,
            mode=self._mode,
            context_builder=ContextBuilder(
                self._stack.prices,
                self._ledger,
                self._clock,
                timeframes=basket.timeframes,
                indicators=basket.indicators,
                protective_orders_supported=self._stack.broker.capabilities().protective_orders,
                trading_history=self._history,
                news_feed=self._news_feed,
            ),
            decision_engine=decision_engine,
            risk_engine=Tier1RiskEngine(self._clock),
            tier2=Tier2RiskEngine(policy),
            watchdog=self._watchdog,
            history=self._history,
            execution=self._execution,
            monitor=self._monitor,
            ledger=self._ledger,
            store=self._store,
            clock=self._clock,
            global_policy=policy,
            config_refs=(record.ref, policy_record.ref),
            quote_currency=self._quote_currency,
        )

    def _providers(self, record: ConfigRecord[Basket]) -> dict[str, LLMProvider]:
        """This basket's endpoints, wired once per basket and closed when it is released."""
        pool = self._pools.get(record.ref.config_id)
        if pool is None:
            pool = build_providers(record.document.panel.providers, self._clock)
            self._pools[record.ref.config_id] = pool
        return pool.providers

    async def release(self, basket_id: str) -> None:
        """Close the connections a basket's panel opened. Safe when it opened none."""
        pool = self._pools.pop(basket_id, None)
        if pool is not None:
            await pool.close()

    async def close(self) -> None:
        for basket_id in list(self._pools):
            await self.release(basket_id)


async def _publish(
    configs: ConfigStore,
    *,
    baskets: tuple[Basket, ...] | None,
    global_policy: GlobalRiskPolicy | None,
    default_basket: Basket,
) -> None:
    """Put configuration in the store: what the caller stated, or a default on first run.

    Configuration lives in the database, so the composition root *publishes* it rather than
    holding it. A caller that states a basket or a policy explicitly is publishing a new
    version of it, which is why an existing row does not suppress the write — the alternative
    would silently ignore an argument, and a silently ignored risk policy is the worst kind.
    """
    if baskets is None:
        baskets = () if configs.baskets() else (default_basket,)
    for basket in baskets:
        await configs.put(basket.basket_id, basket, actor=ACTOR, note="published at startup")
    if global_policy is not None or configs.global_risk() is None:
        await configs.put(
            SINGLETON_ID,
            global_policy or GlobalRiskPolicy(),
            actor=ACTOR,
            note="published at startup",
        )


def _quote_currency(instruments: tuple[Instrument, ...]) -> str:
    """The account's quote currency, which every basket in one process must agree on.

    Equity is a single number per venue portfolio, and every Tier-2 limit is a percentage of it.
    Two baskets quoting in different currencies would be measured against two different equities
    while sharing one account — so this refuses rather than picking one (DESIGN §6.6).
    """
    quotes = {instrument.quote_currency for instrument in instruments}
    if len(quotes) != 1:
        raise ConfigError(
            f"every basket in one process must share a quote currency, found {sorted(quotes)}; "
            "portfolio equity and every Tier-2 percentage are denominated in it"
        )
    return quotes.pop()


def _instruments_of(baskets: tuple[Basket, ...]) -> tuple[Instrument, ...]:
    """Every instrument any basket may trade, deduplicated — two baskets may share one."""
    seen: dict[str, Instrument] = {}
    for basket in baskets:
        seen.update({instrument.key: instrument for instrument in basket.instruments})
    return tuple(seen.values())


async def _assemble(
    clock: Clock,
    *,
    mode: Mode,
    db_path: Path | None,
    broker_choice: BrokerChoice,
    start_equity: Decimal,
    baskets: tuple[Basket, ...] | None,
    global_policy: GlobalRiskPolicy | None,
    market_data: MarketDataProvider | None,
    news_sources: tuple[str, ...],
    panel_id: str,
    confirmation: str | None = None,
) -> Application:
    """Wire one mode. Everything above the venue is identical for all of them (DESIGN §5)."""
    engine = create_database(db_path)
    writer = SingleWriter(engine)
    store = EventStore(engine, writer)
    states = RiskStateStore(engine, writer, clock)
    configs = ConfigStore(engine, writer, store, clock)

    await _publish(
        configs,
        baskets=baskets,
        global_policy=global_policy,
        default_basket=demo_basket(select_panel(panel_id)),
    )
    records = configs.baskets()
    if not records:
        raise ConfigError("no baskets are configured; nothing to run")
    configured = tuple(record.document for record in records)
    instruments = _instruments_of(configured)
    quote_currency = _quote_currency(instruments)

    # Unconditional, and a no-op for every mode but live. Having the gate *inside* the assembly is
    # what makes it structurally impossible to wire live without evaluating it (PLAN §2.4).
    live_cap = assert_live_preconditions(
        mode,
        confirmation=confirmation,
        arming=ArmingStore(engine, writer, clock).load(),
        credentials=broker_choice.is_venue and has_credentials(broker_choice.value, mode),
    )
    policy_record = configs.global_risk()
    assert policy_record is not None  # `_publish` guarantees one exists
    policy = capped(policy_record.document, live_cap)

    stack = _STACKS[broker_choice](
        StackRequest(instruments, clock, mode, market_data, start_equity, quote_currency)
    )
    # A real venue's balances are *discovered*: the ledger starts empty and the startup
    # reconciliation adopts what the venue actually holds. Seeding it would invent funds and, worse,
    # would hide a discrepancy on the very first diff (DESIGN §6.8).
    ledger = Ledger(
        clock,
        venue=stack.broker.venue_id,
        balances={} if broker_choice.is_venue else {quote_currency: start_equity},
    )

    news_feed, news_closers = build_news_hub(
        engine, writer, clock, news_sources or _news_sources_of(configured)
    )

    history = HistoryReader(engine, clock)
    execution = ExecutionService(stack.broker, store, ledger, clock)
    monitor = ExecutionMonitor(stack.broker, execution, store, clock)
    watchdog = Watchdog(policy, states, store, clock, calendar=stack.calendar)
    reconciler = Reconciler(
        stack.broker,
        ledger,
        store,
        clock,
        mode=mode,
        instruments=instruments,
        announcements=stack.announcements,
    )

    builder = RunnerBuilder(
        clock=clock,
        mode=mode,
        configs=configs,
        stack=stack,
        store=store,
        ledger=ledger,
        execution=execution,
        monitor=monitor,
        watchdog=watchdog,
        history=history,
        news_feed=news_feed,
        live_cap=live_cap,
        quote_currency=quote_currency,
    )
    return Application(
        mode=mode,
        store=store,
        ledger=ledger,
        supervisor=Supervisor(builder, configs, Scheduler(clock), watchdog, states, clock),
        configs=configs,
        startup=StartupSequence(
            store,
            ledger,
            reconciler,
            execution,
            monitor,
            states,
            watchdog,
            clock,
            instruments=instruments,
            quote_currency=quote_currency,
            # A simulated venue's books die with the process; without this an ordinary restart is
            # indistinguishable from a testnet wipe.
            venue_restore=stack.venue_restore,
            preflight=stack.preflight,
        ),
        watchdog=watchdog,
        states=states,
        manual_close=ManualCloser(
            clock=clock,
            mode=mode,
            configs=configs,
            ledger=ledger,
            prices=stack.prices,
            history=history,
            execution=execution,
            monitor=monitor,
            store=store,
            quote_currency=quote_currency,
        ),
        quote_currency=quote_currency,
        _writer=writer,
        _closers=(builder.close, *news_closers, *stack.closers),
    )


def _news_sources_of(baskets: tuple[Basket, ...]) -> tuple[str, ...]:
    """Every source any basket asks for. One hub serves them all; relevance is per instrument."""
    return tuple(dict.fromkeys(source for b in baskets for source in b.news_sources))


async def build_sim(
    *,
    clock: Clock | None = None,
    db_path: Path | None = None,
    baskets: tuple[Basket, ...] | None = None,
    start_equity: Decimal = Decimal(10_000),
    global_policy: GlobalRiskPolicy | None = None,
    market_data: MarketDataProvider | None = None,
    news_sources: tuple[str, ...] = (),
    panel_id: str = "stub",
    broker: BrokerChoice = BrokerChoice.SIM,
) -> Application:
    """Wire the simulation stack: replayed data, a panel, and `SimBroker`.

    `market_data` overrides the synthetic series with any provider; `panel_id` swaps the scripted
    panel for real models. `SimBroker` still matches every fill, so no combination of the two can
    move real money.
    """
    if broker.is_venue:
        raise ConfigError(
            f"simulation cannot use the {broker.value!r} broker: sim mode's promise is that it is "
            "offline and reproducible, and a run that reaches a venue is neither. Use "
            f"`--mode paper --broker {broker.value}` for that."
        )
    return await _assemble(
        clock or SystemClock(),
        mode=Mode.SIM,
        db_path=db_path,
        broker_choice=BrokerChoice.SIM,
        start_equity=start_equity,
        baskets=baskets,
        global_policy=global_policy,
        market_data=market_data,
        news_sources=news_sources,
        panel_id=panel_id,
    )


async def build_paper(
    *,
    clock: Clock | None = None,
    db_path: Path | None = None,
    baskets: tuple[Basket, ...] | None = None,
    start_equity: Decimal = Decimal(10_000),
    global_policy: GlobalRiskPolicy | None = None,
    market_data: MarketDataProvider | None = None,
    news_sources: tuple[str, ...] = (),
    panel_id: str = "stub",
    broker: BrokerChoice = BrokerChoice.SIM,
) -> Application:
    """Wire the paper stack. Default: **live market data with `SimBroker`** (DESIGN §9 rung 5).

    That default is the evidence base — real prices, deterministic fills, nothing at a venue that
    can reset underneath a soak. `broker=BINANCE` or `broker=ALPACA` swaps in the real adapter
    against its test endpoint as an *integration check*; those runs are excluded from promotion
    accounting because Binance's spot testnet wipes itself roughly monthly (R15) and its fills are
    unrealistically good.
    """
    clock = clock or SystemClock()
    return await _assemble(
        clock,
        mode=Mode.PAPER,
        db_path=db_path,
        broker_choice=broker,
        start_equity=start_equity,
        baskets=baskets,
        global_policy=global_policy,
        market_data=market_data or _paper_market_data(broker, clock),
        news_sources=news_sources,
        panel_id=panel_id,
    )


def _paper_market_data(broker: BrokerChoice, clock: Clock) -> MarketDataProvider | None:
    """Real Binance prices for the crypto paths; nothing to offer for equities yet.

    A paper run reaching the internet for prices is the point of paper — unlike simulation, where
    it would be a surprise. The testnet's own book is used when the testnet is also taking the
    orders, so an order and the prices it was sized from describe the same market.
    """
    if broker is BrokerChoice.ALPACA:
        return None
    provider, _ = live_binance_spot(clock, sandbox=broker is BrokerChoice.BINANCE)
    return provider


async def build(mode: Mode, *, confirmation: str | None = None, **kwargs: object) -> Application:
    """Wire the stack for `mode`, refusing anything that could reach a real exchange.

    Live is gated, not wired. Every one of PLAN §2.4's preconditions is evaluated — the typed
    phrase, the armed database row, the notional cap, the credentials — and then the build still
    refuses, because live wiring is Phase 8's deliverable and arming it is a human act, never mine.
    A mode that quietly does something other than what was asked is the mode confusion PLAN §2.4
    treats as catastrophic, so nothing here degrades to simulation.
    """
    if mode is Mode.SIM:
        return await build_sim(**kwargs)  # type: ignore[arg-type]
    if mode is Mode.PAPER:
        return await build_paper(**kwargs)  # type: ignore[arg-type]
    return _refuse_live(confirmation, kwargs)


def _refuse_live(confirmation: str | None, kwargs: dict[str, object]) -> Application:
    """Evaluate every live precondition, then refuse anyway (PLAN Phase 8).

    The preconditions are checked *first* and deliberately: an operator working towards live must
    find out about a missing cap or an unarmed database now, from the same refusal they will see
    when the wiring lands, rather than discovering the list one item at a time later.
    """
    db_path = kwargs.get("db_path")
    engine = create_database(db_path if isinstance(db_path, Path) else None)
    writer = SingleWriter(engine)
    clock = SystemClock()
    try:
        broker = kwargs.get("broker")
        venue = broker.value if isinstance(broker, BrokerChoice) else BrokerChoice.BINANCE.value
        assert_live_preconditions(
            Mode.LIVE,
            confirmation=confirmation,
            arming=ArmingStore(engine, writer, clock).load(),
            credentials=has_credentials(venue, Mode.LIVE),
        )
    finally:
        writer.close()
    raise ConfigError(
        "live preconditions are satisfied, and live wiring is still deliberately absent: "
        "completing it is PLAN Phase 8, delivered with docs/OPERATIONS.md and armed by a human."
    )
