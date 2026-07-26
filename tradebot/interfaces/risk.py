"""Risk rules. Deterministic, unit-tested code evaluated against the *reconciled* ledger.

Rules express **caps**, never mutations. A rule returns the largest quantity it permits and the
engine composes rules with `min()`, so no ordering of rules can widen a limit an earlier rule
imposed — the failure mode that turns a risk engine into decoration.

Failure semantics: a rule that cannot evaluate (missing ATR, unknown position) must return a
`VETO`, never a pass. Absence of evidence is not evidence of safety (PLAN §1.1).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from tradebot.core.config import RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.instrument import Instrument
from tradebot.core.orders import RiskCheckResult
from tradebot.core.portfolio import Position
from tradebot.core.schema import DomainModel, Money


class TradingHistory(DomainModel):
    """What this basket has already done, as the rules that meter activity need to see it.

    Derived from the event log rather than kept in memory, so a restart cannot reset a cooldown
    or a daily trade count — which would turn a crash loop into an unmetered trading loop.
    """

    #: Cycles completed since this instrument last traded. `None` means it has never traded,
    #: which no cooldown can block.
    cycles_since_trade: int | None = None
    #: Orders this basket has placed since the current day boundary.
    trades_today: int = 0
    #: Losing round trips in a row, most recent first. Reset by any winning trip.
    consecutive_losses: int = 0
    #: Orders placed across all baskets in the trailing hour — the Tier-2 rate budget.
    orders_last_hour: int = 0


class RiskProposal(DomainModel):
    """Everything a Tier-1 rule may consider about one proposed trade.

    Rules see only this: no venue access, no ledger writes, no clock. That is what makes them
    pure functions and therefore exhaustively testable.
    """

    decision: Decision
    instrument: Instrument
    policy: RiskPolicy
    position: Position
    #: Reference price for notional and value checks — the marketable limit price.
    price: Money
    #: Last traded price, against which Tier-2's collar judges the order price.
    last_price: Money = Decimal(0)
    #: Absolute ATR in quote currency per unit, the stop-distance basis for sizing.
    atr: Money
    #: Portfolio equity in quote currency, and the slice of it this basket may deploy.
    equity: Money
    basket_budget: Money
    #: Value the basket already holds across all its instruments.
    basket_exposure: Money
    #: Value deployed across the whole portfolio, this instrument, and its correlation bucket.
    gross_exposure: Money = Decimal(0)
    instrument_exposure: Money = Decimal(0)
    cluster_exposure: Money = Decimal(0)
    history: TradingHistory = TradingHistory()
    #: True when the venue cannot hold a protective stop, so sizing takes a haircut.
    unprotected: bool = False


@runtime_checkable
class RiskRule(Protocol):
    """One Tier-1 limit."""

    rule_id: str

    def evaluate(self, proposal: RiskProposal, requested_qty: Money) -> RiskCheckResult:
        """Judge `requested_qty` against this rule.

        Returns `PASS`, `ADJUSTED` with a `max_qty` cap, or `VETO`.
        """
        ...
