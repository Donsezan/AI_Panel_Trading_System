"""Control: pause/resume, un-halt, manual close, and the kill switch (DESIGN §6.10 job 3).

The distinction this module exists to keep visible is **pause versus halt**, which the UI must
not conflate:

* A **pause** is the operator's intent. It is *configuration* — a new basket version with
  `status = paused` — and it is undone by publishing another version.
* A **halt** is the system protecting itself, after repeated cycle failures or a fail-closed
  error. It is *database state*, and only a human clears it, with the typed phrase.

Collapsing them into one button would let a config edit silently un-halt a basket the system
stopped for cause — which is the exact failure "a restart never silently un-halts anything"
exists to prevent.

The **kill switch** trips through the same `Watchdog.trip` the drawdown breach uses, and re-arms
through `Watchdog.rearm` behind `assert_rearm_phrase`. Re-arming resets the baselines, so it is
an assertion that a human has looked at what happened.

**Manual close has no side door**: it goes through `ManualCloser`, which builds an `OrderIntent`
and hands it to the same Tier-1 and Tier-2 engines a cycle uses. A metering rule may refuse it,
and when it does, the rule and its reason are shown rather than worked around.

Failure semantics: every action here writes an event naming the dashboard as its actor, whether
it succeeded or was refused. A refused action re-renders the page with the reason and changes
nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from tradebot.control.manual_close import CloseOutcome
from tradebot.core.config import Basket
from tradebot.core.enums import BasketStatus, ConfigKind
from tradebot.core.errors import TradebotError
from tradebot.core.logging import get_logger
from tradebot.dashboard.views import ACTOR, render, state_of
from tradebot.risk.state import REARM_PHRASE, assert_rearm_phrase

logger = get_logger(__name__)

router = APIRouter(prefix="/control", tags=["control"])

#: Typed to trip the switch by hand. Distinct from the re-arm phrase on purpose: the two acts
#: are opposites, and a single phrase that did both could be typed for the wrong one.
KILL_PHRASE = "STOP TRADING NOW"


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _page(request)


@router.post("/baskets/{basket_id}/status")
async def set_status(request: Request, basket_id: str) -> Response:
    """Pause or resume — the operator's intent, expressed as a new configuration version."""
    form = await request.form()
    status = BasketStatus(_field(form, "status"))
    configs = state_of(request).application.configs
    record = configs.latest(ConfigKind.BASKET, basket_id)
    if record is None or not record.usable:
        return _page(request, error=f"no basket {basket_id} is in service")

    basket: Basket = record.document
    await configs.put(
        basket_id,
        basket.model_copy(update={"status": status}),
        actor=ACTOR,
        note=_field(form, "note") or f"{status.value} from the dashboard",
    )
    logger.warning(
        "basket status published from the dashboard",
        extra={"basket_id": basket_id, "status": status.value},
    )
    return RedirectResponse("/control", status_code=303)


@router.post("/baskets/{basket_id}/unhalt")
async def unhalt(request: Request, basket_id: str) -> Response:
    """Clear a halt the *system* imposed. Persisted state, not configuration, and typed."""
    form = await request.form()
    try:
        assert_rearm_phrase(_field(form, "confirm"))
    except TradebotError as exc:
        return _page(request, error=str(exc))
    await state_of(request).application.watchdog.resume_basket(basket_id, actor=ACTOR)
    logger.warning("basket un-halted from the dashboard", extra={"basket_id": basket_id})
    return RedirectResponse("/control", status_code=303)


@router.post("/kill")
async def kill(request: Request) -> Response:
    """Trip the switch by hand — the same call a drawdown breach makes."""
    form = await request.form()
    if _field(form, "confirm") != KILL_PHRASE:
        return _page(request, error=f"tripping the kill switch requires the phrase {KILL_PHRASE!r}")
    detail = _field(form, "note") or "tripped by hand from the dashboard"
    await state_of(request).application.watchdog.trip(ACTOR, detail)
    return RedirectResponse("/control", status_code=303)


@router.post("/rearm")
async def rearm(request: Request) -> Response:
    """Re-arm after a human has looked at what happened. Resets the drawdown baselines."""
    form = await request.form()
    application = state_of(request).application
    try:
        assert_rearm_phrase(_field(form, "confirm"))
    except TradebotError as exc:
        return _page(request, error=str(exc))
    await application.watchdog.rearm(application.equity(), actor=ACTOR)
    logger.warning("kill switch re-armed from the dashboard")
    return RedirectResponse("/control", status_code=303)


@router.post("/close", response_class=HTMLResponse)
async def close_position(request: Request) -> Response:
    """Close a position through the ordinary risk and execution path. No side doors."""
    state = state_of(request)
    if state.observe_only:
        return _page(
            request,
            error=(
                "this process is serving in observe-only mode, so nothing is polling open "
                "orders; an order placed now would rest at the venue unmonitored"
            ),
        )
    form = await request.form()
    try:
        outcome = await state.application.manual_close.close(
            _field(form, "basket_id"), _field(form, "instrument_key"), actor=ACTOR
        )
    except TradebotError as exc:
        return _page(request, error=str(exc))
    return _page(request, outcome=outcome)


def _page(
    request: Request, *, error: str = "", outcome: CloseOutcome | None = None
) -> HTMLResponse:
    state = state_of(request)
    application = state.application
    return render(
        request,
        "control/index.html",
        baskets=application.configs.baskets(),
        closable=application.manual_close.closable(),
        positions={p.instrument_key: p for p in application.ledger.positions() if not p.is_flat},
        equity=application.equity(),
        quote_currency=application.quote_currency,
        error=error,
        outcome=outcome,
        kill_phrase=KILL_PHRASE,
        rearm_phrase=REARM_PHRASE,
        statuses=[BasketStatus.ACTIVE.value, BasketStatus.PAUSED.value],
    )


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""
