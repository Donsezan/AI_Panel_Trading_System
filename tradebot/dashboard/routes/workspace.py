"""The blotter workspace: one screen, and the panes it refreshes independently.

Phase 10 (PHASE_10 §Passes). The page at `/` is a CSS grid of six panes, each with its own GET
partial route, so a live-update notice refreshes one region rather than reloading the screen an
operator is reading. Pass 3 added the control dock and the risk-control pane, and Control stopped
being a page of its own.

**Selection is a navigation, not client state.** A blotter row is an ordinary link to
`/?scope=…`. htmx is used for *refreshing* a pane, never for selecting, which means a reload, a
bookmark and a socket-triggered refresh land on the same view by construction rather than by
keeping two copies of the selection in step (`scope.py`, ADR 0024).

Reliability rules this module implements (PHASE_10 §Reliability rules):

* **Only the chart data route awaits the venue**, through the shared cache, under an explicit
  timeout. Everything else is SQLite reads and in-memory configuration.
* **A failed pane renders as a failed pane.** The chart answers a provider failure with the
  reason and the last bar it managed to read — a spinner that never resolves is not information.
* **Nothing here mutates.** Every route is a GET. `routes/control.py` owns every POST and renders
  its refusals through `page`, so a refused action comes back as this screen with the reason on
  it and the selection intact — never as a page the operator has to navigate out of mid-incident.

Failure semantics: an unparseable `scope` is no selection rather than an error, so a hand-edited
URL degrades to the unfiltered view. A timeframe the provider does not offer falls back to the
default rather than refusing — it is a display preference, not a limit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.responses import Response

from tradebot.control.manual_close import CloseOutcome
from tradebot.core.errors import TradebotError
from tradebot.core.instrument import Instrument
from tradebot.core.market import timeframe_interval
from tradebot.dashboard import blotter, dock
from tradebot.dashboard.chart import chart_payload, marks_of
from tradebot.dashboard.scope import Scope
from tradebot.dashboard.scope import parse as parse_scope
from tradebot.dashboard.views import DashboardState, render, state_of
from tradebot.interfaces.market_data import MarketDataProvider
from tradebot.risk.state import RiskState

router = APIRouter(tags=["workspace"])

#: How long a chart request may wait on the venue before it becomes an error card. The read is
#: single-flight through the shared cache, so this is a cap on one venue call and not on a queue.
CHART_TIMEOUT_SECONDS = 5.0

#: Bars per chart. Enough for a day of 10-minute cycles to sit inside one screen of 1h candles.
CHART_BARS = 200

DEFAULT_TIMEFRAME = "1h"

#: Offered in the selector, in the order they appear. Narrowed to what the provider declares, so
#: a timeframe is never offered that the venue would refuse (DESIGN §6.2).
CHART_TIMEFRAMES = ("1h", "4h", "1d")

#: Rows in the operation log before an operator has to narrow the selection.
LOG_LIMIT = 60

#: Risk events on the RC pane. Short on purpose — it answers "what has risk just done", and the
#: whole history is one click away under Analytics.
RC_EVENTS = 8


@router.get("/", response_class=HTMLResponse)
async def workspace(request: Request, scope: str | None = None, tf: str | None = None) -> Response:
    return page(request, scope=scope, tf=tf)


def page(
    request: Request,
    *,
    scope: str | None = None,
    tf: str | None = None,
    error: str = "",
    outcome: CloseOutcome | None = None,
    pending: dock.PendingQuarantine | None = None,
) -> HTMLResponse:
    """The whole screen. Every pane's first paint is server-rendered, like every later one.

    Also what a refused action renders (`routes/control.py`): the same screen, the same selection,
    with the reason on it. An operator mid-incident acts on what they can see, so a refusal must
    not cost them the blotter, the chart and the log they were reading (PHASE_10 §Reliability 4).
    """
    state = state_of(request)
    selection = parse_scope(scope)
    # Merged rather than splatted side by side: two panes may legitimately want the same figure —
    # equity is on both ① and ⑥ — and each partial route must still be able to render alone.
    panes: dict[str, Any] = {
        **_portfolio(state),
        **_blotter(state, selection),
        **_log(state, selection),
        **_controls(state, selection),
        **_rc(state),
    }
    return render(
        request,
        "workspace/index.html",
        scope=selection,
        timeframe=_timeframe(state, tf),
        timeframes=_offered(state),
        charts=_charts(state, selection),
        error=error,
        outcome=outcome,
        pending=pending,
        **panes,
    )


@router.get("/workspace/portfolio", response_class=HTMLResponse)
async def portfolio_pane(request: Request) -> Response:
    state = state_of(request)
    return render(request, "workspace/_portfolio.html", **_portfolio(state))


@router.get("/workspace/blotter", response_class=HTMLResponse)
async def blotter_pane(
    request: Request, scope: str | None = None, tf: str | None = None
) -> Response:
    """The blotter alone. Carries the timeframe so a refreshed row still links to the chart the
    operator is looking at."""
    state = state_of(request)
    selection = parse_scope(scope)
    return render(
        request,
        "workspace/_blotter.html",
        scope=selection,
        timeframe=_timeframe(state, tf),
        **_blotter(state, selection),
    )


@router.get("/workspace/log", response_class=HTMLResponse)
async def log_pane(request: Request, scope: str | None = None) -> Response:
    state = state_of(request)
    selection = parse_scope(scope)
    return render(request, "workspace/_log.html", scope=selection, **_log(state, selection))


@router.get("/workspace/controls", response_class=HTMLResponse)
async def controls_pane(
    request: Request, scope: str | None = None, tf: str | None = None
) -> Response:
    """The control dock alone. Carries the selection, because every form posts it back."""
    state = state_of(request)
    selection = parse_scope(scope)
    return render(
        request,
        "workspace/_controls.html",
        scope=selection,
        timeframe=_timeframe(state, tf),
        **_controls(state, selection),
    )


@router.get("/workspace/rc", response_class=HTMLResponse)
async def rc_pane(request: Request, scope: str | None = None, tf: str | None = None) -> Response:
    """The safety states, and the typed acts that clear them."""
    state = state_of(request)
    selection = parse_scope(scope)
    return render(
        request,
        "workspace/_rc.html",
        scope=selection,
        timeframe=_timeframe(state, tf),
        **_rc(state),
    )


@router.get("/workspace/chart/data")
async def chart_data(request: Request, scope: str | None = None, tf: str | None = None) -> Response:
    """Candles and marks for one instrument, as JSON. The only route that awaits the venue.

    The only place a float exists, too: `chart.py` builds the payload and documents the crossing.
    """
    state = state_of(request)
    selection = parse_scope(scope)
    instrument = _instrument(state, selection)
    if selection is None or instrument is None:
        return _chart_error("that scope names no instrument this process trades")
    provider = state.application.market_data
    if provider is None:
        return _chart_error("no market-data provider is wired into this process")

    timeframe = _timeframe(state, tf)
    since = state.application.clock.now() - timeframe_interval(timeframe) * CHART_BARS
    try:
        series = await _candles(provider, instrument, timeframe)
    except (TradebotError, TimeoutError) as exc:
        return _chart_error(f"{type(exc).__name__}: {exc}")
    return JSONResponse(
        chart_payload(
            series,
            marks_of(
                state.queries.decisions_in(selection, since=since),
                state.queries.orders_in(instrument.key, since=since),
                state.queries.fills_in(instrument.key, since=since),
            ),
        )
    )


async def _candles(provider: MarketDataProvider, instrument: Instrument, timeframe: str) -> Any:
    """One cached read, capped. A venue that has stopped answering must not hold a pane open."""
    async with asyncio.timeout(CHART_TIMEOUT_SECONDS):
        return await provider.get_candles(instrument, timeframe, CHART_BARS)


def _chart_error(reason: str) -> JSONResponse:
    """A stated failure, with the status to match. The pane renders it as a card and a retry."""
    return JSONResponse({"error": reason}, status_code=503)


# ------------------------------------------------------------------ pane contexts


def _portfolio(state: DashboardState) -> dict[str, Any]:
    """Pane ① — equity, today, and the venues it is spread across.

    Today's move is measured from `day_start_equity`, the same flow-adjusted baseline the
    daily-loss rule uses (DESIGN §6.6), so the number an operator watches is the number the limit
    is measured against. A deposit moves both and is therefore never mistaken for profit.
    """
    application = state.application
    risk_state = application.states.load()
    valuation = application.valuation()
    equity = valuation.equity
    boundary = day_boundary(risk_state)
    return {
        "equity": equity,
        "valuation": valuation,
        "quote_currency": application.quote_currency,
        "day_start_equity": risk_state.day_start_equity,
        "day_move": equity - risk_state.day_start_equity if risk_state.day_start_equity else None,
        "day_boundary": boundary,
        "day_realized": state.queries.day_realized(boundary) if boundary else None,
        # One venue per process in v1 (Phase 6). Shaped as a list so the PortfolioAggregate of
        # DESIGN §4 drops in without a layout change.
        "venues": ((application.broker.value, equity),),
    }


def _blotter(state: DashboardState, scope: Scope | None) -> dict[str, Any]:
    """Pane ② — the master list. Selection here drives ③ and ④."""
    application, queries = state.application, state.queries
    boundary = day_boundary(application.states.load()) or _utc_midnight(application.clock.now())
    return {
        "baskets": blotter.build(
            application.configs.baskets(),
            positions=queries.positions(),
            decisions=queries.latest_decisions(),
            halted=application.states.halted_baskets(),
            cycles_today=queries.cycles_since(boundary),
            trades_today=queries.entry_orders_since(boundary),
            next_due=application.supervisor.next_due,
            scope=scope,
        )
    }


def _log(state: DashboardState, scope: Scope | None) -> dict[str, Any]:
    """Pane ④ — one row per cycle in the selection, newest first, each a link to its drill-down."""
    return {"activity": state.queries.activity(scope, limit=LOG_LIMIT)}


def _controls(state: DashboardState, scope: Scope | None) -> dict[str, Any]:
    """Pane ⑤ — what an operator may do right now, narrowed to what they have selected.

    `blockers` is rendered whether or not it is empty, so the whole list of what stands between
    this process and cycling is read once rather than discovered one refused Start at a time. The
    phrase is always among them: it is typed into the form, never held anywhere (ADR 0021).
    """
    application = state.application
    return {
        "controls": dock.build(
            application.configs.baskets(),
            positions=state.queries.positions(),
            halted=application.states.halted_baskets(),
            closable=application.manual_close.closable(),
            scope=scope,
        ),
        "blockers": state.controller.blockers(),
        "arming": application.arming.load(),
        "quote_currency": application.quote_currency,
        # Left resting by a Stop, and polled by nothing until supervision starts again.
        "working": state.queries.open_orders() if not state.trading else (),
    }


def _rc(state: DashboardState) -> dict[str, Any]:
    """Pane ⑥ — the safety states: what is stopped, what is excluded, what risk last did.

    `application.policy` rather than the published document: the clamps are what live actually
    enforces, and reporting the published number beside a tighter enforced one is the disagreement
    `enforced_policy` exists to make impossible (ADR 0021).
    """
    application = state.application
    return {
        "quarantines": dock.quarantines(
            application.configs.baskets(), positions=state.queries.positions()
        ),
        "clamps": application.policy.clamps,
        "risk_events": state.queries.risk_events(limit=RC_EVENTS),
        "valuation": application.valuation(),
        "quote_currency": application.quote_currency,
    }


def _charts(state: DashboardState, scope: Scope | None) -> tuple[Instrument, ...]:
    """Pane ③ — what to draw: one instrument, or a basket's as a small-multiple stack.

    A stack rather than an overlay: mixed quote currencies make a shared price axis a lie, so the
    instruments share a time axis and nothing else (PHASE_10 §The chart).
    """
    if scope is None:
        return ()
    instruments = _instruments_of(state, scope.basket_id)
    if scope.instrument_key is None:
        return instruments
    return tuple(i for i in instruments if i.key == scope.instrument_key)


# ------------------------------------------------------------------ helpers


def _instruments_of(state: DashboardState, basket_id: str) -> tuple[Instrument, ...]:
    """The instruments a basket in service holds, or none when it is not in service."""
    for record in state.application.configs.baskets():
        if record.ref.config_id == basket_id:
            return record.document.instruments
    return ()


def _instrument(state: DashboardState, scope: Scope | None) -> Instrument | None:
    """The one instrument a scope names, if a basket in service actually holds it.

    Checked against configuration rather than trusted from the URL: a chart is a venue read, and
    a hand-edited scope must not become a request for a symbol nothing here trades.
    """
    if scope is None or scope.instrument_key is None:
        return None
    held = _instruments_of(state, scope.basket_id)
    return next((i for i in held if i.key == scope.instrument_key), None)


def _offered(state: DashboardState) -> tuple[str, ...]:
    """The timeframes the wired provider can actually serve, in display order."""
    provider = state.application.market_data
    if provider is None:
        return ()
    available = set(provider.capabilities().timeframes)
    return tuple(timeframe for timeframe in CHART_TIMEFRAMES if timeframe in available)


def _timeframe(state: DashboardState, requested: str | None) -> str:
    """The requested timeframe when it is on offer, else the first one that is.

    A display preference, so it degrades rather than refuses. Falling back to `DEFAULT_TIMEFRAME`
    unconditionally would ask a provider for a bar it never declared.
    """
    offered = _offered(state)
    if requested in offered:
        return str(requested)
    return offered[0] if offered else DEFAULT_TIMEFRAME


def day_boundary(risk_state: RiskState) -> datetime | None:
    """The instant the current risk day began, or `None` before one has been established.

    Derived from the persisted `day_started_on` rather than from today's date: the two differ
    exactly when the rollover has not run yet, and then the daily-loss rule is still measuring
    against yesterday. The figure beside it must measure against the same instant.

    UTC-aware, because it is compared against stored instants and a naive datetime would compare
    against them wrongly rather than loudly.
    """
    try:
        day = date.fromisoformat(risk_state.day_started_on)
    except ValueError:
        return None
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def _utc_midnight(now: datetime) -> datetime:
    """The counters' fallback boundary — the same UTC day `HistoryReader` meters against."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
