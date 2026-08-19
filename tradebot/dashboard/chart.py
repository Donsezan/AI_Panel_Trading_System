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

A decision mark reads on **two axes**, because "what was decided" and "what came of it" are
different questions and one glyph used to answer only the first. *Shape* is the decision: an arrow
for BUY or SELL, a tick for a cycle that asked for nothing. *Colour* is the outcome: green or red
when an entry order for that cycle reached the venue, amber when one was decided on and none did,
grey when nothing was decided. Amber is the case this chart was blind to — a basket the risk
engine vetoes every hour drew a column of green buy arrows over a portfolio that bought once.

**A bar carries one decision mark, however many cycles fell inside it.** The cycle schedule is
finer than any timeframe on offer — a basket on a ten-minute grid puts six decisions in one hourly
bar — and six markers sharing one timestamp stack into a column that reads as six trades. The
bar's mark is its strongest outcome, labelled with the bar's own total and its cycle count, so
nothing is hidden, only summarised. Fills and cancellations are never folded: a repeated opinion
is one fact, but two fills are two things that happened, at two prices.

Failure semantics: a mark outside the rendered window is dropped rather than clamped to its edge,
because a marker at the first bar is read as having happened there. A series with no candles
yields an empty payload, which the pane renders as "no data for this window" — never as a chart
with an empty axis that reads like a flat market.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradebot.core.enums import Action, OrderRole, OrderState, Side
from tradebot.core.market import CandleSeries
from tradebot.core.money import ZERO

__all__ = ["Decision", "Mark", "chart_payload", "marks_of"]

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


#: A cycle that asked for nothing. `HOLD` and `WAIT` share a look because they look the same from
#: the outside — nothing happened — while remaining distinct in the label, which is where the
#: research difference lives (DESIGN §6.5). Neither can be "placed", so both tables carry them.
_IDLE_STYLES: dict[str, Style] = {
    Action.HOLD: Style("circle", GREY, "aboveBar"),
    Action.WAIT: Style("circle", GREY, "aboveBar"),
}
#: A decision an entry order came of.
_PLACED_STYLES: dict[str, Style] = {
    **_IDLE_STYLES,
    Action.BUY: Style("arrowUp", GREEN, "belowBar"),
    Action.SELL: Style("arrowDown", RED, "aboveBar"),
}
#: The same decision with no order behind it — a risk veto, or an execution that never got there.
#: Amber rather than a paler green: it is the same family as a cancellation, an intent that never
#: became a position, and it is the one an operator most needs to be able to count.
_REFUSED_STYLES: dict[str, Style] = {
    **_IDLE_STYLES,
    Action.BUY: Style("arrowUp", AMBER, "belowBar"),
    Action.SELL: Style("arrowDown", AMBER, "aboveBar"),
}
_UNDECIDED = Style("circle", GREY, "aboveBar")

#: The actions that ask for an order, read off the enum so a new one cannot be styled here and
#: forgotten there. Keyed by value, because a projection row carries the string.
_TRADABLE = frozenset(action.value for action in Action if action.is_tradable)

#: What a tradable decision with nothing filled behind it says about itself. A dict rather than a
#: branch, like every other two-way in this codebase.
_OUTCOME_WORD: dict[bool, str] = {True: "placed", False: "not placed"}

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
class Decision:
    """What one cycle decided for this instrument, and what came of it.

    `placed` is the presence of an *entry order* for that cycle, never the cycle's own outcome: a
    cycle is basket-wide, so one that placed BTC's order and had ETH's vetoed is `orders_placed`,
    and colouring ETH's mark from it would say the opposite of what happened to ETH.
    """

    action: str
    placed: bool
    #: What that entry actually filled. Zero when nothing was placed, and zero while an entry
    #: rests unfilled — which is why `placed` is a separate fact and not `quantity > 0`.
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class Mark:
    """One thing that happened, ready to be placed on a time axis."""

    at: datetime
    style: Style
    #: What the operator reads. Built from exact `Decimal` strings — never from a coordinate.
    label: str
    #: Where on the price axis, for the styles that are positioned by price. `None` otherwise.
    price: Decimal | None = None
    #: Decision marks only, and what `chart_payload` folds a bar's marks over. `None` on a fill
    #: or a cancellation, which are individual events and are never folded.
    decision: Decision | None = None


def marks_of(
    decisions: Sequence[Any], orders: Sequence[Any], fills: Sequence[Any]
) -> tuple[Mark, ...]:
    """Every mark the projections imply for one instrument's window, oldest first.

    One mark per decision here. Folding a bar's decisions into one is `chart_payload`'s job,
    because only it knows where the bars are.
    """
    entries = _entries_by_cycle(orders)
    return tuple(
        sorted(
            [
                *(_decision_mark(row.decided_at, _decision_of(row, entries)) for row in decisions),
                *(_fill_mark(row) for row in fills),
                *(_cancel_mark(row) for row in orders if row.state in _ABANDONED),
            ],
            key=lambda mark: mark.at,
        )
    )


def _entries_by_cycle(orders: Sequence[Any]) -> dict[str, Decimal]:
    """Quantity filled per cycle — and, by a key's *presence*, that the cycle placed an entry.

    Entries only, because a protective leg filling is the *exit* of the decision that placed it,
    not part of the size that decision took on. Summing both would label a BUY with twice the
    quantity the basket ever held.
    """
    totals: dict[str, Decimal] = {}
    for order in orders:
        if order.role == OrderRole.ENTRY.value:
            totals[order.cycle_id] = totals.get(order.cycle_id, ZERO) + order.filled_qty
    return totals


def _decision_of(row: Any, entries: Mapping[str, Decimal]) -> Decision:
    """A projection row as the two facts a mark is drawn from."""
    return Decision(row.action, row.cycle_id in entries, entries.get(row.cycle_id, ZERO))


def _decision_mark(at: datetime, decision: Decision, cycles: int = 1) -> Mark:
    """A decision as it is drawn: shape from what was decided, colour from what came of it."""
    return Mark(
        at=at,
        style=_decision_style(decision),
        label=_decision_label(decision, cycles),
        decision=decision,
    )


def _decision_style(decision: Decision) -> Style:
    table = _PLACED_STYLES if decision.placed else _REFUSED_STYLES
    return table.get(decision.action, _UNDECIDED)


def _decision_label(decision: Decision, cycles: int) -> str:
    """What the operator reads: the exact digits, and the fold stated rather than implied."""
    census = "" if cycles == 1 else f" · {cycles} cycles"
    if decision.quantity > ZERO:
        return f"{decision.action} {_digits(decision.quantity)}{census}"
    if decision.action in _TRADABLE:
        return f"{decision.action} {_OUTCOME_WORD[decision.placed]}{census}"
    return f"{decision.action}{census}"


def _rank(decision: Decision) -> int:
    """Which of a bar's decisions speaks for the bar. Higher wins; ties go to the later cycle."""
    if decision.placed:
        return 2
    return 1 if decision.action in _TRADABLE else 0


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
    between bars would invent a precision the rendering does not carry. A bar's *decisions* are
    then folded into one mark — see `_markers`.
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
        "markers": _markers(marks, opens),
        # Reported, never filled in: a hole is a bar the venue did not publish, and painting over
        # one on the operator's primary screen is exactly what `CandleSeries` refuses to do.
        "gaps": [{"from": start.isoformat(), "to": end.isoformat()} for start, end in series.gaps],
    }


def _markers(marks: Iterable[Mark], opens: Sequence[datetime]) -> list[dict[str, Any]]:
    """Every mark against the bars, in the order they arrived, each bar's decisions folded to one.

    Folded because the cycle schedule is finer than any timeframe the chart offers: a basket on a
    ten-minute grid puts six decisions inside one hourly bar, and six markers sharing a timestamp
    stack into a column that reads as six trades. A bar keeps the place of its *first* decision,
    so the folded payload is the unfolded one with duplicates removed rather than reordered.
    """
    drawn: list[tuple[int, Mark | None]] = []
    bars: dict[int, list[tuple[datetime, Decision]]] = {}
    for mark in marks:
        index = bisect_right(opens, mark.at) - 1
        if index < 0:
            # Outside the window: dropped, never clamped. At the first bar it would read as
            # having happened there.
            continue
        decision = mark.decision
        if decision is None:
            drawn.append((index, mark))
            continue
        bar = bars.setdefault(index, [])
        if not bar:
            drawn.append((index, None))  # the bar's place; its fold is built once the bar is whole
        bar.append((mark.at, decision))
    folded = {index: _fold(bar) for index, bar in bars.items()}
    return [_marker(folded[index] if mark is None else mark, opens[index]) for index, mark in drawn]


def _fold(bar: Sequence[tuple[datetime, Decision]]) -> Mark:
    """One bar's decisions as a single mark: its strongest outcome, over its own total.

    Strongest rather than latest, because a bar in which one cycle bought and four waited *bought*
    — a chart showing the last word would hide the only cycle that moved money. Ties go to the
    later cycle, which is why the search runs backwards over an already-chronological bar.
    """
    at, winner = max(reversed(bar), key=lambda pair: _rank(pair[1]))
    total = sum((decision.quantity for _, decision in bar), ZERO)
    return _decision_mark(at, replace(winner, quantity=total), len(bar))


def _marker(mark: Mark, open_time: datetime) -> dict[str, Any]:
    """One mark against the bar it happened during."""
    marker: dict[str, Any] = {
        "time": _epoch(open_time),
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
