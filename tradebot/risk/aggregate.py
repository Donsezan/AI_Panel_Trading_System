"""The `PortfolioAggregate`: every venue portfolio summed into one USD-valued view (DESIGN §4).

**This is the one function that answers "what is the portfolio worth."** Every consumer reads it
— the Tier-2 watchdog's drawdown and daily-loss baselines, Tier-1's basket budget, Tier-2's
exposure ceilings, the dashboard, `risk rearm` and the reconciler's mismatch tolerance. A second
implementation is a bug by construction: six call sites each building their own price map is how
the drawdown gate came to measure cost basis and see no unrealized loss at all (PHASE_12
Finding 1).

Equity is **mark-to-market**: cash valued in the notional currency, plus each position at its
current mark. Every balance is valued, counted as a position, or freezes — none is silently worth
zero, which is what made 1,000 USDT beside 9,000 USDC read as 1,000 (Finding 3).

USD stablecoins are valued at par **with a sanity check**. Valuing a depegged stablecoin at 1.00
overstates equity, and every percentage-based risk limit is computed against equity — so a depeg
silently loosens all of them at exactly the moment the market is least safe.

Failure semantics: an aggregate that cannot be valued is `frozen`, and a frozen aggregate blocks
new orders rather than falling back to a guess. **A missing or stale price is a freeze, never a
fallback to cost.** Valuing a position at what it cost is not the conservative choice — it reports
zero drawdown on a portfolio that has halved, which is exactly the defect this module was changed
to fix. Freezing does not trip the kill switch: the switch is for breaches, and this is ignorance.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal

from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.instrument import Instrument, base_currencies_of
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.ledger.marks import Marks
from tradebot.ledger.portfolio import Ledger

#: Valued at par unless a quote says otherwise. Not a hardcoded price — a hardcoded *assumption*
#: that the check below exists to falsify.
USD_STABLECOINS: frozenset[str] = frozenset({"USDT", "USDC", "DAI", "BUSD", "TUSD", "USD"})


def value_cash(
    currency: str,
    amount: Decimal,
    marks: Marks,
    *,
    notional_currency: str,
    position_currencies: frozenset[str],
    now: UtcDatetime,
    tolerance: timedelta,
) -> Decimal | None:
    """What a balance is worth in the notional currency, or `None` if nothing can say.

    Four rungs, first match wins, and **their order is load-bearing**. Rung 3 precedes rung 4
    because a spot venue's base asset is both a balance and a position: `BTC` is a configured
    instrument's base asset *and* a currency with a `BTC/USDT` market, so reaching rung 4 first
    would value every holding twice (PHASE_12 §3.3).

    `None` is not zero. A balance with no admissible valuation means "we do not know what this
    portfolio is worth", and the caller's answer to that is a frozen aggregate — never a silently
    zero-valued balance, which is what made 1,000 USDT + 9,000 USDC read as 1,000 (Finding 3).
    """
    if currency == notional_currency:
        return amount
    if currency in USD_STABLECOINS:
        # Par is the *assumption*; `_peg_check` is what falsifies it, and it freezes the whole
        # aggregate rather than discounting a number here — a depeg is not a valuation nuance.
        return amount
    if currency in position_currencies:
        return ZERO
    mark = marks.price_of(currency, now=now, tolerance=tolerance)
    return None if mark is None else multiply(amount, mark)


class VenueSlice(DomainModel):
    """One venue's contribution to the aggregate."""

    venue: str
    equity: Money
    exposure: Money


class PortfolioAggregate(DomainModel):
    """Read-only summary across every venue portfolio, valued in the notional currency."""

    equity: Money
    #: Cash alone, in the notional currency. Held separately so a reader can tell 10,000 of cash
    #: from 10,000 of marked holdings — drawdown behaves very differently about the two — without
    #: a second summation somewhere else.
    cash: Money = ZERO
    gross_exposure: Money
    #: Pairs rather than a dict: a `frozen` model containing a mutable mapping is only frozen
    #: by convention, and "the aggregate the decision saw" has to be a checkable claim.
    per_instrument: tuple[tuple[str, Money], ...] = ()
    venues: tuple[VenueSlice, ...] = ()
    frozen_reason: str = ""
    as_of: UtcDatetime

    @property
    def frozen(self) -> bool:
        """A frozen aggregate cannot back a risk decision, so nothing new may be sent."""
        return bool(self.frozen_reason)

    def exposure_of(self, *instrument_keys: str) -> Money:
        """Value deployed across a set of instruments — one, a basket, or a whole cluster."""
        wanted = frozenset(instrument_keys)
        return sum((value for key, value in self.per_instrument if key in wanted), start=ZERO)


def peg_deviation_pct(prices: Mapping[str, Decimal], currency: str) -> Decimal:
    """How far a stablecoin's observed USD price sits from par, 0–100. Unquoted reads as par."""
    observed = prices.get(currency)
    if observed is None or observed <= ZERO:
        return ZERO
    return multiply(divide(abs(observed - Decimal(1)), Decimal(1)), Decimal(100))


def aggregate(
    ledgers: Mapping[str, Ledger],
    universe: tuple[Instrument, ...],
    marks: Marks,
    policy: GlobalRiskPolicy,
    *,
    as_of: UtcDatetime,
    notional_currency: str,
) -> PortfolioAggregate:
    """The one answer to "what is this portfolio worth", in the notional currency.

    `universe` is **every configured instrument**, never one basket's. Every number here except a
    basket's own exposure is a portfolio-wide question, and answering it from one basket's slice
    is what let `max_gross_exposure` be enforced against a single basket while its rule claimed to
    span all of them (PHASE_12 Finding 6).

    A position or a balance that cannot be valued **freezes** the aggregate rather than falling
    back to cost. Freezing blocks new orders; it does not trip the kill switch, because the switch
    is for breaches and this is ignorance (PHASE_12 §1.4).
    """
    tolerance = policy.mark_tolerance
    position_currencies = base_currencies_of(universe)

    cash_by_venue: dict[str, Decimal] = {}
    unvaluable: set[str] = set()
    for venue, ledger in ledgers.items():
        total, refused = _cash_of(
            ledger,
            marks,
            notional_currency=notional_currency,
            position_currencies=position_currencies,
            now=as_of,
            tolerance=tolerance,
        )
        cash_by_venue[venue] = total
        unvaluable.update(refused)
    cash = sum(cash_by_venue.values(), start=ZERO)

    held = frozenset(
        position.instrument_key
        for ledger in ledgers.values()
        for position in ledger.positions()
        if not position.is_flat
    )
    prices = {
        key: price
        for key in held
        if (price := marks.price_of(key, now=as_of, tolerance=tolerance)) is not None
    }

    frozen = _frozen_reason(
        unmarked=tuple(sorted(held - prices.keys())),
        unvaluable=tuple(sorted(unvaluable)),
        peg=_peg_check(ledgers, policy, marks, now=as_of, tolerance=tolerance),
    )
    if frozen:
        # Short-circuit, and it is not an optimisation. `Ledger.exposure` raises on a held key it
        # was given no price for — deliberately, so no caller can reintroduce the cost fallback —
        # so computing exposures here would turn a freeze into a crash. A frozen aggregate quotes
        # no figures at all: that is what "we do not know what this is worth" means.
        return PortfolioAggregate(
            equity=ZERO, cash=cash, gross_exposure=ZERO, frozen_reason=frozen, as_of=as_of
        )

    keys = tuple(instrument.key for instrument in universe)
    by_instrument = tuple(
        (
            instrument.key,
            sum(
                (ledger.exposure((instrument.key,), prices) for ledger in ledgers.values()),
                start=ZERO,
            ),
        )
        for instrument in universe
    )
    slices = tuple(
        VenueSlice(
            venue=venue,
            equity=cash_by_venue[venue] + _holdings_of(ledger, prices),
            exposure=ledger.exposure(keys, prices),
        )
        for venue, ledger in sorted(ledgers.items())
    )
    return PortfolioAggregate(
        equity=sum((s.equity for s in slices), start=ZERO),
        cash=cash,
        gross_exposure=sum((s.exposure for s in slices), start=ZERO),
        per_instrument=by_instrument,
        venues=slices,
        frozen_reason="",
        as_of=as_of,
    )


def _cash_of(
    ledger: Ledger,
    marks: Marks,
    *,
    notional_currency: str,
    position_currencies: frozenset[str],
    now: UtcDatetime,
    tolerance: timedelta,
) -> tuple[Decimal, tuple[str, ...]]:
    """One ledger's cash in the notional currency, and the currencies nothing could value."""
    total, refused = ZERO, []
    for balance in ledger.snapshot().balances:
        valued = value_cash(
            balance.currency,
            balance.total,
            marks,
            notional_currency=notional_currency,
            position_currencies=position_currencies,
            now=now,
            tolerance=tolerance,
        )
        if valued is None:
            # Only a *non-zero* balance freezes. Dust already converted away has the same value in
            # every currency, and stopping a live account trading over a residual is fail-useless
            # rather than fail-closed (PHASE_12 D2).
            if balance.total != ZERO:
                refused.append(balance.currency)
            continue
        total += valued
    return total, tuple(sorted(refused))


def _holdings_of(ledger: Ledger, prices: Mapping[str, Decimal]) -> Decimal:
    """Marked value of everything this ledger holds. Every held key is priced by here."""
    return sum(
        (
            position.market_value(prices[position.instrument_key])
            for position in ledger.positions()
            if not position.is_flat
        ),
        start=ZERO,
    )


def _frozen_reason(*, unmarked: tuple[str, ...], unvaluable: tuple[str, ...], peg: str) -> str:
    """Why this portfolio cannot be valued, in the order an operator can act on.

    The peg comes first: a depeg is a market event with an immediate response, while an unmarked
    position is usually a feed that will recover on its own.
    """
    if peg:
        return peg
    if unmarked:
        return (
            f"no fresh mark for {', '.join(unmarked)}; a stale mark is not a mark, so the "
            "portfolio cannot be valued and no new order may be sent"
        )
    if unvaluable:
        return (
            f"balances in {', '.join(unvaluable)} have no admissible valuation in the notional "
            "currency, so equity is unknown"
        )
    return ""


def _peg_check(
    ledgers: Mapping[str, Ledger],
    policy: GlobalRiskPolicy,
    marks: Marks,
    *,
    now: UtcDatetime,
    tolerance: timedelta,
) -> str:
    """Freeze the aggregate if a held USD stablecoin has drifted beyond tolerance.

    Now actually fed. Every call site used to pass an empty `stablecoin_prices`, so this could not
    fire and the depeg guard had never run in production (PHASE_12 Finding 5). A stablecoin with
    no mark still reads as par — the assumption stands until a quote falsifies it, which is
    precisely what `USD_STABLECOINS` documents.
    """
    held = {
        currency
        for ledger in ledgers.values()
        for balance in ledger.snapshot().balances
        if (currency := balance.currency) in USD_STABLECOINS and balance.total > ZERO
    }
    for currency in sorted(held):
        observed = marks.price_of(currency, now=now, tolerance=tolerance)
        quoted = {currency: observed} if observed is not None else {}
        deviation = peg_deviation_pct(quoted, currency)
        if deviation > policy.stablecoin_peg_tolerance_pct:
            return (
                f"{currency} is {deviation}% off par, beyond the "
                f"{policy.stablecoin_peg_tolerance_pct}% tolerance; equity cannot be valued"
            )
    return ""
