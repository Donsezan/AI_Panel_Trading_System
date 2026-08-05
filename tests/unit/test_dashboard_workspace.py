"""The blotter workspace, driven over real HTTP against a real wired sim application.

Rendered rather than mocked, for the reason the Analytics suite gives: a template naming a column
the projection does not have fails on the screen an operator reads during an incident, and only
rendering it catches that.

The assertions that carry weight beyond the pass:

* **A chart request must not move the venue.** In sim and in the primary paper wiring the price
  provider a cycle reads is a *bridge* into `SimBroker`, so an observer reading it would match
  resting orders and set the reference price of the next market order. Looking at a screen may
  never do that.
* **A failed pane is a failed pane.** A chart that cannot be built answers with the reason.
* **Selection lives in the URL**, so the same scope renders the same view however it was reached.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from tradebot.app import Application
from tradebot.core.clock import ManualClock
from tradebot.core.enums import CycleOutcome, OrderRole, OrderState, OrderType, Side
from tradebot.core.errors import SubmitUnknownError
from tradebot.core.instrument import Instrument
from tradebot.core.orders import OrderIntent
from tradebot.dashboard.dock import KILL_PHRASE
from tradebot.dashboard.routes.workspace import CHART_BARS
from tradebot.dashboard.updates import Pane
from tradebot.dashboard.views import PACKAGE
from tradebot.execution.brokers.sim import SimBroker, SimulatedMarket
from tradebot.marketdata.replay import ReplayMarketData

PANES = [
    "/workspace/portfolio",
    "/workspace/blotter",
    "/workspace/log",
    "/workspace/controls",
    "/workspace/rc",
]

BASKET_SCOPE = "basket:demo"
INSTRUMENT = "sim:BTC/USDT"
INSTRUMENT_SCOPE = f"instrument:demo:{INSTRUMENT}"


@pytest.fixture
async def cycled(sim_application: Application) -> Application:
    """One completed cycle, so every pane has something real to render."""
    await sim_application.recover()
    results = await sim_application.supervisor.run_once()
    assert results and results[0].outcome is CycleOutcome.ORDERS_PLACED
    return sim_application


async def chart_of(client: httpx.AsyncClient, scope: str, **params: str) -> httpx.Response:
    return await client.get("/workspace/chart/data", params={"scope": scope, **params})


# ---------------------------------------------------------------- the page


async def test_the_workspace_renders_on_a_system_that_never_cycled(
    client: httpx.AsyncClient,
) -> None:
    """Empty is not the same as broken: a fresh install opens on this screen."""
    response = await client.get("/")
    assert response.status_code == 200
    for pane in ("portfolio", "blotter", "chart", "log", "controls", "rc"):
        assert f"pane-{pane}" in response.text


async def test_the_workspace_renders_after_a_cycle(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get("/")).text
    assert "Demo crypto basket" in body
    assert INSTRUMENT in body


async def test_the_mode_banner_is_on_the_workspace_too(client: httpx.AsyncClient) -> None:
    """Mode confusion is a catastrophic failure class, so it is on the screen that is left open."""
    assert "mode-sim" in (await client.get("/")).text


@pytest.mark.parametrize("pane", PANES)
async def test_each_pane_renders_alone(
    cycled: Application, client: httpx.AsyncClient, pane: str
) -> None:
    """A partial is a fragment, not a page: htmx swaps it into a document that already exists."""
    response = await client.get(pane)
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<section")
    assert "<!doctype" not in response.text.lower()


@pytest.mark.parametrize("pane", PANES)
async def test_every_pane_can_be_refreshed_by_name(client: httpx.AsyncClient, pane: str) -> None:
    """The socket carries pane names; each has to name itself back for the nudge to land."""
    body = (await client.get(pane)).text
    assert 'data-pane="' in body
    assert 'hx-trigger="refresh"' in body


async def test_every_pane_the_socket_can_name_is_on_the_screen(client: httpx.AsyncClient) -> None:
    """`Pane` is a wire contract (ADR 0024). A name nothing answers to is a silent dead notice —
    the event fires, the tail sends it, and the region it was meant to refresh goes stale."""
    body = (await client.get("/")).text

    for pane in Pane:
        assert f'data-pane="{pane.value}"' in body, pane


async def test_the_chart_pane_is_not_swapped_by_htmx(client: httpx.AsyncClient) -> None:
    """Replacing the canvas on every notice would tear down a live chart once a second."""
    body = (await client.get(f"/?scope={BASKET_SCOPE}")).text
    chart_pane = body.split('id="pane-chart"')[1].split(">")[0]
    assert "hx-get" not in chart_pane


# ---------------------------------------------------------------- the layout

#: Every draggable boundary the page publishes, in document order.
SPLITTERS = re.compile(r'data-resize="([^"]+)"')


async def test_every_boundary_between_neighbouring_panes_is_draggable(
    client: httpx.AsyncClient,
) -> None:
    """Stacked columns rather than one grid: pulling the blotter taller must not move the chart."""
    body = (await client.get("/")).text

    assert SPLITTERS.findall(body) == [
        "portfolio:blotter",
        "blotter:log",
        "left:right",
        "chart:controls",
        "controls:rc",
    ]


async def test_a_splitter_only_names_regions_that_are_on_the_screen(
    client: httpx.AsyncClient,
) -> None:
    """A handle naming a region the page does not have is a handle that silently does nothing."""
    body = (await client.get("/")).text

    for pair in SPLITTERS.findall(body):
        for name in pair.split(":"):
            assert f'data-pane="{name}"' in body or f'data-column="{name}"' in body, name


async def test_every_resizable_region_is_defaulted_and_used_by_the_stylesheet(
    client: httpx.AsyncClient,
) -> None:
    """The three-way contract between the markup, the script and the stylesheet.

    The script writes `--size-<name>`; the stylesheet has to both default it and consume it. Miss
    the default and a browser that never ran the script gets a collapsed pane; miss the use and a
    drag writes a variable nothing reads, which looks exactly like a broken handle.
    """
    body = (await client.get("/")).text
    css = (PACKAGE / "static" / "app.css").read_text(encoding="utf-8")

    for pair in SPLITTERS.findall(body):
        for name in pair.split(":"):
            assert f"--size-{name}:" in css, name
            assert f"var(--size-{name})" in css, name


# ---------------------------------------------------------------- selection


async def test_a_basket_scope_selects_its_row(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get(f"/?scope={BASKET_SCOPE}")).text
    assert 'class="basket-row selected"' in body


async def test_an_instrument_scope_selects_the_instrument_not_its_basket(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    body = (await client.get(f"/?scope={INSTRUMENT_SCOPE}")).text
    assert 'class="instrument-row selected"' in body
    assert 'class="basket-row selected"' not in body


async def test_a_basket_scope_charts_every_instrument_it_holds(
    client: httpx.AsyncClient,
) -> None:
    """A small-multiple stack, never an overlay: mixed quote currencies make one axis a lie."""
    body = (await client.get(f"/?scope={BASKET_SCOPE}")).text
    assert body.count("data-chart") == 2


async def test_an_instrument_scope_charts_only_that_instrument(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get(f"/?scope={INSTRUMENT_SCOPE}")).text
    assert body.count("data-chart") == 1


async def test_no_selection_charts_nothing_and_says_so(client: httpx.AsyncClient) -> None:
    body = (await client.get("/")).text
    assert "data-chart" not in body
    assert "Select a basket" in body


@pytest.mark.parametrize(
    "scope", ["", "nonsense", "basket:", "instrument:demo", "instrument::sim:BTC/USDT"]
)
async def test_an_unreadable_scope_is_no_selection_rather_than_an_error(
    client: httpx.AsyncClient, scope: str
) -> None:
    """A hand-edited URL degrades to the unfiltered view; it never guesses and never 500s."""
    response = await client.get("/", params={"scope": scope})
    assert response.status_code == 200
    assert "selected" not in response.text


async def test_the_log_narrows_to_the_selection(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    scoped = await client.get("/workspace/log", params={"scope": "basket:nonexistent"})
    assert "Nothing has cycled yet" in scoped.text
    assert "Nothing has cycled yet" not in (await client.get("/workspace/log")).text


async def test_a_log_row_links_to_its_drill_down(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """The research artifact is one click from the operation log (PHASE_10 decision 3)."""
    assert 'href="/cycles/' in (await client.get("/workspace/log")).text


# ---------------------------------------------------------------- the control dock


async def test_the_dock_narrows_to_the_selection(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Selection drives the acts on offer, exactly as it drives the chart and the log."""
    scoped = await client.get("/workspace/controls", params={"scope": "basket:demo"})
    assert "demo" in scoped.text

    elsewhere = await client.get("/workspace/controls", params={"scope": "basket:nonexistent"})
    assert "names no basket in service" in elsewhere.text


async def test_every_dock_form_carries_the_selection_back(client: httpx.AsyncClient) -> None:
    """Every act posts the scope it was taken from, so a refusal re-renders the same view."""
    body = (await client.get("/workspace/controls", params={"scope": BASKET_SCOPE})).text

    assert body.count(f'name="scope" value="{BASKET_SCOPE}"') == body.count("<form")


async def test_the_phrase_is_typed_and_never_offered(client: httpx.AsyncClient) -> None:
    """A phrase in a `value` would be a phrase the operator did not type (PHASE_10 decision 5)."""
    body = (await client.get("/workspace/controls")).text

    assert KILL_PHRASE in body
    assert f'value="{KILL_PHRASE}"' not in body


async def test_a_stopped_process_offers_no_close(
    cycled: Application, dashboard_observing: httpx.AsyncClient
) -> None:
    """Nothing is polling open orders, so an order placed now would rest unmonitored."""
    body = (await dashboard_observing.get("/workspace/controls")).text

    assert "/control/close" in body
    assert "disabled" in body


# ---------------------------------------------------------------- the risk-control pane


async def test_the_rc_pane_lists_a_quarantine_in_force_and_offers_release(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    await client.post(
        "/control/baskets/demo/quarantine", data={"instrument_key": "", "excluded": "true"}
    )

    body = (await client.get("/workspace/rc")).text

    assert "whole basket" in body
    assert "release" in body


async def test_the_rc_pane_offers_re_arming_only_once_the_switch_has_tripped(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    assert "Re-arm trading" not in (await client.get("/workspace/rc")).text

    await sim_application.watchdog.trip("test", "for the test")

    assert "Re-arm trading" in (await client.get("/workspace/rc")).text


async def test_the_rc_pane_offers_un_halting_only_a_halted_basket(
    sim_application: Application, client: httpx.AsyncClient
) -> None:
    assert "Un-halt" not in (await client.get("/workspace/rc")).text

    await sim_application.watchdog.halt_basket("demo", "three consecutive failed cycles")

    body = (await client.get("/workspace/rc")).text
    assert "Un-halt demo" in body
    assert "three consecutive failed cycles" in body


# ---------------------------------------------------------------- acting from the workspace


async def test_a_refused_action_re_renders_the_workspace_with_the_reason(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """An operator mid-incident acts on what they can see, so a refusal keeps the screen."""
    response = await client.post("/control/kill", data={"confirm": "no", "scope": INSTRUMENT_SCOPE})

    assert response.status_code == 200
    assert "requires the phrase" in response.text
    assert 'class="instrument-row selected"' in response.text


async def test_an_action_returns_to_the_selection_it_was_taken_from(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/control/stop", data={"scope": INSTRUMENT_SCOPE, "tf": "4h"})

    assert response.status_code == 303
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query == {"scope": [INSTRUMENT_SCOPE], "tf": ["4h"]}


async def test_an_action_taken_with_nothing_selected_returns_to_the_whole_screen(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/control/stop", data={"scope": ""})

    assert response.headers["location"] == "/"


# ---------------------------------------------------------------- the chart data route


async def test_the_chart_serves_candles_and_marks(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    payload = (await chart_of(client, INSTRUMENT_SCOPE, tf="1h")).json()
    assert payload["instrument_key"] == INSTRUMENT
    assert len(payload["candles"]) == CHART_BARS
    assert payload["markers"], "a cycle that placed an order must leave a mark"


async def test_the_chart_labels_carry_exact_decimals(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """Coordinates may cross to float; anything a human reads may not (PHASE_10 decision 6)."""
    payload = (await chart_of(client, INSTRUMENT_SCOPE)).json()
    filled = [m for m in payload["markers"] if m["text"].startswith("filled")]
    assert filled
    quantity = filled[0]["text"].split()[1]
    assert Decimal(quantity) > 0


async def test_the_dashboards_price_source_is_not_the_venue_bridge(
    sim_application: Application,
) -> None:
    """Asserted on the wiring, so no later refactor can quietly hand the dashboard the bridge."""
    assert sim_application.market_data is not None
    assert not isinstance(sim_application.market_data, SimulatedMarket)


async def test_reading_the_bridge_moves_the_simulated_venue(
    clock: ManualClock, instrument: Instrument, market_data: ReplayMarketData
) -> None:
    """The hazard itself, stated: a cycle's read *is* how the simulated venue learns the price.

    Which is correct for a cycle and unacceptable for an observer — hence the two fields on
    `VenueStack`. A market order needs a reference price, so submitting one is the public probe
    for whether the venue has been told anything.
    """
    broker = SimBroker(clock, balances={"USDT": Decimal(100_000)})
    bridge = SimulatedMarket(market_data, broker)

    with pytest.raises(SubmitUnknownError):
        await broker.submit(_market_order(instrument, clock.now(), "sim-before"))

    await bridge.get_candles(instrument, "1h", 10)
    ack = await broker.submit(_market_order(instrument, clock.now(), "sim-after"))
    assert ack.state is OrderState.FILLED


async def test_reading_the_observers_source_leaves_the_venue_untold(
    clock: ManualClock, instrument: Instrument, market_data: ReplayMarketData
) -> None:
    """What the chart route actually reads. A chart on the 1d timeframe must not become the
    price a manual close fills at."""
    broker = SimBroker(clock, balances={"USDT": Decimal(100_000)})
    SimulatedMarket(market_data, broker)  # wired, but read around

    for timeframe in ("1h", "4h", "1d"):
        await market_data.get_candles(instrument, timeframe, 10)

    with pytest.raises(SubmitUnknownError):
        await broker.submit(_market_order(instrument, clock.now(), "sim-probe"))


def _market_order(instrument: Instrument, at: datetime, client_order_id: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        basket_id="demo",
        cycle_id="probe",
        instrument_key=instrument.key,
        side=Side.BUY,
        qty=Decimal("0.001"),
        order_type=OrderType.MARKET,
        role=OrderRole.ENTRY,
        created_at=at,
    )


async def test_a_scope_naming_no_instrument_is_a_stated_failure(
    client: httpx.AsyncClient,
) -> None:
    response = await chart_of(client, BASKET_SCOPE)
    assert response.status_code == 503
    assert "names no instrument" in response.json()["error"]


async def test_an_instrument_this_process_does_not_trade_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """Checked against configuration, not trusted from the URL: a chart is a venue read."""
    response = await chart_of(client, "instrument:demo:sim:DOGE/USDT")
    assert response.status_code == 503


async def test_a_failed_chart_renders_as_a_failure_not_a_spinner(
    client: httpx.AsyncClient, sim_application: Application
) -> None:
    """A stated failure is information; a pane that never resolves is not."""
    sim_application.market_data = None
    response = await chart_of(client, INSTRUMENT_SCOPE)
    assert response.status_code == 503
    assert "no market-data provider" in response.json()["error"]


# ---------------------------------------------------------------- the timeframe


async def test_the_selector_offers_only_what_the_provider_serves(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/")).text
    assert ">1h<" in body and ">1d<" in body


async def test_an_unoffered_timeframe_falls_back_rather_than_refusing(
    cycled: Application, client: httpx.AsyncClient
) -> None:
    """A display preference degrades; it is not a limit, and refusing would be theatre."""
    response = await chart_of(client, INSTRUMENT_SCOPE, tf="7y")
    assert response.status_code == 200
    assert response.json()["timeframe"] == "1h"


async def test_the_timeframe_survives_a_pane_refresh(client: httpx.AsyncClient) -> None:
    """A refreshed blotter row must still link to the chart the operator is looking at."""
    body = (await client.get("/workspace/blotter", params={"scope": BASKET_SCOPE, "tf": "4h"})).text
    assert "tf=4h" in body
