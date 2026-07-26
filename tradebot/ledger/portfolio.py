"""The ledger: positions and balances, driven by fills and nothing else.

Two rules make this module trustworthy:

* **Fills only.** A position never moves because an order reached a terminal state. Partial
  fills are the normal case, and terminal states lie about them (PLAN §2.5).
* **Read-only outward.** Everything else — including the panel — receives a snapshot. The LLM
  never reads or modifies the ledger, because a hallucinated balance becomes a real order size
  (DESIGN [L4]).

The ledger is a *projection of venue truth*, not the truth itself. Phase 2c adds the reconciler
that diffs it against the venue, plus the high-water mark and flow-adjusted baselines Tier-2
needs.

Failure semantics: a sell larger than the holding raises rather than going negative — v1 is
long-only, so a negative position is a corrupted ledger, not a short.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from tradebot.core.clock import Clock
from tradebot.core.enums import Side
from tradebot.core.errors import ReconciliationMismatchError
from tradebot.core.money import ZERO, divide, multiply
from tradebot.core.orders import Fill
from tradebot.core.portfolio import AccountState, Balance, Position


class Ledger:
    """Reconciled holdings and balances for one venue portfolio."""

    def __init__(self, clock: Clock, *, venue: str, balances: Mapping[str, Decimal]) -> None:
        self._clock = clock
        self._venue = venue
        self._balances: dict[str, Decimal] = dict(balances)
        self._positions: dict[str, Position] = {}

    def position(self, instrument_key: str) -> Position:
        """The holding, or a flat position. Never `None` — absence is a position of zero."""
        return self._positions.get(instrument_key) or Position(instrument_key=instrument_key)

    def balance(self, currency: str) -> Decimal:
        return self._balances.get(currency, ZERO)

    def apply_fill(self, fill: Fill, *, base_currency: str, quote_currency: str) -> Position:
        """Book a fill. The only mutation this class exposes."""
        updated = _APPLIERS[fill.side](self, fill)
        self._positions[fill.instrument_key] = updated
        self._move_cash(fill, base_currency, quote_currency)
        return updated

    def _apply_buy(self, fill: Fill) -> Position:
        current = self.position(fill.instrument_key)
        new_qty = current.qty + fill.qty
        cost = multiply(current.qty, current.avg_entry) + fill.notional
        return current.model_copy(
            update={
                "qty": new_qty,
                "avg_entry": divide(cost, new_qty),
                "opened_at": current.opened_at or fill.filled_at,
            }
        )

    def _apply_sell(self, fill: Fill) -> Position:
        current = self.position(fill.instrument_key)
        if fill.qty > current.qty:
            raise ReconciliationMismatchError(
                f"sell of {fill.qty} exceeds holding {current.qty} on {fill.instrument_key}; "
                "v1 is long-only, so this is ledger corruption rather than a short"
            )
        realized = multiply(fill.qty, fill.price - current.avg_entry)
        remaining = current.qty - fill.qty
        return current.model_copy(
            update={
                "qty": remaining,
                "avg_entry": current.avg_entry if remaining > ZERO else ZERO,
                "realized_pnl": current.realized_pnl + realized,
                "opened_at": current.opened_at if remaining > ZERO else None,
            }
        )

    def _move_cash(self, fill: Fill, base_currency: str, quote_currency: str) -> None:
        direction = Decimal(1) if fill.side is Side.BUY else Decimal(-1)
        self._balances[base_currency] = self.balance(base_currency) + multiply(fill.qty, direction)
        self._balances[quote_currency] = (
            self.balance(quote_currency) - multiply(fill.notional, direction) - fill.fee
        )

    def mark_cycle_held(self, instrument_key: str) -> None:
        """Advance the holding period, which Tier-1 cooldown rules and the panel context use."""
        current = self.position(instrument_key)
        if not current.is_flat:
            self._positions[instrument_key] = current.model_copy(
                update={"held_cycles": current.held_cycles + 1}
            )

    def equity(self, prices: Mapping[str, Decimal], *, quote_currency: str) -> Decimal:
        """Mark-to-market equity in the quote currency.

        A position with no price is valued at cost rather than skipped: dropping it would
        understate exposure and quietly loosen every percentage-based risk limit.
        """
        holdings = sum(
            (
                position.market_value(prices.get(key, position.avg_entry))
                for key, position in self._positions.items()
            ),
            start=ZERO,
        )
        return self.balance(quote_currency) + holdings

    def exposure(self, instrument_keys: tuple[str, ...], prices: Mapping[str, Decimal]) -> Decimal:
        """Value currently deployed across a set of instruments — a basket's exposure."""
        return sum(
            (
                self.position(key).market_value(prices.get(key, self.position(key).avg_entry))
                for key in instrument_keys
            ),
            start=ZERO,
        )

    def snapshot(self) -> AccountState:
        """Read-only view for the reconciler, the dashboard and the context builder."""
        return AccountState(
            venue=self._venue,
            positions=tuple(self._positions.values()),
            balances=tuple(
                Balance(currency=currency, free=amount)
                for currency, amount in sorted(self._balances.items())
            ),
            observed_at=self._clock.now(),
        )


_APPLIERS = {Side.BUY: Ledger._apply_buy, Side.SELL: Ledger._apply_sell}
