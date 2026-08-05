"""Rung 3: PLAN §6's exit criterion, driven entirely over HTTP.

> A basket can be created, configured, run, paused, and killed entirely from the GUI, with every
> action appearing in the event log.

Phase 10 pass 3 restates it against the workspace, which is now the only screen an operator runs
the bot from: the dock and the risk-control pane carry every act the retired Control page did, and
this test takes all of them from there — create → configure → stop/start → observe → pause →
quarantine → close → kill → re-arm.

Nothing here reaches past the dashboard except to run the cycle the operator's basket is due for —
the supervisor is the thing that cycles, and pressing a button is not one of its triggers. The
assertion at the end is the one that matters: every operator action left an attributable record in
the append-only log, which is the compliance artifact the whole system is built around.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from tests.unit.test_dashboard_configure import as_form, flat

from tradebot.app import Application
from tradebot.control.supervision import SupervisionController
from tradebot.core.enums import BasketStatus, ConfigKind, CycleOutcome, KillSwitchState
from tradebot.core.events import Event, EventType
from tradebot.dashboard.dock import KILL_PHRASE, QUARANTINE_CONFIRM
from tradebot.dashboard.forms import draft_of
from tradebot.dashboard.queries import Queries
from tradebot.dashboard.routes.configure import LOOSEN_PHRASE, unfold_prices
from tradebot.risk.state import REARM_PHRASE

pytestmark = pytest.mark.scenario

BTC = "sim:BTC/USDT"
SCOPE = f"instrument:demo:{BTC}"


async def test_a_basket_is_created_configured_run_paused_and_killed_from_the_workspace(
    sim_application: Application, supervision: SupervisionController, client: httpx.AsyncClient
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

    # 3. Stopped and started from the dock — the GUI equivalent of `--observe`, and back.
    assert (await client.post("/control/stop", data={"scope": SCOPE})).status_code == 303
    assert not supervision.running
    assert "Start trading" in (await client.get("/workspace/controls")).text

    assert (await client.post("/control/start", data={"scope": SCOPE})).status_code == 303
    assert supervision.running

    # 4. Run — by the supervisor, which is the only thing that cycles a basket.
    results = await sim_application.supervisor.run_once()
    assert {result.basket_id for result in results} == {"demo", "alpha"}
    assert all(result.outcome is not CycleOutcome.FAILED for result in results)

    # 5. Observed — on the workspace, scoped to what just happened, with the research artifact
    #    one click away in the operation log.
    workspace = await client.get("/", params={"scope": SCOPE})
    assert workspace.status_code == 200
    assert 'href="/cycles/' in workspace.text
    drill_down = await client.get(f"/cycles/{_latest_cycle_id(sim_application, 'alpha')}")
    assert drill_down.status_code == 200
    assert "basket:alpha" in drill_down.text

    # 6. Paused — the operator's intent, published as a new version, and it stops cycling.
    paused = await client.post(
        "/control/baskets/alpha/status", data={"status": "paused", "scope": "basket:alpha"}
    )
    assert paused.status_code == 303
    assert _selection_of(paused) == "basket:alpha"
    alpha = sim_application.configs.latest(ConfigKind.BASKET, "alpha")
    assert alpha is not None
    assert alpha.document.status is BasketStatus.PAUSED
    assert {r.basket_id for r in await sim_application.supervisor.run_once()} == {"demo"}

    # 7. Quarantined — and because the instrument is held, it takes a second, deliberate click.
    #    The first answers with the warning rather than the version (ADR 0022).
    first_click = await client.post(
        "/control/baskets/demo/quarantine",
        data={"instrument_key": BTC, "excluded": "true", "scope": SCOPE},
    )
    assert first_click.status_code == 200
    assert "holds a position" in first_click.text
    assert _quarantined(sim_application) == ()

    second_click = await client.post(
        "/control/baskets/demo/quarantine",
        data={
            "instrument_key": BTC,
            "excluded": "true",
            "confirm": QUARANTINE_CONFIRM,
            "scope": SCOPE,
        },
    )
    assert second_click.status_code == 303
    assert _quarantined(sim_application) == (BTC,)

    # 8. Closed by hand — the position a quarantine must never orphan, through the ordinary
    #    OrderIntent → Tier-1 → Tier-2 path (ADR 0015, ADR 0022).
    closed = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": BTC, "scope": SCOPE}
    )
    assert closed.status_code == 200
    assert "Close submitted" in closed.text

    # 9. Killed — and nothing cycles at all after that.
    killed = await client.post(
        "/control/kill", data={"confirm": KILL_PHRASE, "note": "end of test", "scope": SCOPE}
    )
    assert killed.status_code == 303
    assert sim_application.states.load().kill_switch is KillSwitchState.TRIPPED
    blocked = await sim_application.supervisor.run_once()
    assert [result.outcome for result in blocked] == [CycleOutcome.BLOCKED]

    # 10. Re-armed — the typed act that undoes it, from the risk-control pane.
    assert (await client.post("/control/rearm", data={"confirm": "go on"})).status_code == 200
    assert sim_application.states.load().kill_switch is KillSwitchState.TRIPPED

    rearmed = await client.post("/control/rearm", data={"confirm": REARM_PHRASE, "scope": SCOPE})
    assert rearmed.status_code == 303
    assert sim_application.states.load().kill_switch is KillSwitchState.ARMED

    _assert_every_action_is_in_the_log(sim_application.store.read_all())


def _assert_every_action_is_in_the_log(log: tuple[Event, ...]) -> None:
    """The exit criterion's second half: every action appears in the event log, with an actor."""
    published = [e for e in log if e.type is EventType.CONFIG_CHANGED]
    by_dashboard = [e for e in published if e.payload["actor"] == "dashboard"]

    # created, Tier-1 edit, Tier-2 loosening, pause, quarantine — five attributable publications.
    assert len(by_dashboard) == 5
    assert {e.payload["config_id"] for e in by_dashboard} == {"alpha", "global", "demo"}

    switched = [e for e in log if e.type is EventType.KILL_SWITCH_CHANGED]
    assert [e.payload["state"] for e in switched[-2:]] == ["tripped", "armed"]
    assert {e.payload["actor"] for e in switched[-2:]} == {"dashboard"}
    assert switched[-2].payload["reason"] == "end of test"

    # The manual close went through risk under its own correlation id, and said who asked.
    closes = [
        e for e in log if e.type is EventType.RISK_EVENT and e.payload["rule"] == "manual_close"
    ]
    assert [e.payload["action_taken"] for e in closes] == ["requested", "order_submitted"]

    assert any(e.type is EventType.CYCLE_STARTED and e.basket_id == "alpha" for e in log)


def _quarantined(application: Application) -> tuple[str, ...]:
    record = application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return tuple(record.document.risk_policy.quarantined_instruments)


def _selection_of(response: httpx.Response) -> str:
    """The scope an action handed back, so the operator lands where they were acting."""
    return parse_qs(urlparse(response.headers["location"]).query)["scope"][0]


def _latest_cycle_id(application: Application, basket_id: str) -> str:
    return str(Queries(application.store).cycles(basket_id=basket_id)[0].cycle_id)


def _set(pairs: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    replaced = [(key, value if key == name else current) for key, current in pairs]
    return replaced if any(key == name for key, _ in pairs) else [*replaced, (name, value)]
