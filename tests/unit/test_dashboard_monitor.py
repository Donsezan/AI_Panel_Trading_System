"""The Analytics pages, driven over real HTTP against a real wired sim application.

Rendered, not mocked: a template that references a column the projection does not have is a
runtime failure on a page an operator reads during an incident, and the only way to catch that
is to render it. The suite stays offline — `SimBroker`, the scripted panel, an in-memory
database, and an ASGI transport with no socket.

The workspace at `/` has its own suite (`test_dashboard_workspace.py`); what is left here is the
record of what happened, which the workspace links out to rather than duplicating.
"""

from __future__ import annotations

import httpx
import pytest

from tradebot.app import Application
from tradebot.core.enums import ConfigKind, CycleOutcome

MONITOR_PAGES = ["/analytics/portfolio", "/cycles", "/risk", "/costs"]


@pytest.fixture
async def cycled(sim_application: Application) -> Application:
    """One completed cycle, so every view has something real to render."""
    await sim_application.recover()
    results = await sim_application.supervisor.run_once()
    assert results and results[0].outcome is CycleOutcome.ORDERS_PLACED
    return sim_application


@pytest.mark.parametrize("path", MONITOR_PAGES)
async def test_pages_render_on_an_empty_system(client: httpx.AsyncClient, path: str) -> None:
    """A system that has never cycled must still render. Empty is not the same as broken."""
    response = await client.get(path)
    assert response.status_code == 200
    assert "<!doctype html>" in response.text.lower()


@pytest.mark.parametrize("path", MONITOR_PAGES)
async def test_pages_render_after_a_cycle(
    cycled: Application, client: httpx.AsyncClient, path: str
) -> None:
    assert (await client.get(path)).status_code == 200


async def test_the_mode_is_unmissable(client: httpx.AsyncClient) -> None:
    """Mode confusion is a catastrophic failure class, so the banner is a control (PLAN §2.4)."""
    body = (await client.get("/cycles")).text
    assert "mode-sim" in body
    assert "SIM" in body


async def test_the_replaced_portfolio_page_redirects(client: httpx.AsyncClient) -> None:
    """A redirect, never a second copy: two pages showing positions is two places to disagree."""
    response = await client.get("/portfolio", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/analytics/portfolio"


async def test_cycle_history_lists_a_completed_cycle(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/cycles")).text
    assert "orders_placed" in body
    assert "drill down" in body


async def test_cycle_history_filters_by_basket(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    assert "orders_placed" in (await client.get("/cycles?basket=demo")).text
    assert "No cycles recorded yet" in (await client.get("/cycles?basket=nonexistent")).text


async def test_unknown_cycle_is_a_404(client: httpx.AsyncClient) -> None:
    """Not an empty page: "this cycle did nothing" and "there is no such cycle" differ."""
    assert (await client.get("/cycles/not-a-cycle")).status_code == 404


async def test_drill_down_is_the_research_artifact(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Snapshot, transcript, risk provenance and orders — the whole "why did it do that"."""
    cycle_id = _latest_cycle_id(cycled)
    body = (await client.get(f"/cycles/{cycle_id}")).text

    assert "Debate transcript" in body
    assert "Technical Analyst" in body  # the seat that answered
    assert "Momentum is constructive" in body  # its thesis, from the log
    assert "Risk checks" in body
    assert "venue_quantization" in body  # a recorded risk check, by rule name
    assert "Frozen snapshot" in body
    assert "snapshot_id" in body  # the packet itself, not just a heading
    assert "BTC/USDT" in body


async def test_drill_down_resolves_the_pinned_configuration(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """A decision is displayed against the limits that produced it (ADR 0013)."""
    body = (await client.get(f"/cycles/{_latest_cycle_id(cycled)}")).text
    assert "basket:demo" in body
    assert "global_risk:global" in body
    assert "no longer resolves" not in body


async def test_drill_down_flags_a_pin_that_no_longer_resolves(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Substituting the current version would misrepresent the decision, so it says so instead."""
    from sqlalchemy import delete

    from tradebot.persistence.schema import config_versions

    with cycled.store.engine.begin() as connection:
        connection.execute(delete(config_versions).where(config_versions.c.kind == "basket"))

    body = (await client.get(f"/cycles/{_latest_cycle_id(cycled)}")).text
    assert "no longer resolves" in body


# ------------------------------------------------- narrowing the drill-down to one instrument
#
# The drill-down of a multi-instrument basket interleaves every instrument's votes, so following
# one instrument's flow means reading past the others. The filter narrows the panel's
# *deliberation* only: what was in force and what happened stay whole, because a portfolio-wide
# veto recorded against one instrument is the reason another instrument's decision came out as
# it did.


async def test_the_drill_down_offers_a_checkbox_per_instrument_it_deliberated_on(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get(f"/cycles/{_latest_cycle_id(cycled)}")).text
    assert body.count('name="instrument"') == 2
    assert 'value="sim:BTC/USDT"' in body
    assert 'value="sim:ETH/USDT"' in body


async def test_a_filter_narrows_the_decisions_and_the_transcript(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    response = await client.get(
        f"/cycles/{_latest_cycle_id(cycled)}", params={"instrument": "sim:BTC/USDT"}
    )

    for heading in ("Decisions", "Debate transcript"):
        section = _section(response.text, heading)
        assert "sim:BTC/USDT" in section
        assert "sim:ETH/USDT" not in section


async def test_a_filter_never_hides_what_was_in_force_or_what_happened(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """The correction that shaped this feature: risk state is the picture, not the noise."""
    response = await client.get(
        f"/cycles/{_latest_cycle_id(cycled)}", params={"instrument": "sim:BTC/USDT"}
    )

    for heading in ("Risk checks", "Orders", "Fills"):
        assert "sim:ETH/USDT" in _section(response.text, heading), heading
    assert "Frozen snapshot" in response.text
    assert "snapshot_id" in response.text


async def test_a_filter_can_always_be_left(cycled: Application, client: httpx.AsyncClient) -> None:
    """The checkbox list is built from the *un-narrowed* cycle, or a filter is a one-way door.

    Built from what is on screen it would collapse to the ticked instrument on the first Apply,
    and there would be no control left to bring the others back.
    """
    body = (
        await client.get(
            f"/cycles/{_latest_cycle_id(cycled)}", params={"instrument": "sim:BTC/USDT"}
        )
    ).text

    assert body.count('name="instrument"') == 2
    assert 'value="sim:ETH/USDT"' in body


async def test_several_instruments_may_be_chosen_at_once(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    response = await client.get(
        f"/cycles/{_latest_cycle_id(cycled)}",
        params=[("instrument", "sim:BTC/USDT"), ("instrument", "sim:ETH/USDT")],
    )

    decisions = _section(response.text, "Decisions")
    assert "sim:BTC/USDT" in decisions
    assert "sim:ETH/USDT" in decisions


async def test_an_unfiltered_drill_down_shows_every_instrument(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Absent means all, as it does for the basket filter on the cycle list."""
    decisions = _section(
        (await client.get(f"/cycles/{_latest_cycle_id(cycled)}")).text, "Decisions"
    )
    assert "sim:BTC/USDT" in decisions
    assert "sim:ETH/USDT" in decisions


async def test_an_instrument_the_cycle_never_mentions_is_reported_not_silently_dropped(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """A hand-edited URL must not blank two sections and read as a cycle that deliberated once.

    The same rule the pinned-configuration table follows: what does not resolve is shown as
    unresolved rather than omitted.
    """
    response = await client.get(
        f"/cycles/{_latest_cycle_id(cycled)}",
        params=[("instrument", "sim:BTC/USDT"), ("instrument", "sim:NOSUCH/USDT")],
    )

    assert response.status_code == 200
    assert "not in this cycle" in response.text
    assert "sim:NOSUCH/USDT" in response.text
    assert "sim:BTC/USDT" in _section(response.text, "Decisions")


async def test_a_filter_that_matches_nothing_says_so_rather_than_blaming_the_cycle(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """ "Nothing here" and "you filtered it out" are different facts about a cycle.

    The unfiltered empty states read "the cycle ended before the panel ran" and "no seat
    responded", which under an active filter would report a panel failure that never happened —
    on the page an operator opens to find out why the bot did what it did.
    """
    body = (
        await client.get(
            f"/cycles/{_latest_cycle_id(cycled)}", params={"instrument": "sim:NOSUCH/USDT"}
        )
    ).text

    assert "the cycle ended before the panel ran" not in body
    assert "No seat responded in this cycle" not in body
    assert body.count("matches this filter") == 2


async def test_a_cycle_with_one_instrument_carries_no_filter(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """A control offering a single choice is noise on the one page an operator reads carefully."""
    from tradebot.app import instrument_of
    from tradebot.core.config import Basket

    await sim_application.configs.put(
        "solo",
        Basket(
            basket_id="solo",
            name="One instrument",
            instruments=(await instrument_of(sim_application.catalogue, "XRP/USDT"),),
            panel=sim_application.baskets[0].panel,
            timeframes=("1h",),
        ),
        actor="test",
    )
    await sim_application.recover()
    await sim_application.supervisor.run_once()

    body = (await client.get(f"/cycles/{_latest_cycle_id(sim_application, 'solo')}")).text
    assert "sim:XRP/USDT" in _section(body, "Decisions")
    assert 'name="instrument"' not in body


def _section(body: str, heading: str) -> str:
    """The rendered page between one `<h2>` and the next.

    Narrowing is a claim about *sections* — that ETH left the transcript and stayed in the risk
    checks — and a substring search over the whole page cannot tell those apart.
    """
    start = body.index(f"<h2>{heading}</h2>")
    rest = body[start + 1 :]
    end = rest.find("<h2>")
    return rest if end == -1 else rest[:end]


async def test_money_renders_exactly(cycled: Application, client: httpx.AsyncClient) -> None:
    """No template may coerce a limit to float on its way to a page (PLAN §2.1)."""
    body = (await client.get("/risk")).text
    policy = cycled.configs.global_risk()
    assert policy is not None
    assert str(policy.document.max_drawdown_pct) in body


async def test_risk_page_shows_the_tier2_policy_in_force(client: httpx.AsyncClient) -> None:
    body = (await client.get("/risk")).text
    assert "Max drawdown" in body
    assert "Tier-2 policy in force" in body


async def test_kill_switch_banner_appears_when_tripped(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """On every page: an operator reading a cycle history must not have to navigate to find out."""
    assert "Kill switch tripped" not in (await client.get("/cycles")).text

    await cycled.watchdog.trip("manual", "tested from the dashboard suite")

    body = (await client.get("/cycles")).text
    assert "Kill switch tripped" in body
    assert "tested from the dashboard suite" in body


async def test_halted_basket_banner_appears(cycled: Application, client: httpx.AsyncClient) -> None:
    await cycled.watchdog.halt_basket("demo", "three consecutive failed cycles")

    body = (await client.get("/costs")).text
    assert "Halted baskets" in body
    assert "three consecutive failed cycles" in body


async def test_portfolio_shows_holdings_and_the_realized_curve(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/analytics/portfolio")).text
    assert "Equity curve (realized)" in body
    assert "BTC/USDT" in body
    assert "not mark-to-market" in body  # the honesty note is part of the artifact


async def test_costs_page_reports_per_cycle_spend(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/costs")).text
    assert "demo" in body
    assert "Per cycle" in body


def _latest_cycle_id(application: Application, basket_id: str | None = None) -> str:
    from sqlalchemy import select

    from tradebot.persistence.schema import cycles

    query = select(cycles.c.cycle_id).order_by(cycles.c.started_at.desc()).limit(1)
    if basket_id is not None:
        query = query.where(cycles.c.basket_id == basket_id)
    with application.store.engine.connect() as connection:
        row = connection.execute(query).one()
    return str(row.cycle_id)


def test_config_kinds_are_the_ones_the_drill_down_resolves() -> None:
    """The pin parser maps `kind:id` back to a `ConfigKind`; a new kind must not break it."""
    assert {kind.value for kind in ConfigKind} == {"basket", "global_risk"}
