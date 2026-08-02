"""Reaching live mode through the composition root (PLAN Phase 8, ADR 0020, ADR 0021).

`test_arming.py` proves the preconditions in isolation. This proves the *wiring* obeys them: that
nothing cycles until every one is satisfied, that what runs runs under the live ceiling, and that
the readiness gates exist in live and only in live.

Since ADR 0021 the gate is a **runtime** one. Wiring live produces a system that can be looked at;
`SupervisionController.start` is what decides whether it trades, and it decides again at every
start. So the refusals are asserted where they now live — on `live_permission`'s `unmet` tuple and
on the controller — rather than on `build` raising.

Nothing here reaches a network. Building a venue stack constructs transports; it does not call
them — the first call is the startup sequence's, which these tests deliberately do not run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from tests.conftest import DASHBOARD_TOKEN, publish_keyed_panel

from tradebot.app import (
    Application,
    BrokerChoice,
    _readiness_for,
    build_live,
    build_sim,
    database_path,
)
from tradebot.control.arming import LIVE_CONFIRMATION_PHRASE, ArmingStore
from tradebot.control.live import CEILED_FIELDS, LIVE_CEILING
from tradebot.control.supervision import SupervisionController
from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
from tradebot.dashboard.app import create_dashboard
from tradebot.persistence.database import SingleWriter, create_database

CAP = Decimal(50)

LIVE_KEYS = {
    "BINANCE_API_KEY": "a-live-key",
    "BINANCE_API_SECRET": "a-live-secret",
}


@pytest.fixture(autouse=True)
def no_ambient_live_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must behave identically on a machine that happens to hold live keys."""
    for name in LIVE_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def live_db(tmp_path: Path) -> Path:
    return database_path(Mode.LIVE, tmp_path)


async def arm(path: Path, clock: ManualClock, *, cap: Decimal = CAP) -> None:
    """Do what a human does with `risk arm-live`, against this mode's own database."""
    engine = create_database(path)
    writer = SingleWriter(engine)
    try:
        await ArmingStore(engine, writer, clock).arm(actor="test", max_live_notional=cap)
    finally:
        writer.close()


def with_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in LIVE_KEYS.items():
        monkeypatch.setenv(name, value)


async def _idle() -> None:
    """Stands in for `Supervisor.serve()`: these tests are about the gate, not the cycling."""
    await asyncio.Event().wait()


@pytest.fixture
async def live_application(
    live_db: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Application]:
    """A live system, wired and **unarmed** — what `serve --mode live` now comes up as."""
    with_keys(monkeypatch)
    application = await build_live(clock=clock, db_path=live_db, broker=BrokerChoice.BINANCE)
    yield application
    await application.shutdown()


class TestRefusals:
    """What live refuses, and where. Wiring refuses the incoherent; the gate refuses the unarmed."""

    async def test_no_confirmation_lists_every_unmet_precondition(
        self, live_application: Application
    ) -> None:
        """An operator fixing them one refusal at a time is one who stops reading."""
        unmet = "; ".join(live_application.live_permission(None).unmet)

        assert "typed confirmation" in unmet
        assert "armed row" in unmet
        assert "positive max_live_notional" in unmet

    async def test_the_phrase_alone_is_not_enough(self, live_application: Application) -> None:
        permission = live_application.live_permission(LIVE_CONFIRMATION_PHRASE)

        assert not permission.granted
        assert any("armed row" in reason for reason in permission.unmet)

    async def test_an_armed_database_without_keys_still_refuses(
        self, live_db: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live keys are read from live-only variable names, so a paper key cannot stand in.

        Asserted on the predicate rather than through the wiring, because a transport cannot be
        constructed without a key at all: the composition root refuses first, and no dashboard
        could have supplied one anyway (PLAN §3.2).
        """
        monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "a-testnet-key")
        monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "a-testnet-secret")
        await arm(live_db, clock)

        with pytest.raises(ConfigError, match="BINANCE_API_KEY"):
            await build_live(clock=clock, db_path=live_db, broker=BrokerChoice.BINANCE)

    async def test_the_simulated_broker_is_refused_outright(self, live_db: Path) -> None:
        """An order that is not sent to a venue is not a live order (PLAN §2.4)."""
        with pytest.raises(ConfigError, match="cannot use the simulated broker"):
            await build_live(db_path=live_db, broker=BrokerChoice.SIM)

    async def test_equities_cannot_go_live_for_want_of_a_market_data_provider(
        self, live_db: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Phase 3 delivered Binance spot data only. Feeding an equities basket crypto candles
        would produce indicators, decisions and orders that all look valid (ADR 0020)."""
        monkeypatch.setenv("ALPACA_KEY_ID", "a-live-key")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "a-live-secret")
        await arm(live_db, clock)

        with pytest.raises(ConfigError, match="equity market-data provider"):
            await build_live(clock=clock, db_path=live_db, broker=BrokerChoice.ALPACA)


class TestTheRuntimeGate:
    """Permission is decided when supervision starts, and decided again every time (ADR 0021)."""

    @pytest.fixture(autouse=True)
    async def recovered(self, live_application: Application) -> None:
        """Baselines the startup sequence establishes, so these assertions are about arming.

        A process that has not been through DESIGN §8.2 reads as never-armed and is refused for
        that reason alone — which `test_supervision.py` covers, and which would otherwise mask
        every arming assertion here.
        """
        await live_application.watchdog.rearm(Decimal(0), actor="test")

    async def test_an_unarmed_live_process_wires_but_does_not_trade(
        self, live_application: Application
    ) -> None:
        """The whole point of the change: something must exist for an operator to arm."""
        controller = SupervisionController(live_application)

        unmet = await controller.start(confirmation=LIVE_CONFIRMATION_PHRASE)

        assert not controller.running
        assert any("armed row" in reason for reason in unmet)

    async def test_arming_in_the_same_process_is_enough_to_start(
        self, live_application: Application, clock: ManualClock
    ) -> None:
        """No restart, no rebuild — the cap is read at the start, not closed over at boot."""
        controller = SupervisionController(live_application, serve=_idle)
        await live_application.arming.arm(actor="dashboard", max_live_notional=CAP)

        assert await controller.start(confirmation=LIVE_CONFIRMATION_PHRASE) == ()
        assert controller.running
        await controller.stop()

    async def test_the_phrase_is_demanded_at_every_start(
        self, live_application: Application
    ) -> None:
        """An armed database alone must never be enough to start (ADR 0012)."""
        await live_application.arming.arm(actor="dashboard", max_live_notional=CAP)
        controller = SupervisionController(live_application, serve=_idle)

        unmet = await controller.start(confirmation="i accept real money risk")

        assert not controller.running
        assert any("typed confirmation" in reason for reason in unmet)

    async def test_a_cap_armed_after_boot_becomes_the_limit_in_force(
        self, live_application: Application
    ) -> None:
        """The wiring, each runner rebuild and `risk status` read one function, so a cap armed
        mid-process cannot be enforced by one of them and reported by another (ADR 0021)."""
        assert live_application.policy.policy.max_order_notional is None

        await live_application.arming.arm(actor="dashboard", max_live_notional=CAP)

        assert live_application.policy.policy.max_order_notional == CAP

    async def test_disarming_revokes_the_permission_a_later_start_needs(
        self, live_application: Application
    ) -> None:
        await live_application.arming.arm(actor="dashboard", max_live_notional=CAP)
        await live_application.arming.disarm(actor="dashboard", reason="done for the day")
        controller = SupervisionController(live_application, serve=_idle)

        unmet = await controller.start(confirmation=LIVE_CONFIRMATION_PHRASE)

        assert not controller.running
        assert any("armed row" in reason for reason in unmet)

    async def test_a_panel_with_no_key_refuses_every_start(
        self, live_application: Application, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sim and paper run degraded on this and only warn; live never does (ADR 0023).

        Checked at each Start rather than trusted from the startup gate, because a panel edited
        while the process was stopped would otherwise be started against a check that ran on the
        version before it.
        """
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        await publish_keyed_panel(live_application)
        await live_application.arming.arm(actor="dashboard", max_live_notional=CAP)
        controller = SupervisionController(live_application, serve=_idle)

        unmet = await controller.start(confirmation=LIVE_CONFIRMATION_PHRASE)

        assert not controller.running
        assert any("OPENROUTER_API_KEY" in reason for reason in unmet)

    async def test_supplying_the_key_clears_that_refusal(
        self, live_application: Application, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-configured")
        await publish_keyed_panel(live_application)
        await live_application.arming.arm(actor="dashboard", max_live_notional=CAP)
        controller = SupervisionController(live_application, serve=_idle)

        assert await controller.start(confirmation=LIVE_CONFIRMATION_PHRASE) == ()
        await controller.stop()


class TestWiredLive:
    """Live *is* reachable — by someone who satisfied all four preconditions, and only then."""

    @pytest.fixture
    async def application(
        self, live_db: Path, clock: ManualClock, live_application: Application
    ) -> Application:
        await arm(live_db, clock)
        return live_application

    async def test_it_builds_in_live_mode(self, application: Application) -> None:
        assert application.mode is Mode.LIVE

    async def test_every_tier2_limit_is_clamped_to_the_ceiling(
        self, application: Application
    ) -> None:
        policy = application.policy.policy
        for field in CEILED_FIELDS:
            assert getattr(policy, field) == getattr(LIVE_CEILING, field), field

    async def test_the_arming_cap_becomes_the_order_notional_limit(
        self, application: Application
    ) -> None:
        assert application.policy.policy.max_order_notional == CAP

    async def test_the_clamp_is_written_when_the_limits_take_effect(
        self, application: Application
    ) -> None:
        """ "What were the limits at 04:12" must be answerable from the log alone (PLAN §3.3).

        Written at the start rather than at the wiring, because the cap is only known then: an
        event recorded at boot would name limits a later arming had already changed.
        """
        assert not _clamps(application)

        await application.record_limits()

        clamped = _clamps(application)
        assert len(clamped) == 1
        assert "max_drawdown_pct" in str(clamped[0].payload["detail"])

    async def test_the_arming_row_is_readable_for_risk_status(
        self, application: Application
    ) -> None:
        arming = application.arming.load()
        assert arming.armed
        assert arming.max_live_notional == CAP


def _clamps(application: Application) -> list[object]:
    return [e for e in application.store.read_all() if e.payload.get("rule") == "live_ceiling"]


class TestOtherModesAreUnaffected:
    async def test_sim_runs_the_published_policy_unclamped(self, clock: ManualClock) -> None:
        """A ceiling in sim would make the soak evidence about limits live would not use."""
        application = await build_sim(clock=clock, db_path=None)
        try:
            assert application.policy.clamps == ()
            assert application.policy.policy == GlobalRiskPolicy()
        finally:
            await application.shutdown()

    @pytest.mark.parametrize(
        ("mode", "gated"), [(Mode.SIM, False), (Mode.PAPER, False), (Mode.LIVE, True)]
    )
    def test_the_readiness_gates_exist_in_live_and_only_in_live(
        self, mode: Mode, gated: bool, clock: ManualClock
    ) -> None:
        """The one branch deciding whether the gates run at all.

        Sim and paper are *allowed* to run degraded — an unreachable panel is a `WAIT` and a holed
        series is a `DATA_STALE` cycle. Live is the mode where the same fault holds real positions.
        """
        gates = _readiness_for(
            mode,
            configs=object(),  # type: ignore[arg-type]
            factory=SimpleNamespace(probe=object()),  # type: ignore[arg-type]
            stack=SimpleNamespace(  # type: ignore[arg-type]
                prices=object(), broker=SimpleNamespace(venue_id="binance")
            ),
            ledger=object(),  # type: ignore[arg-type]
            clock=clock,
            alert_sinks=(),
        )
        assert (gates is not None) is gated


class TestTheLiveControlPage:
    """Arming and starting from the GUI — the capability ADR 0021 exists to deliver.

    The phrase is typed into both forms, every time. Nothing here reads it back out of a session:
    an armed database alone must never be enough to start (ADR 0012).
    """

    @pytest.fixture
    async def controller(self, live_application: Application) -> SupervisionController:
        await live_application.watchdog.rearm(Decimal(0), actor="test")
        return SupervisionController(live_application, serve=_idle)

    @pytest.fixture
    async def client(
        self, live_application: Application, controller: SupervisionController
    ) -> AsyncIterator[httpx.AsyncClient]:
        dashboard = create_dashboard(live_application, token=DASHBOARD_TOKEN, controller=controller)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=dashboard), base_url="http://dashboard"
        ) as connected:
            await connected.post("/login", data={"token": DASHBOARD_TOKEN})
            yield connected

    async def test_an_unarmed_live_dashboard_serves_and_says_so(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/control")).text

        assert "not armed" in body
        assert "Arm live trading" in body
        assert "not trading" in body

    async def test_arming_from_the_gui_records_the_cap_and_the_actor(
        self, live_application: Application, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/control/live/arm",
            data={"confirm": LIVE_CONFIRMATION_PHRASE, "max_notional": "50", "note": "first live"},
        )

        assert response.status_code == 303
        arming = live_application.arming.load()
        assert arming.armed
        assert arming.max_live_notional == CAP
        assert arming.armed_by == "dashboard"

    async def test_arming_without_the_phrase_changes_nothing(
        self, live_application: Application, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/control/live/arm", data={"confirm": "please", "max_notional": "50"}
        )

        assert response.status_code == 200
        assert not live_application.arming.load().armed

    async def test_a_cap_that_is_not_a_number_is_refused_rather_than_raising(
        self, live_application: Application, client: httpx.AsyncClient
    ) -> None:
        response = await client.post(
            "/control/live/arm",
            data={"confirm": LIVE_CONFIRMATION_PHRASE, "max_notional": "fifty"},
        )

        assert response.status_code == 200
        assert not live_application.arming.load().armed

    async def test_arm_then_start_from_the_gui(
        self, controller: SupervisionController, client: httpx.AsyncClient
    ) -> None:
        """The walkthrough: unarmed process → armed → trading, without a restart."""
        await client.post(
            "/control/live/arm",
            data={"confirm": LIVE_CONFIRMATION_PHRASE, "max_notional": "50"},
        )

        response = await client.post("/control/start", data={"confirm": LIVE_CONFIRMATION_PHRASE})

        assert response.status_code == 303
        assert controller.running
        await controller.stop()

    async def test_starting_without_the_phrase_is_refused_even_when_armed(
        self, controller: SupervisionController, client: httpx.AsyncClient
    ) -> None:
        await client.post(
            "/control/live/arm",
            data={"confirm": LIVE_CONFIRMATION_PHRASE, "max_notional": "50"},
        )

        response = await client.post("/control/start")

        assert response.status_code == 200
        assert "typed confirmation" in response.text
        assert not controller.running

    async def test_disarming_also_stops_supervision(
        self,
        live_application: Application,
        controller: SupervisionController,
        client: httpx.AsyncClient,
    ) -> None:
        """The deliberate divergence from the CLI: a basket must never keep cycling against a cap
        that has just been revoked (ADR 0021)."""
        await client.post(
            "/control/live/arm",
            data={"confirm": LIVE_CONFIRMATION_PHRASE, "max_notional": "50"},
        )
        await client.post("/control/start", data={"confirm": LIVE_CONFIRMATION_PHRASE})
        assert controller.running

        response = await client.post("/control/live/disarm", data={"reason": "done for the day"})

        assert response.status_code == 303
        assert not controller.running
        assert not live_application.arming.load().armed
