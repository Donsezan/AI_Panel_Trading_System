"""The chart's JSON payload — and the **only** place in the dashboard where a float exists.

The charting library consumes IEEE-754 doubles, so candles and one marker coordinate have to
cross. That crossing is sanctioned for *rendering* exactly the way `money.from_measurement` is
sanctioned for indicator input (PHASE_10 decision 6, ADR 0024), and it is confined to this module
so the boundary is one grep rather than a habit. Two rules keep it a boundary:

* **It is one-way.** Nothing reads a number back out of the chart. No form, no POST and no
  template consumes this payload; it is drawn and discarded.
* **Every value a human reads is the exact `Decimal`.** Marker labels carry the server's own
  digits as strings. The floats are coordinates — where to draw — never quantities.

Marks answer "what did the bot do, and when", and the three kinds are deliberately different
facts. A **decision** mark sits on the bar the panel decided against, including the decisions that
decided *not* to act: a run of grey ticks is the most common thing this chart has to show, and a
chart that only drew trades would make an idle bot look like a stopped one. A **fill** mark sits
at the price that actually happened, which is not the price that was decided. A **cancel** mark is
a TTL expiring — an intent the market never met.

Failure semantics: a mark outside the rendered window is dropped rather than clamped to its edge,
because a marker at the first bar is read as having happened there. A series with no candles
yields an empty payload, which the pane renders as "no data for this window" — never as a chart
with an empty axis that reads like a flat market.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradebot.core.enums import Action, OrderRole, OrderState, Side
from tradebot.core.market import CandleSeries

__all__ = ["Mark", "chart_payload", "marks_of"]

#: The stylesheet's own tones, restated here because the chart draws on a canvas and cannot read
#: CSS custom properties. Keep in step with `static/app.css`.
GREEN = "#3fb950"
RED = "#f85149"
GREY = "#8b959f"
AMBER = "#d29922"


@dataclass(frozen=True, slots=True)
class Style:
    """How one kind of mark is drawn. `lightweight-charts` marker vocabulary."""

    shape: str
    color: str
    position: str


#: A decision, by what it decided. `HOLD` and `WAIT` share a look because they look the same from
#: the outside — nothing happened — while remaining distinct in the label, which is where the
#: research difference lives (DESIGN §6.5).
_DECISION_STYLES: dict[str, Style] = {
    Action.BUY: Style("arrowUp", GREEN, "belowBar"),
    Action.SELL: Style("arrowDown", RED, "aboveBar"),
    Action.HOLD: Style("circle", GREY, "aboveBar"),
    Action.WAIT: Style("circle", GREY, "aboveBar"),
}
_UNDECIDED = Style("circle", GREY, "aboveBar")

#: A fill, by side. Positioned at its own price rather than against a bar: what a fill is *about*
#: is the price it happened at.
_FILL_STYLES: dict[str, Style] = {
    Side.BUY: Style("circle", GREEN, "atPriceMiddle"),
    Side.SELL: Style("circle", RED, "atPriceMiddle"),
}

_CANCELLED = Style("square", AMBER, "aboveBar")

#: Order states that mean the venue stopped working an order without filling it — a bot-enforced
#: TTL, in the ordinary case (DESIGN §6.7).
_ABANDONED = frozenset({OrderState.CANCELLED.value, OrderState.EXPIRED.value})


@dataclass(frozen=True, slots=True)
class Mark:
    """One thing that happened, ready to be placed on a time axis."""

    at: datetime
    style: Style
    #: What the operator reads. Built from exact `Decimal` strings — never from a coordinate.
    label: str
    #: Where on the price axis, for the styles that are positioned by price. `None` otherwise.
    price: Decimal | None = None


def marks_of(
    decisions: Sequence[Any], orders: Sequence[Any], fills: Sequence[Any]
) -> tuple[Mark, ...]:
    """Every mark the projections imply for one instrument's window, oldest first."""
    filled = _filled_by_cycle(orders)
    return tuple(
        sorted(
            [
                *(_decision_mark(row, filled) for row in decisions),
                *(_fill_mark(row) for row in fills),
                *(_cancel_mark(row) for row in orders if row.state in _ABANDONED),
            ],
            key=lambda mark: mark.at,
        )
    )


def _filled_by_cycle(orders: Sequence[Any]) -> dict[str, Decimal]:
    """Quantity actually filled per cycle, entries only.

    Entries only, because a protective leg filling is the *exit* of the decision that placed it,
    not part of the size that decision took on. Summing both would label a BUY with twice the
    quantity the basket ever held.
    """
    totals: dict[str, Decimal] = {}
    for order in orders:
        if order.role == OrderRole.ENTRY.value:
            totals[order.cycle_id] = totals.get(order.cycle_id, Decimal(0)) + order.filled_qty
    return totals


def _decision_mark(row: Any, filled: dict[str, Decimal]) -> Mark:
    quantity = filled.get(row.cycle_id, Decimal(0))
    suffix = f" {_digits(quantity)}" if quantity > 0 else ""
    return Mark(
        at=row.decided_at,
        style=_DECISION_STYLES.get(row.action, _UNDECIDED),
        label=f"{row.action}{suffix}",
    )


def _fill_mark(row: Any) -> Mark:
    return Mark(
        at=row.filled_at,
        style=_FILL_STYLES.get(row.side, _UNDECIDED),
        label=f"filled {_digits(row.qty)} @ {_digits(row.price)}",
        price=row.price,
    )


def _cancel_mark(row: Any) -> Mark:
    unfilled = row.qty - row.filled_qty
    return Mark(
        at=row.updated_at,
        style=_CANCELLED,
        label=f"{row.state} {_digits(unfilled)} unfilled",
    )


def _digits(value: Decimal) -> str:
    """A quantity or price as the exact digits the server holds. Never a coordinate."""
    return str(value.normalize())


def chart_payload(series: CandleSeries, marks: Iterable[Mark]) -> dict[str, Any]:
    """Candles, marks and gaps for one instrument and timeframe, as the client consumes it.

    Marks are snapped to the bar they fall inside, because that is the resolution the chart
    actually has: a fill at 14:37 on a 1h chart happened during the 14:00 bar, and drawing it
    between bars would invent a precision the rendering does not carry.
    """
    opens = [candle.open_time for candle in series.candles]
    return {
        "instrument_key": series.instrument_key,
        "timeframe": series.timeframe,
        "observed_at": series.observed_at.isoformat(),
        "candles": [
            {
                "time": _epoch(candle.open_time),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
            }
            for candle in series.candles
        ],
        "markers": [marker for mark in marks if (marker := _marker(mark, opens)) is not None],
        # Reported, never filled in: a hole is a bar the venue did not publish, and painting over
        # one on the operator's primary screen is exactly what `CandleSeries` refuses to do.
        "gaps": [{"from": start.isoformat(), "to": end.isoformat()} for start, end in series.gaps],
    }


def _marker(mark: Mark, opens: Sequence[datetime]) -> dict[str, Any] | None:
    """One mark against the bars, or `None` when it falls outside them."""
    index = bisect_right(opens, mark.at) - 1
    if index < 0:
        return None
    marker: dict[str, Any] = {
        "time": _epoch(opens[index]),
        "position": mark.style.position,
        "shape": mark.style.shape,
        "color": mark.style.color,
        "text": mark.label,
    }
    if mark.price is not None:
        marker["price"] = float(mark.price)
    return marker


def _epoch(moment: datetime) -> int:
    """UTC seconds. Whole seconds, so the crossing is exact and stays an `int`."""
    return int(moment.timestamp())
