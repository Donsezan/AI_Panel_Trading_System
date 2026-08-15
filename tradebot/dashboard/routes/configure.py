"""Configure: basket, panel and risk CRUD (DESIGN §6.10 job 1).

Every Tier-1 and Tier-2 limit is editable here and DB-persisted; nothing risk-related is a
constant in code. Publishing is one call — `configs.put(...)` — which writes a new version and
its `CONFIG_CHANGED` event in one transaction, and the basket's worker picks it up at its next
cycle boundary. There is no other write path.

Three rules this module exists to honour:

* **Validation is the engine's own pydantic models, unchanged.** The form surfaces their
  messages; it never restates a rule (`forms.py`). The one thing the models cannot check is
  whether an instrument's trading rules are the venue's, because that needs the venue —
  `control/reference.py` asks it on the way to the store, for every basket write path (ADR 0025).
* **Loosening a Tier-2 limit needs a second typed act** (DESIGN §6.10). Which edits count as
  loosening is decided by `risk/loosening.py`, so tightening is never made to feel like a risk.
* **A fallback binding is picked, never typed.** The seat editor's selects are built from the
  providers the panel declares, so an undeclared provider cannot be entered at all — and
  `PanelConfig`'s validator is still the thing that proves it.
* **Both panels are edited by one macro** (`_panel.html`). The A/B challenger is a `PanelConfig`
  like the champion, and a form that rendered only the champion would silently drop a configured
  challenger the first time anyone edited the basket (ADR 0018).

Editing is stateless: the form round-trips as a *draft* dict, so adding a seat, removing an
instrument or fixing a validation error never loses what the operator has already typed. Only
`publish` validates into a document, and only a document that validates is stored.

Failure semantics: a draft that fails validation is re-rendered with the errors located on
their fields and nothing is written. A secret pasted into any field is refused by `ConfigStore`
at publish time and reported as such — configuration references secrets by env-var name only.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from tradebot.control.config_store import SINGLETON_ID, ConfigStore
from tradebot.control.reference import VERIFIED_FIELDS, holders_of, store_basket
from tradebot.core.config import (
    Basket,
    GlobalRiskPolicy,
    PanelConfig,
    ProviderSettings,
    RiskPolicy,
    Schedule,
    SeatConfig,
)
from tradebot.core.enums import AssetClass, BasketStatus, ConfigKind, DecisionMode, ProviderKind
from tradebot.core.errors import ConfigError, TradebotError
from tradebot.core.logging import get_logger
from tradebot.dashboard.editor import (
    PANEL_PATHS,
    SHADOW_PATH,
    declared_providers,
    focus_for,
    instrument_keys,
    instrument_rows,
    panel_providers,
    provider_rows,
    seat_rows,
)
from tradebot.dashboard.forms import FieldError, add_row, draft_of, nest, remove_row, validate
from tradebot.dashboard.views import ACTOR, render, state_of
from tradebot.decision.protocols import PROTOCOLS
from tradebot.indicators.library import REGISTRY
from tradebot.marketdata.catalogue import instrument_of
from tradebot.news.rss import FEEDS
from tradebot.risk.loosening import describe, looser_limits

logger = get_logger(__name__)

router = APIRouter(prefix="/configure", tags=["configure"])

#: Typed to publish a Tier-2 policy that permits more than the one in force (DESIGN §6.10).
LOOSEN_PHRASE = "LOOSEN GLOBAL LIMITS"

#: Timeframes an operator may pick. The indicator engine's own set, so a basket cannot ask for a
#: timeframe the engine has no candles for.
TIMEFRAMES = ("15m", "1h", "4h", "1d")

#: Which parts of the snapshot a seat is shown. Giving seats *different* slices is what
#: manufactures genuine disagreement, so it is editable rather than fixed (DESIGN §6.5).
EVIDENCE_SLICES = ("indicators", "news", "position")

#: Tab-selection fields. Outside the `doc.` namespace, so `nest()` ignores them and no parser
#: change is needed to round-trip which tab the operator was on.
UI_PREFIX = "ui."


def ui_of(form: FormData) -> dict[str, str]:
    """The tabs the submitted page was showing, keyed by radio group name without its prefix."""
    return {
        name[len(UI_PREFIX) :]: value
        for name, value in form.multi_items()
        if name.startswith(UI_PREFIX) and isinstance(value, str) and value
    }


# ---------------------------------------------------------------------- index


@router.get("", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    state = state_of(request)
    return render(
        request,
        "configure/index.html",
        baskets=state.application.configs.baskets(),
        policy=state.application.configs.global_risk(),
    )


@router.get("/history/{kind}/{config_id}", response_class=HTMLResponse)
async def history(request: Request, kind: str, config_id: str) -> HTMLResponse:
    """Every version of one document — who changed what, and when (ADR 0013)."""
    try:
        config_kind = ConfigKind(kind)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"no configuration kind {kind}") from exc
    versions = state_of(request).application.configs.history(config_kind, config_id)
    if not versions:
        raise HTTPException(status_code=404, detail=f"no configuration {kind}:{config_id}")
    return render(
        request, "configure/history.html", versions=versions, kind=kind, config_id=config_id
    )


# ---------------------------------------------------------------------- baskets


@router.get("/baskets/new", response_class=HTMLResponse)
async def new_basket(request: Request) -> HTMLResponse:
    venue_id = state_of(request).application.catalogue.venue_id
    return _basket_form(request, blank_basket_draft(venue_id), existing=None)


@router.get("/baskets/{basket_id}", response_class=HTMLResponse)
async def edit_basket(request: Request, basket_id: str) -> HTMLResponse:
    record = _basket_record(request, basket_id)
    return _basket_form(
        request,
        draft_of(record.document),
        existing=record.ref.version,
        released=tuple(request.query_params.getlist("released")),
    )


@router.post("/baskets/{basket_id}/draft", response_class=HTMLResponse)
async def redraft_basket(request: Request, basket_id: str) -> HTMLResponse:
    """Add or remove a row, look up an instrument, and re-render, publishing nothing.

    This is what keeps the provider picker honest: a provider added in this form appears in
    every seat's fallback select the moment the draft is re-rendered, so the chain is still
    built from declared providers rather than from free text (DESIGN §6.10). The same round trip
    is what lets **Look up** fill a row from the venue before anything is published (ADR 0025).
    """
    form = await request.form()
    draft = nest(form.multi_items())
    ui = ui_of(form) | _apply_row_action(draft, form)
    errors = await _apply_lookup(request, draft, form)
    if errors:
        ui = ui | {"section": "instruments"}
    return _basket_form(
        request,
        draft,
        existing=_version_field(form),
        basket_id=basket_id,
        errors=errors,
        ui=ui,
    )


def carry_quarantine(basket: Basket, previous: Basket | None) -> tuple[Basket, tuple[str, ...]]:
    """Re-attach the quarantine this form does not carry, from the *stored* basket.

    Quarantine left Settings because it is an operational act with a held-position guard the
    workspace has and a form cannot: from the moment a scope holding a position is excluded the bot
    is hands-off it, and inaction compounds a loss as readily as action causes one (ADR 0022).

    Deleting the two controls is not sufficient — it is actively dangerous. The form is the whole
    document and `nest()` omits absent fields, so `quarantined` would fall back to `False` and
    `quarantined_instruments` to `()`, and **every publish from Settings would silently release
    every quarantine in force**, including one set on the workspace ten seconds earlier.

    Three deliberate properties:

    * It **overwrites unconditionally, never merges**, so a hand-crafted POST cannot set one either.
      Publishing from Settings cannot change a quarantine in *either* direction.
    * `previous is None` — a new basket, or an id renamed in this edit — forces an empty quarantine
      rather than trusting the draft. Correct: it is a different basket.
    * A carried key naming an instrument this edit removed is dropped and **returned**, because
      `Basket._check_quarantine` would otherwise refuse the document over a key nobody typed.

    Read at publish time rather than carried in a hidden field, so a quarantine set on the
    workspace while this form was open survives.

    Known gap: `previous` is read here, outside the `configs.publishing()` lock that `store_basket`
    holds across its own read-check-write. A quarantine published on the workspace *after* this read
    but before that write is therefore still overwritten. The window is one venue verification wide,
    and it is not specific to quarantine — `control.py`'s pause and quarantine routes read the
    stored basket the same way, so either can drop the other's edit. Closing it properly means a
    version-checked publish (reject when the stored version moved under the editor), which belongs
    on `ConfigStore` rather than here; it is recorded in the Phase 11 document as outstanding.

    `model_copy` skips validation, which is safe here **by construction**: `named` is filtered to
    keys the basket holds, so `_check_quarantine`'s invariant cannot be violated. `RiskPolicy.
    with_quarantine` uses the same pattern.
    """
    policy = previous.risk_policy if previous else None
    stored = policy.quarantined_instruments if policy else ()
    held = {instrument.key for instrument in basket.instruments}
    named = tuple(key for key in stored if key in held)
    dropped = tuple(key for key in stored if key not in held)
    carried = basket.risk_policy.model_copy(
        update={
            "quarantined": bool(policy and policy.quarantined),
            "quarantined_instruments": named,
        }
    )
    return basket.model_copy(update={"risk_policy": carried}), dropped


@router.post("/baskets/{basket_id}", response_class=HTMLResponse)
async def publish_basket(request: Request) -> Response:
    """Publish the submitted document. The id comes from the *form*, not from the path.

    Deliberate: the operator can rename a basket in the identity field, and taking the id from
    the URL would then publish version 2 of the old id carrying a document that names a new one.
    """
    form = await request.form()
    draft = fold_prices(drop_blank_shadow(drop_quarantine(nest(form.multi_items()))))
    basket, errors = validate(Basket, draft)
    if basket is None:
        return _basket_form(
            request,
            draft,
            existing=_version_field(form),
            errors=ask_for_lookup(draft, errors),
            ui=ui_of(form),
        )

    application = state_of(request).application
    previous = application.configs.latest(ConfigKind.BASKET, basket.basket_id)
    basket, released = carry_quarantine(
        basket, previous.document if previous and previous.usable else None
    )
    try:
        record = await store_basket(
            application.configs,
            application.catalogue,
            basket,
            actor=ACTOR,
            note=_note(form, "edited in the dashboard"),
        )
    except ConfigError as exc:
        return _basket_form(
            request,
            draft,
            existing=_version_field(form),
            errors=_refusal(exc),
            ui=ui_of(form),
        )
    logger.warning(
        "basket published from the dashboard",
        extra={
            "basket_id": record.ref.config_id,
            "version": record.ref.version,
            "released": list(released),
        },
    )
    query = urlencode([("released", key) for key in released])
    target = f"/configure/baskets/{basket.basket_id}"
    return RedirectResponse(f"{target}?{query}" if query else target, status_code=303)


@router.post("/baskets/{basket_id}/retire")
async def retire_basket(request: Request, basket_id: str) -> Response:
    """Withdraw a basket from service. Every version stays resolvable for the cycles that ran it."""
    form = await request.form()
    await state_of(request).application.configs.retire(
        ConfigKind.BASKET, basket_id, actor=ACTOR, reason=_note(form, "retired in the dashboard")
    )
    logger.warning("basket retired from the dashboard", extra={"basket_id": basket_id})
    return RedirectResponse("/configure", status_code=303)


# ---------------------------------------------------------------------- tier 2


@router.get("/risk", response_class=HTMLResponse)
async def edit_risk(request: Request) -> HTMLResponse:
    record = state_of(request).application.configs.global_risk()
    draft = draft_of(record.document) if record else draft_of(GlobalRiskPolicy())
    return _risk_form(request, draft)


@router.post("/risk", response_class=HTMLResponse)
async def publish_risk(request: Request) -> Response:
    form = await request.form()
    draft = nest(form.multi_items())
    policy, errors = validate(GlobalRiskPolicy, draft)
    if policy is None:
        return _risk_form(request, draft, errors=errors)

    configs = state_of(request).application.configs
    record = configs.global_risk()
    current = record.document if record else GlobalRiskPolicy()
    loosened = describe(current, policy)
    if loosened and _confirmation(form) != LOOSEN_PHRASE:
        return _risk_form(request, draft, loosened=loosened, needs_confirmation=True)

    try:
        published = await configs.put(
            SINGLETON_ID, policy, actor=ACTOR, note=_note(form, "edited in the dashboard")
        )
    except ConfigError as exc:
        return _risk_form(request, draft, errors=_refusal(exc))
    logger.warning(
        "global risk policy published from the dashboard",
        extra={"version": published.ref.version, "loosened": list(looser_limits(current, policy))},
    )
    return RedirectResponse("/configure/risk", status_code=303)


# ---------------------------------------------------------------------- rendering


def _basket_form(
    request: Request,
    draft: dict[str, Any],
    *,
    existing: int | None,
    errors: tuple[FieldError, ...] = (),
    basket_id: str = "",
    ui: dict[str, str] | None = None,
    released: tuple[str, ...] = (),
) -> HTMLResponse:
    draft = unfold_prices(draft)
    application = state_of(request).application
    basket_key = str(draft.get("basket_id") or basket_id)
    quarantine, basket_quarantined = _stored_quarantine(application.configs, basket_key)
    return render(
        request,
        "configure/basket.html",
        draft=draft,
        errors=errors,
        existing=existing,
        basket_id=basket_key,
        ui=ui or {},
        released=released,
        instrument_keys=instrument_keys(draft),
        providers=declared_providers(draft, "panel"),
        shadow_providers=declared_providers(draft, SHADOW_PATH),
        panel_seats=seat_rows(draft.get("panel") or {}),
        panel_providers_view=provider_rows(draft.get("panel") or {}),
        shadow_seats=seat_rows(draft.get(SHADOW_PATH) or {}),
        shadow_providers_view=provider_rows(draft.get(SHADOW_PATH) or {}),
        timeframes=TIMEFRAMES,
        indicators=sorted(REGISTRY),
        news_sources=sorted(FEEDS),
        protocols=sorted(PROTOCOLS),
        evidence_slices=EVIDENCE_SLICES,
        asset_classes=[cls.value for cls in AssetClass],
        provider_kinds=[kind.value for kind in ProviderKind],
        decision_modes=[mode.value for mode in DecisionMode],
        statuses=[status.value for status in BasketStatus],
        catalogue=application.catalogue,
        basket_quarantined=basket_quarantined,
        instrument_rows=instrument_rows(
            draft,
            venue_id=application.catalogue.venue_id,
            quarantined=quarantine,
            holders=holders_of(application.configs.baskets()),
            basket_id=basket_key,
        ),
    )


def _stored_quarantine(configs: ConfigStore, basket_id: str) -> tuple[tuple[str, ...], bool]:
    """What the *stored* basket excludes: the named instruments, and the whole-basket flag.

    Read from the store rather than from the draft, because the form does not carry quarantine at
    all — the act lives on the workspace, which has the held-position guard this form cannot offer
    (ADR 0022). Read at render time too, so a quarantine set on the workspace while this form was
    open is shown the next time the page paints.
    """
    record = configs.latest(ConfigKind.BASKET, basket_id) if basket_id else None
    if record is None or not record.usable:
        return (), False
    policy = record.document.risk_policy
    return tuple(policy.quarantined_instruments), policy.quarantined


def _risk_form(
    request: Request,
    draft: dict[str, Any],
    *,
    errors: tuple[FieldError, ...] = (),
    loosened: tuple[str, ...] = (),
    needs_confirmation: bool = False,
) -> HTMLResponse:
    return render(
        request,
        "configure/risk.html",
        draft=draft,
        errors=errors,
        loosened=loosened,
        needs_confirmation=needs_confirmation,
        phrase=LOOSEN_PHRASE,
    )


#: A provider's per-model prices are a `Mapping[str, ModelPricing]`, and an HTML form cannot
#: express a mapping whose keys are arbitrary model ids — `gpt-4.1` and `qwen/qwen3:free` both
#: contain characters the field-path parser splits on. The form therefore edits *rows* carrying
#: the model id as a value, and these two inverses convert between the two shapes. They move a
#: shape, never a rule: the prices themselves are still validated by `PriceList`.
PRICE_ROWS = "price_rows"


def fold_prices(draft: dict[str, Any]) -> dict[str, Any]:
    """Form price rows → the `prices.models` mapping the model expects."""
    for provider in _providers_of(draft):
        rows = provider.pop(PRICE_ROWS, None)
        if not isinstance(rows, list):
            continue
        provider["prices"] = {
            "models": {
                str(row["model"]): {
                    key: row[key]
                    for key in ("prompt_per_million", "completion_per_million")
                    if key in row
                }
                for row in rows
                if isinstance(row, dict) and str(row.get("model", "")).strip()
            }
        }
    return draft


def unfold_prices(draft: dict[str, Any]) -> dict[str, Any]:
    """The stored mapping → the rows the form renders. Leaves an already-unfolded draft alone."""
    for provider in _providers_of(draft):
        if PRICE_ROWS in provider:
            continue
        prices = provider.get("prices") or {}
        models = prices.get("models") or {} if isinstance(prices, dict) else {}
        provider[PRICE_ROWS] = [
            {"model": model, **(pricing if isinstance(pricing, dict) else {})}
            for model, pricing in sorted(models.items())
        ]
    return draft


def _providers_of(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """Provider rows across **both** panels — the champion's and the challenger's."""
    return [row for path in PANEL_PATHS for row in panel_providers(draft, path)]


def drop_blank_shadow(draft: dict[str, Any]) -> dict[str, Any]:
    """An untouched challenger section is *no challenger*, not an invalid one.

    The shadow panel is optional and its fields are always rendered, so a basket that wants no
    challenger still posts the section — and a `<select>` always submits something, so the draft
    arrives carrying a protocol and nothing else. Without this, publishing any basket at all would
    fail on a panel the operator never asked for. A panel id *is* content, so anything typed there
    keeps the section and lets `PanelConfig` say what else it needs.
    """
    panel = draft.get(SHADOW_PATH)
    if isinstance(panel, dict) and not str(panel.get("panel_id", "")).strip():
        draft.pop(SHADOW_PATH)
    return draft


def drop_quarantine(draft: dict[str, Any]) -> dict[str, Any]:
    """Quarantine is not a field of this form, so it is discarded before the draft is validated.

    Dropped *before* validation rather than overwritten after it, which is what makes "Settings is
    not a surface for this act" total. A hand-crafted POST naming an instrument this same edit
    removed would otherwise be refused by `Basket._check_quarantine` — a refusal quoting a key the
    operator never typed and cannot see anywhere on the page. `carry_quarantine` then re-attaches
    what the *stored* basket says, which is the only answer this route will accept.
    """
    policy = draft.get("risk_policy")
    if isinstance(policy, dict):
        policy.pop("quarantined", None)
        policy.pop("quarantined_instruments", None)
    return draft


#: What one unresolved instrument row is told, in place of the venue-owned fields it is missing.
LOOKUP_REFUSAL = (
    "this row has not been resolved: name the venue's own symbol and press Look up. An "
    "instrument's trading rules are what the venue publishes and are never typed here (ADR 0025)"
)


def ask_for_lookup(draft: dict[str, Any], errors: tuple[FieldError, ...]) -> tuple[FieldError, ...]:
    """Say "press Look up" rather than demand fields the operator is not allowed to type.

    An instrument row is blank until it is resolved and `nest()` omits empty values, so a row that
    was never looked up reaches the models as four missing fields and is refused — correctly — as
    `lot_size — Field required`. That describes the document instead of the act, and the act it
    appears to ask for is the one thing ADR 0025 exists to prevent: those inputs are `readonly`
    precisely because a hand-typed `min_notional` sizes the risk layer against a floor the venue
    never set. An operator reading it has no way forward that is not the wrong one.

    Presentation only. The same document is refused by the same validator; this relocates the
    refusal onto the row's identifier, which is the one field on it a human fills in. A row that
    *was* resolved is untouched, so a rule the venue disagrees with still reads as itself
    (`store_basket` is what checks that, and it is unaffected either way).

    `VERIFIED_FIELDS` is shared with the verifier rather than restated here, so the form's notion
    of "the venue answers for this" cannot drift from the set `store_basket` re-resolves.
    """
    rows = draft.get("instruments")
    unresolved = tuple(
        index
        for index, row in enumerate(rows if isinstance(rows, list) else ())
        # *None* of them present, not "some missing": a row carrying any venue-owned value has
        # been filled in somehow, and then the models' own messages are the right answer —
        # including "the venue publishes 0.00100000", which is the refusal ADR 0025 turns on.
        if isinstance(row, dict) and not any(field in row for field in VERIFIED_FIELDS)
    )
    owned = {f"instruments[{index}].{field}" for index in unresolved for field in VERIFIED_FIELDS}
    return tuple(
        FieldError(field=f"instruments[{index}].symbol", message=LOOKUP_REFUSAL)
        for index in unresolved
    ) + tuple(error for error in errors if error.field not in owned)


def blank_basket_draft(venue_id: str = "sim") -> dict[str, Any]:
    """A new basket, with the models' own defaults filled in and one row of each repeatable part.

    Built from the models rather than from a literal, so a default changed in `core/config.py`
    is the default the form offers — a form with its own copy of the numbers is a second source
    of truth for a risk limit. `venue_id` defaults to `"sim"` only so this stays free of a running
    application; every real caller passes the wired catalogue's own id, because a new row must
    never silently prefill a venue this process cannot verify it against.
    """
    panel = PanelConfig(
        panel_id="",
        providers=(ProviderSettings(provider_id="stub", kind=ProviderKind.STUB),),
        seats=(
            SeatConfig(seat_id="seat-1", role="Analyst", provider_id="stub", model="stub-analyst"),
        ),
    )
    return {
        "basket_id": "",
        "name": "",
        "instruments": [{"venue": venue_id, "asset_class": AssetClass.CRYPTO.value}],
        "panel": draft_of(panel),
        "risk_policy": draft_of(RiskPolicy()),
        "schedule": draft_of(Schedule()),
        "decision_mode": DecisionMode.PER_ASSET.value,
        "status": BasketStatus.ACTIVE.value,
        "timeframes": [],
        "indicators": [],
        "news_sources": [],
    }


# ---------------------------------------------------------------------- form plumbing


def _apply_row_action(draft: dict[str, Any], form: FormData) -> dict[str, str]:
    """Apply the add/remove button the operator pressed, and say which tab that lands on.

    At most one per submission. The focus overrides whatever tab was posted: adding an instrument
    must show the operator the instrument, not the tab they happened to press the button from.
    """
    added = _field(form, "add")
    if added:
        add_row(draft, added)
        return focus_for(added) | _selected_seat(draft, added)
    removed = _field(form, "remove")
    if removed:
        remove_row(draft, removed)
        return focus_for(removed)
    return {}


def _selected_seat(draft: dict[str, Any], path: str) -> dict[str, str]:
    """A seat just added is the seat the detail pane should be showing."""
    prefix, _, tail = path.partition(".")
    if tail != "seats" or prefix not in PANEL_PATHS:
        return {}
    seats = (draft.get(prefix) or {}).get("seats") or []
    return {f"seat.{prefix}": str(len(seats) - 1)} if seats else {}


async def _apply_lookup(
    request: Request, draft: dict[str, Any], form: FormData
) -> tuple[FieldError, ...]:
    """Resolve one row's identifier against the venue and fill the rest of it in.

    The button is convenience; `control/reference.py` is the guarantee. What it buys is that the
    operator sees the venue's own numbers *before* publishing rather than a refusal afterwards —
    and that `venue`, `asset_class` and both currencies come from the catalogue that answered
    rather than from whoever typed the identifier (ADR 0025).

    Failure semantics: a refusal is located on the row's symbol field in the venue's own words, and
    nothing else in the draft is touched. An unreachable venue reads as itself, not as "not listed".
    """
    index = _field(form, "lookup")
    rows = draft.get("instruments")
    if not index.isdigit() or not isinstance(rows, list) or int(index) >= len(rows):
        return ()
    slot = int(index)
    path = f"instruments[{slot}].symbol"
    symbol = str((rows[slot] or {}).get("symbol", "")).strip()
    try:
        instrument = await instrument_of(state_of(request).application.catalogue, symbol)
    except ConfigError as exc:
        return (FieldError(field=path, message=str(exc)),)
    except TradebotError as exc:
        return (FieldError(field=path, message=f"the venue could not be reached: {exc}"),)
    rows[slot] = draft_of(instrument)
    return ()


def _basket_record(request: Request, basket_id: str) -> Any:
    record = state_of(request).application.configs.latest(ConfigKind.BASKET, basket_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no basket {basket_id}")
    return record


def _refusal(exc: ConfigError) -> tuple[FieldError, ...]:
    """A store-level refusal — a secret in the document — shown where the operator is looking."""
    return (FieldError(field="", message=str(exc)),)


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value.strip() if isinstance(value, str) else ""


def _note(form: FormData, default: str) -> str:
    return _field(form, "note") or default


def _confirmation(form: FormData) -> str:
    return _field(form, "confirm")


def _version_field(form: FormData) -> int | None:
    raw = _field(form, "existing")
    return int(raw) if raw.isdigit() else None
