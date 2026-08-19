"""Analytics: cycle history, decision drill-down, risk history, cost (DESIGN §6.10 job 2).

These are the pages an operator reads *about* the bot rather than the screen they run it from.
The running screen is the workspace at `/` (`workspace.py`); what stayed here is what the
workspace deliberately does not carry — history, provenance and totals, reached from a log row or
from the Analytics menu.

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

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from tradebot.control.config_store import ConfigRecord, ConfigStore
from tradebot.core.config import ConfigRef
from tradebot.core.errors import ConfigError
from tradebot.dashboard.views import render, state_of

router = APIRouter(tags=["monitor"])


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
async def cycle_detail(
    request: Request, cycle_id: str, instrument: Annotated[list[str] | None, Query()] = None
) -> HTMLResponse:
    """The "why did it do that" view: snapshot, transcript, risk provenance, orders.

    `instrument` narrows the *deliberation* to the ones named — repeat the parameter to follow
    several at once. Absent means all, as it does for the basket filter on the cycle list, so
    unticking every box (which sends no parameter at all) restores the whole cycle rather than
    emptying the page.

    Two things the narrowing must not do, both handled here rather than in the template:

    * The checkbox list comes from the **un-narrowed** cycle. Built from what is on screen it
      would collapse to the ticked instrument on the first Apply, and the filter would be a
      one-way door with no control left to leave it by.
    * An instrument this cycle never deliberated on is **reported**, not dropped — the rule the
      pinned-configuration table already follows. A hand-edited URL that silently blanked the
      decisions and the transcript would read as a cycle whose panel never ran.
    """
    state = state_of(request)
    detail = state.queries.cycle(cycle_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no cycle {cycle_id}")
    selected = frozenset(instrument or ())
    return render(
        request,
        "monitor/cycle.html",
        detail=detail.narrowed_to(selected),
        instruments=detail.instruments,
        selected=selected,
        unmatched=tuple(sorted(selected.difference(detail.instruments))),
        pinned=[(ref, resolve(state.application.configs, ref)) for ref in detail.pins],
    )


@router.get("/portfolio")
async def portfolio_moved() -> Response:
    """Replaced by the workspace, so it redirects rather than being kept as a second copy.

    Two pages showing positions is two places for them to disagree (PHASE_10 decision 3). What the
    workspace does *not* carry — the realized curve and the closed round trips — is analysis
    rather than operation, and lives under Analytics.
    """
    return RedirectResponse("/analytics/portfolio", status_code=303)


@router.get("/analytics/portfolio", response_class=HTMLResponse)
async def portfolio(request: Request) -> HTMLResponse:
    """Holdings, the realized equity curve, and every closed round trip."""
    state = state_of(request)
    application, queries = state.application, state.queries
    return render(
        request,
        "monitor/portfolio.html",
        valuation=application.valuation(),
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
