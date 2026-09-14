"""The render shell: what every page is given, and how a value reaches a template.

Separate from the factory so routers import it without importing the factory that imports them.
Everything a route needs arrives through `LabState`, hung on `app.state` — a route never reaches
for a global.

**Every number a human reads is the server's exact `Decimal` as a string.** There is no `float`
in this package at all (`test_discipline.py`), so a percentage is formatted from `Decimal` and a
bar width is an `int`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import HTTPConnection

from decision_lab.dashboard.cache import AnalysisCache
from tradebot.core.clock import Clock
from tradebot.core.money import to_decimal
from tradebot.dashboard.auth import Session

PACKAGE = Path(__file__).parent

#: Rendered where a value is genuinely absent, so an empty cell is never read as a zero.
ABSENT = "—"

#: The banner every page carries. The tool is a comparison instrument and not evidence of alpha
#: (§14), and a page that omitted it would be the one place that claim is not made.
DISCLAIMER = (
    "A comparison instrument, not evidence of alpha. Every number here is one panel measured "
    "against another over recorded history."
)


@dataclass(frozen=True, slots=True)
class LabState:
    """Everything a route may reach. Read through `state_of`."""

    workspace: Path | None
    templates: Jinja2Templates
    session: Session
    clock: Clock
    cache: AnalysisCache


def state_of(connection: HTTPConnection) -> LabState:
    return cast(LabState, connection.app.state.lab)


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.filters.update(money=money, percent=percent, moment=moment, count=count)
    templates.env.globals.update(absent=ABSENT, disclaimer=DISCLAIMER)
    return templates


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    """Render a page with the context every page needs. The only place templates are called."""
    from decision_lab import jobs

    state = state_of(request)
    running = jobs.holder(workspace=state.workspace)
    return state.templates.TemplateResponse(
        request,
        template,
        {
            # On every page, because §12.4's meta refresh is what makes a running job visible and
            # a page that omitted it would silently stop updating while a sweep spent money.
            "busy": running,
            **context,
        },
    )


def money(value: Decimal | str | int | None, places: int = 2) -> str:
    if value is None:
        return ABSENT
    exact = to_decimal(value)
    try:
        return f"{exact.quantize(Decimal(f'1e-{places}')):,}"
    except InvalidOperation:
        return str(exact)


def percent(value: Decimal | str | int | None, places: int = 1) -> str:
    """A ratio as a percentage, exactly. `Decimal * 100`, never a float."""
    if value is None:
        return ABSENT
    exact = to_decimal(value) * 100
    try:
        return f"{exact.quantize(Decimal(f'1e-{places}'))}%"
    except InvalidOperation:
        return f"{exact}%"


def moment(value: datetime | None) -> str:
    return ABSENT if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def count(value: int | None) -> str:
    return ABSENT if value is None else f"{value:,}"
