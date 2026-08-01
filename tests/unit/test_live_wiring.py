"""Reaching live mode through the composition root (PLAN Phase 8, ADR 0020).

`test_arming.py` proves the preconditions in isolation. This proves the *wiring* obeys them: that
`build` cannot produce a live application until every one is satisfied, that what it produces runs
under the live ceiling, and that the readiness gates exist in live and only in live.

Nothing here reaches a network. Building a venue stack constructs transports; it does not call
them — the first call is the startup sequence's, which these tests deliberately do not run.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradebot.app import (
    Application,
    BrokerChoice,
    _readiness_for,
    build,
    build_live,
    build_sim,
    database_path,
)
from tradebot.control.arming import LIVE_CONFIRMATION_PHRASE, ArmingStore
from tradebot.control.live import CEILED_FIELDS, LIVE_CEILING
from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
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


class TestRefusals:
    async def test_no_confirmation_lists_every_unmet_precondition(self, live_db: Path) -> None:
        """An operator fixing them one refusal at a time is one who stops reading."""
        with pytest.raises(ConfigError) as refusal:
            await build(Mode.LIVE, db_path=live_db, broker=BrokerChoice.BINANCE)

        message = str(refusal.value)
        assert "typed confirmation" in message
        assert "armed row" in message
        assert "credentials" in message

    async def test_the_phrase_alone_is_not_enough(self, live_db: Path) -> None:
        with pytest.raises(ConfigError, match="armed row"):
            await build(
                Mode.LIVE,
                confirmation=LIVE_CONFIRMATION_PHRASE,
                db_path=live_db,
                broker=BrokerChoice.BINANCE,
            )

    async def test_an_armed_database_without_keys_still_refuses(
        self, live_db: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Live keys are read from live-only variable names, so a paper key cannot stand in."""
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
        monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "a-testnet-key")
        monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "a-testnet-secret")
        await arm(live_db, clock)

        with pytest.raises(ConfigError, match="credentials"):
            await build(
                Mode.LIVE,
                confirmation=LIVE_CONFIRMATION_PHRASE,
                db_path=live_db,
                broker=BrokerChoice.BINANCE,
            )

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
            await build_live(
                clock=clock,
                db_path=live_db,
                broker=BrokerChoice.ALPACA,
                confirmation=LIVE_CONFIRMATION_PHRASE,
            )

    async def test_the_refusal_precedes_every_other_precondition(self, live_db: Path) -> None:
        """Asked for something incoherent, live says so rather than reporting a missing key."""
        with pytest.raises(ConfigError, match="cannot use the simulated broker"):
            await build_live(
                db_path=live_db, broker=BrokerChoice.SIM, confirmation=LIVE_CONFIRMATION_PHRASE
            )


class TestWiredLive:
    """Live *is* reachable — by someone who satisfied all four preconditions, and only then."""

    @pytest.fixture
    async def application(
        self, live_db: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
    ) -> Application:
        with_keys(monkeypatch)
        await arm(live_db, clock)
        return await build_live(
            clock=clock,
            db_path=live_db,
            broker=BrokerChoice.BINANCE,
            confirmation=LIVE_CONFIRMATION_PHRASE,
        )

    async def test_it_builds_in_live_mode(self, application: Application) -> None:
        assert application.mode is Mode.LIVE
        await application.shutdown()

    async def test_every_tier2_limit_is_clamped_to_the_ceiling(
        self, application: Application
    ) -> None:
        policy = application.policy.policy
        for field in CEILED_FIELDS:
            assert getattr(policy, field) == getattr(LIVE_CEILING, field), field
        await application.shutdown()

    async def test_the_arming_cap_becomes_the_order_notional_limit(
        self, application: Application
    ) -> None:
        assert application.policy.policy.max_order_notional == CAP
        await application.shutdown()

    async def test_the_clamp_is_written_to_the_event_log(self, application: Application) -> None:
        """ "What were the limits at 04:12" must be answerable from the log alone (PLAN §3.3)."""
        clamped = [
            event
            for event in application.store.read_all()
            if event.payload.get("rule") == "live_ceiling"
        ]
        assert len(clamped) == 1
        assert "max_drawdown_pct" in str(clamped[0].payload["detail"])
        await application.shutdown()

    async def test_the_arming_row_is_readable_for_risk_status(
        self, application: Application
    ) -> None:
        arming = application.arming.load()
        assert arming.armed
        assert arming.max_live_notional == CAP
        await application.shutdown()


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
