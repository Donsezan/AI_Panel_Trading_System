"""The chart payload, and the boundary that keeps floats out of everything else.

Two things are asserted harder than the rest, because they are the ones that would cost money if
they drifted: that no *quantity or price a human reads* is ever a float, and that `float` appears
in exactly one module of `dashboard/`. The chart is allowed its coordinates; nothing else is
allowed anything (PHASE_10 decision 6, ADR 0024).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tradebot.core.enums import Action, OrderRole, OrderState, Side
from tradebot.core.market import Candle, CandleSeries
from tradebot.dashboard import chart
from tradebot.dashboard.chart import AMBER, GREEN, GREY, RED, chart_payload, marks_of

START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def candles(count: int = 4, *, gap_after: int | None = None) -> CandleSeries:
    """A run of hourly bars, optionally with one hour missing — a hole the venue never published."""
    bars = []
    opens = START
    for index in range(count):
        bars.append(
            Candle(
                open_time=opens,
                close_time=opens + HOUR,
                open=Decimal(100 + index),
                high=Decimal(105 + index),
                low=Decimal(95 + index),
                close=Decimal(102 + index),
                volume=Decimal(7),
            )
        )
        opens += HOUR * 2 if index == gap_after else HOUR
    return CandleSeries(
        instrument_key="sim:BTC/USDT", timeframe="1h", candles=tuple(bars), observed_at=opens
    )


def decision(action: str, *, at: datetime, cycle_id: str = "c1") -> SimpleNamespace:
    return SimpleNamespace(cycle_id=cycle_id, action=action, decided_at=at)


def order(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "cycle_id": "c1",
        "role": OrderRole.ENTRY.value,
        "state": OrderState.FILLED.value,
        "qty": Decimal("0.5"),
        "filled_qty": Decimal("0.5"),
        "updated_at": START,
    }
    return SimpleNamespace(**{**defaults, **overrides})


def fill(**overrides: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
        "side": Side.BUY.value,
        "qty": Decimal("0.5"),
        "price": Decimal("101.25"),
        "filled_at": START,
    }
    return SimpleNamespace(**{**defaults, **overrides})


# ---------------------------------------------------------------- marks


def test_a_decision_carries_the_quantity_that_actually_filled() -> None:
    """The label is the fill, not the intent: what happened is what the chart is a record of."""
    marks = marks_of([decision(Action.BUY, at=START)], [order()], [])
    assert marks[0].label == "BUY 0.5"
    assert marks[0].style.color == GREEN


def test_a_decision_that_placed_nothing_says_only_what_it_decided() -> None:
    marks = marks_of([decision(Action.HOLD, at=START)], [], [])
    assert marks[0].label == "HOLD"
    assert marks[0].style.color == GREY


@pytest.mark.parametrize("action", [Action.HOLD, Action.WAIT])
def test_the_cycles_that_decided_nothing_are_still_marked(action: str) -> None:
    """A chart that only drew trades would make an idle bot look like a stopped one."""
    assert marks_of([decision(action, at=START)], [], [])


def test_a_sell_is_toned_apart_from_a_buy() -> None:
    marks = marks_of([decision(Action.SELL, at=START)], [order()], [])
    assert marks[0].style.color == RED
    assert marks[0].style.shape == "arrowDown"


def test_a_decision_no_order_came_of_is_toned_apart_from_one_that_traded() -> None:
    """Shape is what was decided; colour is what came of it.

    Sixteen of the twenty-three arrows on the reported screenshot were risk vetoes with no order
    behind them, drawn in the same green as the seven that traded (docs/img/DashBoards.png).
    """
    refused = marks_of([decision(Action.BUY, at=START)], [], [])[0]
    traded = marks_of([decision(Action.BUY, at=START)], [order()], [])[0]
    assert refused.style.shape == traded.style.shape == "arrowUp"
    assert refused.style.color == AMBER
    assert traded.style.color == GREEN
    assert refused.label == "BUY not placed"


def test_an_order_placed_but_not_yet_filled_is_a_decision_that_acted() -> None:
    """A resting entry is a commitment the venue holds — not a refusal, and not a quantity."""
    marks = marks_of(
        [decision(Action.BUY, at=START)],
        [order(state=OrderState.OPEN.value, filled_qty=Decimal(0))],
        [],
    )
    assert marks[0].style.color == GREEN
    assert marks[0].label == "BUY placed"


def test_an_idle_cycle_is_grey_whether_or_not_the_basket_traded_elsewhere() -> None:
    """A cycle is basket-wide: one that placed BTC's order and vetoed ETH's is `orders_placed`.

    The mark reads this instrument's own entry orders, so ETH cannot borrow BTC's colour.
    """
    marks = marks_of([decision(Action.WAIT, at=START)], [], [])
    assert marks[0].style.color == GREY
    assert marks[0].label == "WAIT"


def test_an_unknown_action_is_drawn_rather_than_dropped() -> None:
    """A decision this build cannot style is still a decision that was made."""
    assert marks_of([decision("SOMETHING_NEW", at=START)], [], [])[0].style.color == GREY


def test_protective_legs_do_not_inflate_a_decisions_quantity() -> None:
    """A leg filling is the exit of that decision, not more size taken on by it."""
    marks = marks_of(
        [decision(Action.BUY, at=START)],
        [order(), order(role=OrderRole.STOP_LOSS.value, filled_qty=Decimal("0.5"))],
        [],
    )
    assert marks[0].label == "BUY 0.5"


def test_a_fill_is_marked_at_the_price_it_happened_at() -> None:
    """Not at the decision price — that is the difference the chart exists to show."""
    mark = marks_of([], [], [fill()])[0]
    assert mark.price == Decimal("101.25")
    assert mark.label == "filled 0.5 @ 101.25"
    assert mark.style.position == "atPriceMiddle"


def test_an_abandoned_order_is_marked_with_what_never_filled() -> None:
    marks = marks_of([], [order(state=OrderState.CANCELLED.value, filled_qty=Decimal("0.2"))], [])
    assert marks[0].label == "cancelled 0.3 unfilled"


def test_a_working_order_is_not_a_mark() -> None:
    """Only an order the venue stopped working is an event; an open one has not happened yet."""
    assert marks_of([], [order(state=OrderState.OPEN.value)], []) == ()


def test_marks_are_ordered_oldest_first() -> None:
    marks = marks_of(
        [decision(Action.BUY, at=START + HOUR * 2)],
        [],
        [fill(filled_at=START), fill(filled_at=START + HOUR)],
    )
    assert [mark.at for mark in marks] == [START, START + HOUR, START + HOUR * 2]


# ---------------------------------------------------------------- payload


def test_candles_are_the_series_in_order() -> None:
    payload = chart_payload(candles(3), [])
    assert [bar["time"] for bar in payload["candles"]] == [
        int((START + HOUR * n).timestamp()) for n in range(3)
    ]
    assert payload["candles"][0] == {
        "time": int(START.timestamp()),
        "open": 100.0,
        "high": 105.0,
        "low": 95.0,
        "close": 102.0,
    }


def test_a_mark_snaps_to_the_bar_it_happened_during() -> None:
    """A fill at 13:37 on an hourly chart happened during the 13:00 bar, and nowhere finer."""
    marks = marks_of([], [], [fill(filled_at=START + HOUR + timedelta(minutes=37))])
    marker = chart_payload(candles(3), marks)["markers"][0]
    assert marker["time"] == int((START + HOUR).timestamp())


def test_a_bar_carries_one_decision_mark_however_many_cycles_fell_inside_it() -> None:
    """The reported defect. A basket on a ten-minute grid puts six decisions in one hourly bar,
    and six markers at one timestamp stack into a column that reads as six trades."""
    every_ten_minutes = [
        decision(Action.BUY, at=START + timedelta(minutes=10 * n), cycle_id=f"c{n}")
        for n in range(6)
    ]
    payload = chart_payload(candles(3), marks_of(every_ten_minutes, [], []))
    assert len(payload["markers"]) == 1
    assert payload["markers"][0]["time"] == int(START.timestamp())
    assert payload["markers"][0]["text"] == "BUY not placed · 6 cycles"


def test_a_folded_bar_totals_the_quantity_its_cycles_took_on() -> None:
    """The bar's own total, not the winning cycle's — two entries in an hour bought both."""
    marks = marks_of(
        [
            decision(Action.BUY, at=START, cycle_id="c1"),
            decision(Action.BUY, at=START + timedelta(minutes=10), cycle_id="c2"),
        ],
        [
            order(cycle_id="c1", qty=Decimal("0.2"), filled_qty=Decimal("0.2")),
            order(cycle_id="c2", qty=Decimal("0.3"), filled_qty=Decimal("0.3")),
        ],
        [],
    )
    assert chart_payload(candles(3), marks)["markers"][0]["text"] == "BUY 0.5 · 2 cycles"


def test_a_bar_that_traded_once_and_waited_four_times_shows_the_trade() -> None:
    """Strongest outcome, not the last word: a chart showing the final cycle would hide the only
    one that moved money."""
    rows = [
        decision(Action.BUY, at=START, cycle_id="c1"),
        *(
            decision(Action.WAIT, at=START + timedelta(minutes=10 * n), cycle_id=f"w{n}")
            for n in range(1, 5)
        ),
    ]
    marker = chart_payload(candles(3), marks_of(rows, [order(cycle_id="c1")], []))["markers"][0]
    assert marker["shape"] == "arrowUp"
    assert marker["color"] == GREEN
    assert marker["text"] == "BUY 0.5 · 5 cycles"


def test_a_bar_the_panel_only_waited_through_folds_to_one_idle_tick() -> None:
    rows = [
        decision(Action.WAIT, at=START + timedelta(minutes=10 * n), cycle_id=f"c{n}")
        for n in range(3)
    ]
    marker = chart_payload(candles(3), marks_of(rows, [], []))["markers"][0]
    assert marker["shape"] == "circle"
    assert marker["text"] == "WAIT · 3 cycles"


def test_decisions_in_different_bars_are_never_folded_together() -> None:
    """The fold is per bar. Folding across bars would move a decision to a price it never saw."""
    rows = [decision(Action.BUY, at=START + HOUR * n, cycle_id=f"c{n}") for n in range(3)]
    markers = chart_payload(candles(3), marks_of(rows, [], []))["markers"]
    assert [marker["time"] for marker in markers] == [
        int((START + HOUR * n).timestamp()) for n in range(3)
    ]


def test_fills_inside_one_bar_are_never_folded() -> None:
    """A repeated opinion is one fact; two fills are two things that happened at two prices."""
    marks = marks_of([], [], [fill(filled_at=START), fill(filled_at=START + timedelta(minutes=20))])
    assert len(chart_payload(candles(3), marks)["markers"]) == 2


def test_a_mark_before_the_window_is_dropped_not_clamped() -> None:
    """Drawn at the first bar it would read as having happened there, which it did not."""
    marks = marks_of([], [], [fill(filled_at=START - HOUR)])
    assert chart_payload(candles(3), marks)["markers"] == []


def test_a_gap_is_reported_rather_than_filled_in() -> None:
    """A hole is a bar the venue never published; painting over one is inventing data."""
    payload = chart_payload(candles(4, gap_after=1), [])
    assert len(payload["gaps"]) == 1
    assert len(payload["candles"]) == 4


def test_an_empty_series_is_an_empty_payload() -> None:
    """Not a chart with an empty axis, which reads like a flat market."""
    empty = CandleSeries(
        instrument_key="sim:BTC/USDT", timeframe="1h", candles=(), observed_at=START
    )
    payload = chart_payload(empty, marks_of([], [], [fill()]))
    assert payload["candles"] == []
    assert payload["markers"] == []


def test_only_a_fill_carries_a_price_coordinate() -> None:
    """Everything else is positioned against a bar, so there is no float to carry."""
    marks = marks_of([decision(Action.BUY, at=START)], [order()], [fill()])
    markers = chart_payload(candles(3), marks)["markers"]
    assert [("price" in marker) for marker in markers] == [False, True]


def test_every_label_is_the_exact_decimal_the_server_holds() -> None:
    """The one rule the float boundary exists for: coordinates may cross, quantities may not."""
    marks = marks_of(
        [decision(Action.BUY, at=START)],
        [order(qty=Decimal("0.10000001"), filled_qty=Decimal("0.10000001"))],
        [fill(qty=Decimal("0.10000001"), price=Decimal("30000.10000001"))],
    )
    labels = [mark.label for mark in marks]
    assert "0.10000001" in labels[0]
    assert labels[1] == "filled 0.10000001 @ 30000.10000001"


# ---------------------------------------------------------------- the boundary itself


def test_float_appears_in_exactly_one_dashboard_module() -> None:
    """The grep-able boundary, asserted rather than remembered (PHASE_10 §Risks).

    `dashboard/` is outside the money packages `test_money_discipline` walks, so without this the
    chart's sanctioned crossing would quietly license a second one on a page that renders limits.
    """
    package = Path(chart.__file__).parent
    offenders = {
        path.name
        for path in package.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "float"
    }
    assert offenders == {"chart.py"}
