"""Live readiness: what must be *working*, not merely permitted, before real money moves.

`control/arming.py` answers "is this system allowed to trade live" — four facts a human put in
place. This module answers the different question of whether it can actually do the job today, and
it asks it in the only place the answer is trustworthy: at startup, against the real dependencies,
before the first cycle. Four gates, each a documented way a live run goes wrong quietly:

* **Alerting has a destination.** Every other control in the system ends in "halt and tell
  someone". Live starting with nothing configured means the telling never happens, and a halted
  live account with open positions is discovered by whoever looks first (DESIGN §8.3, ADR 0019).
* **The panel is real and can be reached.** No seat may bind the offline stub — not even as a
  fallback, which would put a real order one outage away from canned text — and the probe is a
  real sixteen-token completion down each seat's chain, the only thing that proves a model id
  still resolves and a key is still accepted (`decision/probe.py`, R11).
* **Market data arrives complete.** Fresh, deep enough for the indicators, and **without gaps**.
  ATR sizes every position, and an ATR computed across a hole in the tape is a stop distance
  derived from a bar the venue never published (DESIGN §6.2, §6.6).
* **Every configuration builds, for this venue.** The stored baskets are built through the real
  runner factory, so a panel whose `secret_ref` is missing, an unknown indicator, or an absent
  Tier-2 policy is a refusal now rather than a failed cycle later, holding a position, at 03:00 —
  and an instrument belonging to another venue is named as such rather than surfacing as a data
  fault three steps downstream.

Two deliberate asymmetries. A seat answering on its *fallback* binding is a warning, not a
failure — the chain exists so an outage is survivable, and refusing over a healthy fallback would
make the fallback pointless. And a basket that is paused has its configuration checked but is
neither probed nor fetched for: it cannot cycle, and spending provider calls on it would make
every live start cost more than it needs to.

Failure semantics: `run` never raises — it mirrors `control/preflight.py`, returning findings that
the startup sequence turns into a halt. The process stays **up and not trading**, which is the
only state from which an operator can ask what went wrong (DESIGN §8.2 step 5).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.control.context_builder import ContextBuilder
from tradebot.control.supervisor import RunnerFactory
from tradebot.core.clock import Clock
from tradebot.core.config import Basket
from tradebot.core.enums import ProviderKind
from tradebot.core.errors import TradebotError
from tradebot.core.instrument import Instrument
from tradebot.core.logging import get_logger
from tradebot.core.market import CandleSeries
from tradebot.decision.probe import PanelProbeResult
from tradebot.indicators.library import required_history
from tradebot.interfaces.alerts import AlertSink
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.ledger.portfolio import Ledger

logger = get_logger(__name__)

#: Probes a basket's panels against the endpoints that basket declares. Supplied by the
#: composition root, which is the only thing that owns provider connections.
PanelProbe = Callable[[ConfigRecord[Basket]], Awaitable[PanelProbeResult]]


class LiveReadiness:
    """The live-only startup gates. Constructed for no other mode."""

    def __init__(
        self,
        *,
        configs: ConfigStore,
        factory: RunnerFactory,
        market_data: MarketDataProvider,
        ledger: Ledger,
        clock: Clock,
        venue: str,
        alert_sinks: Sequence[AlertSink] = (),
        panel_probe: PanelProbe | None = None,
    ) -> None:
        self._configs = configs
        self._factory = factory
        self._market_data = market_data
        self._ledger = ledger
        self._clock = clock
        self._venue = venue
        self._alert_sinks = tuple(alert_sinks)
        self._panel_probe = panel_probe

    async def run(self) -> tuple[str, ...]:
        """Every finding, so an operator fixes the whole list rather than one refusal per start."""
        failures = [*self._check_alerting()]
        for record in self._configs.baskets():
            failures.extend(await self._check_basket(record))
        if not failures:
            logger.warning("live readiness passed; the system is clear to trade real money")
        return tuple(failures)

    def _check_alerting(self) -> tuple[str, ...]:
        if self._alert_sinks:
            return ()
        return (
            "no ops alert destination is configured; live refuses to run unheard. Set "
            "TRADEBOT_ALERT_WEBHOOK_URL, or both TRADEBOT_TELEGRAM_BOT_TOKEN and "
            "TRADEBOT_TELEGRAM_CHAT_ID (docs/OPERATIONS.md)",
        )

    async def _check_basket(self, record: ConfigRecord[Basket]) -> tuple[str, ...]:
        """Configuration for every basket; connections and data only for one that may cycle."""
        basket = record.document
        failures = [*await self._check_config(record), *self._check_venue(record)]
        if failures or not basket.status.may_trade:
            return tuple(failures)
        failures.extend(await self._check_panels(record))
        failures.extend(await self._check_data(basket))
        return tuple(failures)

    def _check_venue(self, record: ConfigRecord[Basket]) -> tuple[str, ...]:
        """Every instrument must belong to the venue this process is wired to.

        A fresh database seeds a demo basket on the `sim` venue. Wired to a real exchange, its
        instruments would be priced and quantized against trading rules recorded for a different
        market — so this refuses by name rather than letting it surface later as a data fault.
        """
        foreign = [i.key for i in record.document.instruments if i.venue != self._venue]
        if not foreign:
            return ()
        return (
            f"basket {record.ref.config_id!r} holds instruments on another venue "
            f"({', '.join(foreign)}) while this process is wired to {self._venue!r}; "
            "publish a basket for this venue before trading it",
        )

    async def _check_config(self, record: ConfigRecord[Basket]) -> tuple[str, ...]:
        """Build this basket for real. Nothing here restates a rule the factory already enforces.

        The runner is discarded; the provider connections it opened are not, because the
        supervisor is about to ask for the same basket and the factory caches them per basket.
        """
        try:
            await self._factory.build(record)
        except TradebotError as exc:
            return (f"basket {record.ref.config_id!r} does not build: {exc}",)
        return ()

    async def _check_panels(self, record: ConfigRecord[Basket]) -> tuple[str, ...]:
        scripted = _scripted_bindings(record.document)
        if scripted:
            return (
                f"basket {record.ref.config_id!r} deliberates on a stub provider "
                f"({', '.join(scripted)}); the stub returns canned JSON, so live would place real "
                "orders from a script. Publish a panel of real providers before arming live",
            )
        if self._panel_probe is None:
            return ()
        try:
            result = await self._panel_probe(record)
        except TradebotError as exc:
            return (f"basket {record.ref.config_id!r} panel probe failed: {exc}",)
        if result.substitutions:
            logger.warning(
                "a seat answered on a fallback binding; heterogeneity is already reduced",
                extra={
                    "basket_id": record.ref.config_id,
                    "substitutions": list(result.substitutions),
                },
            )
        return result.failures

    async def _check_data(self, basket: Basket) -> tuple[str, ...]:
        """Fetch what this basket decides on, and refuse anything short, stale, or holed.

        The builder is constructed here rather than borrowed, so the freshness budget and the fetch
        depth are the cycle's own — nothing in this module restates a data rule. Constructing it
        validates the basket's timeframes and indicators, which is a refusal rather than a raise:
        `run` never raises, whatever it finds.
        """
        try:
            builder = ContextBuilder(
                self._market_data,
                self._ledger,
                self._clock,
                timeframes=basket.timeframes,
                indicators=basket.indicators,
            )
        except TradebotError as exc:
            return (f"basket {basket.basket_id!r} cannot build a context: {exc}",)
        depth = required_history(builder.indicators)
        failures: list[str] = []
        for instrument in basket.instruments:
            for timeframe in builder.timeframes:
                failures.extend(await self._check_series(builder, instrument, timeframe, depth))
        return tuple(failures)

    async def _check_series(
        self, builder: ContextBuilder, instrument: Instrument, timeframe: str, depth: int
    ) -> tuple[str, ...]:
        try:
            series = await builder.series_for(instrument, timeframe)
        except TradebotError as exc:
            return (f"{instrument.key} {timeframe}: {exc}",)
        return _series_findings(series, depth)


def _scripted_bindings(basket: Basket) -> tuple[str, ...]:
    """Champion bindings served by the offline stub, anywhere in a seat's chain.

    A *fallback* to a stub is as disqualifying as a primary one: it means one vendor outage away
    from an order sized by canned text. The stub is what makes the demo free and the suite
    repeatable, and it is the one provider that must never be reachable from a live cycle.
    """
    kinds = {provider.provider_id: provider.kind for provider in basket.panel.providers}
    return tuple(
        f"{seat.seat_id}→{binding.fingerprint}"
        for seat in basket.panel.seats
        for binding in seat.bindings
        if kinds.get(binding.provider_id) is ProviderKind.STUB
    )


def _series_findings(series: CandleSeries, depth: int) -> tuple[str, ...]:
    """What is wrong with a series that arrived. Freshness was already asserted by the fetch.

    Gaps are reported with their edges rather than counted, because the interesting question when
    one shows up is *when* the venue stopped publishing — a maintenance window, a halt, or a feed
    that has been quietly lagging since the last restart.
    """
    findings: list[str] = []
    usable = len(series.indicator_window())
    if usable < depth:
        findings.append(
            f"{series.instrument_key} {series.timeframe}: {usable} usable bars, and the "
            f"configured indicators need {depth}; every cycle would abort before sizing anything"
        )
    gaps = series.gaps
    if gaps:
        edges = ", ".join(f"{start.isoformat()}..{end.isoformat()}" for start, end in gaps[:3])
        findings.append(
            f"{series.instrument_key} {series.timeframe}: {len(gaps)} gap(s) in the tape "
            f"({edges}{', …' if len(gaps) > 3 else ''}); indicators computed across a hole size "
            "positions against bars the venue never published"
        )
    return tuple(findings)
