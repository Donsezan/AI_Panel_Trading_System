"""The retention windows are configuration, not constants (spec §3.7).

The cross-field rule is the one that matters: inverted windows would make a day deletable before
it was ever archived, and every pass would rewrite and re-delete the same file forever.

An absent document means the model's *defaults*, never a refusal. Maintenance shares its tick with
the daily backup, and refusing to back anything up because nobody published a retention policy
would be fail-*useless* — so `MaintenancePolicy()` has to be constructible with no arguments and
mean something sensible.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradebot.control.config_store import DOCUMENTS, SINGLETON_ID, ConfigStore
from tradebot.core.clock import ManualClock
from tradebot.core.config import MaintenancePolicy
from tradebot.core.enums import ConfigKind
from tradebot.persistence.store import EventStore


@pytest.fixture
def configs(store: EventStore, clock: ManualClock) -> ConfigStore:
    return ConfigStore(store.engine, store._writer, store, clock)


class TestDefaults:
    def test_the_defaults_are_the_designed_policy(self) -> None:
        """DESIGN §6.9: full transcripts and snapshots 90 days, summaries forever."""
        policy = MaintenancePolicy()

        assert policy.compact_after_days == 30
        assert policy.archive_keep_days == 90


class TestValidation:
    def test_archives_must_outlive_the_hot_window(self) -> None:
        """Inverted, a day becomes deletable before it is ever archived."""
        with pytest.raises(ValidationError, match="archive_keep_days"):
            MaintenancePolicy(compact_after_days=30, archive_keep_days=30)

    def test_equal_windows_are_refused_not_merely_inverted_ones(self) -> None:
        with pytest.raises(ValidationError, match="must exceed"):
            MaintenancePolicy(compact_after_days=45, archive_keep_days=45)

    def test_a_zero_hot_window_is_refused(self) -> None:
        """Zero would compact the transcripts of cycles that are still running."""
        with pytest.raises(ValidationError):
            MaintenancePolicy(compact_after_days=0, archive_keep_days=90)

    def test_a_negative_window_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            MaintenancePolicy(compact_after_days=-1, archive_keep_days=90)


class TestKind:
    def test_maintenance_is_a_singleton_kind_like_global_risk(self) -> None:
        assert ConfigKind.MAINTENANCE.is_singleton
        assert ConfigKind.GLOBAL_RISK.is_singleton
        assert not ConfigKind.BASKET.is_singleton

    def test_the_kind_resolves_to_its_model(self) -> None:
        """Without this the store cannot validate the document, and `put` refuses at runtime."""
        assert DOCUMENTS[ConfigKind.MAINTENANCE] is MaintenancePolicy

    def test_every_kind_has_a_model(self) -> None:
        """A kind added later without one must fail here, not on an operator's first publish."""
        assert set(DOCUMENTS) == set(ConfigKind)


class TestStorage:
    async def test_a_published_policy_round_trips_through_the_store(
        self, configs: ConfigStore
    ) -> None:
        await configs.put(
            SINGLETON_ID,
            MaintenancePolicy(compact_after_days=45, archive_keep_days=120),
            actor="test",
        )

        record = configs.latest(ConfigKind.MAINTENANCE, SINGLETON_ID)

        assert record is not None
        assert record.document.compact_after_days == 45
        assert record.document.archive_keep_days == 120

    async def test_shortening_retention_is_a_new_version_not_an_overwrite(
        self, configs: ConfigStore
    ) -> None:
        """ "Who shortened retention to 7 days, and when" is the substance of OPERATIONS 17."""
        await configs.put(SINGLETON_ID, MaintenancePolicy(), actor="first")
        await configs.put(
            SINGLETON_ID,
            MaintenancePolicy(compact_after_days=3, archive_keep_days=7),
            actor="second",
        )

        history = configs.history(ConfigKind.MAINTENANCE, SINGLETON_ID)

        assert [record.ref.version for record in history] == [1, 2]
        assert history[0].document.archive_keep_days == 90
