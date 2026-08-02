"""Control: start/stop, arming, pause/resume, un-halt, manual close, and the kill switch.

DESIGN §6.10 job 3, plus Phase 9's operator control. Four mechanisms live on this page and the UI
must not conflate any of them:

* **Start / Stop** is whether this process cycles baskets at all. Stop is the GUI equivalent of
  `--observe`: it cancels nothing at the venue and needs no phrase. It does end the only thing
  polling open orders, so the page says so, listing whatever is still working (ADR 0021).
* **Arm / Disarm** is live's *permission*, and only live's — three of the four facts of ADR 0012
  live here, with the fourth, the phrase, retyped at both arming and starting and never cached.
  Disarming also stops supervision, deliberately diverging from the CLI's `disarm-live`, which has
  no running process to reach into: a basket left cycling against a cap that was just revoked is
  the one silent state this must never produce.

The distinction this module exists to keep visible is **pause versus halt**, which the UI must
not conflate:

* A **pause** is the operator's intent. It is *configuration* — a new basket version with
  `status = paused` — and it is undone by publishing another version.
* A **halt** is the system protecting itself, after repeated cycle failures or a fail-closed
  error. It is *database state*, and only a human clears it, with the typed phrase.

Collapsing them into one button would let a config edit silently un-halt a basket the system
stopped for cause — which is the exact failure "a restart never silently un-halts anything"
exists to prevent.

**Quarantine** is a third thing again, and the page keeps all three apart. It is the operator's
judgement that one instrument — or a whole basket — should not be traded automatically for now,
while its data keeps flowing so they can put it back on evidence. Like a pause it is versioned
configuration and needs no typed phrase; unlike a pause the cycle still runs, and unlike a halt
nothing about it is the system's own doing (ADR 0022). The one thing it must not do quietly is
strand a position, so quarantining a scope that holds one takes a second, deliberate click.

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

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from tradebot.control.arming import LIVE_CONFIRMATION_PHRASE, assert_live_confirmation
from tradebot.control.manual_close import CloseOutcome
from tradebot.core.config import Basket
from tradebot.core.enums import BasketStatus, ConfigKind
from tradebot.core.errors import ModeConfusionError, TradebotError
from tradebot.core.logging import get_logger
from tradebot.core.money import to_decimal
from tradebot.dashboard.views import ACTOR, render, state_of
from tradebot.risk.state import REARM_PHRASE, assert_rearm_phrase

logger = get_logger(__name__)

router = APIRouter(prefix="/control", tags=["control"])

#: Typed to trip the switch by hand. Distinct from the re-arm phrase on purpose: the two acts
#: are opposites, and a single phrase that did both could be typed for the wrong one.
KILL_PHRASE = "STOP TRADING NOW"

#: Sent by the second click that quarantines a scope holding a position. Deliberately *not* a
#: typed phrase — quarantine is reversible configuration, and demanding one here would make it
#: feel like a halt. What the click buys is that the consequence was read: from that moment the
#: bot is hands-off the position, and only a manual close will move it (ADR 0022).
QUARANTINE_CONFIRM = "quarantine-anyway"


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return _page(request)


@router.post("/start")
async def start(request: Request) -> Response:
    """Begin cycling baskets. In live, every precondition is re-checked at this exact moment."""
    form = await request.form()
    unmet = await state_of(request).controller.start(confirmation=_field(form, "confirm"))
    if unmet:
        return _page(request, error="nothing was started; missing: " + "; ".join(unmet))
    return RedirectResponse("/control", status_code=303)


@router.post("/stop")
async def stop(request: Request) -> Response:
    """Pause supervision. Never refused — an operator reaches for this during an incident."""
    await state_of(request).controller.stop()
    return RedirectResponse("/control", status_code=303)


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
        return _page(request, error=str(exc))
    logger.warning(
        "LIVE TRADING ARMED from the dashboard",
        extra={"max_live_notional": str(arming.max_live_notional), "note": arming.note},
    )
    return RedirectResponse("/control", status_code=303)


@router.post("/live/disarm")
async def disarm_live(request: Request) -> Response:
    """Withdraw live permission — and stop supervision, which the CLI cannot do (ADR 0021)."""
    form = await request.form()
    state = state_of(request)
    try:
        _assert_live_mode(request)
    except TradebotError as exc:
        return _page(request, error=str(exc))
    reason = _field(form, "reason") or "disarmed from the dashboard"
    await state.application.arming.disarm(actor=ACTOR, reason=reason)
    await state.controller.stop()
    logger.warning("live trading disarmed from the dashboard", extra={"reason": reason})
    return RedirectResponse("/control", status_code=303)


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


@dataclass(frozen=True, slots=True)
class PendingQuarantine:
    """A quarantine that would leave a held position unmanaged, waiting for a second click."""

    basket_id: str
    #: Empty for a whole-basket quarantine — the same convention the form and the policy use.
    instrument_key: str
    held: tuple[str, ...]

    @property
    def scope(self) -> str:
        return self.instrument_key or f"basket {self.basket_id}"


@router.post("/baskets/{basket_id}/quarantine")
async def set_quarantine(request: Request, basket_id: str) -> Response:
    """Exclude an instrument, or a whole basket, from automated trading — or release it.

    Configuration, like a pause: a new version, attributable, reversible by publishing another.
    The cycle is untouched, so the data an operator needs in order to decide when to release it
    keeps arriving. `QuarantineRule` is what refuses the order (ADR 0022).
    """
    form = await request.form()
    configs = state_of(request).application.configs
    record = configs.latest(ConfigKind.BASKET, basket_id)
    if record is None or not record.usable:
        return _page(request, error=f"no basket {basket_id} is in service")

    basket: Basket = record.document
    instrument_key = _field(form, "instrument_key")
    excluded = _field(form, "excluded") == "true"
    held = _held_within(request, basket, instrument_key)
    if excluded and held and _field(form, "confirm") != QUARANTINE_CONFIRM:
        return _page(request, pending=PendingQuarantine(basket_id, instrument_key, held))

    policy = basket.risk_policy.with_quarantine(instrument_key, excluded=excluded)
    published = await configs.put(
        basket_id,
        basket.model_copy(update={"risk_policy": policy}),
        actor=ACTOR,
        note=_field(form, "note") or _quarantine_note(instrument_key, excluded=excluded),
    )
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
    return RedirectResponse("/control", status_code=303)


def _held_within(request: Request, basket: Basket, instrument_key: str) -> tuple[str, ...]:
    """Positions the quarantine would leave the bot hands-off — the reason for the warning.

    Inaction can compound a loss as readily as action can cause one, so an operator excluding
    something they still hold is told exactly what they are about to stop managing.
    """
    scope = (instrument_key,) if instrument_key else tuple(i.key for i in basket.instruments)
    ledger = state_of(request).application.ledger
    held = {p.instrument_key for p in ledger.positions() if not p.is_flat}
    return tuple(key for key in scope if key in held)


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
                "supervision is stopped, so nothing is polling open orders; an order placed now "
                "would rest at the venue unmonitored. Start supervision above, then close"
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
    request: Request,
    *,
    error: str = "",
    outcome: CloseOutcome | None = None,
    pending: PendingQuarantine | None = None,
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
        pending=pending,
        arming=application.arming.load(),
        # Shown whether or not they are met, so the operator reads the whole list rather than
        # discovering it one refused Start at a time. The phrase is always among them: it is
        # typed into the form below, never held anywhere (ADR 0021).
        blockers=state.controller.blockers(),
        # Left resting by a Stop, and polled by nothing until supervision starts again.
        working=state.queries.open_orders() if not state.trading else (),
        kill_phrase=KILL_PHRASE,
        rearm_phrase=REARM_PHRASE,
        live_phrase=LIVE_CONFIRMATION_PHRASE,
        quarantine_confirm=QUARANTINE_CONFIRM,
        statuses=[BasketStatus.ACTIVE.value, BasketStatus.PAUSED.value],
    )


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""
