"""Configure, driven over HTTP: what publishes, what refuses, and what it writes to the log.

Every assertion here is about one of three things — that the engine's own validators are what
guard the write path, that a loosening of Tier-2 cannot happen without the typed phrase, and
that nothing is published without a `CONFIG_CHANGED` event naming the dashboard as its actor.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import httpx
import pytest
from tests.conftest import DASHBOARD_TOKEN

from tradebot.app import Application
from tradebot.core.enums import AssetClass, ConfigKind
from tradebot.core.errors import VenueError
from tradebot.core.events import EventType
from tradebot.dashboard.forms import draft_of
from tradebot.dashboard.routes.configure import (
    LOOSEN_PHRASE,
    blank_basket_draft,
    fold_prices,
    unfold_prices,
)
from tradebot.dashboard.views import PACKAGE
from tradebot.interfaces.exchange import IdType, VenueMarket


def as_form(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Pairs → httpx's form encoding. A repeated key becomes a list, which is how a multi-select
    and the `[]` sentinel reach the server the way a browser sends them."""
    grouped: dict[str, list[str]] = {}
    for key, value in pairs:
        grouped.setdefault(key, []).append(value)
    return grouped


def flat(draft: dict[str, Any], prefix: str = "doc") -> list[tuple[str, str]]:
    """A draft dict as the flat form fields a browser would submit."""
    pairs: list[tuple[str, str]] = []
    for key, value in draft.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            pairs.extend(flat(value, path))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    pairs.extend(flat(item, f"{path}[{index}]"))
                else:
                    pairs.append((f"{path}[]", str(item)))
            if not value:
                pairs.append((f"{path}[]", ""))
        elif value is not None:
            pairs.append((path, str(value)))
    return pairs


def _typed_basket_draft() -> dict[str, Any]:
    """The blank new-basket form with everything an operator may actually type filled in.

    `SOL/USDT` rather than `BTC/USDT`: the seeded demo basket holds BTC and ETH, and an instrument
    belongs to exactly one basket in service (ADR 0026).
    """
    draft = blank_basket_draft()
    draft["basket_id"] = "alpha"
    draft["name"] = "Alpha"
    draft["panel"]["panel_id"] = "alpha-panel"
    draft["timeframes"] = ["1h"]
    draft["instruments"][0]["symbol"] = "SOL/USDT"
    return draft


def unresolved_basket_form() -> list[tuple[str, str]]:
    """That form as a browser posts it, with the row still unresolved — no Look up was pressed.

    The state every new basket starts in: the venue-owned fields are `readonly` and blank, so they
    reach the server omitted (ADR 0025, and `forms.nest`).
    """
    return flat(_typed_basket_draft())


def new_basket_form(*, lot_size: str) -> list[tuple[str, str]]:
    """The same form with the trading rules *typed*, which is what ADR 0025 exists to catch."""
    draft = _typed_basket_draft()
    draft["instruments"][0].update(
        base_currency="SOL", quote_currency="USDT", lot_size=lot_size, tick_size="0.01"
    )
    return flat(draft)


@pytest.fixture
def basket_form(sim_application: Application) -> list[tuple[str, str]]:
    """The seeded demo basket, as its own edit form."""
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return flat(unfold_prices(draft_of(record.document)))


def config_events(application: Application) -> list[Any]:
    return [e for e in application.store.read_all() if e.type is EventType.CONFIG_CHANGED]


# ---------------------------------------------------------------- pages render


@pytest.mark.parametrize(
    "path", ["/configure", "/configure/baskets/new", "/configure/baskets/demo", "/configure/risk"]
)
async def test_configure_pages_render(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 200


async def test_editing_an_unknown_basket_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/configure/baskets/ghost")).status_code == 404


async def test_history_of_an_unknown_document_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/configure/history/basket/ghost")).status_code == 404
    assert (await client.get("/configure/history/nosuchkind/demo")).status_code == 404


async def test_history_lists_every_version(client: httpx.AsyncClient) -> None:
    body = (await client.get("/configure/history/basket/demo")).text
    assert "composition_root" in body
    assert "published at startup" in body


# ---------------------------------------------------------------- baskets


async def test_editing_a_limit_publishes_a_new_version_and_an_event(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The whole write path: `configs.put`, a new version, and the audit record of who did it."""
    before = len(config_events(sim_application))
    edited = _replace(basket_form, "doc.risk_policy.min_conviction", "0.8")

    response = await client.post(
        "/configure/baskets/demo", data=as_form([*edited, ("note", "raised the conviction floor")])
    )

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.ref.version == 2
    assert record.document.risk_policy.min_conviction == Decimal("0.8")
    assert record.actor == "dashboard"
    assert record.note == "raised the conviction floor"
    assert len(config_events(sim_application)) == before + 1


async def test_an_invalid_limit_is_refused_with_the_models_own_message(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    broken = _replace(basket_form, "doc.risk_policy.min_conviction", "5")

    response = await client.post("/configure/baskets/demo", data=as_form(broken))

    assert response.status_code == 200
    assert "0–1 scale" in response.text
    assert sim_application.configs.latest(ConfigKind.BASKET, "demo").ref.version == 1  # type: ignore[union-attr]


async def test_a_number_that_is_not_a_number_is_refused_and_not_a_crash(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The new-basket form is where an operator types raw decimals: `lot_size` has no default.

    A decimal comma is the likeliest typo there, and `MoneyError` is not a `ValueError` — so it
    used to leave the form handler unconverted and reach the operator as a 500 that named no
    field and lost the whole draft.
    """
    typed = new_basket_form(lot_size="0,001")
    typed = _replace(typed, "doc.name", "Alpha, still typed")

    response = await client.post("/configure/baskets/alpha", data=as_form(typed))

    assert response.status_code == 200
    assert "instruments[0].lot_size" in response.text
    assert "not a valid decimal amount" in response.text
    assert "Alpha, still typed" in response.text
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo"}


async def test_a_readable_number_the_venue_disagrees_with_is_still_refused(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The number parses; it is simply not the venue's. That used to publish (ADR 0025).

    `sim` publishes `SOL/USDT` at a lot size of 0.00100000, recorded from a real `exchangeInfo`.
    A basket pinning 0.01 would quantize every order against a floor the venue never set.
    """
    response = await client.post(
        "/configure/baskets/alpha", data=as_form(new_basket_form(lot_size="0.01"))
    )

    assert response.status_code == 200
    assert "the venue publishes 0.00100000" in response.text
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo"}


async def test_the_same_basket_publishes_with_the_venues_own_rules(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """What the Look up button will fill in for the operator — the venue's published answer."""
    market = await sim_application.catalogue.resolve("SOL/USDT")
    typed = new_basket_form(lot_size=str(market.lot_size))
    for field in ("tick_size", "min_qty", "min_notional"):
        typed = _replace(typed, f"doc.instruments[0].{field}", str(getattr(market, field)))

    response = await client.post("/configure/baskets/alpha", data=as_form(typed))

    assert response.status_code == 303
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo", "alpha"}


async def test_a_tier2_limit_that_is_not_a_number_is_refused_and_not_a_crash(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The same hole on the Tier-2 form, where a 500 would also hide whether anything published."""
    policy = sim_application.configs.global_risk()
    assert policy is not None
    broken = _replace(flat(draft_of(policy.document)), "doc.max_drawdown_pct", "0,5")

    response = await client.post("/configure/risk", data=as_form(broken))

    assert response.status_code == 200
    assert "not a valid decimal amount" in response.text
    assert sim_application.configs.global_risk().document.max_drawdown_pct == Decimal(10)  # type: ignore[union-attr]


async def test_a_cross_field_rule_on_a_nested_model_is_still_shown(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Located on the parent field, so it matches no input: the summary is what catches it."""
    broken = _replace(basket_form, "doc.risk_policy.take_profit_atr_multiple", "1")

    body = (await client.post("/configure/baskets/demo", data=as_form(broken))).text

    assert "This was not published" in body
    assert "must exceed stop_loss_atr_multiple" in body


async def test_a_refused_edit_keeps_what_the_operator_typed(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Re-rendering from the draft, not from the store: a validation error must not lose work."""
    broken = _replace(basket_form, "doc.risk_policy.min_conviction", "5")
    broken = _replace(broken, "doc.name", "renamed while editing")

    body = (await client.post("/configure/baskets/demo", data=as_form(broken))).text

    assert "renamed while editing" in body


async def test_a_pasted_api_key_is_refused_at_publish_time(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Configuration references secrets by env-var name; a value never reaches the database."""
    leaked = _replace(
        basket_form, "doc.panel.providers[0].secret_ref", "sk-abcdefghijklmnopqrstuvwxyz012345"
    )

    response = await client.post("/configure/baskets/demo", data=as_form(leaked))

    assert response.status_code == 200
    assert "looks like a secret" in response.text
    assert sim_application.configs.latest(ConfigKind.BASKET, "demo").ref.version == 1  # type: ignore[union-attr]


async def test_creating_a_basket_from_the_new_form(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """A second basket needs its own instruments (ADR 0026), so it is built from the blank form."""
    market = await sim_application.catalogue.resolve("SOL/USDT")
    typed = new_basket_form(lot_size=str(market.lot_size))
    for field in ("tick_size", "min_qty", "min_notional"):
        typed = _replace(typed, f"doc.instruments[0].{field}", str(getattr(market, field)))

    response = await client.post("/configure/baskets/alpha", data=as_form(typed))

    assert response.status_code == 303
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo", "alpha"}


async def test_a_second_basket_may_not_take_an_instrument_demo_already_holds(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Two baskets over one instrument oversell the portfolio holding through reduce-only."""
    cloned = _replace(basket_form, "doc.basket_id", "alpha")

    response = await client.post("/configure/baskets/alpha", data=as_form(cloned))

    assert response.status_code == 200
    assert "already held by basket &#39;demo&#39;" in response.text
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo"}


async def test_retiring_a_basket_removes_it_from_service_but_not_from_history(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    response = await client.post(
        "/configure/baskets/demo/retire", data={"note": "finished with it"}
    )

    assert response.status_code == 303
    assert sim_application.configs.baskets() == ()
    versions = sim_application.configs.history(ConfigKind.BASKET, "demo")
    assert [v.retired for v in versions] == [False, True]
    assert versions[-1].note == "finished with it"


# ---------------------------------------------------------------- the tab shell

DOC_FIELD = re.compile(r'name="doc\.([^"]+)"')

#: Row-managed lists: the operator adds/removes whole rows through `f.row_buttons`, never types a
#: scalar list directly. `flat()` synthesises an empty-list sentinel path (`path[]`) for *any*
#: empty list, scalar or not, so `document_paths()` sees the same shape for both kinds. The
#: distinction matters because only one of them is load-bearing:
#:
#: * A checkbox group (`f.checkboxes` — `timeframes`, `indicators`, `news_sources`, a seat's
#:   `evidence`) needs its sentinel: `SeatConfig.evidence` defaults to a *non-empty* tuple, so an
#:   operator clearing every box must still submit something, or the absent field would silently
#:   revert to the default instead of staying cleared.
#: * A row list (`instruments`, a panel's `providers`/`seats`, a provider's `price_rows`, a seat's
#:   `fallbacks`) has no default to silently revert to — absence and empty are *the same document*.
#:   Verified by round-tripping both shapes through `nest()` -> `fold_prices()` ->
#:   `PanelConfig.model_validate()` and comparing the resulting models: `fold_prices` skips an
#:   absent `price_rows` entirely, `ProviderSettings.prices` defaults to `PriceList()`, and
#:   `SeatConfig.fallbacks` defaults to `()`. Demanding a literal submitted field here would fail a
#:   perfectly round-trippable basket the moment a provider has no priced models or a seat has no
#:   fallback chain configured — the common case, not an edge case.
#:
#: So `document_paths()` drops a bare path ending in one of these names — it is never a real field,
#: only ever `flat()`'s sentinel for "this row list happens to be empty right now".
ROW_MANAGED_LISTS = {"instruments", "providers", "seats", "price_rows", "fallbacks"}


def submitted_paths(body: str) -> set[str]:
    """Every document path the rendered page will post, with indices stripped."""
    return {re.sub(r"\[\d*\]", "", name) for name in DOC_FIELD.findall(body)}


def document_paths(draft: dict[str, Any]) -> set[str]:
    """Every document path the stored basket carries, with indices and the `doc.` prefix stripped.

    `flat` emits the browser's own field names (`doc.risk_policy.min_conviction`), which is what
    makes this comparable to what the page renders. Excludes `ROW_MANAGED_LISTS`' bare container
    paths — see the comment there for why those are never a field the page needs to submit.
    """
    paths = (re.sub(r"\[\d*\]", "", name).removeprefix("doc.") for name, _ in flat(draft))
    return {path for path in paths if path.rsplit(".", 1)[-1] not in ROW_MANAGED_LISTS}


#: Fields the page deliberately does not submit. Quarantine is an operational act and lives on the
#: workspace, which has the held-position guard this form does not (ADR 0022). `publish_basket`
#: re-attaches it from the stored record, so this omission cannot release anything. The assertion
#: below is two-sided, so this set is the *only* licence to omit anything.
OMITTED_FROM_THE_FORM = {"risk_policy.quarantined", "risk_policy.quarantined_instruments"}


async def test_every_document_field_is_still_submitted(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """A tab may hide inputs; it may never omit them.

    The form round-trips the whole document and `nest()` drops absent fields, so a tab that
    conditionally renders its contents deletes that part of the basket on save. This is the
    concrete form of that rule, and it is two-sided on purpose: the first assertion catches a
    dropped field, the second catches one quietly added to the licence.
    """
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    expected = document_paths(unfold_prices(draft_of(record.document)))

    body = (await client.get("/configure/baskets/demo")).text
    submitted = submitted_paths(body)

    assert expected - submitted == OMITTED_FROM_THE_FORM
    assert expected - OMITTED_FROM_THE_FORM <= submitted


def tab_checked(body: str, tab_id: str) -> bool:
    """Whether the tab radio `tab_id` renders `checked`, regardless of the tag's own whitespace.

    The radio's `checked` attribute sits on a second, indented line in the template
    (`configure/basket.html`), and this project's Jinja environment has neither `trim_blocks` nor
    `lstrip_blocks` set — so the rendered tag is not one contiguous line. A literal substring match
    would test the raw byte layout of the tag rather than whether the right radio is checked, and
    would break on a re-indent or an added attribute with no behaviour change at all.
    """
    match = re.search(rf'<input[^>]*\bid="{re.escape(tab_id)}"[^>]*>', body)
    assert match is not None, f"no <input id={tab_id!r}> in the rendered page"
    return "checked" in match.group(0)


async def test_the_tab_the_operator_was_on_survives_a_row_action(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    body = (
        await client.post(
            "/configure/baskets/demo/draft",
            data=as_form([*basket_form, ("ui.section", "risk"), ("add", "instruments")]),
        )
    ).text

    # The action wins over the posted tab: adding an instrument shows the operator the instrument.
    assert tab_checked(body, "s-instruments")
    assert not tab_checked(body, "s-risk")


async def test_a_posted_tab_is_kept_when_no_row_action_happened(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    body = (
        await client.post(
            "/configure/baskets/demo/draft", data=as_form([*basket_form, ("ui.section", "risk")])
        )
    ).text

    assert tab_checked(body, "s-risk")
    assert not tab_checked(body, "s-identity")


# ---------------------------------------------------------------- draft editing


async def test_adding_a_row_re_renders_and_publishes_nothing(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    response = await client.post(
        "/configure/baskets/demo/draft", data=as_form([*basket_form, ("add", "panel.seats")])
    )

    assert response.status_code == 200
    assert "new seat" in response.text
    assert sim_application.configs.latest(ConfigKind.BASKET, "demo").ref.version == 1  # type: ignore[union-attr]


async def test_removing_a_row_re_renders_without_it(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    body = (
        await client.post(
            "/configure/baskets/demo/draft",
            data=as_form([*basket_form, ("remove_row", "instruments[1]")]),
        )
    ).text

    assert "BTC/USDT" in body
    assert "ETH/USDT" not in body


async def test_a_provider_added_to_the_draft_appears_in_every_seat_picker(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The chain stays a picker over declared providers, never free text (DESIGN §6.10)."""
    added = [*basket_form, ("doc.panel.providers[1].provider_id", "local"), ("add", "panel.seats")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(added))).text

    assert body.count('<option value="local"') >= 2  # the primary picker and the new seat's


async def test_two_seats_on_one_binding_are_flagged_in_the_seat_list(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Heterogeneity is a design control; losing it should be visible while it is configured."""
    # The seeded demo panel's one seat (`STUB_PANEL`) is bound to stub/stub-technical, so the
    # added seat repeats that exact binding to be a true duplicate rather than a merely similar one.
    doubled = [
        *basket_form,
        ("doc.panel.seats[1].seat_id", "twin"),
        ("doc.panel.seats[1].role", "Analyst"),
        ("doc.panel.seats[1].provider_id", "stub"),
        ("doc.panel.seats[1].model", "stub-technical"),
    ]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(doubled))).text

    assert "homogeneous" in body


async def test_a_provider_row_says_how_many_seats_use_it(client: httpx.AsyncClient) -> None:
    body = (await client.get("/configure/baskets/demo")).text
    assert "used by 1 seat" in body


def button(body: str, name: str) -> str:
    """The first `<button name="…">` tag, whitespace and attribute order irrelevant.

    Asserted against the tag rather than the whole page on purpose: the **Look up** button already
    carries these `hx-*` attributes, so a page-wide substring match would pass while `row_buttons`
    still reloaded the page — a test that cannot fail for the reason it exists.
    """
    match = re.search(rf'<button[^>]*\bname="{re.escape(name)}"[^>]*>', body)
    assert match is not None, f'no <button name="{name}"> in the rendered page'
    return match.group(0)


def lookup_cell(body: str) -> str:
    """The grid cell holding the Look up button, from its own `<div>` to that div's close."""
    at = body.index('name="lookup"')
    opened = body.rindex('<div class="field">', 0, at)
    return body[opened : body.index("</div>", at)]


async def test_look_up_shares_the_identifier_input_s_line(client: httpx.AsyncClient) -> None:
    """`.grid.tight` aligns its cells to the *end*, so what lines the button up with the input is
    whatever sits below the button — not above it.

    The identifier carries a hint beneath its input. A spacer placed above the button therefore
    pushed nothing anywhere, and end-alignment put the button level with that hint: one line below
    the field it acts on, reading as an unrelated control rather than the second half of "name an
    identifier and press Look up".
    """
    cell = lookup_cell((await client.get("/configure/baskets/demo")).text)

    assert 'class="muted small"' in cell, "the button has nothing standing in for the hint"
    assert cell.index('name="lookup"') < cell.index('class="muted small"'), (
        "the spacer is above the button; end-aligned cells need it below"
    )


async def test_row_buttons_swap_the_form_rather_than_reload_the_page(
    client: httpx.AsyncClient,
) -> None:
    """htmx does not scroll on a swap, so an add or remove keeps the operator where they were.

    `formaction` stays beside it: with scripting off the button performs the full POST it always
    did, so this is progressive enhancement and the no-JS path is unchanged. The server returns the
    whole page either way and `hx-select` picks the form out of it — no second rendering path.
    """
    body = (await client.get("/configure/baskets/demo")).text

    for name in ("add", "remove_row"):
        tag = button(body, name)
        assert 'hx-post="/configure/baskets/demo/draft"' in tag
        assert 'hx-target="#basket-form"' in tag
        assert 'hx-select="#basket-form"' in tag
        assert 'formaction="/configure/baskets/demo/draft"' in tag


#: Members of `HTMLFormElement`, `Element` and `Node` that a control's `name` may not shadow.
#:
#: HTML exposes every named control as a property of its own form, so `<button name="remove">` makes
#: `form.remove` a `RadioNodeList` rather than `Element.prototype.remove`. htmx's `outerHTML` swap
#: ends in `target.remove()`: it inserts the new form, throws `TypeError: t.remove is not a
#: function`, and never removes the old one — leaving the operator two stacked copies of the basket
#: form, the second still holding the pre-edit draft.
DOM_MEMBERS_A_CONTROL_MAY_NOT_SHADOW = frozenset(
    {
        # Node/Element mutation — what a swap calls on the element it is replacing.
        "after",
        "append",
        "appendChild",
        "before",
        "cloneNode",
        "insertAdjacentElement",
        "insertAdjacentHTML",
        "insertBefore",
        "prepend",
        "remove",
        "removeChild",
        "replaceChild",
        "replaceChildren",
        "replaceWith",
        # Traversal and matching.
        "children",
        "closest",
        "contains",
        "matches",
        "parentNode",
        "querySelector",
        "querySelectorAll",
        # Identity and attributes a script reads to find or restyle the form.
        "attributes",
        "classList",
        "className",
        "dataset",
        "id",
        "innerHTML",
        "outerHTML",
        "style",
        "tagName",
        "textContent",
        # Events.
        "addEventListener",
        "dispatchEvent",
        "removeEventListener",
        # HTMLFormElement's own API. Shadowing `submit` would stop the form submitting at all.
        "acceptCharset",
        "action",
        "checkValidity",
        "elements",
        "encoding",
        "enctype",
        "length",
        "method",
        "name",
        "noValidate",
        "reportValidity",
        "requestSubmit",
        "reset",
        "submit",
        "target",
    }
)


async def test_no_control_shadows_a_dom_member_of_its_own_form(client: httpx.AsyncClient) -> None:
    """A control's `name` is also a property of its form, and the page swaps that form by script.

    This is not a style rule. `<button name="remove">` made `form.remove` the button, so htmx's
    `outerHTML` swap inserted the re-rendered form and then threw instead of removing the old one:
    every `add`, `remove` and `Look up` left a second copy of the whole basket form stacked below
    the first, each with its own Publish button and its own draft. An operator publishing from the
    lower one publishes the edit they just made *without* it.

    Asserted over the rendered markup rather than the macro, because the hazard is the pair —
    a name and the form it lands in — and only the rendered page has both.
    """
    body = (await client.get("/configure/baskets/demo")).text
    names = {match.group(1) for match in re.finditer(r'\sname="([^"]+)"', form_markup(body))}

    assert names, "no named controls found; this test no longer guards anything"
    shadowing = sorted(names & DOM_MEMBERS_A_CONTROL_MAY_NOT_SHADOW)
    assert not shadowing, f"these control names shadow a DOM member of their form: {shadowing}"


def form_markup(body: str) -> str:
    """Just the basket form's own markup.

    Scoped deliberately: the retire form below it is a *separate* `<form>` with its own `note`
    field, which is correct — only fields inside `#basket-form` are submitted by Publish.
    """
    match = re.search(r'id="basket-form".*?</form>', body, re.DOTALL)
    assert match is not None, "no #basket-form in the rendered page"
    return match.group(0)


async def test_the_form_carries_exactly_one_note_field(client: httpx.AsyncClient) -> None:
    """Publish moved into the sticky bar, and the note moved with it.

    Two inputs named `note` inside one form are both submitted and `_note` reads the first, so
    leaving the old one behind loses the operator's reason for the change without saying so.
    """
    body = form_markup((await client.get("/configure/baskets/demo")).text)
    assert len(re.findall(r'<input[^>]*\bname="note"', body)) == 1


# ---------------------------------------------------------------- tier 2


async def test_tightening_tier2_publishes_without_a_confirmation(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    policy = sim_application.configs.global_risk()
    assert policy is not None
    tighter = _replace(flat(draft_of(policy.document)), "doc.max_drawdown_pct", "5")

    response = await client.post("/configure/risk", data=as_form(tighter))

    assert response.status_code == 303
    assert sim_application.configs.global_risk().document.max_drawdown_pct == Decimal(5)  # type: ignore[union-attr]


async def test_loosening_tier2_demands_the_typed_phrase(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    policy = sim_application.configs.global_risk()
    assert policy is not None
    looser = _replace(flat(draft_of(policy.document)), "doc.max_drawdown_pct", "40")

    response = await client.post("/configure/risk", data=as_form(looser))

    assert response.status_code == 200
    assert "loosens a global limit" in response.text
    assert "max_drawdown_pct: 10 → 40" in response.text
    assert sim_application.configs.global_risk().document.max_drawdown_pct == Decimal(10)  # type: ignore[union-attr]


async def test_loosening_tier2_publishes_with_the_phrase(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    policy = sim_application.configs.global_risk()
    assert policy is not None
    looser = _replace(flat(draft_of(policy.document)), "doc.max_drawdown_pct", "40")

    response = await client.post(
        "/configure/risk", data=as_form([*looser, ("confirm", LOOSEN_PHRASE)])
    )

    assert response.status_code == 303
    assert sim_application.configs.global_risk().document.max_drawdown_pct == Decimal(40)  # type: ignore[union-attr]


async def test_the_wrong_phrase_does_not_publish(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    policy = sim_application.configs.global_risk()
    assert policy is not None
    looser = _replace(flat(draft_of(policy.document)), "doc.max_drawdown_pct", "40")

    response = await client.post(
        "/configure/risk", data=as_form([*looser, ("confirm", "yes go on")])
    )

    assert response.status_code == 200
    assert sim_application.configs.global_risk().document.max_drawdown_pct == Decimal(10)  # type: ignore[union-attr]


async def test_an_invalid_tier2_limit_is_refused(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    policy = sim_application.configs.global_risk()
    assert policy is not None
    broken = _replace(flat(draft_of(policy.document)), "doc.max_drawdown_pct", "-1")

    response = await client.post("/configure/risk", data=as_form(broken))

    assert response.status_code == 200
    assert sim_application.configs.global_risk().ref.version == 1  # type: ignore[union-attr]


# ---------------------------------------------------------------- price rows


def test_price_rows_fold_into_the_mapping_the_model_expects() -> None:
    draft = {
        "panel": {
            "providers": [
                {
                    "provider_id": "openrouter",
                    "price_rows": [
                        {"model": "qwen/qwen3:free", "prompt_per_million": "0"},
                        {"model": "gpt-4.1", "completion_per_million": "8"},
                        {"model": "   "},  # a blank row the operator added and left
                    ],
                }
            ]
        }
    }

    folded = fold_prices(draft)

    prices = folded["panel"]["providers"][0]["prices"]["models"]
    assert set(prices) == {"qwen/qwen3:free", "gpt-4.1"}
    assert "price_rows" not in folded["panel"]["providers"][0]


def test_price_rows_unfold_back_for_the_form() -> None:
    """Model ids contain `.`, `/` and `:` — characters the field-path parser splits on."""
    draft = {
        "panel": {"providers": [{"prices": {"models": {"gpt-4.1": {"prompt_per_million": "3"}}}}]}
    }

    rows = unfold_prices(draft)["panel"]["providers"][0]["price_rows"]

    assert rows == [{"model": "gpt-4.1", "prompt_per_million": "3"}]


def test_unfolding_leaves_an_already_unfolded_draft_alone() -> None:
    draft = {"panel": {"providers": [{"price_rows": [{"model": "kept"}]}]}}
    assert unfold_prices(draft)["panel"]["providers"][0]["price_rows"] == [{"model": "kept"}]


@pytest.mark.parametrize("draft", [{}, {"panel": {}}, {"panel": {"providers": "not a list"}}])
def test_price_folding_tolerates_a_half_built_draft(draft: dict[str, Any]) -> None:
    fold_prices(dict(draft))
    unfold_prices(dict(draft))


# ---------------------------------------------------------------- auth


async def test_publishing_needs_a_session(http: httpx.AsyncClient) -> None:
    """A config write is a state change, so it is refused rather than redirected."""
    assert (await http.post("/configure/risk", data={})).status_code == 401
    assert (await http.post("/login", data={"token": DASHBOARD_TOKEN})).status_code == 303


def _replace(pairs: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    """Set one field, adding it if the form did not carry it."""
    replaced = [(key, value if key == name else current) for key, current in pairs]
    return replaced if any(key == name for key, _ in pairs) else [*replaced, (name, value)]


# ---------------------------------------------------------------- the section rail


def test_the_rail_places_every_label_ahead_of_the_open_pane() -> None:
    """The section labels must not move when a different section is opened.

    Source order interleaves labels and panes, so a label after the open pane is laid out after it
    and lands below it — the rail's labels then shift with the selection. `order` is what decouples
    placement from source order, and the rail is unusable without it, so it is worth an assertion
    rather than a comment alone.

    Asserted on the stylesheet because the layout lives nowhere else: the markup is the same
    `radio, label, pane` triples either way, so no rendered page can show the difference.
    """
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    rail = css[css.index(".tabs.rail {") : css.index(".tabs.strip {")]

    assert "--rail-width:" in rail
    assert "var(--rail-width)" in rail
    assert "order: 0" in rail and "order: 1" in rail
    # `-1` resolves against the explicit grid, so the rows have to be declared for the pane to span
    # them; and the span has to end on a flexible track or the pane's height is distributed back
    # across the label rows.
    assert "min-content) 1fr" in rail
    assert "grid-row: 1 / -1" in rail


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) for every rule in the stylesheet, comments removed."""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", stripped)]


def test_the_seat_rail_is_wider_than_the_section_rail_it_overrides() -> None:
    """`--rail-width` is declared twice for one element, and only one of them can win.

    The seat rail is `<div class="tabs rail seat-master">`, so `.tabs.rail`'s narrow default and
    `.seat-master`'s wider one are a specificity contest rather than an override — and the base
    rule wins it on class count. The rail then renders narrower than the labels it carries, and
    `white-space: nowrap` walks `seat-1 stub - stub-analyst` out of its box and over the top of
    the pane's first field, which is what an operator sees.

    Asserted on specificity rather than on the literal selector, so the next widening — another
    `.tabs.rail` variant, a second master-detail rail — cannot reintroduce it by a new spelling.
    """
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    widths = {sel: body for sel, body in _rules(css) if "--rail-width:" in body}
    base = next(sel for sel in widths if sel == ".tabs.rail")
    override = next(sel for sel in widths if "seat-master" in sel)

    assert override.count(".") > base.count("."), f"{override} cannot override {base}"


def test_a_long_seat_label_is_clipped_rather_than_written_over_the_pane() -> None:
    """The rail's width is fixed on purpose — one that measured its widest label would move the
    pane every time a seat was renamed — so the label has to contain its own overflow. Widening it
    alone only buys a bigger box to spill out of: `white-space: nowrap` puts a long seat id, or the
    `homogeneous` pill, on top of the pane's first field again.
    """
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    seat = css[css.index(".seat-master {") : css.index(".checkgroup {")]

    # A grid item's automatic minimum size is its content, so the fixed track alone does not hold
    # it — the label has to be told it may be smaller than what it contains.
    assert "min-width: 0" in seat and "overflow: hidden" in seat
    assert "text-overflow: ellipsis" in seat
    # The pill warns that two seats resolve to the same model. It is the one part of the label that
    # must never be what gets clipped away.
    assert "flex-shrink: 0" in seat


def test_a_seat_id_gets_the_rail_to_itself() -> None:
    """Clipping alone is not legibility. On one line the binding and the `homogeneous` pill take
    most of the rail, and the id — the only part naming *which* seat the pane is showing — is what
    ellipsis eats first: `momentum-analyst-secondary` and `momentum-analyst-primary` both render as
    `momentum...`, and an operator edits the wrong seat.

    So the label wraps and the id claims a full line of its own. The rail's rows are `min-content`,
    so the second line costs height that was free anyway.
    """
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    seat = css[css.index(".seat-master {") : css.index(".checkgroup {")]

    assert "flex-wrap: wrap" in seat
    assert "flex-basis: 100%" in seat


def test_a_checkbox_is_never_stretched_to_the_width_of_its_field() -> None:
    """`width: 100%` is right for a text box and wrong for a checkbox: it stretches the control
    across the whole field and shoves its label to the far side of the pane.

    `f.flag` renders four of these — Devil's advocate, Supports JSON mode, Long only, Flatten on
    kill — and none of them sits in `.checkgroup`, which carried the only escape hatch. So the
    exclusion belongs on the declaration, where a fifth flag inherits it, rather than on each
    container that happens to hold one.
    """
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")
    stretched = [
        sel
        for sel, body in _rules(css)
        if re.search(r"\bwidth:\s*100%", body) and re.search(r"\binput\b", sel)
    ]

    assert stretched, "the full-width input rule has moved; this test no longer guards anything"
    for selector in stretched:
        assert 'not([type="checkbox"])' in selector, selector


# ---------------------------------------------------------------- checkbox groups


async def test_multi_selects_are_gone_from_the_basket_form(client: httpx.AsyncClient) -> None:
    """A `<select multiple>` deselects everything on a stray click. For `indicators` that means
    quietly publishing a basket that computes nothing."""
    body = (await client.get("/configure/baskets/demo")).text

    assert "<select multiple" not in body
    assert "multiple size=" not in body


async def test_a_checkbox_group_still_reaches_the_server_as_nothing_selected(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The hidden empty sentinel is how "nothing selected" is expressible at all: with every box
    unticked the browser sends no key, which would read as "leave it as it was"."""
    cleared = [(k, v) for k, v in basket_form if not k.startswith("doc.timeframes")]
    cleared.append(("doc.timeframes[]", ""))

    response = await client.post("/configure/baskets/demo", data=as_form(cleared))

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.document.timeframes == ()


def test_the_multi_macro_is_gone() -> None:
    """One caller left behind keeps a control whose failure mode is silent deselection."""
    source = (PACKAGE / "templates" / "_fields.html").read_text(encoding="utf-8")
    assert "macro multi(" not in source


# ---------------------------------------------------------------- quarantine carry-over


async def _quarantine(client: httpx.AsyncClient, key: str, *, excluded: bool = True) -> None:
    """Set or release a quarantine the way the workspace does — the only surface that may."""
    response = await client.post(
        "/control/baskets/demo/quarantine",
        data={"instrument_key": key, "excluded": "true" if excluded else "false"},
    )
    assert response.status_code in (200, 303), response.text


def _policy(application: Application) -> Any:
    record = application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return record.document.risk_policy


async def test_an_unrelated_edit_from_settings_leaves_an_instrument_quarantined(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The form no longer carries quarantine, and `nest()` omits absent fields — so without
    carry-over every publish from Settings would silently release every quarantine in force."""
    await _quarantine(client, "sim:BTC/USDT")

    edited = flat(unfold_prices(draft_of(_record(sim_application).document)))
    response = await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(edited, "doc.risk_policy.min_conviction", "0.7")),
    )

    assert response.status_code == 303
    assert _policy(sim_application).quarantined_instruments == ("sim:BTC/USDT",)
    assert _policy(sim_application).min_conviction == Decimal("0.7")


async def test_an_unrelated_edit_leaves_a_whole_basket_quarantine(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await _quarantine(client, "")

    edited = flat(unfold_prices(draft_of(_record(sim_application).document)))
    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(edited, "doc.risk_policy.min_conviction", "0.7")),
    )

    assert _policy(sim_application).quarantined is True


async def test_a_quarantine_set_after_the_form_was_opened_survives_the_publish(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Read at publish time, not carried in a hidden field. The form here is deliberately stale."""
    stale = list(basket_form)

    await _quarantine(client, "sim:ETH/USDT")
    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(stale, "doc.risk_policy.min_conviction", "0.65")),
    )

    assert _policy(sim_application).quarantined_instruments == ("sim:ETH/USDT",)


async def test_removing_a_quarantined_instrument_publishes_and_reports_the_dropped_key(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """`Basket._check_quarantine` would otherwise refuse the document over a key nobody typed."""
    await _quarantine(client, "sim:ETH/USDT")
    edited = flat(unfold_prices(draft_of(_record(sim_application).document)))
    edited = [(k, v) for k, v in edited if not k.startswith("doc.instruments[1]")]

    response = await client.post("/configure/baskets/demo", data=as_form(edited))

    assert response.status_code == 303
    assert "released=sim%3AETH%2FUSDT" in response.headers["location"]
    assert _policy(sim_application).quarantined_instruments == ()
    body = (await client.get(response.headers["location"])).text
    assert "sim:ETH/USDT" in body


async def test_a_renamed_basket_starts_with_no_quarantine(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """No prior version under the new id, so it is a different basket and inherits nothing."""
    await _quarantine(client, "sim:BTC/USDT")
    renamed = flat(unfold_prices(draft_of(_record(sim_application).document)))
    renamed = _replace(renamed, "doc.basket_id", "alpha")

    response = await client.post("/configure/baskets/alpha", data=as_form(renamed))

    # Refused by ADR 0026 — `alpha` would take demo's instruments — which is itself the assertion
    # that carry-over did not invent a quarantine on a basket that does not exist yet.
    assert response.status_code == 200
    assert "already held by basket" in response.text


async def test_settings_cannot_set_a_quarantine_even_if_the_fields_are_posted(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """Overwritten, never merged: the form is not a surface for this act in either direction."""
    forged = [
        *basket_form,
        ("doc.risk_policy.quarantined", "true"),
        ("doc.risk_policy.quarantined_instruments[]", "sim:BTC/USDT"),
    ]

    await client.post("/configure/baskets/demo", data=as_form(forged))

    assert _policy(sim_application).quarantined is False
    assert _policy(sim_application).quarantined_instruments == ()


def _record(application: Application) -> Any:
    record = application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return record


# ---------------------------------------------------------------- shadow panel


async def test_a_basket_without_a_challenger_publishes_unchanged(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The challenger section is always rendered, and a `<select>` always submits something.

    Without `drop_blank_shadow` that stray protocol would fail validation and no basket could be
    published at all.
    """
    response = await client.post(
        "/configure/baskets/demo",
        data=as_form([*basket_form, ("doc.shadow_panel.protocol", "single_round")]),
    )

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.document.shadow_panel is None


async def test_a_challenger_can_be_added_from_the_form(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    response = await client.post(
        "/configure/baskets/demo", data=as_form([*basket_form, *_shadow_fields()])
    )

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    shadow = record.document.shadow_panel
    assert shadow is not None
    assert shadow.panel_id == "challenger"
    assert [seat.seat_id for seat in shadow.seats] == ["contrarian"]


async def test_editing_a_basket_does_not_silently_drop_its_challenger(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The hazard the shared macro exists to remove: a form that renders one panel of two."""
    await client.post("/configure/baskets/demo", data=as_form([*basket_form, *_shadow_fields()]))

    page = await client.get("/configure/baskets/demo")
    assert "challenger" in page.text

    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    reposted = flat(unfold_prices(draft_of(record.document)))
    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(reposted, "doc.risk_policy.min_conviction", "0.75")),
    )

    latest = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert latest is not None
    assert latest.document.risk_policy.min_conviction == Decimal("0.75")
    assert latest.document.shadow_panel is not None
    assert latest.document.shadow_panel.panel_id == "challenger"


async def test_the_challenger_is_still_submitted_from_its_own_tab(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """A tab may hide inputs; it may never omit them — the hazard the shared macro removes."""
    await client.post("/configure/baskets/demo", data=as_form([*basket_form, *_shadow_fields()]))
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    reposted = flat(unfold_prices(draft_of(record.document)))

    await client.post(
        "/configure/baskets/demo",
        data=as_form(_replace(reposted, "doc.risk_policy.min_conviction", "0.75")),
    )

    latest = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert latest is not None
    assert latest.document.shadow_panel is not None


async def test_a_challenger_repeating_the_champions_id_is_refused(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    clashing = [
        (key, "stub" if key == "doc.shadow_panel.panel_id" else value)
        for key, value in _shadow_fields()
    ]

    response = await client.post("/configure/baskets/demo", data=as_form([*basket_form, *clashing]))

    assert response.status_code == 200
    assert "repeats the champion&#39;s id" in response.text


def _shadow_fields() -> list[tuple[str, str]]:
    """A minimal, valid challenger panel as the form would submit it."""
    return [
        ("doc.shadow_panel.panel_id", "challenger"),
        ("doc.shadow_panel.protocol", "single_round"),
        ("doc.shadow_panel.providers[0].provider_id", "stub"),
        ("doc.shadow_panel.providers[0].kind", "stub"),
        ("doc.shadow_panel.seats[0].seat_id", "contrarian"),
        ("doc.shadow_panel.seats[0].role", "Devil's advocate"),
        ("doc.shadow_panel.seats[0].provider_id", "stub"),
        ("doc.shadow_panel.seats[0].model", "stub-contrarian"),
    ]


# ---------------------------------------------------------------- look up


async def test_look_up_fills_the_row_from_the_venue(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """The operator names an identifier; the venue publishes the rest (ADR 0025)."""
    typed = [*basket_form, ("doc.instruments[0].symbol", "SOL/USDT"), ("lookup", "0")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(typed))).text

    assert "SOL/USDT" in body
    assert 'value="SOL"' in body  # base_currency, resolved rather than typed


async def test_look_up_refuses_a_symbol_the_venue_does_not_list(
    client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    typed = [*basket_form, ("doc.instruments[0].symbol", "FOO/BAR"), ("lookup", "0")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(typed))).text

    assert "does not list" in body
    assert "instruments[0].symbol" in body


# A delisted symbol is refused too, but the committed sim capture holds only tradable entries, so
# that path is asserted where it belongs — `tests/contract/test_catalogue_contract.py`, over every
# catalogue at once — rather than duplicated here against a fake.


async def test_look_up_names_an_unreachable_venue_as_itself(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    """An outage is not "the venue does not list it". Sending the operator to check their spelling
    when the real problem is the network is the wrong instruction at the worst moment."""

    class Unreachable:
        venue_id = "sim"
        asset_class = AssetClass.CRYPTO
        source = ""
        as_of = None

        async def list_markets(self) -> tuple[VenueMarket, ...]:
            raise VenueError("connection reset")

        async def resolve(self, identifier: str, id_type: IdType = IdType.SYMBOL) -> VenueMarket:
            return (await self.list_markets())[0]

    # `Application` is `@dataclass(slots=True)` and not frozen, so this is a plain assignment.
    sim_application.catalogue = Unreachable()  # type: ignore[assignment]
    typed = [*basket_form, ("doc.instruments[0].symbol", "SOL/USDT"), ("lookup", "0")]

    body = (await client.post("/configure/baskets/demo/draft", data=as_form(typed))).text

    assert "could not be reached" in body
    assert "does not list" not in body


async def test_look_up_publishes_nothing(
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    await client.post(
        "/configure/baskets/demo/draft",
        data=as_form([*basket_form, ("doc.instruments[0].symbol", "SOL/USDT"), ("lookup", "0")]),
    )

    assert sim_application.configs.latest(ConfigKind.BASKET, "demo").ref.version == 1  # type: ignore[union-attr]


#: The row's inputs on the rendered page. Narrow on purpose: nine known names, so this reads what
#: the page will *submit* rather than re-deriving it from the catalogue.
RESOLVED_ROW = re.compile(r'name="(doc\.instruments\[0\]\.[a-z_]+)"[^>]*value="([^"]*)"')

#: What a catalogue answers for, and therefore what Look up has to have filled in for a publish to
#: be possible at all. Spelled out rather than imported so a field quietly dropped from
#: `VERIFIED_FIELDS` fails here instead of agreeing with itself.
VENUE_OWNED = {
    "base_currency",
    "quote_currency",
    "lot_size",
    "tick_size",
    "min_qty",
    "min_notional",
}


async def test_publishing_a_row_that_was_never_looked_up_asks_for_the_lookup(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The refusal must name the act, not the fields — those inputs are readonly (ADR 0025).

    A new basket starts with a blank instrument row, so this is the first thing every operator
    creating one meets. It used to answer with four `Field required` messages naming `lot_size`
    and `tick_size`, which asks for the one thing nobody may type and which the page will not let
    them type anyway.
    """
    response = await client.post("/configure/baskets/alpha", data=as_form(unresolved_basket_form()))

    assert response.status_code == 200
    assert "press Look up" in response.text
    assert "<code>instruments[0].lot_size</code>" not in response.text
    assert "<code>instruments[0].base_currency</code>" not in response.text
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo"}


async def test_an_unrelated_error_on_a_resolved_row_still_reads_as_itself(
    client: httpx.AsyncClient,
) -> None:
    """The relocation is for unresolved rows only; a typed-and-wrong value must not be swallowed."""
    body = (
        await client.post(
            "/configure/baskets/alpha", data=as_form(new_basket_form(lot_size="0,001"))
        )
    ).text

    assert "not a valid decimal amount" in body
    assert "press Look up" not in body


async def test_looking_up_then_publishing_creates_the_basket(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The whole operator journey, which nothing covered end to end: name it, resolve it, publish.

    The row is rebuilt from what the *rendered* page carries rather than from the catalogue, so
    this fails if Look up resolves a field the form then does not submit. Both halves have to agree
    or a basket cannot be created from the GUI at all — the one thing `test_every_document_field_
    is_still_submitted` cannot say, because it starts from a basket that is already stored.
    """
    typed = unresolved_basket_form()
    body = (
        await client.post("/configure/baskets/alpha/draft", data=as_form([*typed, ("lookup", "0")]))
    ).text
    resolved = RESOLVED_ROW.findall(body)
    assert {name.rsplit(".", 1)[-1] for name, _ in resolved} >= VENUE_OWNED

    kept = [(key, value) for key, value in typed if not key.startswith("doc.instruments[0].")]
    response = await client.post("/configure/baskets/alpha", data=as_form([*kept, *resolved]))

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "alpha")
    assert record is not None
    assert record.document.instruments[0].lot_size == Decimal("0.001")  # sim's own SOL/USDT rule


async def test_the_instruments_pane_states_where_the_rules_came_from(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/configure/baskets/demo")).text
    assert "recorded from binance" in body


async def test_an_instrument_another_basket_holds_is_named_on_the_row(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """ADR 0026's refusal, shown where the instrument is picked rather than at publish."""
    market = await sim_application.catalogue.resolve("SOL/USDT")
    typed = new_basket_form(lot_size=str(market.lot_size))
    for field in ("tick_size", "min_qty", "min_notional"):
        typed = _replace(typed, f"doc.instruments[0].{field}", str(getattr(market, field)))
    await client.post("/configure/baskets/alpha", data=as_form(typed))

    body = (await client.get("/configure/baskets/alpha")).text
    body_demo = (await client.get("/configure/baskets/demo")).text

    assert "held by basket" not in body  # alpha holds SOL alone
    assert "held by basket" not in body_demo  # demo holds BTC and ETH alone
