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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from tradebot.control.config_store import SINGLETON_ID
from tradebot.control.reference import store_basket
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
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.dashboard.forms import FieldError, add_row, draft_of, nest, remove_row, validate
from tradebot.dashboard.views import ACTOR, render, state_of
from tradebot.decision.protocols import PROTOCOLS
from tradebot.indicators.library import REGISTRY
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

#: The optional A/B challenger (ADR 0018). Edited by the same macro as the champion, so a field
#: cannot exist on one panel's form and not the other's.
SHADOW_PATH = "shadow_panel"
PANEL_PATHS = ("panel", SHADOW_PATH)


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
    return _basket_form(request, blank_basket_draft(), existing=None)


@router.get("/baskets/{basket_id}", response_class=HTMLResponse)
async def edit_basket(request: Request, basket_id: str) -> HTMLResponse:
    record = _basket_record(request, basket_id)
    return _basket_form(request, draft_of(record.document), existing=record.ref.version)


@router.post("/baskets/{basket_id}/draft", response_class=HTMLResponse)
async def redraft_basket(request: Request, basket_id: str) -> HTMLResponse:
    """Add or remove a row and re-render, publishing nothing.

    This is what keeps the provider picker honest: a provider added in this form appears in
    every seat's fallback select the moment the draft is re-rendered, so the chain is still
    built from declared providers rather than from free text (DESIGN §6.10).
    """
    form = await request.form()
    draft = nest(form.multi_items())
    _apply_row_action(draft, form)
    return _basket_form(request, draft, existing=_version_field(form), basket_id=basket_id)


@router.post("/baskets/{basket_id}", response_class=HTMLResponse)
async def publish_basket(request: Request) -> Response:
    """Publish the submitted document. The id comes from the *form*, not from the path.

    Deliberate: the operator can rename a basket in the identity field, and taking the id from
    the URL would then publish version 2 of the old id carrying a document that names a new one.
    """
    form = await request.form()
    draft = fold_prices(drop_blank_shadow(nest(form.multi_items())))
    basket, errors = validate(Basket, draft)
    if basket is None:
        return _basket_form(request, draft, existing=_version_field(form), errors=errors)

    application = state_of(request).application
    try:
        record = await store_basket(
            application.configs,
            application.catalogue,
            basket,
            actor=ACTOR,
            note=_note(form, "edited in the dashboard"),
        )
    except ConfigError as exc:
        return _basket_form(request, draft, existing=_version_field(form), errors=_refusal(exc))
    logger.warning(
        "basket published from the dashboard",
        extra={"basket_id": record.ref.config_id, "version": record.ref.version},
    )
    return RedirectResponse(f"/configure/baskets/{basket.basket_id}", status_code=303)


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
) -> HTMLResponse:
    draft = unfold_prices(draft)
    return render(
        request,
        "configure/basket.html",
        draft=draft,
        errors=errors,
        existing=existing,
        basket_id=str(draft.get("basket_id") or basket_id),
        instrument_keys=_instrument_keys(draft),
        providers=_declared_providers(draft, "panel"),
        shadow_providers=_declared_providers(draft, SHADOW_PATH),
        timeframes=TIMEFRAMES,
        indicators=sorted(REGISTRY),
        news_sources=sorted(FEEDS),
        protocols=sorted(PROTOCOLS),
        evidence_slices=EVIDENCE_SLICES,
        asset_classes=[cls.value for cls in AssetClass],
        provider_kinds=[kind.value for kind in ProviderKind],
        decision_modes=[mode.value for mode in DecisionMode],
        statuses=[status.value for status in BasketStatus],
    )


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
    return [row for path in PANEL_PATHS for row in _panel_providers(draft, path)]


def _panel_providers(draft: dict[str, Any], path: str) -> list[dict[str, Any]]:
    panel = draft.get(path)
    providers = panel.get("providers") if isinstance(panel, dict) else None
    return [p for p in providers if isinstance(p, dict)] if isinstance(providers, list) else []


def _instrument_keys(draft: dict[str, Any]) -> tuple[str, ...]:
    """`venue:symbol` for each instrument row — the only scopes a quarantine may name.

    Read from the draft rather than from the stored document, so an instrument added in this same
    edit is immediately selectable, exactly as a provider added here appears in every seat's
    picker. `Basket` still refuses a key it does not hold, so this is a convenience, not the check.
    """
    rows = draft.get("instruments")
    return tuple(
        f"{str(row['venue']).strip()}:{str(row['symbol']).strip()}"
        for row in (rows if isinstance(rows, list) else ())
        if isinstance(row, dict) and str(row.get("venue", "")).strip()
        if str(row.get("symbol", "")).strip()
    )


def _declared_providers(draft: dict[str, Any], path: str) -> tuple[str, ...]:
    """Provider ids one panel declares — the only options its seats' pickers may offer."""
    return tuple(
        str(p["provider_id"])
        for p in _panel_providers(draft, path)
        if str(p.get("provider_id", "")).strip()
    )


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


def blank_basket_draft() -> dict[str, Any]:
    """A new basket, with the models' own defaults filled in and one row of each repeatable part.

    Built from the models rather than from a literal, so a default changed in `core/config.py`
    is the default the form offers — a form with its own copy of the numbers is a second source
    of truth for a risk limit.
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
        "instruments": [{"venue": "sim", "asset_class": AssetClass.CRYPTO.value}],
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


def _apply_row_action(draft: dict[str, Any], form: FormData) -> None:
    """Apply the add/remove button the operator pressed. At most one per submission."""
    added = _field(form, "add")
    if added:
        add_row(draft, added)
    removed = _field(form, "remove")
    if removed:
        remove_row(draft, removed)


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
