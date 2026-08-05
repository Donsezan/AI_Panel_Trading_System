"""The render shell: what every page is given, and how values reach a template.

Separate from the factory so routers can import it without importing the factory that imports
them. Everything a route needs from the running system arrives through `DashboardState`, which
the factory hangs on `app.state` — a route never reaches for a global.

**Money is `Decimal` and renders through a filter that keeps it exact.** The filters here format
for a human and feed nothing: no template may coerce a limit to `float` on its way to a page,
because the same numbers are read back out of forms on the Configure page (PLAN §2.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import HTTPConnection

from tradebot.app import Application
from tradebot.control.arming import LIVE_CONFIRMATION_PHRASE
from tradebot.control.supervision import SupervisionController
from tradebot.core.enums import Mode
from tradebot.core.money import to_decimal
from tradebot.dashboard.auth import Session
from tradebot.dashboard.dock import KILL_PHRASE, QUARANTINE_CONFIRM
from tradebot.dashboard.queries import Queries
from tradebot.dashboard.updates import UpdateHub
from tradebot.risk.state import REARM_PHRASE

PACKAGE = Path(__file__).parent

#: Who the dashboard records as the author of everything it publishes. Distinct from
#: `composition_root` and `cli`, so the audit trail says a human acted through the GUI.
ACTOR = "dashboard"

#: Colour-codes the header. Mode confusion is a catastrophic failure class, so the banner is a
#: control rather than decoration (PLAN §2.4).
MODE_TONE: dict[Mode, str] = {Mode.SIM: "sim", Mode.PAPER: "paper", Mode.LIVE: "live"}

#: Rendered where a value is genuinely absent, so an empty cell is never read as a zero.
ABSENT = "—"

#: What a template must retype in front of the operator to authorise an act. Four different acts,
#: four different phrases: one phrase that did two of them could be typed for the wrong one.
PHRASES = {
    "kill_phrase": KILL_PHRASE,
    "rearm_phrase": REARM_PHRASE,
    "live_phrase": LIVE_CONFIRMATION_PHRASE,
    "quarantine_confirm": QUARANTINE_CONFIRM,
}


@dataclass(frozen=True, slots=True)
class DashboardState:
    """Everything a route may reach. Hung on `app.state`, read through `state_of`."""

    application: Application
    queries: Queries
    templates: Jinja2Templates
    session: Session
    #: Owns the supervisor's task. Asked rather than remembered, so a page never reports a
    #: boot-time decision that a Start or a Stop has since changed (ADR 0021).
    controller: SupervisionController
    #: The live-update transport. Idle until a page opens a socket, and it carries pane names
    #: outward and nothing inward (ADR 0024).
    updates: UpdateHub

    @property
    def trading(self) -> bool:
        """Whether baskets are cycling right now."""
        return self.controller.running

    @property
    def observe_only(self) -> bool:
        """Nothing is cycling, so nothing is polling open orders.

        Which is why nothing may *create* one: an order placed now would rest at the venue
        unmonitored until supervision starts again. The operator's way out of a position is to
        start supervision first, not to place an order nobody is watching.
        """
        return not self.controller.running


def state_of(connection: HTTPConnection) -> DashboardState:
    """Everything the running system exposes, for a request or a socket.

    Typed on `HTTPConnection` — `Request`'s and `WebSocket`'s common base — so the update socket
    reaches the same state through the same accessor, with no second way in.
    """
    return cast(DashboardState, connection.app.state.dashboard)


def build_templates(application: Application) -> Jinja2Templates:
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.filters.update(
        money=money, quantity=quantity, moment=moment, fromjson=fromjson, prettyjson=prettyjson
    )
    templates.env.globals.update(
        mode=application.mode.value,
        mode_tone=MODE_TONE[application.mode],
        absent=ABSENT,
        # The words that authorise an act, available to every template that renders one. Globals
        # rather than per-route context: a phrase a route forgot to pass would render as an empty
        # `<code>` beside a field the operator then cannot fill (PHASE_10 decision 5).
        **PHRASES,
    )
    return templates


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    """Render a page with the context every page needs. The only place templates are called.

    The kill switch, the halted baskets and an unreachable panel are on *every* page deliberately:
    an operator reading a cycle history must not have to navigate elsewhere to discover that
    nothing is trading, or that every cycle in front of them decided nothing because a seat had no
    key (ADR 0023).
    """
    state = state_of(request)
    return state.templates.TemplateResponse(
        request,
        template,
        {
            "trading": state.trading,
            "observe_only": state.observe_only,
            "risk_state": state.application.states.load(),
            "halted": state.application.states.halted_baskets(),
            "panel_warnings": state.application.panel_warnings,
            **context,
        },
    )


# ---------------------------------------------------------------------- filters


def money(value: Decimal | str | int | None, places: int = 2) -> str:
    """An exact decimal at a fixed number of places, for display and nothing else.

    Rounding here changes what a human reads and never what is traded. A value too large to
    quantize is shown in full rather than replaced: an unwieldy number beats a wrong one.
    """
    if value is None:
        return ABSENT
    exact = to_decimal(value)
    try:
        return f"{exact.quantize(Decimal(f'1e-{places}')):,}"
    except InvalidOperation:
        return str(exact)


def quantity(value: Decimal | str | int | None) -> str:
    """A holding at full precision: a lot size can be 0.00001, and truncating one lies."""
    return ABSENT if value is None else str(to_decimal(value).normalize())


def moment(value: datetime | None) -> str:
    """UTC to the second. Local time in a trading log is unusable for reconstruction."""
    return ABSENT if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def fromjson(value: str | None) -> Any:
    """A projection's JSON column as data. Unreadable JSON renders as nothing, never as a crash.

    `dissent_json` and `flags_json` are written by a projector from an event payload; if one is
    ever malformed, the rest of the drill-down is still the audit record worth showing.
    """
    try:
        return json.loads(value or "null")
    except ValueError:
        return None


def prettyjson(value: Any) -> str:
    """The frozen snapshot, readable. `default=str` so a Decimal survives as its exact digits."""
    return json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)
