"""Reaching live mode, and every way it must refuse (PLAN §2.4).

This is the phase's other exit criterion: *the process refuses to start on every one of the §2.4
missing-precondition cases*. Each one is asserted separately, and then together — because the
failure this guards against is not one missing check, it is an operator who satisfied three
conditions and assumed the fourth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradebot.control.arming import (
    LIVE_CONFIRMATION_PHRASE,
    ArmingStore,
    LiveArming,
    assert_live_confirmation,
    assert_live_preconditions,
    capped,
)
from tradebot.core.clock import ManualClock
from tradebot.core.config import GlobalRiskPolicy
from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError
from tradebot.core.money import ZERO
from tradebot.persistence.database import SingleWriter, create_database

CAP = Decimal(500)
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(clock: ManualClock, tmp_path: Path) -> ArmingStore:
    engine = create_database(tmp_path / "live.db")
    return ArmingStore(engine, SingleWriter(engine), clock)


def armed(**overrides: object) -> LiveArming:
    base: dict[str, object] = {
        "armed": True,
        "max_live_notional": CAP,
        "armed_by": "human",
        "updated_at": NOW,
    }
    return LiveArming(**{**base, **overrides})  # type: ignore[arg-type]


class TestPreconditions:
    def test_everything_present_returns_the_cap_to_enforce(self) -> None:
        cap = assert_live_preconditions(
            Mode.LIVE, confirmation=LIVE_CONFIRMATION_PHRASE, arming=armed(), credentials=True
        )
        assert cap == CAP

    def test_a_missing_confirmation_refuses(self) -> None:
        with pytest.raises(ConfigError, match="typed confirmation"):
            assert_live_preconditions(
                Mode.LIVE, confirmation=None, arming=armed(), credentials=True
            )

    def test_a_wrong_confirmation_refuses(self) -> None:
        with pytest.raises(ConfigError, match="typed confirmation"):
            assert_live_preconditions(
                Mode.LIVE, confirmation="i accept real money risk", arming=armed(), credentials=True
            )

    def test_an_unarmed_database_refuses(self) -> None:
        with pytest.raises(ConfigError, match="armed row"):
            assert_live_preconditions(
                Mode.LIVE,
                confirmation=LIVE_CONFIRMATION_PHRASE,
                arming=armed(armed=False),
                credentials=True,
            )

    def test_a_missing_cap_refuses(self) -> None:
        """An absent cap is not a permissive one: nobody chose "unlimited"."""
        with pytest.raises(ConfigError, match="max_live_notional"):
            assert_live_preconditions(
                Mode.LIVE,
                confirmation=LIVE_CONFIRMATION_PHRASE,
                arming=armed(max_live_notional=None),
                credentials=True,
            )

    def test_missing_credentials_refuse(self) -> None:
        with pytest.raises(ConfigError, match="credentials"):
            assert_live_preconditions(
                Mode.LIVE,
                confirmation=LIVE_CONFIRMATION_PHRASE,
                arming=armed(),
                credentials=False,
            )

    def test_every_unmet_precondition_is_listed_at_once(self) -> None:
        """An operator fixing them one refusal at a time is an operator who stops reading."""
        with pytest.raises(ConfigError) as raised:
            assert_live_preconditions(
                Mode.LIVE,
                confirmation=None,
                arming=LiveArming(updated_at=NOW),
                credentials=False,
            )
        message = str(raised.value)
        assert "typed confirmation" in message
        assert "armed row" in message
        assert "max_live_notional" in message
        assert "credentials" in message

    @pytest.mark.parametrize("mode", [Mode.SIM, Mode.PAPER])
    def test_other_modes_assert_nothing_and_get_no_cap(self, mode: Mode) -> None:
        """The gates exist only on the one path that can lose real money."""
        assert (
            assert_live_preconditions(
                mode,
                confirmation=None,
                arming=LiveArming(updated_at=NOW),
                credentials=False,
            )
            == ZERO
        )


class TestArmingStore:
    def test_an_absent_row_is_not_armed(self, store: ArmingStore) -> None:
        """The safe default, and the state of every database that has never been armed."""
        arming = store.load()
        assert not arming.armed
        assert not arming.ready

    async def test_arming_records_the_cap_and_the_actor(self, store: ArmingStore) -> None:
        await store.arm(actor="nik", max_live_notional=CAP, note="first live test")
        arming = store.load()
        assert arming.armed
        assert arming.max_live_notional == CAP
        assert arming.armed_by == "nik"
        assert arming.ready

    async def test_a_non_positive_cap_is_refused_at_arming_time(self, store: ArmingStore) -> None:
        """A zero cap would arm live and veto everything, which reads as a bug, not a decision."""
        with pytest.raises(ConfigError, match="must be positive"):
            await store.arm(actor="nik", max_live_notional=ZERO)

    async def test_disarming_survives_a_reload(self, store: ArmingStore) -> None:
        await store.arm(actor="nik", max_live_notional=CAP)
        await store.disarm(actor="nik", reason="done testing")
        arming = store.load()
        assert not arming.armed
        assert not arming.ready
        # The cap is kept, so re-arming shows what it was.
        assert arming.max_live_notional == CAP

    async def test_arming_survives_a_restart(self, clock: ManualClock, tmp_path: Path) -> None:
        """A row is the whole point: the other preconditions are transient by design."""
        path = tmp_path / "live.db"
        engine = create_database(path)
        writer = SingleWriter(engine)
        await ArmingStore(engine, writer, clock).arm(actor="nik", max_live_notional=CAP)
        writer.close()

        reopened = create_database(path)
        assert ArmingStore(reopened, SingleWriter(reopened), clock).load().ready


class TestCapApplication:
    def test_the_cap_becomes_a_tier2_limit(self) -> None:
        """Enforced by the same rule every other mode exercises, not a live-only branch."""
        policy = capped(GlobalRiskPolicy(), CAP)
        assert policy.max_order_notional == CAP

    def test_no_cap_leaves_the_policy_untouched(self) -> None:
        policy = GlobalRiskPolicy()
        assert capped(policy, ZERO) is policy

    def test_a_configured_cap_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="max_order_notional"):
            GlobalRiskPolicy(max_order_notional=Decimal(0))


class TestConfirmationPhrase:
    def test_the_exact_phrase_is_required(self) -> None:
        assert_live_confirmation(LIVE_CONFIRMATION_PHRASE)

    @pytest.mark.parametrize("phrase", [None, "", "yes", "i accept real money risk"])
    def test_anything_else_is_refused(self, phrase: str | None) -> None:
        with pytest.raises(ConfigError, match="exact phrase"):
            assert_live_confirmation(phrase)

    def test_it_is_not_the_rearm_phrase(self) -> None:
        """Two different authorisations; one muscle-memorised phrase should not do both."""
        from tradebot.risk.state import REARM_PHRASE

        assert LIVE_CONFIRMATION_PHRASE != REARM_PHRASE
