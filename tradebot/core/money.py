"""All money arithmetic. Every price, quantity, notional, fee and balance is a `Decimal`.

**No `float` ever touches a money path** (PLAN §2.1). Floats are permitted only inside
indicator math; indicator outputs must pass through `to_decimal` before they can size an order.

Rounding is deliberately asymmetric, because symmetric rounding loses money:

* **Quantity** always rounds *down* to `lot_size` — rounding up can exceed a risk limit or the
  available balance.
* **Prices** always round to the *more passive* side (buy down, sell up) — a marketable limit
  crosses the spread deliberately, and quantization must never deepen that crossing.
* Below a venue minimum is a **veto**, never a bump up to the minimum: bumping silently
  oversizes past the risk limit that produced the quantity.

`ROUND_HALF_EVEN` and every other half-rounding mode are banned here and test-enforced; they
would round *up* half the time, which is the one thing sizing must never do.

Failure semantics: this module has no dependencies and cannot fail from the outside. Invalid
input (negative size, non-positive step, a `float`) raises `MoneyError` — a `FailClosed`
condition for callers, never something to paper over with a best-effort value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Context, Decimal, DivisionByZero, InvalidOperation
from decimal import Overflow as DecimalOverflow
from enum import StrEnum
from typing import Final

from tradebot.core.enums import Side
from tradebot.core.errors import MoneyError

#: Precision wide enough for any venue's price × quantity without silent truncation.
#: Traps are the point: an invalid operation must raise, never return NaN into a money path.
MONEY_CONTEXT: Final = Context(
    prec=34,
    traps=[InvalidOperation, DivisionByZero, DecimalOverflow],
)

ZERO: Final = Decimal(0)
ONE: Final = Decimal(1)

#: Rounding per side — always the more passive price (PLAN §2.1).
_PRICE_ROUNDING: Final[dict[Side, str]] = {Side.BUY: ROUND_DOWN, Side.SELL: ROUND_UP}


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert to `Decimal`, refusing `float`.

    Venue payloads and config carry numbers as strings for exactly this reason; accepting a
    float here would reintroduce binary rounding error into the one place it must not exist.
    """
    if isinstance(value, float):
        raise MoneyError(
            f"float is not accepted in money paths: {value!r}. "
            "Pass the original string or Decimal instead."
        )
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MoneyError(f"not a valid decimal amount: {value!r}") from exc


def from_measurement(value: float) -> Decimal:
    """The single sanctioned `float` → `Decimal` crossing in the system.

    Indicator math (RSI, ATR, MACD) runs in float, and ATR feeds position sizing. Rather than
    pretend that boundary doesn't exist, it is named, centralised and greppable: everything
    else in a money path is banned from touching `float` at all.

    `repr` is used deliberately — it is the shortest string that round-trips the float exactly,
    so no precision is invented and none is lost.
    """
    try:
        finite = math.isfinite(value)
    except TypeError as exc:
        raise MoneyError(f"measurement is not a number: {value!r}") from exc
    if not finite:
        raise MoneyError(f"measurement is not finite: {value!r}")
    return Decimal(repr(value))


def multiply(a: Decimal, b: Decimal) -> Decimal:
    """Multiply in the money context (explicit precision, trapping)."""
    return MONEY_CONTEXT.multiply(a, b)


def divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Divide in the money context. A zero denominator raises rather than yielding infinity."""
    if denominator == ZERO:
        raise MoneyError("division by zero in a money path")
    return MONEY_CONTEXT.divide(numerator, denominator)


def notional(qty: Decimal, price: Decimal) -> Decimal:
    """Order value in quote currency."""
    return multiply(qty, price)


def percent_of(value: Decimal, percent: Decimal) -> Decimal:
    """`percent` expressed 0–100, as risk limits are written and displayed."""
    return divide(multiply(value, percent), Decimal(100))


def round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    """Round `value` to a multiple of `step`.

    Venue steps are not always powers of ten (lot size 5, tick 0.05), so this is floor/ceil
    division by the step rather than `Decimal.quantize`, which only handles decimal exponents.
    """
    if step <= ZERO:
        raise MoneyError(f"step must be positive, got {step}")
    steps = MONEY_CONTEXT.divide(value, step).to_integral_value(rounding=rounding)
    return MONEY_CONTEXT.multiply(steps, step)


def quantize_quantity(qty: Decimal, lot_size: Decimal) -> Decimal:
    """Round a quantity **down** to the venue lot size. Never rounds up (PLAN §2.1)."""
    if qty < ZERO:
        raise MoneyError(f"quantity must not be negative, got {qty}")
    return round_to_step(qty, lot_size, ROUND_DOWN)


def quantize_price(price: Decimal, tick_size: Decimal, side: Side) -> Decimal:
    """Round a limit price to the venue tick, always to the side's *passive* direction.

    Buy prices round down, sell prices round up: quantization must never make a price more
    aggressive than the strategy intended.
    """
    if price <= ZERO:
        raise MoneyError(f"price must be positive, got {price}")
    return round_to_step(price, tick_size, _PRICE_ROUNDING[side])


@dataclass(frozen=True, slots=True)
class TradingRules:
    """Venue precision and minimums for one instrument."""

    lot_size: Decimal
    tick_size: Decimal
    min_qty: Decimal
    min_notional: Decimal

    def __post_init__(self) -> None:
        for name in ("lot_size", "tick_size"):
            if getattr(self, name) <= ZERO:
                raise MoneyError(f"{name} must be positive, got {getattr(self, name)}")
        for name in ("min_qty", "min_notional"):
            if getattr(self, name) < ZERO:
                raise MoneyError(f"{name} must not be negative, got {getattr(self, name)}")


class SizingVeto(StrEnum):
    """Why a quantized order cannot be submitted. Recorded as risk provenance."""

    NON_POSITIVE_QTY = "non_positive_qty"
    BELOW_MIN_QTY = "below_min_qty"
    BELOW_MIN_NOTIONAL = "below_min_notional"


@dataclass(frozen=True, slots=True)
class QuantizedOrder:
    """Venue-ready size and price, or the veto that stops the order.

    `price` is the quantized limit price; for market orders it is the reference price used to
    evaluate `min_notional` and should not be sent to the venue.
    """

    qty: Decimal
    price: Decimal
    notional: Decimal
    veto: SizingVeto | None = None

    @property
    def approved(self) -> bool:
        return self.veto is None


def check_minimums(qty: Decimal, order_notional: Decimal, rules: TradingRules) -> SizingVeto | None:
    """Return the veto that applies after quantization, or `None` if the order clears."""
    if qty <= ZERO:
        return SizingVeto.NON_POSITIVE_QTY
    if qty < rules.min_qty:
        return SizingVeto.BELOW_MIN_QTY
    if order_notional < rules.min_notional:
        return SizingVeto.BELOW_MIN_NOTIONAL
    return None


def quantize_order(qty: Decimal, price: Decimal, side: Side, rules: TradingRules) -> QuantizedOrder:
    """Quantize a sized order to venue precision and apply the minimums check.

    This is the last deterministic gate before an `OrderIntent` becomes submittable. It only
    ever shrinks the order; if shrinking drops it below a venue minimum the result is a veto.
    """
    final_qty = quantize_quantity(qty, rules.lot_size)
    final_price = quantize_price(price, rules.tick_size, side)
    final_notional = notional(final_qty, final_price)
    return QuantizedOrder(
        qty=final_qty,
        price=final_price,
        notional=final_notional,
        veto=check_minimums(final_qty, final_notional, rules),
    )
