"""Configure, driven over HTTP: what publishes, what refuses, and what it writes to the log.

Every assertion here is about one of three things — that the engine's own validators are what
guard the write path, that a loosening of Tier-2 cannot happen without the typed phrase, and
that nothing is published without a `CONFIG_CHANGED` event naming the dashboard as its actor.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from tests.conftest import DASHBOARD_TOKEN

from tradebot.app import Application
from tradebot.core.enums import ConfigKind
from tradebot.core.events import EventType
from tradebot.dashboard.forms import draft_of
from tradebot.dashboard.routes.configure import (
    LOOSEN_PHRASE,
    blank_basket_draft,
    fold_prices,
    unfold_prices,
)


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


def new_basket_form(*, lot_size: str) -> list[tuple[str, str]]:
    """The blank new-basket form, filled in as an operator would — `lot_size` is theirs to type.

    Built from the route's own blank draft rather than from a literal, so it stays the form the
    operator is actually served.
    """
    draft = blank_basket_draft()
    draft["basket_id"] = "alpha"
    draft["name"] = "Alpha"
    draft["panel"]["panel_id"] = "alpha-panel"
    draft["timeframes"] = ["1h"]
    draft["instruments"][0].update(
        symbol="BTC/USDT",
        base_currency="BTC",
        quote_currency="USDT",
        lot_size=lot_size,
        tick_size="0.01",
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


async def test_the_same_new_basket_publishes_once_the_decimal_is_readable(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    response = await client.post(
        "/configure/baskets/alpha", data=as_form(new_basket_form(lot_size="0.001"))
    )

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
    sim_application: Application, client: httpx.AsyncClient, basket_form: list[tuple[str, str]]
) -> None:
    created = _replace(basket_form, "doc.basket_id", "alpha")
    created = _replace(created, "doc.name", "Alpha basket")

    response = await client.post("/configure/baskets/alpha", data=as_form(created))

    assert response.status_code == 303
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo", "alpha"}


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
            data=as_form([*basket_form, ("remove", "instruments[1]")]),
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
