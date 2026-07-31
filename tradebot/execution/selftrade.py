"""Refusing to trade with ourselves (PLAN §3.3).

Both venues treat self-matching as an abuse pattern, and both can act on it — so this is an
account-safety control, not tidiness. It is also cheap: an order that would cross our own resting
order is a mistake in every case, because the two sides came from the same equity.

Two rules keep the check from becoming the hazard it guards against:

* **Only entries are checked.** A protective leg is the exit of a position we already hold, and
  refusing to place it would leave that position unguarded between cycles — a far worse outcome
  than a self-match (DESIGN §6.7, R12).
* **A trigger is not a resting price.** An untriggered `STOP_LOSS_LIMIT` has a limit price below
  the market that becomes live only once the stop is hit. Comparing it as though it were resting
  would veto every entry made while a stop is in place, which is every entry after the first.

Failure semantics: fails *closed* on ambiguity. An opposite-side resting order whose price the
venue does not report counts as crossing, because "we cannot tell" and "it is fine" are not the
same answer.
"""

from __future__ import annotations

from collections.abc import Iterable

from tradebot.core.enums import OrderType, Side
from tradebot.core.orders import OrderIntent
from tradebot.interfaces.broker import OrderStatus

SELF_TRADE_RULE = "self_trade"

#: Whether our price crosses theirs, per side of *our* order. A buy crosses a sell at or below
#: our limit; a sell crosses a buy at or above it.
_CROSSES = {
    Side.BUY: lambda ours, theirs: theirs <= ours,
    Side.SELL: lambda ours, theirs: theirs >= ours,
}


def crossing_order(intent: OrderIntent, resting: Iterable[OrderStatus]) -> OrderStatus | None:
    """The first of our own resting orders `intent` would trade against, if any."""
    if intent.role.is_protective:
        return None
    return next(
        (
            other
            for other in resting
            if other.instrument_key == intent.instrument_key
            and other.state.is_open
            and other.client_order_id != intent.client_order_id
            and _opposes(intent.side, other.side)
            and _would_match(intent, other)
        ),
        None,
    )


def _opposes(ours: Side, theirs: Side | None) -> bool:
    """An unreported side counts as opposing: unknown is not the same as harmless."""
    return theirs is None or theirs is not ours


def _would_match(intent: OrderIntent, other: OrderStatus) -> bool:
    """Whether the two orders' prices actually meet."""
    if other.order_type is not None and other.order_type.needs_stop_price:
        return False
    # A market order crosses whatever is there, which is exactly why it is the dangerous case.
    if intent.order_type is OrderType.MARKET or intent.limit_price is None:
        return True
    if other.limit_price is None:
        return True
    return bool(_CROSSES[intent.side](intent.limit_price, other.limit_price))
