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
  notional cap, and credentials — enumerated in `control/arming.py` and evaluated by
  `control/supervision.py` at the moment supervision is asked to start (ADR 0021). Any one missing
  and nothing cycles.

Live is wired (PLAN Phase 8) and ships **disarmed**. Wiring it produces a system that can be
*looked at*; only a stop→start transition satisfying all four facts makes it trade. It is the paper
wiring with the same objects and two subtractions — Tier-2 limits clamped to `control/live.py`'s
ceiling, and `control/readiness.py` refusing to start unless alerting, the panel, the market data
and every stored configuration are actually working. See
[docs/OPERATIONS.md](../docs/OPERATIONS.md) for the checklist, the arming procedure, and the
incident runbook.

**Paper's primary shape is live market data plus `SimBroker`** (DESIGN §9 rung 5): real prices,
deterministic fills, no venue-side test artifacts. A real venue adapter is opt-in per venue and
runs as an *integration check*, not as the evidence base — Binance's spot testnet resets to a
blank state roughly monthly and its fills are unrealistically good.

Ops alerting is wired here too, and it is **on exactly when a destination is configured in the
environment** — there is no flag. That is deliberate: an operator starting a six-week soak should
not be able to forget to turn alerting on, and a developer running the demo should never be asked
for a webhook (ADR 0019).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import httpx
from sqlalchemy import Engine

from tradebot.control.arming import ArmingStore, LivePermission, live_permission
from tradebot.control.basket_runner import BasketRunner
from tradebot.control.config_store import SINGLETON_ID, ConfigRecord, ConfigStore
from tradebot.control.context_builder import ContextBuilder
from tradebot.control.live import EffectivePolicy, effective_policy
from tradebot.control.manual_close import ManualCloser
from tradebot.control.preflight import VenuePreflight
from tradebot.control.readiness import LiveReadiness
from tradebot.control.reference import DriftWatch, configured_instruments
from tradebot.control.scheduler import Scheduler
from tradebot.control.startup import Recovery, StartupSequence
from tradebot.control.supervisor import DEFAULT_RESYNC_SECONDS, Supervisor
from tradebot.control.valuation import PortfolioWatch
from tradebot.core.clock import Clock, SystemClock
from tradebot.core.config import (
    Basket,
    GlobalRiskPolicy,
    MaintenancePolicy,
    PanelConfig,
    ProviderSettings,
    RiskPolicy,
    Schedule,
)
from tradebot.core.enums import AssetClass, ConfigKind, Mode, RiskTier
from tradebot.core.errors import ConfigError
from tradebot.core.events import EventFactory
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.market import timeframe_interval
from tradebot.core.money import ZERO
from tradebot.decision.engine import DecisionEngine
from tradebot.decision.presets import PANELS, STUB_PANEL
from tradebot.decision.probe import PanelProbeResult, probe_panel
from tradebot.decision.providers.registry import ProviderPool, build_providers, reach_of
from tradebot.decision.seat import SeatRunner
from tradebot.decision.shadow import ShadowEvaluator
from tradebot.execution.brokers.alpaca import AlpacaAnnouncements, AlpacaBroker, AlpacaCalendar
from tradebot.execution.brokers.binance import BinanceSpotBroker
from tradebot.execution.brokers.calendars import ContinuousCalendar
from tradebot.execution.brokers.sim import SimBroker, SimulatedMarket
from tradebot.execution.monitor import ExecutionMonitor
from tradebot.execution.service import ExecutionService
from tradebot.interfaces.alerts import AlertSink
from tradebot.interfaces.broker import (
    BrokerAdapter,
    CorporateActionSource,
    RestorableVenue,
    TradingCalendar,
)
from tradebot.interfaces.exchange import InstrumentCatalogue
from tradebot.interfaces.llm import LLMProvider
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.interfaces.news import NewsFeed
from tradebot.ledger.history import HistoryReader
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import Ledger
from tradebot.ledger.reconciler import Reconciler
from tradebot.maintenance.archive import archive_destination
from tradebot.maintenance.backup import backup_destination
from tradebot.maintenance.service import MaintenanceService
from tradebot.marketdata.binance import BinanceSpotGateway
from tradebot.marketdata.catalogue import (
    UnavailableCatalogue,
    VenueCatalogue,
    instrument_of,
    replay_catalogue,
    sim_catalogue,
)
from tradebot.marketdata.factory import binance_spot_market_data, live_binance_spot
from tradebot.marketdata.recorder import ReplayDataset
from tradebot.marketdata.synthetic import SyntheticMarketData
from tradebot.news.http import build_fetcher
from tradebot.news.hub import NewsHub
from tradebot.news.relevance import KeywordRelevanceFilter
from tradebot.news.rss import build_sources
from tradebot.news.store import NewsStore
from tradebot.news.vectorstore import SqliteVectorStore
from tradebot.ops.cursor import AlertCursorStore
from tradebot.ops.dispatcher import AlertDispatcher
from tradebot.ops.sinks import build_sinks
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore
from tradebot.risk.aggregate import PortfolioAggregate, aggregate
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

__all__ = ["Application", "BrokerChoice", "build", "database_path"]

#: Who the composition root records as the author of the configuration it publishes. Distinct
#: from `cli` and from a dashboard user, so the audit trail says whether a human chose a limit.
ACTOR = "composition_root"

#: The panel a fresh database is seeded with when none is named: offline, free, and keyless, so a
#: zero-configuration run neither reaches the internet nor demands an API key.
DEFAULT_PANEL_ID = "stub"


@dataclass(slots=True)
class Application:
    """A wired system for one mode. Owns the resources it created."""

    mode: Mode
    clock: Clock
    store: EventStore
    ledger: Ledger
    supervisor: Supervisor
    configs: ConfigStore
    startup: StartupSequence
    watchdog: Watchdog
    states: RiskStateStore
    #: Which venue takes the orders. Held because live permission asks whether *this* venue's
    #: credentials are present, and only the composition root knows which venue was wired.
    broker: BrokerChoice
    #: The live arming row for *this* mode's database. Read by `risk status`, so an operator can
    #: see what live is permitted to do without opening the database (ADR 0012).
    arming: ArmingStore
    #: The operator's "close this position" action, wired to the same Tier-1 → Tier-2 →
    #: execution path a cycle uses. Exposed here because the dashboard may not build one.
    manual_close: ManualCloser
    #: The one poller. Exposed for the backtest harness, which steps time itself and therefore
    #: has to drive between cycles what a running process drives from its own loop.
    monitor: ExecutionMonitor
    #: Ops alerting, off unless a destination is configured in the environment (ADR 0019). It
    #: tails the log beside the supervisor and can never reach the money path.
    alerts: AlertDispatcher
    #: The daily housekeeping pass — backup, archive, compact, delete (ADR 0028). `None` for an
    #: in-memory database, which is the whole test suite: there is no file to copy and no
    #: directory to archive into, and a pass that invented one would put filesystem writes under
    #: every test in the repo.
    maintenance: MaintenanceService | None
    #: The shared price source an observer may read, exposed for the dashboard's chart (PHASE_10
    #: decision 4). It is the same cache the runners read through, so a chart request spends the
    #: same single-flight budget a cycle does (ADR 0008) and no candle is persisted for the UI —
    #: but it is `VenueStack.read_only_prices`, never the simulated stack's bridge, so no amount
    #: of looking at a chart can move the venue. `None` only where no provider was wired, which
    #: the chart pane renders as a stated absence.
    market_data: MarketDataProvider | None
    #: What this process's instruments are quantized against. **Not** optional: every mode has a
    #: catalogue, because the simulated venue publishes one exactly as a real venue does, and the
    #: parity that makes a paper result predictive is expressed in the type rather than promised
    #: in a comment (ADR 0025). A venue that cannot answer refuses when asked, and is still one.
    catalogue: InstrumentCatalogue
    quote_currency: str
    #: The process-wide price cache every valuation reads. Shared like the ledger, and for the same
    #: reason: equity is a property of the portfolio, not of a basket (DESIGN §4, ADR 0027).
    marks: Marks
    #: Refreshes `marks` for every held instrument and measures drawdown between cycles. Exposed
    #: like `monitor`, because the backtest harness steps time itself and must drive from its own
    #: loop what a running process drives from the supervisor's.
    portfolio_watch: PortfolioWatch
    _writer: SingleWriter
    #: Async resources that hold sockets — HTTP clients, exchange sessions. Closed by `shutdown`.
    _closers: tuple[Callable[[], Awaitable[None]], ...] = ()

    @property
    def baskets(self) -> tuple[Basket, ...]:
        """The baskets currently in service, read from the ConfigStore."""
        return tuple(record.document for record in self.configs.baskets())

    @property
    def panel_warnings(self) -> tuple[str, ...]:
        """Panels that cannot be fully reached with the keys in this environment (ADR 0023).

        Read fresh rather than held: an operator fixes this either by setting a key and restarting
        or by editing the panel in the dashboard, and the second takes effect without a restart.
        Empty is the healthy answer, and in sim and paper a non-empty one is a *warning* — those
        modes are allowed to run degraded, and a seat that cannot be reached abstains. In live it
        is a refusal, applied by `control/readiness.py` at startup and by `SupervisionController`
        at every Start.
        """
        return panel_findings(self.baskets)

    @property
    def policy(self) -> EffectivePolicy:
        """The Tier-2 limits in force *now*: published, capped by arming, clamped in live.

        Read fresh rather than held, because arming can change while the process runs (ADR 0021):
        a cap fixed at boot would be a number the CLI and the dashboard reported while the runners
        enforced another.
        """
        return enforced_policy(self.configs, self.arming, self.mode)

    def live_permission(self, confirmation: str | None = None) -> LivePermission:
        """The four facts of ADR 0012, evaluated against this process's venue and database."""
        return live_permission(
            self.mode,
            confirmation=confirmation,
            arming=self.arming.load(),
            credentials=self.broker.is_venue and has_credentials(self.broker.value, self.mode),
        )

    async def record_limits(self) -> EffectivePolicy:
        """Put the limits about to be enforced in the log, and return them."""
        effective = self.policy
        await _record_effective_policy(self.store, self.clock, self.mode, effective)
        return effective

    async def recover(self) -> Recovery:
        """Run DESIGN §8.2 before anything trades. Nothing else may be called first."""
        return await self.startup.recover()

    def valuation(self) -> PortfolioAggregate:
        """What the portfolio is worth right now, or why that cannot be said.

        Returns the aggregate rather than a bare `Decimal` deliberately: a method called `equity`
        returning a number is what let six call sites each build their own price map, and every one
        of them built it out of `avg_entry` (PHASE_12 §3.5, ADR 0027). Callers that need a figure
        must first decide what they do when there is none.
        """
        return aggregate(
            {self.ledger.venue: self.ledger},
            configured_instruments(self.configs),
            self.marks,
            self.policy.policy,
            as_of=self.clock.now(),
            notional_currency=self.quote_currency,
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


def database_path(mode: Mode, root: Path = Path("data")) -> Path:
    """One database per mode. Never shared, never inferred (PLAN §2.4)."""
    return root / f"{mode.value}.db"


def backtest_database_path(root: Path = Path("data")) -> Path:
    """A backtest gets its own database, separate from the interactive simulation.

    It is still sim mode — same prefix on every `client_order_id`, same refusal to reach a venue
    — but a replay of last year's prices sharing a ledger with the demo would make both
    unreadable, and the promotion report counts cycles out of exactly one database.
    """
    return root / "backtest.db"


#: What the seeded basket trades. Two correlated instruments rather than one, so the Tier-2
#: cluster limit is exercised by the demo instead of only by its tests.
DEMO_SYMBOLS = ("BTC/USDT", "ETH/USDT")


async def demo_basket(catalogue: InstrumentCatalogue, panel: PanelConfig | None = None) -> Basket:
    """The two-instrument basket a fresh database is seeded with, on the *simulated* venue.

    Published into the ConfigStore as version 1 on first run and never consulted again: from then
    on the stored basket is the truth, and editing it in the dashboard creates version 2. It is
    seeded from the simulated venue's catalogue in every mode, because that is what it is — a
    demonstration. A fresh database wired to a real exchange gets a basket that names instruments
    the process cannot trade, and `control/readiness.py` says so by name rather than letting it
    surface as a data fault; publishing a basket for the venue is the operator's deliberate act.

    **The trading rules come from the catalogue, not from literals here.** They used to be written
    out — and were wrong: the demo quantized `BTC/USDT` against a `min_notional` of 10 where the
    venue publishes 5, so the first thing a fresh database did was disagree with the venue about
    which orders exist (ADR 0025). A default that is a second source of truth for a risk input is
    a default that drifts.

    The panel defaults to the offline stub for the same reason news is off unless a source is
    named: a zero-configuration run that reaches the internet — and demands an API key to do it —
    is a surprise, not a default.
    """
    return Basket(
        basket_id="demo",
        name="Demo crypto basket",
        instruments=tuple([await instrument_of(catalogue, symbol) for symbol in DEMO_SYMBOLS]),
        panel=panel or STUB_PANEL,
        risk_policy=RiskPolicy(),
    )


def dataset_basket(
    dataset: ReplayDataset,
    panel: PanelConfig,
    *,
    basket_id: str = "backtest",
    every_seconds: int = 3600,
) -> Basket:
    """The basket a recorded dataset implies — every instrument in it, on its own timeframes.

    Built from the dataset rather than from the database on purpose: a backtest is a
    self-contained experiment, and a basket carrying instruments the dataset has no prices for
    would abort every cycle as `DATA_STALE` and read as a fault of the system. The trading rules
    come from the manifest, so quantization matches the venue as it was when the prices were
    recorded (`marketdata/recorder.py`).
    """
    return Basket(
        basket_id=basket_id,
        name=f"Backtest over {dataset.manifest.source}",
        instruments=dataset.instruments,
        panel=panel,
        timeframes=dataset.timeframes,
        schedule=Schedule(every_seconds=every_seconds),
        ttl_buffer_seconds=min(60, every_seconds // 2),
    )


def dataset_catalogue(dataset: ReplayDataset) -> InstrumentCatalogue:
    """The rules a dataset's prices were recorded under, as its venue's catalogue.

    A replay is the one mode whose venue no longer exists to be asked, so the manifest *is* the
    venue: verifying a backtest basket against today's Binance would compare last year's prices to
    this year's filters and refuse a replay for having been recorded (ADR 0025).
    """
    return replay_catalogue(
        dataset.instruments, source=f"recorded dataset — {dataset.manifest.source}"
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
class PriceFeed:
    """A mode's price source, the catalogue answering for it, and the socket both hold open.

    The three travel together because they are one decision. Reading Binance's prices means
    quantizing against *Binance's* published rules and closing *Binance's* session; splitting them
    up is how a paper soak ends up sized against a rule set nobody fetched, or how an HTTP client
    outlives the shutdown that was supposed to release it.
    """

    prices: MarketDataProvider | None = None
    catalogue: InstrumentCatalogue | None = None
    closers: tuple[Callable[[], Awaitable[None]], ...] = ()


@dataclass(frozen=True, slots=True)
class VenueStack:
    """Everything one venue contributes, so `_assemble` never branches on which venue it is."""

    broker: BrokerAdapter
    prices: MarketDataProvider
    #: What this stack's instruments are quantized against — the venue whose *prices* are read,
    #: which is not always the venue taking the orders. A paper soak is `SimBroker` fed by live
    #: Binance data (DESIGN §9 rung 5): its orders go nowhere near a venue, but its lot sizes and
    #: minimum notionals have to be Binance's or the fills it simulates are not the fills the live
    #: system would get (ADR 0025).
    catalogue: InstrumentCatalogue
    #: The same prices, minus any side effect — what an observer may read.
    #:
    #: Required rather than defaulted, because the difference between the two is invisible at the
    #: call site and getting it wrong is silent. In the simulated stack `prices` is a *bridge*:
    #: reading it feeds the tick to `SimBroker`, which matches resting orders and becomes the
    #: reference price for the next market order. A dashboard reading that would let a chart
    #: refresh fill a stop, and let a chart left open on the 1d timeframe set the price a manual
    #: close executes at. Venue stacks read the venue, so for them the two are the same object.
    read_only_prices: MarketDataProvider
    calendar: TradingCalendar
    announcements: CorporateActionSource | None = None
    preflight: VenuePreflight | None = None
    #: Only a *simulated* venue can be handed its books back after a restart (`RestorableVenue`).
    venue_restore: RestorableVenue | None = None
    closers: tuple[Callable[[], Awaitable[None]], ...] = ()


@dataclass(frozen=True, slots=True)
class StackRequest:
    """What every venue builder needs, so the three share one signature and one dispatch table.

    `universe` is the union across every configured basket, not one basket's: a venue account is
    shared by all of them, so the broker, the reconciler and the market feed have to know about
    every instrument any basket might trade (DESIGN §4).

    It is a **callable, read at each use**, not the set that existed when the process was wired. A
    basket published from the dashboard is picked up by the resync sweep, and on a spot venue an
    instrument's base asset *is* its position — so a broker holding a boot-time set would refuse
    to trade the new instrument, drop its resting orders from `fetch_open_orders`, and fail to
    project its venue holding as a position, which reads to the reconciler as a position that
    vanished (ADR 0021, the same defect the Tier-2 cap had).
    """

    universe: Callable[[], tuple[Instrument, ...]]
    clock: Clock
    mode: Mode
    feed: PriceFeed
    start_equity: Decimal
    quote_currency: str


def _sim_stack(request: StackRequest) -> VenueStack:
    """`SimBroker` over whatever prices it is given — synthetic, replayed, or live.

    Live prices plus simulated fills is DESIGN §9's primary paper mode, and it is this same code:
    only the market-data provider changes, so nothing about the venue path differs between the
    simulation and the soak.

    The catalogue comes with the prices and is only defaulted when there are none to come with:
    unfed, this is the *simulated venue*, which publishes a recorded rule set of its own.
    """
    broker = SimBroker(
        request.clock,
        balances={request.quote_currency: request.start_equity},
        default_quote_currency=request.quote_currency,
    )
    source = request.feed.prices or SyntheticMarketData(request.clock)
    return VenueStack(
        broker=broker,
        prices=SimulatedMarket(source, broker),
        catalogue=request.feed.catalogue or sim_catalogue(),
        read_only_prices=source,
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
    broker = BinanceSpotBroker(trading_transport, clock, universe=request.universe)
    prices = request.feed.prices or binance_spot_market_data(data_transport, clock)
    return VenueStack(
        broker=broker,
        prices=prices,
        # Always this venue's own, never the caller's: the orders go here, so the rules that decide
        # whether they are legal are the ones this transport can be asked for.
        catalogue=VenueCatalogue(BinanceSpotGateway(data_transport, clock), clock),
        read_only_prices=prices,
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
    prices = request.feed.prices
    if prices is None:
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
    broker = AlpacaBroker(transport, clock, universe=request.universe)
    return VenueStack(
        broker=broker,
        prices=prices,
        # Alpaca publishes no rule set this system can read: Phase 3 built no equity gateway. The
        # catalogue therefore refuses by naming that, which is what makes an equity basket's rules
        # unverifiable *loudly* rather than accepted because nobody could check them (ADR 0025).
        catalogue=request.feed.catalogue
        or UnavailableCatalogue(
            broker.venue_id,
            AssetClass.EQUITY,
            "there is no equity VenueGateway in v1, so nothing can be asked what alpaca lists",
        ),
        read_only_prices=prices,
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


def enforced_policy(configs: ConfigStore, arming: ArmingStore, mode: Mode) -> EffectivePolicy:
    """The one answer to "which Tier-2 limits apply", for everything that needs to know.

    The wiring, each runner rebuild, `risk status` and the dashboard all read this, so none of
    them can report a limit another is enforcing. Both inputs are re-read on every call: the
    published policy is versioned configuration and the cap is the arming row, and either can move
    while the process is up.
    """
    return effective_policy(
        global_risk(configs).document, mode=mode, max_order_notional=arming.load().cap
    )


def global_risk(configs: ConfigStore) -> ConfigRecord[GlobalRiskPolicy]:
    """The published Tier-2 policy, or a refusal. A basket with no global limits does not run."""
    record = configs.global_risk()
    if record is None:
        raise ConfigError(
            "no Tier-2 policy is published; refusing to run a basket with no global limits"
        )
    return record


class RunnerBuilder:
    """Turns a stored basket into a running `BasketRunner` (`supervisor.RunnerFactory`).

    A basket owns its panel, and therefore the provider connections that panel opens; everything
    else — the ledger, the execution service, the monitor, the watchdog, the event store — is
    **shared**, because positions belong to the venue portfolio and not to any basket (DESIGN §4).
    Giving each basket its own ledger would give two baskets two views of one account, which is
    precisely the concentration failure Tier-2 exists to prevent.

    Each build re-reads the Tier-2 policy from the store *and the arming row*, then re-applies the
    mode's limits — the cap, and in live the ceiling of `control/live.py`. So a policy edited in the
    dashboard takes effect at the next cycle boundary, a cap armed while the process is up is
    picked up at the next stop→start, and neither can be edited away from underneath a runner
    (PLAN §2.4, DESIGN §9 rung 6, ADR 0021).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        mode: Mode,
        configs: ConfigStore,
        arming: ArmingStore,
        stack: VenueStack,
        store: EventStore,
        ledger: Ledger,
        execution: ExecutionService,
        monitor: ExecutionMonitor,
        watchdog: Watchdog,
        history: HistoryReader,
        news_feed: NewsFeed | None,
        quote_currency: str,
        marks: Marks,
    ) -> None:
        self._clock = clock
        self._mode = mode
        self._configs = configs
        self._arming = arming
        self._stack = stack
        self._store = store
        self._ledger = ledger
        self._execution = execution
        self._monitor = monitor
        self._watchdog = watchdog
        self._history = history
        self._news_feed = news_feed
        self._quote_currency = quote_currency
        #: One cache for the whole process, shared by every runner and the supervisor's sweep.
        self._marks = marks
        self._pools: dict[str, ProviderPool] = {}

    def calendar_for(self, basket: Basket) -> TradingCalendar:  # noqa: ARG002 — one venue in v1
        """The venue calendar. One venue per process in v1, so every basket shares it."""
        return self._stack.calendar

    async def build(self, record: ConfigRecord[Basket]) -> BasketRunner:
        basket = record.document
        policy_record = global_risk(self._configs)
        policy = enforced_policy(self._configs, self._arming, self._mode).policy
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
            # Sizing divides by ATR, so it has to be read from a timeframe the snapshot actually
            # carries. A basket configured for 4h/1d bars would otherwise ask for a 1h ATR that
            # was never computed, and every entry would veto for want of a volatility estimate.
            risk_timeframe=min(basket.timeframes, key=timeframe_interval, default="1h"),
            tier2=Tier2RiskEngine(policy),
            watchdog=self._watchdog,
            history=self._history,
            execution=self._execution,
            monitor=self._monitor,
            ledger=self._ledger,
            store=self._store,
            clock=self._clock,
            venue=self._stack.broker.venue_id,
            marks=self._marks,
            universe=lambda: configured_instruments(self._configs),
            global_policy=policy,
            config_refs=(record.ref, policy_record.ref),
            quote_currency=self._quote_currency,
            shadow=ShadowEvaluator(decision_engine, self._store) if basket.shadow_panel else None,
        )

    def _providers(self, record: ConfigRecord[Basket]) -> dict[str, LLMProvider]:
        """This basket's endpoints, wired once per basket and closed when it is released.

        Both panels' endpoints, on one pool: the challenger is deliberated by the same engine on
        the same connections, and `Basket` has already refused a provider id the two declare
        differently — so deduplicating by id here cannot silently pick one of two meanings.
        """
        pool = self._pools.get(record.ref.config_id)
        if pool is None:
            pool = build_providers(declared_providers(record.document), self._clock)
            self._pools[record.ref.config_id] = pool
        return pool.providers

    async def probe(self, record: ConfigRecord[Basket]) -> PanelProbeResult:
        """Prove this basket's *champion* panel can actually reach a model (live readiness).

        The champion only. A challenger that cannot be reached costs its own `SHADOW_EVALUATED`
        event an opinion and nothing else — it never trades, and its every exception is already
        caught and recorded (ADR 0018). Refusing to start live over the research panel would be
        refusing over the one panel that cannot affect an order.
        """
        return await probe_panel(
            record.document.panel, self._providers(record), label=record.ref.config_id
        )

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
) -> bool:
    """Put configuration in the store: what the caller stated, or a default on first run.

    Configuration lives in the database, so the composition root *publishes* it rather than
    holding it. A caller that states a basket or a policy explicitly is publishing a new
    version of it, which is why an existing row does not suppress the write — the alternative
    would silently ignore an argument, and a silently ignored risk policy is the worst kind.

    Returns whether the default basket was seeded, which is the only case in which `panel_id`
    chose anything: on a database that already holds baskets, the stored panel is what runs.
    """
    seeded = baskets is None and not configs.baskets()
    if baskets is None:
        baskets = (default_basket,) if seeded else ()
    for basket in baskets:
        await configs.put(basket.basket_id, basket, actor=ACTOR, note="published at startup")
    if global_policy is not None or configs.global_risk() is None:
        await configs.put(
            SINGLETON_ID,
            global_policy or GlobalRiskPolicy(),
            actor=ACTOR,
            note="published at startup",
        )
    return seeded


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


def panel_findings(baskets: Sequence[Basket]) -> tuple[str, ...]:
    """Every panel-reachability finding across the baskets that may cycle, named by basket.

    One rule (`reach_of`), one prefix, three readers: the wiring's warning, the dashboard's banner,
    and — in live only — `SupervisionController.blockers`, which refuses Start on it. A paused
    basket is skipped for the same reason readiness skips it: it cannot cycle, so a panel it
    cannot reach is not a fact about what this process is going to do (ADR 0023).
    """
    return tuple(
        f"basket {basket.basket_id!r}: {finding}"
        for basket in baskets
        if basket.status.may_trade
        for finding in reach_of(basket.panel).findings
    )


def declared_providers(basket: Basket) -> tuple[ProviderSettings, ...]:
    """Every endpoint this basket may reach — champion and challenger — deduplicated by id."""
    seen: dict[str, ProviderSettings] = {}
    for panel in basket.panels:
        seen.update({provider.provider_id: provider for provider in panel.providers})
    return tuple(seen.values())


def _maintenance_policy(configs: ConfigStore) -> MaintenancePolicy:
    """The published retention windows, or the model's defaults when none was published.

    Defaults rather than a refusal (spec §3.7): maintenance shares its tick with the daily backup,
    and refusing to back anything up because nobody published a retention policy would be
    fail-*useless*. The `MAINTENANCE_RAN` event records which windows were in force, defaults
    included, so the log always says what policy a pass ran under.
    """
    record = configs.latest(ConfigKind.MAINTENANCE, SINGLETON_ID)
    document = record.document if record else None
    return document if isinstance(document, MaintenancePolicy) else MaintenancePolicy()


def _maintenance_service(
    db_path: Path | None,
    *,
    store: EventStore,
    writer: SingleWriter,
    clock: Clock,
    mode: Mode,
    configs: ConfigStore,
) -> MaintenanceService | None:
    """The daily pass, or `None` for a database that lives inside its own connection.

    An in-memory database — the whole test suite — has no file to copy and nothing that would
    outlive the process to archive, so there is nothing for maintenance to do and a service built
    anyway would put filesystem writes under every test in the repo.

    The policy is passed as a **callable**, not a value: it is read fresh at each pass, so an edit
    on the Parameters page takes effect at the next tick with no restart (ADR 0021's rule).
    """
    if db_path is None:
        return None
    return MaintenanceService(
        store=store,
        writer=writer,
        clock=clock,
        mode=mode.value,
        archive_root=archive_destination(db_path),
        backup_dir=backup_destination(db_path),
        policy=lambda: _maintenance_policy(configs),
    )


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
    feed: PriceFeed,
    news_sources: tuple[str, ...],
    panel_id: str,
) -> Application:
    """Wire one mode. Everything above the venue is identical for all of them (DESIGN §5).

    Wiring live no longer asserts the arming preconditions: they are evaluated when supervision is
    asked to start, so an unarmed live process comes up able to *show* what is missing instead of
    refusing before there is anything to show it with (ADR 0021).
    """
    engine = create_database(db_path)
    writer = SingleWriter(engine)
    store = EventStore(engine, writer)
    states = RiskStateStore(engine, writer, clock)
    configs = ConfigStore(engine, writer, store, clock)

    seeded = await _publish(
        configs,
        baskets=baskets,
        global_policy=global_policy,
        default_basket=await demo_basket(sim_catalogue(), select_panel(panel_id)),
    )
    records = configs.baskets()
    if not records:
        raise ConfigError("no baskets are configured; nothing to run")
    configured = tuple(record.document for record in records)
    _warn_unused_panel(panel_id, seeded=seeded, records=records)
    await _record_panel_reach(store, clock, configured)
    instruments = _instruments_of(configured)
    quote_currency = _quote_currency(instruments)

    def universe() -> tuple[Instrument, ...]:
        """Read fresh everywhere the answer can change while the process runs.

        `instruments` above stays the boot snapshot only where the question is itself a boot-time
        one: the quote currency every basket must agree on, and the DESIGN §8.2 recovery, which
        completes before anything can be published.
        """
        return configured_instruments(configs)

    arming = ArmingStore(engine, writer, clock)
    policy = enforced_policy(configs, arming, mode).policy

    stack = _STACKS[broker_choice](
        StackRequest(universe, clock, mode, feed, start_equity, quote_currency)
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

    alert_sinks, alert_closers = build_sinks()
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
        universe=universe,
        announcements=stack.announcements,
    )

    # One price cache for the process. Shared like the ledger and for the same reason: equity is a
    # property of the portfolio, not of a basket, so basket A's cycle must refresh the mark basket
    # B is valued against (DESIGN §4, PHASE_12 Finding 2).
    marks = Marks()

    # One price cache for the process. Shared like the ledger and for the same reason: equity is a
    # property of the portfolio, not of a basket, so basket A's cycle must refresh the mark basket
    # B is valued against (DESIGN §4, PHASE_12 Finding 2).
    portfolio_watch = PortfolioWatch(
        ledger,
        marks,
        universe,
        watchdog,
        store,
        clock,
        # `read_only_prices`, never `prices`: in the sim stack the latter is a bridge that feeds
        # the tick to `SimBroker` and matches resting orders. A valuation sweep must observe the
        # market, never move it.
        market_data=stack.read_only_prices,
        catalogue=stack.catalogue,
        notional_currency=quote_currency,
        policy_of=lambda: enforced_policy(configs, arming, mode).policy,
        resync_seconds=DEFAULT_RESYNC_SECONDS,
    )

    drift = DriftWatch(stack.catalogue, configs, watchdog, states, store, clock, mode=mode)
    builder = RunnerBuilder(
        clock=clock,
        mode=mode,
        configs=configs,
        arming=arming,
        stack=stack,
        store=store,
        ledger=ledger,
        execution=execution,
        monitor=monitor,
        watchdog=watchdog,
        history=history,
        news_feed=news_feed,
        quote_currency=quote_currency,
        marks=marks,
    )
    return Application(
        mode=mode,
        clock=clock,
        store=store,
        ledger=ledger,
        supervisor=Supervisor(
            builder,
            configs,
            Scheduler(clock),
            watchdog,
            states,
            clock,
            drift=drift,
            portfolio=portfolio_watch,
        ),
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
            # A simulated venue's books die with the process; without this an ordinary restart is
            # indistinguishable from a testnet wipe.
            venue_restore=stack.venue_restore,
            preflight=stack.preflight,
            readiness=_readiness_for(
                mode,
                configs=configs,
                factory=builder,
                stack=stack,
                ledger=ledger,
                clock=clock,
                alert_sinks=alert_sinks,
            ),
            drift=drift,
            portfolio=portfolio_watch,
        ),
        watchdog=watchdog,
        states=states,
        broker=broker_choice,
        arming=arming,
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
            marks=marks,
            valuation=portfolio_watch.valuation,
        ),
        monitor=monitor,
        alerts=AlertDispatcher(
            store,
            AlertCursorStore(engine, writer, clock),
            alert_sinks,
            clock,
            calendar=stack.calendar,
        ),
        maintenance=_maintenance_service(
            db_path, store=store, writer=writer, clock=clock, mode=mode, configs=configs
        ),
        market_data=stack.read_only_prices,
        catalogue=stack.catalogue,
        quote_currency=quote_currency,
        marks=marks,
        portfolio_watch=portfolio_watch,
        _writer=writer,
        _closers=(
            builder.close,
            *news_closers,
            *alert_closers,
            *stack.closers,
            *feed.closers,
        ),
    )


def _readiness_for(
    mode: Mode,
    *,
    configs: ConfigStore,
    factory: RunnerBuilder,
    stack: VenueStack,
    ledger: Ledger,
    clock: Clock,
    alert_sinks: Sequence[AlertSink],
) -> LiveReadiness | None:
    """The live-only startup gates, or nothing at all (`control/readiness.py`).

    Sim and paper are *allowed* to run degraded — an unreachable panel is a `WAIT`, and a holed
    series is a `DATA_STALE` cycle. That is what those modes are for. Live is the mode where the
    same fault is holding real positions, so it refuses to begin instead.
    """
    if not mode.is_live:
        return None
    return LiveReadiness(
        configs=configs,
        factory=factory,
        market_data=stack.prices,
        ledger=ledger,
        clock=clock,
        venue=stack.broker.venue_id,
        alert_sinks=alert_sinks,
        panel_probe=factory.probe,
    )


def _warn_unused_panel(
    panel_id: str, *, seeded: bool, records: Sequence[ConfigRecord[Basket]]
) -> None:
    """Say so when `--panel` chose nothing, because a stored basket already carried one.

    A seeded panel is a *default for a fresh database*, not an override: a CLI flag that rewrote a
    panel edited in the dashboard would discard the operator's own configuration on the next run.
    So the flag is ignored and named as ignored — a silently inert flag is how someone concludes
    the panel is broken when it was simply never selected.
    """
    if seeded or panel_id == DEFAULT_PANEL_ID:
        return
    logger.warning(
        "--panel had no effect: this database already holds baskets, and a stored basket's own "
        "panel is what runs. Edit it on the dashboard's Configure page",
        extra={
            "requested_panel": panel_id,
            "panels_in_service": [record.document.panel.panel_id for record in records],
        },
    )


async def _record_panel_reach(store: EventStore, clock: Clock, baskets: Sequence[Basket]) -> None:
    """Put a panel that cannot be fully reached in the log, once, at wiring (ADR 0023).

    Once rather than per cycle: a cycle that decided nothing because seats abstained already
    records `PANEL_DEGRADED`, and an event repeated every cycle saying the same thing is an event
    nobody reads. What this adds is the *cause* — a key absent from this machine's environment —
    which no cycle event carries, and which is the difference between "the vendor was down" and
    "this was never configured" when the log is read months later.
    """
    findings = panel_findings(baskets)
    if not findings:
        return
    logger.warning("a configured panel cannot be fully reached", extra={"findings": list(findings)})
    await store.append(
        EventFactory(clock=clock, basket_id="global", cycle_id="startup").risk_event(
            tier=RiskTier.PANEL,
            rule="panel_unconfigured",
            scope="panel",
            action="degraded",
            detail="; ".join(findings),
        )
    )


async def _record_effective_policy(
    store: EventStore, clock: Clock, mode: Mode, effective: EffectivePolicy
) -> None:
    """Put the limits actually in force in the log, not just in two documents to be joined.

    Live clamps the published policy to `LIVE_CEILING`, so "what were the limits at 04:12 on the
    day of the incident" must be answerable from the event log alone — which is the compliance
    artifact (PLAN §3.3). Nothing is written when nothing was clamped: an event per start that
    always says the same thing is an event nobody reads.
    """
    if not effective.clamps:
        return
    logger.warning(
        "tier-2 limits clamped to the live ceiling",
        extra={"mode": mode.value, "clamps": [str(clamp) for clamp in effective.clamps]},
    )
    await store.append(
        EventFactory(clock=clock, basket_id="global", cycle_id="startup").risk_event(
            tier=RiskTier.TIER2,
            rule="live_ceiling",
            scope="portfolio",
            action="clamped",
            detail=effective.detail,
        )
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
    catalogue: InstrumentCatalogue | None = None,
    news_sources: tuple[str, ...] = (),
    panel_id: str = DEFAULT_PANEL_ID,
    broker: BrokerChoice = BrokerChoice.SIM,
) -> Application:
    """Wire the simulation stack: replayed data, a panel, and `SimBroker`.

    `market_data` overrides the synthetic series with any provider; `panel_id` swaps the scripted
    panel for real models. `SimBroker` still matches every fill, so no combination of the two can
    move real money.

    `catalogue` travels with `market_data` and answers for the same venue — a backtest replaying
    recorded Binance prices is quantized against the rules that dataset recorded, not against the
    simulated venue's (`marketdata/recorder.py`). Left unset, the prices are synthetic and so is
    the venue, which publishes its own recorded rule set.
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
        feed=PriceFeed(market_data, catalogue),
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
    catalogue: InstrumentCatalogue | None = None,
    news_sources: tuple[str, ...] = (),
    panel_id: str = DEFAULT_PANEL_ID,
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
        feed=_feed_for(broker, clock, market_data, catalogue),
        news_sources=news_sources,
        panel_id=panel_id,
    )


def _feed_for(
    broker: BrokerChoice,
    clock: Clock,
    market_data: MarketDataProvider | None,
    catalogue: InstrumentCatalogue | None,
) -> PriceFeed:
    """The prices a venue-backed mode reads, and the catalogue that answers for the same venue.

    Only the **simulated broker** needs a feed built here: it takes the orders itself, so nothing
    else in its stack is going to open a connection to a venue, and DESIGN §9 rung 5's primary
    paper shape is exactly that — real Binance prices, deterministic fills. Production prices, not
    the testnet's: nothing is being sent to a venue, so there is no book to agree with.

    A *venue* broker gets an empty feed on purpose. Its own stack builds one transport and hands
    the trading side that transport's limiter, so a feed built here would give one IP a second,
    independent rate budget — the failure ADR 0010 exists to prevent — plus an HTTP session nobody
    registers for shutdown. Alpaca gets nothing either way, and `_alpaca_stack` refuses by name.
    """
    if market_data is not None:
        return PriceFeed(market_data, catalogue)
    if broker is not BrokerChoice.SIM:
        return PriceFeed(catalogue=catalogue)
    provider, transport = live_binance_spot(clock, sandbox=False)
    return PriceFeed(
        provider,
        catalogue or VenueCatalogue(BinanceSpotGateway(transport, clock), clock),
        (transport.close,),
    )


async def build_live(
    *,
    clock: Clock | None = None,
    db_path: Path | None = None,
    baskets: tuple[Basket, ...] | None = None,
    global_policy: GlobalRiskPolicy | None = None,
    market_data: MarketDataProvider | None = None,
    catalogue: InstrumentCatalogue | None = None,
    news_sources: tuple[str, ...] = (),
    panel_id: str = DEFAULT_PANEL_ID,
    broker: BrokerChoice = BrokerChoice.BINANCE,
    start_equity: Decimal = ZERO,
) -> Application:
    """Wire the live stack. Real venue, real money, and five ways it will refuse.

    Structurally this is the paper wiring with a different `Mode`, and that is the point: the
    runner, the risk engines, the ledger and the event log are the *same objects* a soak proved
    (DESIGN §5). A separate live path would mean the thing that was tested is not the thing that
    trades. What live adds is subtraction — the ceiling of `control/live.py` tightens every Tier-2
    limit, and `control/readiness.py` refuses to begin unless alerting, the panel, the data and
    every stored configuration are actually working.

    `start_equity` is ignored beyond its signature: a real venue's balances are *discovered* by the
    startup reconciliation. Seeding a live ledger would invent funds and hide the first discrepancy.

    Arming is a human act and is never done here. What this returns is a live system that can be
    inspected, not one that trades: `SupervisionController.start` still demands the phrase, the
    armed row and the cap before a single cycle runs (ADR 0021). Credentials are the one
    precondition that must hold *here*, because a transport cannot be constructed without a key —
    and no dashboard could supply one, since keys are environment-only (PLAN §3.2).
    """
    if not broker.is_venue:
        raise ConfigError(
            "live mode cannot use the simulated broker: an order that is not sent to a venue is "
            "not a live order, and a mode that quietly simulates is the mode confusion PLAN §2.4 "
            "treats as catastrophic. Use --mode paper for simulated fills."
        )
    clock = clock or SystemClock()
    return await _assemble(
        clock,
        mode=Mode.LIVE,
        db_path=db_path,
        broker_choice=broker,
        start_equity=start_equity,
        baskets=baskets,
        global_policy=global_policy,
        # Live refuses the simulated broker above, so the feed is always empty here and the venue
        # stack builds its own prices, catalogue and transport — one connection, one rate budget.
        feed=_feed_for(broker, clock, market_data, catalogue),
        news_sources=news_sources,
        panel_id=panel_id,
    )


async def build(mode: Mode, **kwargs: object) -> Application:
    """Wire the stack for `mode`. One dispatch, no defaults, nothing that degrades quietly.

    A mode that quietly does something other than what was asked is the mode confusion PLAN §2.4
    treats as catastrophic, so no builder here falls back to another.
    """
    if mode is Mode.SIM:
        return await build_sim(**kwargs)  # type: ignore[arg-type]
    if mode is Mode.PAPER:
        return await build_paper(**kwargs)  # type: ignore[arg-type]
    return await build_live(**kwargs)  # type: ignore[arg-type]
