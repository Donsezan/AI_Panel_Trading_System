"""Every mutation the workspace offers: start/stop, arming, pause, un-halt, close, kill.

DESIGN §6.10 job 3, plus Phase 9's operator control. Control stopped being a *page* in Phase 10
pass 3 — `GET /control` redirects to the workspace, whose dock and risk-control panes render these
actions beside the blotter they act on. The POST routes keep their URLs, because that is what they
are: control actions, not view fragments.

Two consequences of losing the page, and both are deliberate:

* **A refusal re-renders the workspace**, through `workspace.page`, with the reason on it and the
  selection intact. An operator mid-incident acts on what they can see, so a refusal must not cost
  them the blotter, the chart and the log they were reading.
* **A success returns to the same selection** (`_back`), so acting on an instrument leaves the
  operator looking at that instrument rather than at the unfiltered screen.

Four mechanisms live here and the UI must not conflate any of them:

* **Start / Stop** is whether this process cycles baskets at all. Stop is the GUI equivalent of
  `--observe`: it cancels nothing at the venue and needs no phrase. It does end the only thing
  polling open orders, so the dock says so, listing whatever is still working (ADR 0021).
* **Arm / Disarm** is live's *permission*, and only live's — three of the four facts of ADR 0012
  live here, with the fourth, the phrase, retyped at both arming and starting and never cached.
  Disarming also stops supervision, deliberately diverging from the CLI's `disarm-live`, which has
  no running process to reach into: a basket left cycling against a cap that was just revoked is
  the one silent state this must never produce.
* **A pause** is the operator's intent. It is *configuration* — a new basket version with
  `status = paused` — and it is undone by publishing another version.
* **A halt** is the system protecting itself, after repeated cycle failures or a fail-closed
  error. It is *database state*, and only a human clears it, with the typed phrase.

Collapsing the last two into one button would let a config edit silently un-halt a basket the
system stopped for cause — which is the exact failure "a restart never silently un-halts anything"
exists to prevent.

**Quarantine** is a third thing again, and the workspace keeps all three apart. It is the
operator's judgement that one instrument — or a whole basket — should not be traded automatically
for now, while its data keeps flowing so they can put it back on evidence. Like a pause it is
versioned configuration and needs no typed phrase; unlike a pause the cycle still runs, and unlike
a halt nothing about it is the system's own doing (ADR 0022). The one thing it must not do quietly
is strand a position, so quarantining a scope that holds one takes a second, deliberate click.

The **kill switch** trips through the same `Watchdog.trip` the drawdown breach uses, and re-arms
through `Watchdog.rearm` behind `assert_rearm_phrase`. Re-arming resets the baselines, so it is
an assertion that a human has looked at what happened.

**Manual close has no side door**: it goes through `ManualCloser`, which builds an `OrderIntent`
and hands it to the same Tier-1 and Tier-2 engines a cycle uses. A metering rule may refuse it,
and when it does, the rule and its reason are shown rather than worked around.

Failure semantics: every action here writes an event naming the dashboard as its actor, whether
it succeeded or was refused. A refused action re-renders the workspace with the reason and changes
nothing.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from tradebot.control.arming import assert_live_confirmation
from tradebot.control.reference import store_basket
from tradebot.core.config import Basket
from tradebot.core.enums import BasketStatus, ConfigKind
from tradebot.core.errors import ConfigError, ModeConfusionError, TradebotError
from tradebot.core.logging import get_logger
from tradebot.core.money import to_decimal
from tradebot.dashboard.dock import (
    KILL_PHRASE,
    QUARANTINE_CONFIRM,
    PendingQuarantine,
    held_within,
)
from tradebot.dashboard.routes.workspace import page
from tradebot.dashboard.views import ACTOR, state_of
from tradebot.risk.state import assert_rearm_phrase

logger = get_logger(__name__)

router = APIRouter(prefix="/control", tags=["control"])


@router.get("")
async def control_moved() -> Response:
    """Replaced by the workspace's dock, so it redirects rather than staying a second copy.

    Two surfaces for the kill switch is two places for its state to disagree, which on this
    particular control is not a cosmetic problem (PHASE_10 decision 3).
    """
    return RedirectResponse("/", status_code=303)


@router.post("/start")
async def start(request: Request) -> Response:
    """Begin cycling baskets. In live, every precondition is re-checked at this exact moment."""
    form = await request.form()
    unmet = await state_of(request).controller.start(confirmation=_field(form, "confirm"))
    if unmet:
        return _refused(request, form, "nothing was started; missing: " + "; ".join(unmet))
    return _back(form)


@router.post("/stop")
async def stop(request: Request) -> Response:
    """Pause supervision. Never refused — an operator reaches for this during an incident."""
    form = await request.form()
    await state_of(request).controller.stop()
    return _back(form)


@router.post("/live/arm")
async def arm_live(request: Request) -> Response:
    """Record that a human armed live trading, with an explicit per-order cap (ADR 0012).

    Permission only: arming does not start anything, and the phrase is demanded again by Start.
    """
    form = await request.form()
    application = state_of(request).application
    try:
        _assert_live_mode(request)
        assert_live_confirmation(_field(form, "confirm"))
        arming = await application.arming.arm(
            actor=ACTOR,
            max_live_notional=to_decimal(_field(form, "max_notional")),
            note=_field(form, "note"),
        )
    except TradebotError as exc:
        return _refused(request, form, str(exc))
    logger.warning(
        "LIVE TRADING ARMED from the dashboard",
        extra={"max_live_notional": str(arming.max_live_notional), "note": arming.note},
    )
    return _back(form)


@router.post("/live/disarm")
async def disarm_live(request: Request) -> Response:
    """Withdraw live permission — and stop supervision, which the CLI cannot do (ADR 0021)."""
    form = await request.form()
    state = state_of(request)
    try:
        _assert_live_mode(request)
    except TradebotError as exc:
        return _refused(request, form, str(exc))
    reason = _field(form, "reason") or "disarmed from the dashboard"
    await state.application.arming.disarm(actor=ACTOR, reason=reason)
    await state.controller.stop()
    logger.warning("live trading disarmed from the dashboard", extra={"reason": reason})
    return _back(form)


def _assert_live_mode(request: Request) -> None:
    """Arming is per-database, and only the live one has anything to arm (ADR 0012)."""
    mode = state_of(request).application.mode
    if not mode.is_live:
        raise ModeConfusionError(
            f"live arming applies to the live database only; this process is in {mode.value} mode"
        )


@router.post("/baskets/{basket_id}/status")
async def set_status(request: Request, basket_id: str) -> Response:
    """Pause or resume — the operator's intent, expressed as a new configuration version."""
    form = await request.form()
    status = BasketStatus(_field(form, "status"))
    configs = state_of(request).application.configs
    record = configs.latest(ConfigKind.BASKET, basket_id)
    if record is None or not record.usable:
        return _refused(request, form, f"no basket {basket_id} is in service")

    basket: Basket = record.document
    try:
        await store_basket(
            configs,
            state_of(request).application.catalogue,
            basket.model_copy(update={"status": status}),
            actor=ACTOR,
            note=_field(form, "note") or f"{status.value} from the dashboard",
        )
    except ConfigError as exc:
        return _refused(request, form, str(exc))
    logger.warning(
        "basket status published from the dashboard",
        extra={"basket_id": basket_id, "status": status.value},
    )
    return _back(form)


@router.post("/baskets/{basket_id}/quarantine")
async def set_quarantine(request: Request, basket_id: str) -> Response:
    """Exclude an instrument, or a whole basket, from automated trading — or release it.

    Configuration, like a pause: a new version, attributable, reversible by publishing another.
    The cycle is untouched, so the data an operator needs in order to decide when to release it
    keeps arriving. `QuarantineRule` is what refuses the order (ADR 0022).
    """
    form = await request.form()
    state = state_of(request)
    configs = state.application.configs
    record = configs.latest(ConfigKind.BASKET, basket_id)
    if record is None or not record.usable:
        return _refused(request, form, f"no basket {basket_id} is in service")

    basket: Basket = record.document
    instrument_key = _field(form, "instrument_key")
    excluded = _field(form, "excluded") == "true"
    held = held_within(basket, instrument_key, _held_keys(request))
    if excluded and held and _field(form, "confirm") != QUARANTINE_CONFIRM:
        return page(
            request,
            scope=_field(form, "scope"),
            tf=_field(form, "tf"),
            pending=PendingQuarantine(basket_id, instrument_key, held),
        )

    policy = basket.risk_policy.with_quarantine(instrument_key, excluded=excluded)
    try:
        published = await store_basket(
            configs,
            state.application.catalogue,
            basket.model_copy(update={"risk_policy": policy}),
            actor=ACTOR,
            note=_field(form, "note") or _quarantine_note(instrument_key, excluded=excluded),
        )
    except ConfigError as exc:
        return _refused(request, form, str(exc))
    logger.warning(
        "quarantine published from the dashboard",
        extra={
            "basket_id": basket_id,
            "instrument_key": instrument_key,
            "excluded": excluded,
            "version": published.ref.version,
            "held": list(held),
        },
    )
    return _back(form)


def _held_keys(request: Request) -> set[str]:
    """What the portfolio actually holds — the reason a quarantine can need a second click.

    Inaction can compound a loss as readily as action can cause one, so an operator excluding
    something they still hold is told exactly what they are about to stop managing.
    """
    ledger = state_of(request).application.ledger
    return {p.instrument_key for p in ledger.positions() if not p.is_flat}


def _quarantine_note(instrument_key: str, *, excluded: bool) -> str:
    scope = instrument_key or "the whole basket"
    return f"{'quarantined' if excluded else 'released'} {scope} from the dashboard"


@router.post("/baskets/{basket_id}/unhalt")
async def unhalt(request: Request, basket_id: str) -> Response:
    """Clear a halt the *system* imposed. Persisted state, not configuration, and typed."""
    form = await request.form()
    try:
        assert_rearm_phrase(_field(form, "confirm"))
    except TradebotError as exc:
        return _refused(request, form, str(exc))
    await state_of(request).application.watchdog.resume_basket(basket_id, actor=ACTOR)
    logger.warning("basket un-halted from the dashboard", extra={"basket_id": basket_id})
    return _back(form)


@router.post("/kill")
async def kill(request: Request) -> Response:
    """Trip the switch by hand — the same call a drawdown breach makes."""
    form = await request.form()
    if _field(form, "confirm") != KILL_PHRASE:
        return _refused(
            request, form, f"tripping the kill switch requires the phrase {KILL_PHRASE!r}"
        )
    detail = _field(form, "note") or "tripped by hand from the dashboard"
    await state_of(request).application.watchdog.trip(ACTOR, detail)
    return _back(form)


@router.post("/rearm")
async def rearm(request: Request) -> Response:
    """Re-arm after a human has looked at what happened. Resets the drawdown baselines."""
    form = await request.form()
    application = state_of(request).application
    try:
        assert_rearm_phrase(_field(form, "confirm"))
    except TradebotError as exc:
        return _refused(request, form, str(exc))
    # Re-arming writes both baselines from current equity, so it cannot proceed on a number the
    # system has just said it cannot compute — that would persist a fiction outliving the outage
    # and would spend the operator's typed phrase on nothing (ADR 0027).
    current = application.valuation()
    if current.frozen:
        return _refused(
            request,
            form,
            "the portfolio cannot be valued, so there is no equity to re-arm against: "
            f"{current.frozen_reason}",
        )
    await application.watchdog.rearm(current.equity, actor=ACTOR)
    logger.warning("kill switch re-armed from the dashboard")
    return _back(form)


@router.post("/close", response_class=HTMLResponse)
async def close_position(request: Request) -> Response:
    """Close a position through the ordinary risk and execution path. No side doors.

    Never redirects, whatever the answer: an order id, a shrink, or the rule that refused is what
    the operator asked for, and a redirect would throw it away before it was read.
    """
    state = state_of(request)
    form = await request.form()
    if state.observe_only:
        return _refused(
            request,
            form,
            "supervision is stopped, so nothing is polling open orders; an order placed now "
            "would rest at the venue unmonitored. Start supervision first, then close",
        )
    try:
        outcome = await state.application.manual_close.close(
            _field(form, "basket_id"), _field(form, "instrument_key"), actor=ACTOR
        )
    except TradebotError as exc:
        return _refused(request, form, str(exc))
    return page(request, scope=_field(form, "scope"), tf=_field(form, "tf"), outcome=outcome)


def _back(form: FormData) -> RedirectResponse:
    """Back to the workspace, still showing what the operator was acting on.

    303 rather than 302, so the browser turns a POST into a GET and a refresh cannot resubmit the
    act — which for a kill switch or an arming is the whole point.
    """
    query = urlencode({name: value for name in ("scope", "tf") if (value := _field(form, name))})
    return RedirectResponse(f"/?{query}" if query else "/", status_code=303)


def _refused(request: Request, form: FormData, error: str) -> HTMLResponse:
    """The workspace again, with the reason and the selection the operator started from."""
    return page(request, scope=_field(form, "scope"), tf=_field(form, "tf"), error=error)


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""
