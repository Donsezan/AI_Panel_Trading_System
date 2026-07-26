"""Risk rules. Deterministic, unit-tested code evaluated against the *reconciled* ledger.

Rules express **caps**, never mutations. A rule returns the largest quantity it permits and the
engine composes rules with `min()`, so no ordering of rules can widen a limit an earlier rule
imposed — the failure mode that turns a risk engine into decoration.

Failure semantics: a rule that cannot evaluate (missing ATR, unknown position) must return a
`VETO`, never a pass. Absence of evidence is not evidence of safety (PLAN §1.1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradebot.core.config import RiskPolicy
from tradebot.core.decision import Decision
from tradebot.core.instrument import Instrument
from tradebot.core.orders import RiskCheckResult
from tradebot.core.portfolio import Position
from tradebot.core.schema import DomainModel, Money


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
    #: Absolute ATR in quote currency per unit, the stop-distance basis for sizing.
    atr: Money
    #: Portfolio equity in quote currency, and the slice of it this basket may deploy.
    equity: Money
    basket_budget: Money
    #: Value the basket already holds across all its instruments.
    basket_exposure: Money
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
