"""Monitor: portfolio, cycle history, decision drill-down, cost (DESIGN §6.10 job 2).

Read-only, and reads **projections** — the log is the audit artifact, not the query surface
(DESIGN §6.9). The exception is the drill-down, which is *about* the log: seat responses, the
frozen snapshot and risk-check provenance have no projector because nothing but this view reads
them.

The drill-down is the core research artifact, so it resolves each cycle's pinned configuration
versions with `configs.at(ref)`: a six-week-old decision is displayed against the limits that
produced it, never against today's (DESIGN §6.1, ADR 0013).

Failure semantics: an unknown cycle is a 404, not an empty page that reads as "this cycle did
nothing". A pin that no longer resolves is shown as unresolved rather than omitted.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.core.config import ConfigRef
from tradebot.core.errors import ConfigError
from tradebot.dashboard.views import render, state_of

router = APIRouter(tags=["monitor"])


@router.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    """Portfolio, equity, and what has happened lately — the page an operator leaves open."""
    state = state_of(request)
    application, queries = state.application, state.queries
    return render(
        request,
        "monitor/overview.html",
        equity=application.equity(),
        quote_currency=application.quote_currency,
        positions=queries.positions(),
        open_orders=queries.open_orders(),
        recent_cycles=queries.cycles(limit=10),
        risk_events=queries.risk_events(limit=5),
        baskets=application.configs.baskets(),
    )


@router.get("/cycles", response_class=HTMLResponse)
async def cycle_history(request: Request, basket: str | None = None) -> HTMLResponse:
    state = state_of(request)
    return render(
        request,
        "monitor/cycles.html",
        cycles=state.queries.cycles(basket_id=basket),
        baskets=state.application.configs.baskets(),
        selected=basket,
    )


@router.get("/cycles/{cycle_id}", response_class=HTMLResponse)
async def cycle_detail(request: Request, cycle_id: str) -> HTMLResponse:
    """The "why did it do that" view: snapshot, transcript, risk provenance, orders."""
    state = state_of(request)
    detail = state.queries.cycle(cycle_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no cycle {cycle_id}")
    return render(
        request,
        "monitor/cycle.html",
        detail=detail,
        pinned=[(ref, resolve(state.application.configs, ref)) for ref in detail.pins],
    )


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio(request: Request) -> HTMLResponse:
    """Holdings, the realized equity curve, and every closed round trip."""
    state = state_of(request)
    application, queries = state.application, state.queries
    return render(
        request,
        "monitor/portfolio.html",
        equity=application.equity(),
        quote_currency=application.quote_currency,
        positions=queries.positions(),
        round_trips=queries.round_trips(),
        curve=queries.equity_curve(),
        orders=queries.orders(limit=25),
    )


@router.get("/risk", response_class=HTMLResponse)
async def risk(request: Request) -> HTMLResponse:
    """Every veto, halt and kill-switch trip, and every reconciliation sweep."""
    state = state_of(request)
    return render(
        request,
        "monitor/risk.html",
        risk_events=state.queries.risk_events(),
        reconciliations=state.queries.reconciliations(),
        policy=state.application.configs.global_risk(),
    )


@router.get("/costs", response_class=HTMLResponse)
async def costs(request: Request) -> HTMLResponse:
    """`$/decision` per basket — the number that makes panel configurations comparable."""
    state = state_of(request)
    return render(request, "monitor/costs.html", costs=state.queries.cost_by_basket())


def resolve(configs: ConfigStore, ref: ConfigRef) -> ConfigRecord[Any] | None:
    """A pinned version, or `None` when the log points at something unreadable.

    Unresolvable is shown as unresolvable. Substituting the current version would present a
    decision against limits it was never gated on, which is exactly what pinning exists to
    prevent (ADR 0013).
    """
    try:
        return configs.at(ref)
    except ConfigError:
        return None
