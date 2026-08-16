"""Control: the operator's safety surface, and what each action writes to the log.

The actions kept their URLs when the Control *page* retired into the workspace's dock (Phase 10
pass 3), so this suite is about what each POST does — and it now reads the answers off the
workspace, which is the only screen there is.

Three things are load-bearing here. **Pause and halt must stay distinct** — a config edit may
never clear a halt the system imposed for cause. **Manual close has no side door**: it goes
through Tier-1 and Tier-2, and every rule that stands aside for an operator's exit records that
it did, so the exemption is visible in the log rather than implied by the absence of a veto
(ADR 0015). And **quarantine is a third thing again** — versioned configuration that stops the
bot acting while the cycle, and therefore the data, keeps running (ADR 0022).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from tests.conftest import idle_supervision, publish_keyed_panel

from tradebot.app import Application
from tradebot.control.arming import LIVE_CONFIRMATION_PHRASE
from tradebot.control.config_store import SINGLETON_ID
from tradebot.control.supervision import SupervisionController
from tradebot.core.enums import BasketStatus, ConfigKind, CycleOutcome, KillSwitchState
from tradebot.core.events import EventType
from tradebot.dashboard.dock import KILL_PHRASE, QUARANTINE_CONFIRM
from tradebot.dashboard.queries import Queries
from tradebot.risk.rules import STOOD_ASIDE
from tradebot.risk.state import REARM_PHRASE

BTC = "sim:BTC/USDT"


@pytest.fixture
async def cycled(sim_application: Application) -> Application:
    """A completed cycle, so a position exists to close."""
    await sim_application.recover()
    results = await sim_application.supervisor.run_once()
    assert results and results[0].outcome is CycleOutcome.ORDERS_PLACED
    return sim_application


def events_of(application: Application, event_type: EventType) -> list[Any]:
    return [e for e in application.store.read_all() if e.type is event_type]


def risk_events(application: Application, rule: str) -> list[Any]:
    return [e for e in events_of(application, EventType.RISK_EVENT) if e.payload["rule"] == rule]


async def test_the_control_page_redirects_to_the_workspace(client: httpx.AsyncClient) -> None:
    """Two surfaces for the kill switch is two places for its state to disagree."""
    response = await client.get("/control")

    assert response.status_code == 303
    assert response.headers["location"] == "/"


# ---------------------------------------------------------------- unreachable panel


async def test_a_panel_with_no_key_is_a_banner_and_not_a_refusal(
    sim_application: Application, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0023, the sim/paper half: the process is up and says what is wrong, because that is
    what an operator needs in order to fix it. The banner is on every page, like the switch."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    await publish_keyed_panel(sim_application)

    page = (await client.get("/")).text

    assert "cannot be fully reached" in page
    assert "OPENROUTER_API_KEY" in page
    assert "/configure" in page


async def test_the_banner_names_the_environment_and_not_a_place_to_type_a_key(
    sim_application: Application, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys are environment-only (PLAN §3.2), so the page must not imply it can accept one."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    await publish_keyed_panel(sim_application)

    page = (await client.get("/")).text

    assert "the dashboard cannot accept" in page


async def test_an_unreachable_panel_does_not_block_starting_outside_live(
    sim_application: Application, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running degraded is what sim and paper are *for*: the seats abstain and the cycle waits."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    await publish_keyed_panel(sim_application)
    await sim_application.recover()
    controller = SupervisionController(sim_application, serve=idle_supervision)

    assert await controller.start() == ()
    await controller.stop()


# ---------------------------------------------------------------- pause / resume


async def test_pausing_publishes_a_new_configuration_version(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """A pause is the operator's intent, so it is configuration and it is versioned."""
    response = await client.post(
        "/control/baskets/demo/status", data={"status": "paused", "note": "stepping away"}
    )

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.ref.version == 2
    assert record.document.status is BasketStatus.PAUSED
    assert record.actor == "dashboard"
    assert record.note == "stepping away"


async def test_resuming_publishes_another_version(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await client.post("/control/baskets/demo/status", data={"status": "paused"})
    await client.post("/control/baskets/demo/status", data={"status": "active"})

    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.ref.version == 3
    assert record.document.status is BasketStatus.ACTIVE


async def test_a_paused_basket_stops_cycling(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()
    await client.post("/control/baskets/demo/status", data={"status": "paused"})

    assert await sim_application.supervisor.run_once() == ()


async def test_pausing_an_unknown_basket_reports_rather_than_raising(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/control/baskets/ghost/status", data={"status": "paused"})
    assert response.status_code == 200
    assert "no basket ghost" in response.text


# ---------------------------------------------------------------- halt / un-halt


async def test_resuming_a_basket_does_not_clear_a_halt(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The load-bearing separation: configuration may never un-halt what the system stopped."""
    await sim_application.watchdog.halt_basket("demo", "three consecutive failed cycles")

    await client.post("/control/baskets/demo/status", data={"status": "active"})

    assert sim_application.states.status_of("demo") is BasketStatus.HALTED


async def test_un_halting_demands_the_typed_phrase(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.watchdog.halt_basket("demo", "failed cycles")

    response = await client.post("/control/baskets/demo/unhalt", data={"confirm": "yes please"})

    assert response.status_code == 200
    assert sim_application.states.status_of("demo") is BasketStatus.HALTED


async def test_un_halting_with_the_phrase_clears_it_and_records_it(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.watchdog.halt_basket("demo", "failed cycles")

    response = await client.post("/control/baskets/demo/unhalt", data={"confirm": REARM_PHRASE})

    assert response.status_code == 303
    assert sim_application.states.status_of("demo") is BasketStatus.ACTIVE
    changed = events_of(sim_application, EventType.BASKET_STATUS_CHANGED)
    assert changed[-1].payload["status"] == "active"
    assert "dashboard" in changed[-1].payload["reason"]


# ---------------------------------------------------------------- quarantine


def quarantine_of(application: Application) -> Any:
    record = application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    return record.document.risk_policy


async def quarantine(
    client: httpx.AsyncClient, *, instrument_key: str = "", excluded: bool = True, **extra: str
) -> httpx.Response:
    return await client.post(
        "/control/baskets/demo/quarantine",
        data={
            "instrument_key": instrument_key,
            "excluded": "true" if excluded else "false",
            **extra,
        },
    )


async def test_quarantining_an_instrument_publishes_a_new_version(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """Configuration, like a pause: versioned, attributable, and no typed phrase."""
    response = await quarantine(client, instrument_key=BTC)

    assert response.status_code == 303
    record = sim_application.configs.latest(ConfigKind.BASKET, "demo")
    assert record is not None
    assert record.ref.version == 2
    assert record.actor == "dashboard"
    assert record.document.risk_policy.quarantined_instruments == (BTC,)
    assert not record.document.risk_policy.quarantined


async def test_quarantining_the_whole_basket_leaves_the_instrument_list_alone(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await quarantine(client)

    policy = quarantine_of(sim_application)
    assert policy.quarantined
    assert policy.quarantined_instruments == ()
    assert policy.excludes(BTC)


async def test_releasing_publishes_another_version(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await quarantine(client, instrument_key=BTC)
    await quarantine(client, instrument_key=BTC, excluded=False)

    assert quarantine_of(sim_application).quarantined_instruments == ()


async def test_a_quarantined_instrument_gets_no_order(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """The whole point, asserted through the real loop: the panel decides and nothing is sent."""
    await sim_application.recover()
    await quarantine(client, instrument_key=BTC)

    results = await sim_application.supervisor.run_once()

    assert results
    assert not [
        order for result in results for order in result.orders if order.instrument_key == BTC
    ]
    vetoed = [
        check
        for event in events_of(sim_application, EventType.RISK_CHECKED)
        for check in event.payload["checks"]
        if check["rule"] == "quarantine" and check["decision"] == "veto"
    ]
    assert vetoed, "the veto must be recorded as an ordinary Tier-1 verdict"


async def test_a_quarantined_basket_still_takes_a_snapshot_but_runs_no_panel(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """Data keeps flowing — that is the difference from a pause — and no model call is spent."""
    await sim_application.recover()
    await quarantine(client)

    results = await sim_application.supervisor.run_once()

    assert [result.outcome for result in results] == [CycleOutcome.QUARANTINED]
    chain = sim_application.store.event_types(results[0].cycle_id)
    assert EventType.SNAPSHOT_FROZEN in chain
    assert EventType.SEAT_RESPONDED not in chain
    assert EventType.ORDER_SUBMITTED not in chain


async def test_quarantining_a_held_position_asks_for_a_second_click(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Inaction can compound a loss; the operator is told what they are about to stop managing."""
    response = await quarantine(client, instrument_key=BTC)

    assert response.status_code == 200
    assert "holds a position" in response.text
    assert quarantine_of(cycled).quarantined_instruments == ()


async def test_the_second_click_publishes_it(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    response = await quarantine(client, instrument_key=BTC, confirm=QUARANTINE_CONFIRM)

    assert response.status_code == 303
    assert quarantine_of(cycled).quarantined_instruments == (BTC,)


async def test_releasing_a_held_position_needs_no_confirmation(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Only the exclusion is consequential; putting something back in service is not."""
    await quarantine(client, instrument_key=BTC, confirm=QUARANTINE_CONFIRM)

    response = await quarantine(client, instrument_key=BTC, excluded=False)

    assert response.status_code == 303
    assert quarantine_of(cycled).quarantined_instruments == ()


async def test_a_manual_close_still_works_on_a_quarantined_instrument(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """The consequence that makes quarantine safe: a held position is never orphaned by it."""
    await quarantine(client, instrument_key=BTC, confirm=QUARANTINE_CONFIRM)

    response = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": BTC}
    )

    assert "Close submitted" in response.text
    stood_aside = [
        check
        for event in events_of(cycled, EventType.RISK_CHECKED)
        if event.cycle_id.startswith("manual-")
        for check in event.payload["checks"]
        if check["rule"] == "quarantine"
    ]
    assert [check["detail"] for check in stood_aside] == [STOOD_ASIDE]


async def test_the_dock_shows_what_is_excluded_and_why(client: httpx.AsyncClient) -> None:
    """An instrument excluded by its basket rather than by name must read as excluded anyway —
    and must not offer a release button that would publish a version changing nothing."""
    await quarantine(client)

    body = (await client.get("/")).text

    assert "quarantined" in body
    assert "excluded with its basket" in body
    assert "release" in body


async def test_quarantining_an_unknown_basket_reports_rather_than_raising(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/control/baskets/ghost/quarantine", data={"instrument_key": "", "excluded": "true"}
    )

    assert response.status_code == 200
    assert "no basket ghost" in response.text


# ---------------------------------------------------------------- kill switch


async def test_tripping_the_kill_switch_demands_its_phrase(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()

    response = await client.post("/control/kill", data={"confirm": "STOP"})

    assert response.status_code == 200
    assert sim_application.states.load().kill_switch is KillSwitchState.ARMED


async def test_tripping_the_kill_switch_stops_everything_and_records_it(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()

    response = await client.post(
        "/control/kill", data={"confirm": KILL_PHRASE, "note": "market looks broken"}
    )

    assert response.status_code == 303
    assert sim_application.states.load().kill_switch is KillSwitchState.TRIPPED
    switched = events_of(sim_application, EventType.KILL_SWITCH_CHANGED)
    assert switched[-1].payload["state"] == "tripped"
    assert switched[-1].payload["actor"] == "dashboard"
    assert await sim_application.supervisor.run_once() != ()  # the cycle is recorded as blocked


async def test_a_tripped_switch_blocks_the_next_cycle(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()
    await client.post("/control/kill", data={"confirm": KILL_PHRASE})

    results = await sim_application.supervisor.run_once()

    assert [result.outcome for result in results] == [CycleOutcome.BLOCKED]


async def test_rearming_demands_the_typed_phrase(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()
    await client.post("/control/kill", data={"confirm": KILL_PHRASE})

    response = await client.post("/control/rearm", data={"confirm": "go on then"})

    assert response.status_code == 200
    assert sim_application.states.load().kill_switch is KillSwitchState.TRIPPED


async def test_rearming_with_the_phrase_restores_trading(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()
    await client.post("/control/kill", data={"confirm": KILL_PHRASE})

    response = await client.post("/control/rearm", data={"confirm": REARM_PHRASE})

    assert response.status_code == 303
    state = sim_application.states.load()
    assert state.kill_switch is KillSwitchState.ARMED
    assert state.high_water_mark == sim_application.valuation().equity


# ---------------------------------------------------------------- manual close


async def test_a_manual_close_goes_through_the_normal_path(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Risk provenance and an order, both recorded under one `manual-…` correlation id."""
    response = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:BTC/USDT"}
    )

    assert response.status_code == 200
    assert "Close submitted" in response.text

    submitted = risk_events(cycled, "manual_close")
    assert [event.payload["action_taken"] for event in submitted] == [
        "requested",
        "order_submitted",
    ]
    assert all(event.cycle_id.startswith("manual-") for event in submitted)
    # The intent passed both tiers, and the provenance says which rules it passed.
    checked = [
        e for e in events_of(cycled, EventType.RISK_CHECKED) if e.cycle_id.startswith("manual-")
    ]
    assert len(checked) == 2
    assert all(event.payload["approved"] for event in checked)


async def test_the_cooldown_the_entry_created_does_not_trap_the_operator(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """The close lands immediately after the entry, inside its own cooldown (ADR 0015).

    The metering rules stand aside for a human's exit — and say so in the provenance, which is
    what makes it a decision by the risk layer rather than a bypass around it.
    """
    await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:BTC/USDT"}
    )

    checks = [
        check
        for event in events_of(cycled, EventType.RISK_CHECKED)
        if event.cycle_id.startswith("manual-")
        for check in event.payload["checks"]
    ]
    stood_aside = {check["rule"] for check in checks if check["detail"] == STOOD_ASIDE}
    assert stood_aside == {
        "cooldown",
        "max_trades_per_day",
        "max_consecutive_losses",
        "max_orders_per_hour",
    }


async def test_the_panel_is_still_metered_by_the_same_rules(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """The control for the exemption: the *panel* re-running immediately places no second order."""
    before = len(events_of(cycled, EventType.ORDER_SUBMITTED))

    results = await cycled.supervisor.run_once()

    assert results and results[0].outcome is not CycleOutcome.ORDERS_PLACED
    assert len(events_of(cycled, EventType.ORDER_SUBMITTED)) == before


async def test_a_tripped_kill_switch_does_not_trap_the_operator(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """The switch stops the bot trading; it must not stop a human getting out (DESIGN §6.6).

    Asserted rather than left implicit — `flatten_on_kill` existing at all shows the design
    contemplates leaving positions at kill time, and this is the manual equivalent.
    """
    await client.post("/control/kill", data={"confirm": KILL_PHRASE})
    assert cycled.states.load().kill_switch is KillSwitchState.TRIPPED

    response = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:BTC/USDT"}
    )

    assert "Close submitted" in response.text
    assert risk_events(cycled, "manual_close")[-1].payload["action_taken"] == "order_submitted"


async def test_a_close_that_is_shrunk_is_reported_as_partial(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Tier-2's notional cap shrinks rather than vetoes, and "closed" would then be a lie."""
    policy = cycled.configs.global_risk()
    assert policy is not None
    await cycled.configs.put(
        SINGLETON_ID,
        policy.document.model_copy(update={"max_order_notional": Decimal(20)}),
        actor="test",
        note="a cap small enough to shrink the close",
    )

    response = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:BTC/USDT"}
    )

    assert "you are not flat" in response.text
    assert "Close submitted" not in response.text


async def test_the_request_is_recorded_even_when_it_is_refused(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """That a human asked must survive the refusal — it is the audit record of the intent."""
    await sim_application.recover()

    await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:BTC/USDT"}
    )

    actions = [
        event.payload["action_taken"] for event in risk_events(sim_application, "manual_close")
    ]
    assert actions == ["requested"]  # refused before any rule ran: nothing is held


async def test_closing_a_position_no_basket_holds_is_refused(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    response = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:DOGE/USDT"}
    )

    assert response.status_code == 200
    assert "Not done" in response.text


async def test_closing_when_flat_is_refused(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await sim_application.recover()

    response = await client.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:ETH/USDT"}
    )

    assert response.status_code == 200
    assert "nothing to close" in response.text


async def test_a_stopped_process_refuses_to_place_an_order(
    sim_application: Application, dashboard_observing: httpx.AsyncClient
) -> None:
    """Nothing is polling open orders, so an order placed now would rest unmonitored."""
    await sim_application.recover()

    response = await dashboard_observing.post(
        "/control/close", data={"basket_id": "demo", "instrument_key": "sim:BTC/USDT"}
    )

    assert response.status_code == 200
    assert "supervision is stopped" in response.text
    assert not risk_events(sim_application, "manual_close")


async def test_the_closable_list_offers_every_held_position(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/")).text
    assert {key for _, key in cycled.manual_close.closable()} == {"sim:BTC/USDT", "sim:ETH/USDT"}
    assert "sim:BTC/USDT" in body
    assert "sim:ETH/USDT" in body


async def test_nothing_is_closable_before_a_position_exists(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """No holding, no close button — the dock offers an act only where there is one to take."""
    assert sim_application.manual_close.closable() == ()
    assert '"/control/close"' not in (await client.get("/")).text


async def test_no_cycle_ever_records_a_stand_aside(cycled: Application) -> None:
    """The exemption has exactly one writer, and it is not the runner.

    Asserted from the log rather than by reading the code: if `BasketRunner` ever started
    flagging its proposals, every panel decision would quietly stop being metered.
    """
    cycle_checks = [
        check
        for event in events_of(cycled, EventType.RISK_CHECKED)
        if not event.cycle_id.startswith("manual-")
        for check in event.payload["checks"]
    ]

    assert cycle_checks, "the cycle recorded no risk checks; this test would pass vacuously"
    assert not [check for check in cycle_checks if check["detail"] == STOOD_ASIDE]


# ---------------------------------------------------------------- start / stop


async def test_stop_then_start_toggles_cycling(
    supervision: SupervisionController, client: httpx.AsyncClient
) -> None:
    """The GUI equivalent of `--observe`, and back again, without restarting the process."""
    assert (await client.post("/control/stop")).status_code == 303
    assert not supervision.running

    assert (await client.post("/control/start")).status_code == 303
    assert supervision.running


async def test_the_page_reports_what_the_controller_is_doing(
    supervision: SupervisionController, client: httpx.AsyncClient
) -> None:
    assert "Stop trading" in (await client.get("/")).text

    await supervision.stop()

    body = (await client.get("/")).text
    assert "Start trading" in body
    assert "not trading" in body


async def test_a_start_that_cannot_be_granted_says_why(
    sim_application: Application, supervision: SupervisionController, client: httpx.AsyncClient
) -> None:
    """A refused action re-renders the page with the reason and changes nothing."""
    await client.post("/control/stop")
    await sim_application.watchdog.trip("manual", "tripped by hand")

    response = await client.post("/control/start")

    assert response.status_code == 200
    assert "nothing was started" in response.text
    assert not supervision.running


async def test_stopping_warns_about_orders_left_working(
    cycled: Application, supervision: SupervisionController, client: httpx.AsyncClient
) -> None:
    """Stop is never refused, but the orders it leaves unpolled are named on the page."""
    assert Queries(cycled.store).open_orders(), "no order is working; this would pass vacuously"

    assert (await client.post("/control/stop")).status_code == 303
    body = (await client.get("/")).text

    assert "still working at the venue" in body
    assert not supervision.running


# ---------------------------------------------------------------- live arming


async def test_a_sim_process_has_nothing_to_arm(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    """Arming is per-database, and only the live one has anything to arm (ADR 0012)."""
    response = await client.post(
        "/control/live/arm", data={"confirm": LIVE_CONFIRMATION_PHRASE, "max_notional": "50"}
    )

    assert response.status_code == 200
    assert "live database only" in response.text
    assert not sim_application.arming.load().armed


async def test_the_arming_form_is_absent_outside_live(client: httpx.AsyncClient) -> None:
    assert "Live arming" not in (await client.get("/")).text
