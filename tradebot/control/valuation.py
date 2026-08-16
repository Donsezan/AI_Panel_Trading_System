"""The continuous portfolio sweep: refresh every mark, then measure the drawdown (DESIGN §5, §6.6).

DESIGN describes a watchdog that "monitors the reconciled portfolio continuously" and can act
"without waiting for a cycle". Before this it was a per-cycle gate, so a fully paused system stopped
measuring its own drawdown entirely. This is that sweep, and it does two jobs that must stay
together:

* **It refreshes the marks.** Cycles only mark the instruments of the basket that is cycling, so
  without a shared sweep a paused, halted or quarantined basket's position would go unmarked and
  freeze the whole portfolio — a system-wide denial caused by a routine operator action. It is what
  makes mark freshness independent of basket cadence, and therefore what makes the staleness
  tolerance a real limit rather than a number chosen to avoid tripping (PHASE_12 D1).
* **It measures.** The same `Watchdog.check` a cycle's gate calls, on the same aggregate, so a
  breach is caught between cycles rather than at the next one.

It reads **`read_only_prices`**, never the simulated stack's bridge. In the sim stack — which is
also the primary paper venue — reading `prices` feeds the tick to `SimBroker`, matching resting
orders and setting the reference price of the next market order. A valuation sweep running every
thirty seconds must never do that (ADR 0024's rule, arriving through a second door).

Failure semantics: a sweep that cannot reach the venue leaves the previous marks to age out, and
aging out is a freeze. It never raises past `sweep`, never halts a basket, and never trips the
switch — an unreachable venue is not a breach, and the freeze it eventually causes clears on its
own the moment prices return.
"""

from __future__ import annotations

from collections.abc import Callable

from tradebot.control.config_store import ConfigStore
from tradebot.control.reference import configured_instruments
from tradebot.core.clock import Clock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.errors import ConfigError, TradebotError
from tradebot.core.instrument import Instrument, base_currencies_of
from tradebot.core.logging import get_logger
from tradebot.core.money import ZERO
from tradebot.interfaces.exchange import InstrumentCatalogue
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import Ledger
from tradebot.marketdata.catalogue import instrument_for
from tradebot.risk.aggregate import USD_STABLECOINS, PortfolioAggregate, aggregate
from tradebot.risk.watchdog import Watchdog

logger = get_logger(__name__)

#: How much fresher than the sweep the tolerance must be. A tolerance merely *equal* to the cadence
#: freezes on any jitter at all; three sweeps is enough that a transient venue failure costs a
#: warning rather than a stopped portfolio.
MIN_TOLERANCE_MULTIPLE = 3


class PortfolioWatch:
    """Refreshes the marks the whole portfolio is valued against, and checks the baselines."""

    def __init__(
        self,
        ledger: Ledger,
        marks: Marks,
        configs: ConfigStore,
        watchdog: Watchdog,
        clock: Clock,
        *,
        market_data: MarketDataProvider | None,
        catalogue: InstrumentCatalogue,
        notional_currency: str,
        policy_of: Callable[[], GlobalRiskPolicy],
        resync_seconds: float,
    ) -> None:
        self._ledger = ledger
        self._marks = marks
        self._configs = configs
        self._watchdog = watchdog
        self._clock = clock
        #: The source *under* the sim stack's bridge — reading it cannot move the venue.
        self._market_data = market_data
        self._catalogue = catalogue
        self._notional = notional_currency
        self._policy_of = policy_of
        self._assert_tolerance_outlives_the_sweep(resync_seconds)
        #: Currency → the synthetic `{CUR}/{notional}` instrument that prices it, or `None` once
        #: the catalogue has said it lists no such market. Cached because the answer is reference
        #: data: it cannot change without a catalogue refresh, and re-asking every thirty seconds
        #: would spend venue weight to be told the same thing.
        self._currency_markets: dict[str, Instrument | None] = {}

    def _assert_tolerance_outlives_the_sweep(self, resync_seconds: float) -> None:
        """Refuse to wire a tolerance the sweep cannot possibly keep inside.

        A tolerance below the resync cadence means every mark is stale before the next refresh, so
        the portfolio freezes permanently and nothing trades. Caught here rather than in the model,
        because `core/` may not import the supervisor's cadence — and caught at wiring rather than
        at 03:00 (PHASE_12 §3.2).
        """
        tolerance = self._policy_of().mark_tolerance.total_seconds()
        if tolerance < resync_seconds * MIN_TOLERANCE_MULTIPLE:
            raise ConfigError(
                f"mark_staleness_seconds is {tolerance:.0f}s but the portfolio is only swept every "
                f"{resync_seconds:.0f}s; a tolerance below {MIN_TOLERANCE_MULTIPLE}× the sweep "
                "freezes the portfolio permanently and nothing would ever trade"
            )

    async def sweep(self) -> PortfolioAggregate:
        """Refresh every mark the portfolio needs, then evaluate the baselines."""
        await self.refresh()
        valuation = self.valuation()
        await self._watchdog.check(valuation)
        if valuation.frozen:
            logger.warning("the portfolio cannot be valued", extra={"why": valuation.frozen_reason})
        return valuation

    def valuation(self) -> PortfolioAggregate:
        """The aggregate as it stands, without touching the venue."""
        return aggregate(
            {self._ledger.venue: self._ledger},
            configured_instruments(self._configs),
            self._marks,
            self._policy_of(),
            as_of=self._clock.now(),
            notional_currency=self._notional,
        )

    async def refresh(self) -> None:
        """Mark every held position and every balance that needs a price. Never raises."""
        if self._market_data is None:
            return
        for instrument in await self._to_mark():
            try:
                self._marks.observe_quote(await self._market_data.get_quote(instrument))
            except TradebotError as exc:
                # Left to age out rather than dropped. An unreachable venue is not a breach, and
                # the freeze staleness eventually causes is the correct, self-clearing response.
                logger.warning(
                    "could not refresh a mark",
                    extra={"instrument": instrument.key, "error": str(exc)},
                )

    async def _to_mark(self) -> tuple[Instrument, ...]:
        """Held positions, plus the currencies whose balances nothing else can value.

        A position in an instrument no longer in any basket has no `Instrument` to fetch a quote
        for, so it cannot be marked and the portfolio freezes. The remedy is to keep the instrument
        configured or close the position by hand — the same constraint `manual_close.closable()`
        already imposes, for the same reason (PHASE_12 §3.6).
        """
        universe = configured_instruments(self._configs)
        by_key = {instrument.key: instrument for instrument in universe}
        held = tuple(
            instrument
            for position in self._ledger.positions()
            if not position.is_flat and (instrument := by_key.get(position.instrument_key))
        )
        return held + await self._currencies_to_price(universe)

    async def _currencies_to_price(
        self, universe: tuple[Instrument, ...]
    ) -> tuple[Instrument, ...]:
        """Synthetic `{CUR}/{notional}` instruments for balances at rung 4 of `value_cash`.

        Only what the earlier rungs cannot answer: the notional currency, a USD stablecoin and a
        configured base asset are all already valued, and asking the venue about them would spend
        weight to learn nothing.
        """
        already = base_currencies_of(universe) | USD_STABLECOINS | {self._notional}
        wanted = sorted(
            balance.currency
            for balance in self._ledger.snapshot().balances
            if balance.currency not in already and balance.total != ZERO
        )
        resolved = [await self._market_for(currency) for currency in wanted]
        return tuple(instrument for instrument in resolved if instrument is not None)

    async def _market_for(self, currency: str) -> Instrument | None:
        """The venue market pricing this currency in the notional one, if it lists one.

        Cached — including the negative answer. It is reference data and cannot change without a
        catalogue refresh, so re-asking every sweep would spend venue weight to be told the same
        thing, and would log the same warning every thirty seconds.
        """
        if currency in self._currency_markets:
            return self._currency_markets[currency]
        try:
            markets = await self._catalogue.list_markets()
        except TradebotError as exc:
            # Not cached: the catalogue was unreachable, which is not an answer about this
            # currency. Caching it would make one bad second permanent.
            logger.warning(
                "could not ask the catalogue how to value a balance",
                extra={"currency": currency, "error": str(exc)},
            )
            return None
        market = next(
            (
                m
                for m in markets
                if m.base_currency == currency and m.quote_currency == self._notional and m.tradable
            ),
            None,
        )
        resolved = (
            instrument_for(market, self._catalogue.venue_id, self._catalogue.asset_class)
            if market is not None
            else None
        )
        if resolved is None:
            logger.warning(
                "no venue market prices this balance in the notional currency; the portfolio will "
                "freeze until it is converted or an instrument is configured for it",
                extra={"currency": currency, "notional": self._notional},
            )
        self._currency_markets[currency] = resolved
        return resolved
