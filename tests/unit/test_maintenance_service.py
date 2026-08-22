"""One pass a day, in one order, recorded as one event.

The order is the safety property: back up, then archive, then compact **only what was archived**,
then delete what has aged out. A failure anywhere stops the destructive steps that would follow it
(spec §3.5, §6.4). Nothing here may raise: a maintenance defect must never be what stops the bot
trading, so a failed pass is a recorded fact rather than an exception.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_maintenance_compaction import writer_of

from tradebot.core.clock import ManualClock
from tradebot.core.config import MaintenancePolicy
from tradebot.core.events import Event, EventType
from tradebot.maintenance.archive import ArchiveError, archive_path
from tradebot.maintenance.backup import BackupError
from tradebot.maintenance.service import MaintenanceService
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=40)
ANCIENT = NOW - timedelta(days=120)


def seat_event(at: datetime) -> Event:
    return Event(
        ts=at,
        type=EventType.SEAT_RESPONDED,
        aggregate_id="c-1",
        cycle_id="c-1",
        payload={"response": {"seat_id": "s", "raw_text": "text", "cost_usd": "0"}},
    )


def refuse(*_args: object, **_kwargs: object) -> None:
    raise BackupError("no room")


def build(
    store: EventStore,
    tmp_path: Path,
    *,
    at: datetime = NOW,
    policy: MaintenancePolicy | None = None,
    take: object = None,
) -> MaintenanceService:
    return MaintenanceService(
        store=store,
        writer=writer_of(store),
        clock=ManualClock(at),
        mode="sim",
        archive_root=tmp_path / "archive",
        backup_dir=tmp_path / "backups",
        policy=lambda: policy or MaintenancePolicy(),
        take=take if take is not None else (lambda *_a, **_k: None),
    )


@pytest.fixture
def service(store: EventStore, tmp_path: Path) -> MaintenanceService:
    return build(store, tmp_path)


class TestDueness:
    """Derived from the log, so a restart can neither skip the day nor take a second pass."""

    async def test_the_first_pass_of_the_day_runs(self, service: MaintenanceService) -> None:
        assert await service.run_once() is not None

    async def test_a_second_pass_the_same_day_does_nothing(
        self, service: MaintenanceService
    ) -> None:
        await service.run_once()

        assert await service.run_once() is None

    async def test_dueness_survives_a_restart_because_it_is_read_from_the_log(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await build(store, tmp_path).run_once()

        restarted = build(store, tmp_path, at=NOW + timedelta(hours=1))

        assert await restarted.run_once() is None

    async def test_the_next_day_is_due_again(self, store: EventStore, tmp_path: Path) -> None:
        await build(store, tmp_path).run_once()

        tomorrow = build(store, tmp_path, at=NOW + timedelta(days=1))

        assert await tomorrow.run_once() is not None

    async def test_a_failed_pass_still_counts_as_the_day_s_run(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """It raised a HIGH notification; retrying every five minutes would only repeat it."""
        await build(store, tmp_path, take=refuse).run_once()

        assert await build(store, tmp_path, take=refuse).run_once() is None


class TestThePass:
    async def test_an_old_day_is_archived_and_compacted(self, service: MaintenanceService) -> None:
        await service.store.append(seat_event(LONG_AGO))

        report = await service.run_once()

        assert report is not None
        assert report.archived_days == 1
        assert report.compacted_rows == 1
        assert all("raw_text" not in str(e.payload) for e in service.store.read_all())

    async def test_the_archive_holds_what_the_database_gave_up(
        self, service: MaintenanceService, tmp_path: Path
    ) -> None:
        """Archive *then* compact: the payload exists in a file before the row loses it."""
        from tradebot.maintenance.archive import read_archive

        await service.store.append(seat_event(LONG_AGO))

        await service.run_once()

        path = archive_path(tmp_path / "archive", "sim", LONG_AGO.date())
        (line,) = read_archive(path)
        assert line["payload"]["response"]["raw_text"] == "text"

    async def test_a_recent_day_is_left_alone(self, service: MaintenanceService) -> None:
        await service.store.append(seat_event(NOW - timedelta(days=2)))

        report = await service.run_once()

        assert report is not None
        assert report.compacted_rows == 0
        assert any("raw_text" in str(e.payload) for e in service.store.read_all())

    async def test_the_pass_records_one_event_naming_the_windows_it_ran_under(
        self, service: MaintenanceService
    ) -> None:
        """So "why did that get deleted" is answerable from the log alone."""
        await service.run_once()

        (recorded,) = service.store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["compact_after_days"] == 30
        assert recorded.payload["archive_keep_days"] == 90
        assert recorded.payload["outcome"] == "ok"
        assert recorded.payload["mode"] == "sim"

    async def test_the_windows_are_read_fresh_at_every_pass(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """An edit takes effect at the next tick with no restart (ADR 0021's rule, applied here)."""
        await store.append(seat_event(NOW - timedelta(days=10)))

        report = await build(
            store,
            tmp_path,
            policy=MaintenancePolicy(compact_after_days=5, archive_keep_days=400),
        ).run_once()

        assert report is not None
        assert report.compacted_rows == 1

    async def test_archives_past_the_keep_window_are_deleted_last(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(ANCIENT))

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert report.archived_days == 1
        assert report.compacted_rows == 1
        assert report.deleted_archives == 1
        assert not archive_path(tmp_path / "archive", "sim", ANCIENT.date()).exists()

    async def test_a_long_gap_can_move_a_day_straight_to_gone_and_says_so(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Spec §3.5 names this rather than leaving it to be discovered.

        On a system that has not run maintenance for months the first pass archives a day already
        older than `archive_keep_days`, compacts it, and deletes that archive in the same run. The
        policy is behaving correctly, but the daily line has to say how many files went.
        """
        await store.append(seat_event(ANCIENT))

        await build(store, tmp_path).run_once()

        (recorded,) = store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["deleted_archives"] == 1


class TestFailure:
    async def test_a_failed_backup_stops_the_pass_before_anything_is_compacted(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Compacting without a fresh backup is the one ordering that can lose data."""
        await store.append(seat_event(LONG_AGO))

        report = await build(store, tmp_path, take=refuse).run_once()

        assert report is not None
        assert "no room" in report.failure
        assert report.compacted_rows == 0
        assert report.archived_days == 0
        assert any("raw_text" in str(e.payload) for e in store.read_all())

    async def test_a_failure_is_recorded_as_one(self, store: EventStore, tmp_path: Path) -> None:
        await build(store, tmp_path, take=refuse).run_once()

        (recorded,) = store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["outcome"] == "failed"
        assert "no room" in recorded.payload["detail"]

    async def test_an_unverifiable_archive_compacts_nothing_for_that_day(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`raw_text` from 31 days ago is never the last copy of itself."""
        await store.append(seat_event(LONG_AGO))

        def unverifiable(*_args: object, **_kwargs: object) -> None:
            raise ArchiveError("hash mismatch")

        monkeypatch.setattr("tradebot.maintenance.service.archive_day", unverifiable)

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert "hash mismatch" in report.failure
        assert report.compacted_rows == 0
        assert any("raw_text" in str(e.payload) for e in store.read_all())

    async def test_an_unclassified_defect_is_recorded_rather_than_raised(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """A maintenance defect must never be what stops the bot, and must never be silent."""

        def explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("something nobody classified")

        report = await build(store, tmp_path, take=explode).run_once()

        assert report is not None
        assert "something nobody classified" in report.failure
        (recorded,) = store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["outcome"] == "failed"


class TestAForcedPass:
    """`tradebot maintenance compact` — a human who typed the command meant it."""

    async def test_force_ignores_dueness(self, store: EventStore, tmp_path: Path) -> None:
        await build(store, tmp_path).run_once()

        report = await build(store, tmp_path).run_once(force=True)

        assert report is not None
        assert len(store.read_types(EventType.MAINTENANCE_RAN)) == 2

    async def test_an_override_replaces_the_published_windows_for_that_pass_only(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOW - timedelta(days=10)))

        report = await build(store, tmp_path).run_once(
            force=True,
            override=MaintenancePolicy(compact_after_days=5, archive_keep_days=400),
        )

        assert report is not None
        assert report.compacted_rows == 1

    async def test_an_override_is_recorded_as_one(self, store: EventStore, tmp_path: Path) -> None:
        """Otherwise the log attributes a deletion to a policy that was never in force."""
        await build(store, tmp_path).run_once(
            force=True,
            override=MaintenancePolicy(compact_after_days=45, archive_keep_days=120),
        )

        (recorded,) = store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["overridden"] is True
        assert recorded.payload["compact_after_days"] == 45

    async def test_an_ordinary_tick_is_never_marked_as_overridden(
        self, service: MaintenanceService
    ) -> None:
        await service.run_once()

        (recorded,) = service.store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["overridden"] is False
