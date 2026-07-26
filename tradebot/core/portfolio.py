"""Positions and balances — the reconciled projection of venue truth.

The ledger owns these; everything else, including the panel, receives them read-only. The LLM
must never be able to read-modify the ledger, because a hallucinated balance becomes a real
order size (DESIGN [L4]).
"""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.schema import DomainModel, Money, UtcDatetime


class Position(DomainModel):
    """Holding in one instrument. v1 is long-only, so `qty` is never negative."""

    instrument_key: str
    qty: Money = Decimal(0)
    avg_entry: Money = Decimal(0)
    realized_pnl: Money = Decimal(0)
    opened_at: UtcDatetime | None = None
    held_cycles: int = 0

    @property
    def is_flat(self) -> bool:
        return self.qty <= ZERO

    def market_value(self, price: Money) -> Money:
        return multiply(self.qty, price)

    def unrealized_pnl(self, price: Money) -> Money:
        return multiply(self.qty, price - self.avg_entry)

    def unrealized_pnl_pct(self, price: Money) -> Money:
        """Percent on cost basis, 0–100 scale. Flat or zero-cost positions return 0."""
        cost = multiply(self.qty, self.avg_entry)
        if cost <= ZERO:
            return ZERO
        return multiply(divide(self.unrealized_pnl(price), cost), Decimal(100))


class Balance(DomainModel):
    """Free and locked funds in one currency. `locked` is collateral behind resting orders."""

    currency: str
    free: Money = Decimal(0)
    locked: Money = Decimal(0)

    @property
    def total(self) -> Money:
        return self.free + self.locked


class AccountState(DomainModel):
    """A venue's own view of the account — what the reconciler diffs the ledger against."""

    venue: str
    positions: tuple[Position, ...] = ()
    balances: tuple[Balance, ...] = ()
    observed_at: UtcDatetime

    def position(self, instrument_key: str) -> Position | None:
        return next((p for p in self.positions if p.instrument_key == instrument_key), None)

    def balance(self, currency: str) -> Balance | None:
        return next((b for b in self.balances if b.currency == currency), None)
