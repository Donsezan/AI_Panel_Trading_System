"""Rung 3: PLAN §6's exit criterion, driven entirely over HTTP.

> A basket can be created, configured, run, paused, and killed entirely from the GUI, with every
> action appearing in the event log.

Nothing here reaches past the dashboard except to run the cycle the operator's basket is due for
— the supervisor is the thing that cycles, and pressing a button is not one of its triggers. The
assertion at the end is the one that matters: every operator action left an attributable record
in the append-only log, which is the compliance artifact the whole system is built around.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from tests.unit.test_dashboard_configure import as_form, flat

from tradebot.app import Application
from tradebot.core.enums import BasketStatus, ConfigKind, CycleOutcome, KillSwitchState
from tradebot.core.events import Event, EventType
from tradebot.dashboard.forms import draft_of
from tradebot.dashboard.queries import Queries
from tradebot.dashboard.routes.configure import LOOSEN_PHRASE, unfold_prices
from tradebot.dashboard.routes.control import KILL_PHRASE

pytestmark = pytest.mark.scenario


async def test_a_basket_is_created_configured_run_paused_and_killed_from_the_gui(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()

    # 1. Created — from the seeded basket's own form, renamed. The `new` form is a blank of the
    #    same shape; reusing this one keeps the test about the lifecycle, not about typing.
    source = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert source is not None
    form = flat(unfold_prices(draft_of(source.document)))
    created = _set(_set(form, "doc.basket_id", "alpha"), "doc.name", "Alpha basket")

    assert (await client.post("/configure/baskets/alpha", data=as_form(created))).status_code == 303
    assert {r.ref.config_id for r in sim_application.configs.baskets()} == {"demo", "alpha"}

    # 2. Configured — a Tier-1 limit, and a Tier-2 limit that needs its typed confirmation.
    tightened = _set(created, "doc.risk_policy.max_trades_per_day", "3")
    assert (
        await client.post("/configure/baskets/alpha", data=as_form(tightened))
    ).status_code == 303

    policy = sim_application.configs.global_risk()
    assert policy is not None
    loosened = _set(flat(draft_of(policy.document)), "doc.max_gross_exposure_pct", "90")
    refused = await client.post("/configure/risk", data=as_form(loosened))
    assert refused.status_code == 200  # a loosening without the phrase changes nothing
    assert sim_application.configs.global_risk().ref.version == 1  # type: ignore[union-attr]

    published = await client.post(
        "/configure/risk", data=as_form([*loosened, ("confirm", LOOSEN_PHRASE)])
    )
    assert published.status_code == 303
    assert sim_application.configs.global_risk().document.max_gross_exposure_pct == Decimal(90)  # type: ignore[union-attr]

    # 3. Run — by the supervisor, which is the only thing that cycles a basket.
    results = await sim_application.supervisor.run_once()
    assert {result.basket_id for result in results} == {"demo", "alpha"}
    assert all(result.outcome is not CycleOutcome.FAILED for result in results)

    # The Monitor can now read what happened, against the versions that produced it.
    assert (await client.get("/cycles?basket=alpha")).status_code == 200
    drill_down = await client.get(f"/cycles/{_latest_cycle_id(sim_application, 'alpha')}")
    assert drill_down.status_code == 200
    assert "basket:alpha" in drill_down.text

    # 4. Paused — the operator's intent, published as a new version, and it stops cycling.
    assert (
        await client.post("/control/baskets/alpha/status", data={"status": "paused"})
    ).status_code == 303
    alpha = sim_application.configs.latest(ConfigKind.BASKET, "alpha")
    assert alpha is not None
    assert alpha.document.status is BasketStatus.PAUSED
    assert {r.basket_id for r in await sim_application.supervisor.run_once()} == {"demo"}

    # 5. Killed — and nothing cycles at all after that.
    assert (
        await client.post("/control/kill", data={"confirm": KILL_PHRASE, "note": "end of test"})
    ).status_code == 303
    assert sim_application.states.load().kill_switch is KillSwitchState.TRIPPED
    blocked = await sim_application.supervisor.run_once()
    assert [result.outcome for result in blocked] == [CycleOutcome.BLOCKED]

    _assert_every_action_is_in_the_log(sim_application.store.read_all())


def _assert_every_action_is_in_the_log(log: tuple[Event, ...]) -> None:
    """The exit criterion's second half: every action appears in the event log, with an actor."""
    published = [e for e in log if e.type is EventType.CONFIG_CHANGED]
    by_dashboard = [e for e in published if e.payload["actor"] == "dashboard"]

    # created, Tier-1 edit, Tier-2 loosening, pause — four attributable publications.
    assert len(by_dashboard) == 4
    assert {e.payload["config_id"] for e in by_dashboard} == {"alpha", "global"}

    killed = [e for e in log if e.type is EventType.KILL_SWITCH_CHANGED]
    assert killed[-1].payload["state"] == "tripped"
    assert killed[-1].payload["actor"] == "dashboard"
    assert killed[-1].payload["reason"] == "end of test"

    assert any(e.type is EventType.CYCLE_STARTED and e.basket_id == "alpha" for e in log)


def _latest_cycle_id(application: Application, basket_id: str) -> str:
    return str(Queries(application.store).cycles(basket_id=basket_id)[0].cycle_id)


def _set(pairs: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    replaced = [(key, value if key == name else current) for key, current in pairs]
    return replaced if any(key == name for key, _ in pairs) else [*replaced, (name, value)]
