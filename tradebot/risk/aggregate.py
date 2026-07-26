"""The `PortfolioAggregate`: every venue portfolio summed into one USD-valued view (DESIGN §4).

Cross-venue Tier-2 rules and the equity curve read this rather than any single ledger, because
the concentration risk that matters is the one nobody's individual venue can see.

USD stablecoins are valued at par **with a sanity check**. Valuing a depegged stablecoin at
1.00 overstates equity, and every percentage-based risk limit is computed against equity — so a
depeg silently loosens all of them at exactly the moment the market is least safe. Beyond
`stablecoin_peg_tolerance_pct` the aggregate is marked unusable and new orders stop.

Failure semantics: an aggregate that cannot be valued is `frozen`, and a frozen aggregate blocks
new orders rather than falling back to a guess. Missing prices are treated the same way the
ledger treats them — valued at cost, never dropped, because dropping a holding understates
exposure and loosens the limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime
from tradebot.ledger.portfolio import Ledger

#: Valued at par unless a quote says otherwise. Not a hardcoded price — a hardcoded *assumption*
#: that the check below exists to falsify.
USD_STABLECOINS: frozenset[str] = frozenset({"USDT", "USDC", "DAI", "BUSD", "TUSD", "USD"})


class VenueSlice(DomainModel):
    """One venue's contribution to the aggregate."""

    venue: str
    equity: Money
    exposure: Money


class PortfolioAggregate(DomainModel):
    """Read-only summary across every venue portfolio."""

    equity: Money
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
    instruments: tuple[Instrument, ...],
    prices: Mapping[str, Decimal],
    policy: GlobalRiskPolicy,
    *,
    as_of: UtcDatetime,
    quote_currency: str = "USDT",
    stablecoin_prices: Mapping[str, Decimal] | None = None,
) -> PortfolioAggregate:
    """Sum every venue portfolio into one USD-valued summary."""
    frozen = _peg_check(ledgers, policy, stablecoin_prices or {})
    by_instrument = tuple(
        (
            instrument.key,
            sum(
                (ledger.exposure((instrument.key,), prices) for ledger in ledgers.values()),
                start=ZERO,
            ),
        )
        for instrument in instruments
    )
    slices = tuple(
        VenueSlice(
            venue=venue,
            equity=ledger.equity(prices, quote_currency=quote_currency),
            exposure=ledger.exposure(tuple(i.key for i in instruments), prices),
        )
        for venue, ledger in sorted(ledgers.items())
    )
    return PortfolioAggregate(
        equity=sum((s.equity for s in slices), start=ZERO),
        gross_exposure=sum((s.exposure for s in slices), start=ZERO),
        per_instrument=by_instrument,
        venues=slices,
        frozen_reason=frozen,
        as_of=as_of,
    )


def _peg_check(
    ledgers: Mapping[str, Ledger],
    policy: GlobalRiskPolicy,
    stablecoin_prices: Mapping[str, Decimal],
) -> str:
    """Freeze the aggregate if a held USD stablecoin has drifted beyond tolerance."""
    held = {
        currency
        for ledger in ledgers.values()
        for balance in ledger.snapshot().balances
        if (currency := balance.currency) in USD_STABLECOINS and balance.total > ZERO
    }
    for currency in sorted(held):
        deviation = peg_deviation_pct(stablecoin_prices, currency)
        if deviation > policy.stablecoin_peg_tolerance_pct:
            return (
                f"{currency} is {deviation}% off par, beyond the "
                f"{policy.stablecoin_peg_tolerance_pct}% tolerance; equity cannot be valued"
            )
    return ""
