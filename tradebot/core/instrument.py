"""The tradable thing. Asset-class differences live in adapters, never in core logic."""

from __future__ import annotations

from decimal import Decimal

from tradebot.core.enums import AssetClass
from tradebot.core.money import TradingRules
from tradebot.core.schema import DomainModel, Money


class Instrument(DomainModel):
    """One tradable instrument and the venue precision that constrains every order on it.

    `symbol` is the venue's own symbol; `key` is the system-wide identifier. They differ because
    the same ticker can exist on two venues, and a position belongs to a venue portfolio.
    """

    symbol: str
    venue: str
    asset_class: AssetClass
    base_currency: str
    quote_currency: str
    lot_size: Money
    tick_size: Money
    min_qty: Money = Decimal(0)
    min_notional: Money = Decimal(0)

    @property
    def key(self) -> str:
        """Stable system-wide identifier, e.g. `binance:BTC/USDT`."""
        return f"{self.venue}:{self.symbol}"

    @property
    def trading_rules(self) -> TradingRules:
        """Precision and minimums, in the shape the money layer consumes."""
        return TradingRules(
            lot_size=self.lot_size,
            tick_size=self.tick_size,
            min_qty=self.min_qty,
            min_notional=self.min_notional,
        )
