"""Backups: a copy you can open, taken without stopping the bot.

The copy is the whole recovery story — restoring is copying the file back — so these tests assert
against the *restored* database rather than against the fact that a file appeared (spec §4.1).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

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

    def test_a_backup_finishing_during_another_s_copy_is_refused_not_overwritten(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The early `target.exists()` check runs *before* `VACUUM INTO`, so it cannot see a
        second call finishing at the same name during this one's copy — the whole duration of
        the copy is the window (fix round 2). Simulated by making the publish step's own
        `os.link` call find the name already taken, exactly as a faster concurrent call would
        leave it: nothing this module writes is ever destroyed by it, including this way (D4).
        """
        destination = tmp_path / "backups"
        real_link = os.link

        def collide_then_link(src: Path, dst: Path) -> None:
            Path(dst).write_bytes(b"already finished")
            real_link(src, dst)

        monkeypatch.setattr("tradebot.maintenance.backup.os.link", collide_then_link)

        with pytest.raises(BackupError, match="already exists"):
            take_backup(database, destination, mode="sim", clock=ManualClock(AT))

        # The "other backup" that won the race must survive untouched.
        assert (destination / backup_name("sim", AT)).read_bytes() == b"already finished"
        # And this call's own failed attempt must not linger either (fix round 2, Finding 2).
        assert list(destination.glob("*.tmp")) == []

    def test_a_failed_copy_does_not_leave_its_tmp_behind(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither the `except` around `VACUUM INTO` nor the one around publishing unlinked the
        `.tmp` before re-raising (fix round 2, Finding 3) — a persistent failure (a full volume,
        say) would accumulate orphaned `.tmp` files that eat exactly the headroom
        `HEADROOM_BYTES` exists to protect. The fake connection below writes to the `.tmp` before
        raising, standing in for a real `VACUUM INTO` that started writing before the underlying
        failure — so the cleanup this proves runs against a file that is actually there.
        """
        destination = tmp_path / "backups"
        tmp_target = destination / (backup_name("sim", AT) + ".tmp")

        class PartiallyWrittenThenFailing:
            def execution_options(self, **_kwargs: object) -> PartiallyWrittenThenFailing:
                return self

            def __enter__(self) -> PartiallyWrittenThenFailing:
                return self

            def __exit__(self, *_exc_info: object) -> None:
                return None

            def exec_driver_sql(self, _statement: str) -> None:
                tmp_target.write_bytes(b"partial")
                raise RuntimeError("simulated disk failure mid-copy")

        monkeypatch.setattr(database, "connect", lambda *a, **kw: PartiallyWrittenThenFailing())

        with pytest.raises(BackupError):
            take_backup(database, destination, mode="sim", clock=ManualClock(AT))

        assert list(destination.glob("*.tmp")) == []


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


def _raising(error: OSError) -> Callable[..., NoReturn]:
    """A stand-in for a filesystem call that fails. The message is what the refusal must quote."""

    def boom(*_args: object, **_kwargs: object) -> NoReturn:
        raise error

    return boom


class TestWhereHardLinksAreUnsupported:
    """exFAT, FAT32 and several SMB shares — exactly what `TRADEBOT_BACKUP_DIR` is pointed at.

    There `os.link` raises a plain `OSError` on *every* call, not just a colliding one, so the
    publish falls back to `Path.replace` after re-proving the name is free. Both halves matter:
    without the fallback every backup fails on such a volume, and without the re-check the
    fallback would silently overwrite a completed backup (D4).
    """

    def test_a_volume_without_hard_links_still_publishes_the_copy(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "tradebot.maintenance.backup.os.link",
            _raising(OSError("hard links are not supported on this filesystem")),
        )

        result = take_backup(database, tmp_path / "backups", mode="sim", clock=ManualClock(AT))

        assert result.path.exists()
        assert list(result.path.parent.glob("*.tmp")) == []

    def test_a_name_taken_during_the_copy_is_still_refused_without_hard_links(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback re-proves the name is free, so the narrow window still fails closed."""
        destination = tmp_path / "backups"

        def unsupported(_source: Path, target: Path) -> NoReturn:
            Path(target).write_bytes(b"already finished")
            raise OSError("hard links are not supported on this filesystem")

        monkeypatch.setattr("tradebot.maintenance.backup.os.link", unsupported)

        with pytest.raises(BackupError, match="already exists"):
            take_backup(database, destination, mode="sim", clock=ManualClock(AT))

        assert (destination / backup_name("sim", AT)).read_bytes() == b"already finished"
        assert list(destination.glob("*.tmp")) == []


class TestFilesystemFailuresFailClosed:
    """Every way the volume itself can refuse. None may escape as a raw `OSError`.

    The caller of a backup is either `run_migrations`, which must not proceed on one, or the
    daily tick, which has to report one — and both are written against `BackupError`.
    """

    def test_a_publish_that_cannot_complete_at_all_is_refused(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "tradebot.maintenance.backup.os.link", _raising(OSError("no hard links here"))
        )
        monkeypatch.setattr(Path, "replace", _raising(OSError("read-only file system")))

        with pytest.raises(BackupError, match="could not finalize"):
            take_backup(database, tmp_path / "backups", mode="sim", clock=ManualClock(AT))

    def test_a_scratch_file_that_will_not_delete_does_not_fail_a_finished_backup(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once the hard link exists the backup *is* complete under its final name.

        A stuck `.tmp` after that point is a housekeeping matter, and reporting it as a failed
        backup would send the daily tick down the fail-closed path over a copy that is on disk
        and readable.
        """
        monkeypatch.setattr(Path, "unlink", _raising(OSError("locked by another process")))

        result = take_backup(database, tmp_path / "backups", mode="sim", clock=ManualClock(AT))

        assert result.path.exists()
        assert result.size_bytes > 0

    def test_a_stale_scratch_file_that_cannot_be_cleared_refuses(
        self, database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refused rather than written around: a `.tmp` nobody can remove is a volume in a state
        this module has no business guessing about."""
        destination = tmp_path / "backups"
        destination.mkdir()
        (destination / (backup_name("sim", AT) + ".tmp")).write_bytes(b"leftover")
        monkeypatch.setattr(Path, "unlink", _raising(OSError("locked by another process")))

        with pytest.raises(BackupError, match="could not clear stale"):
            take_backup(database, destination, mode="sim", clock=ManualClock(AT))

    def test_a_volume_whose_free_space_cannot_be_read_refuses(
        self, database: Engine, tmp_path: Path
    ) -> None:
        """Unknown headroom is not headroom. A backup that might fill the volume is not taken."""
        with pytest.raises(BackupError, match="could not read free space"):
            take_backup(
                database,
                tmp_path / "backups",
                mode="sim",
                clock=ManualClock(AT),
                probe=_raising(OSError("volume disappeared")),
            )

    def test_a_source_that_cannot_be_measured_refuses(self, tmp_path: Path) -> None:
        """A database that vanished between wiring and the tick is a refusal, not a traceback."""
        with pytest.raises(BackupError, match="could not read size"):
            required_bytes(tmp_path / "gone.db")
