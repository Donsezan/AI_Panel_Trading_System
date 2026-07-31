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
from tradebot.core.enums import Side
from tradebot.core.instrument import Instrument
from tradebot.core.money import ZERO
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

    #: True only when a human asked for this through the dashboard's Control page. The panel
    #: cannot set it: `BasketRunner` never passes it, and `test_risk.py` asserts that.
    operator_initiated: bool = False

    @property
    def is_operator_exit(self) -> bool:
        """A human reducing an existing long — the one case a *metering* rule stands aside for.

        The metering rules — cooldown, the daily cap, the loss streak, the hourly order rate —
        all exist to stop the **panel** over-trading. None was written with a human exit in mind,
        and a system that cannot be flattened by its operator during a loss streak has the
        control exactly backwards (DESIGN §6.6, §6.10).

        All three conditions carry weight. `operator_initiated` alone would let a human *open* a
        position past the daily cap; the SELL test alone would exempt the panel's own churn,
        which is precisely what the cooldown exists to meter. Together they describe a strictly
        risk-reducing act: v1 is long-only and `LongOnlyRule` still clamps the quantity to what
        is held, so nothing here can open, enlarge or invert a position.

        A rule that stands aside still *answers*, and its `RiskCheckResult` says so — the event
        log records which rules stood aside and why. That is what separates this from a bypass:
        the risk layer decides, in deterministic tested code, and the decision is auditable
        ([ADR 0015](../../docs/adr/0015-an-operator-exit-is-exempt-from-metering-rules.md)).
        """
        return (
            self.operator_initiated
            and self.decision.action.side is Side.SELL
            and self.position.qty > ZERO
        )


@runtime_checkable
class RiskRule(Protocol):
    """One Tier-1 limit."""

    rule_id: str

    def evaluate(self, proposal: RiskProposal, requested_qty: Money) -> RiskCheckResult:
        """Judge `requested_qty` against this rule.

        Returns `PASS`, `ADJUSTED` with a `max_qty` cap, or `VETO`.
        """
        ...
