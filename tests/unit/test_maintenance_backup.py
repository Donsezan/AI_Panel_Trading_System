"""Backups: a copy you can open, taken without stopping the bot.

The copy is the whole recovery story — restoring is copying the file back — so these tests assert
against the *restored* database rather than against the fact that a file appeared (spec §4.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from tradebot.core.clock import ManualClock
from tradebot.core.events import Event, EventType
from tradebot.maintenance.backup import (
    HEADROOM_BYTES,
    BackupError,
    backup_name,
    required_bytes,
    take_backup,
)
from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.schema import events
from tradebot.persistence.store import EventStore

AT = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Engine:
    """A real file database. The in-memory one the suite usually runs on cannot be copied."""
    return create_database(tmp_path / "sim.db")


async def _write_one_event(engine: Engine) -> None:
    store = EventStore(engine, SingleWriter(engine))
    # The projector for RISK_EVENT requires the full risk_events row shape (tier, rule, scope,
    # action_taken, detail) — this fixture only needs *an* event in the log, so the values
    # themselves are arbitrary.
    await store.append(
        Event(
            ts=AT,
            type=EventType.RISK_EVENT,
            aggregate_id="portfolio",
            payload={
                "tier": "tier1",
                "rule": "test",
                "scope": "portfolio",
                "action_taken": "recorded",
                "detail": "test",
            },
        )
    )


class TestName:
    def test_a_daily_backup_is_named_for_its_mode_and_instant(self) -> None:
        assert backup_name("sim", AT) == "sim-20260820T040000Z.db"

    def test_a_pre_migration_backup_names_the_revision_it_leaves(self) -> None:
        assert backup_name("sim", AT, revision="a1b2c3") == "sim-pre-a1b2c3-20260820T040000Z.db"


class TestTakeBackup:
    async def test_the_copy_holds_what_the_source_held(
        self, database: Engine, tmp_path: Path
    ) -> None:
        await _write_one_event(database)

        result = take_backup(database, tmp_path / "backups", mode="sim", clock=ManualClock(AT))

        restored = create_database(result.path)
        with restored.connect() as connection:
            rows = connection.execute(select(events.c.type)).all()
        assert [row.type for row in rows] == ["RISK_EVENT"]

    def test_the_destination_is_created_if_absent(self, database: Engine, tmp_path: Path) -> None:
        result = take_backup(
            database, tmp_path / "deep" / "backups", mode="sim", clock=ManualClock(AT)
        )

        assert result.path.exists()
        assert result.size_bytes > 0

    def test_an_in_memory_database_is_refused_rather_than_silently_skipped(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(BackupError, match="in-memory"):
            take_backup(create_database(None), tmp_path, mode="sim", clock=ManualClock(AT))

    def test_a_second_backup_at_the_same_instant_is_refused_not_overwritten(
        self, database: Engine, tmp_path: Path
    ) -> None:
        """Nothing this module writes is ever destroyed by it, including by collision (D4)."""
        take_backup(database, tmp_path, mode="sim", clock=ManualClock(AT))

        with pytest.raises(BackupError):
            take_backup(database, tmp_path, mode="sim", clock=ManualClock(AT))

    def test_a_misconfigured_destination_is_refused_not_leaked_as_a_raw_oserror(
        self, database: Engine, tmp_path: Path
    ) -> None:
        """A destination whose parent path component is a file, not a directory, must fail
        closed through `BackupError` — never as the raw `OSError` `mkdir` raises underneath
        (fix round 1, Finding 1)."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")

        with pytest.raises(BackupError):
            take_backup(database, blocker / "backups", mode="sim", clock=ManualClock(AT))

    def test_no_tmp_file_survives_a_completed_backup(
        self, database: Engine, tmp_path: Path
    ) -> None:
        """The copy lands under a `.tmp` sibling and is renamed onto the final name only once
        `VACUUM INTO` finishes (fix round 1, Finding 2) — nothing should be left behind."""
        result = take_backup(database, tmp_path / "backups", mode="sim", clock=ManualClock(AT))

        assert result.path.exists()
        assert list(result.path.parent.glob("*.tmp")) == []


class TestFreeSpaceGuard:
    """Because nothing rotates (spec D4), the backup must not be what fills the volume."""

    def test_it_refuses_when_the_volume_is_too_full(self, database: Engine, tmp_path: Path) -> None:
        with pytest.raises(BackupError, match="bytes free"):
            take_backup(
                database,
                tmp_path / "backups",
                mode="sim",
                clock=ManualClock(AT),
                probe=lambda _: 1024,
            )

    def test_a_refused_backup_writes_nothing_at_all(self, database: Engine, tmp_path: Path) -> None:
        destination = tmp_path / "backups"

        with pytest.raises(BackupError):
            take_backup(
                database, destination, mode="sim", clock=ManualClock(AT), probe=lambda _: 1024
            )

        assert list(destination.glob("*.db")) == []

    def test_the_requirement_counts_the_wal_the_copy_will_absorb(self, tmp_path: Path) -> None:
        """VACUUM INTO folds the WAL into the copy, so ignoring it would under-reserve.

        Asserted on plain files rather than a live engine: writing to a real `-wal` to make it
        large would corrupt the database the other tests are reading.
        """
        source = tmp_path / "sim.db"
        source.write_bytes(b"\0" * 1000)
        source.with_name("sim.db-wal").write_bytes(b"\0" * 500)

        # 1000 + 500 live, a fifth again (300), plus the headroom.
        assert required_bytes(source) == 1500 + 300 + HEADROOM_BYTES

    def test_an_absent_wal_is_simply_not_counted(self, tmp_path: Path) -> None:
        source = tmp_path / "sim.db"
        source.write_bytes(b"\0" * 1000)

        assert required_bytes(source) == 1000 + 200 + HEADROOM_BYTES
