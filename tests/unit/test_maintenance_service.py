"""One pass a day, in one order, recorded as one event.

The order is the safety property: back up, then archive, then compact **only what was archived**,
then delete what has aged out. A failure anywhere stops the destructive steps that would follow it
(spec §3.5, §6.4). Nothing here may raise: a maintenance defect must never be what stops the bot
trading, so a failed pass is a recorded fact rather than an exception.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.test_maintenance_compaction import writer_of

from tradebot.core.clock import ManualClock
from tradebot.core.config import MaintenancePolicy
from tradebot.core.events import Event, EventType
from tradebot.maintenance.archive import (
    ArchiveError,
    ArchiveResult,
    archive_day,
    archive_path,
)
from tradebot.maintenance.backup import BackupError
from tradebot.maintenance.compaction import pending_days
from tradebot.maintenance.service import MaintenanceService
from tradebot.persistence.store import EventStore

NOW = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=40)
#: A second pending day, one behind `LONG_AGO`, so `pending_days` yields it first. With one day
#: in the fixture, per-day and per-pass containment are indistinguishable.
OLDER = NOW - timedelta(days=41)
ANCIENT = NOW - timedelta(days=120)

#: What `delete_aged` reports for a file something else has open. Its own behaviour is covered in
#: `test_maintenance_archive.py`; what the *pass* does with it is covered here.
LOCKED = "archive/sim/2026-01/2026-01-01.jsonl.gz: [Errno 13] Permission denied"


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


def unverifiable_on(day: date) -> object:
    """`archive_day`, but one nominated day raises the way a corrupt file does.

    Every other day is archived for real, which is the whole point: a containment claim only means
    something when there is a good day behind the bad one to be contained *from*.
    """

    def wrapped(engine: object, root: Path, *, mode: str, day: date) -> ArchiveResult:
        if day == corrupt:
            raise ArchiveError("hash mismatch")
        return archive_day(engine, root, mode=mode, day=day)  # type: ignore[arg-type]

    corrupt = day
    return wrapped


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
        """`raw_text` from 31 days ago is never the last copy of itself.

        **Two** pending days, because with one the spec's per-day containment and a per-pass
        give-up are indistinguishable — which is how this test passed for a pass that stopped at
        the first bad day and took every day behind it with it. A day file is *verified* rather
        than rewritten once it exists, so a corrupt one fails on this pass and on every pass
        after: giving up at it stopped retention for good (spec §6.4).
        """
        await store.append(seat_event(OLDER), seat_event(LONG_AGO))
        monkeypatch.setattr(
            "tradebot.maintenance.service.archive_day", unverifiable_on(OLDER.date())
        )

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert "hash mismatch" in report.failure
        assert (report.archived_days, report.compacted_rows) == (1, 1)
        kept = {
            e.ts: "raw_text" in str(e.payload) for e in store.read_types(EventType.SEAT_RESPONDED)
        }
        assert kept == {OLDER: True, LONG_AGO: False}

    async def test_a_day_that_will_not_archive_does_not_stop_the_deletions(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deletion is scoped by file name and depends on nothing the archive step did.

        Returning at the first bad day skipped `delete_aged` as well, so one corrupt file also
        stopped the 90-day deletion that makes OPERATIONS precondition 17 answerable — the
        database growing while the thing that trims it silently no longer ran.
        """
        await store.append(seat_event(ANCIENT), seat_event(LONG_AGO))
        monkeypatch.setattr(
            "tradebot.maintenance.service.archive_day", unverifiable_on(LONG_AGO.date())
        )

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert "hash mismatch" in report.failure
        assert report.deleted_archives == 1
        assert not archive_path(tmp_path / "archive", "sim", ANCIENT.date()).exists()

    async def test_many_failed_days_are_summarised_rather_than_recited(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`failure` reaches the event payload *and* the notification body, so it is bounded.

        A permissions fault on the archive root fails every pending day at once, and a database
        that has never run maintenance can have hundreds. The full list goes to the log; the
        operator gets the count and one example.
        """
        await store.append(seat_event(OLDER), seat_event(LONG_AGO), seat_event(ANCIENT))
        monkeypatch.setattr(
            "tradebot.maintenance.service.archive_day",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("read-only file system")),
        )

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert not report.ok
        assert "3 of 3" in report.failure
        assert "read-only file system" in report.failure
        assert report.failure.count("read-only file system") == 1

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


class TestTheLoop:
    """`run` outlives every pass it reports on, so nothing in it may end the task."""

    async def test_a_defect_in_a_pass_does_not_end_the_loop(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`run_once` records its own failures; this is the belt to that braces."""
        import asyncio

        service = build(store, tmp_path)
        survived = asyncio.Event()
        calls = 0

        async def explode() -> None:
            nonlocal calls
            calls += 1
            if calls >= 3:
                survived.set()
            raise RuntimeError("a defect run_once did not catch")

        monkeypatch.setattr(service, "run_once", explode)
        task = asyncio.create_task(service.run(poll_seconds=0))
        await asyncio.wait_for(survived.wait(), timeout=5)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls >= 3

    async def test_cancellation_stops_it(self, store: EventStore, tmp_path: Path) -> None:
        """Shutdown must actually shut it down, not swallow the cancellation as a defect."""
        import asyncio

        task = asyncio.create_task(build(store, tmp_path).run(poll_seconds=0))
        await asyncio.sleep(0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task


class TestNothingBlocksTheEventLoop:
    """Housekeeping shares its loop with the supervisor, the monitor and the dashboard socket."""

    async def test_the_pending_day_scan_runs_off_the_event_loop(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two `LIKE '%...%'` predicates over `payload_json`, the largest column there is.

        No index can serve them, so the scan is linear in the whole log's payload size — 26 ms on
        an 8 MB sim database, and it grows with the log. The three filesystem steps around it
        already hop to a thread for exactly this reason (spec §6.3); this one was missed.
        """
        threads: list[str] = []

        def record(*args: object, **kwargs: object) -> list[date]:
            threads.append(threading.current_thread().name)
            return pending_days(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr("tradebot.maintenance.service.pending_days", record)

        await build(store, tmp_path).run_once()

        assert threads == [threads[0]] and threading.main_thread().name not in threads


class TestAnUndeletableFile:
    """Spec §6.4 gives it its own row: reported and skipped, and counted in the daily line."""

    async def test_it_is_not_a_failed_pass(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It is none of the four things §5.4 lists as a `MAINTENANCE_FAILED`.

        Folding it into `failure` swapped the day's line for an alarm — and HIGH notices
        deliberately never supersede, so a virus scanner holding one file stacked another red row
        every night while the pass's real work went unreported.
        """
        monkeypatch.setattr(
            "tradebot.maintenance.service.delete_aged", lambda *_a, **_k: ([], [LOCKED])
        )

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert report.ok
        assert report.undeletable == (LOCKED,)

    async def test_it_is_recorded_on_the_daily_line(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "tradebot.maintenance.service.delete_aged", lambda *_a, **_k: ([], [LOCKED])
        )

        await build(store, tmp_path).run_once()

        (recorded,) = store.read_types(EventType.MAINTENANCE_RAN)
        assert recorded.payload["outcome"] == "ok"
        assert recorded.payload["undeletable"] == [LOCKED]


class TestNothingIsCompactedWithoutAnArchive:
    """The safety property of the whole piece, asserted where it is enforced."""

    async def test_a_day_whose_archive_wrote_no_file_is_not_compacted(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive, and deliberately kept.

        `pending_days` and `archive_day` query the log separately, so they agree today by
        construction rather than by contract. If they ever stopped agreeing, compacting against
        an `ArchiveResult` that wrote no file is precisely the ordering that loses data — so the
        service checks rather than assumes.
        """
        from tradebot.maintenance.archive import ArchiveResult

        await store.append(seat_event(LONG_AGO))
        monkeypatch.setattr(
            "tradebot.maintenance.service.archive_day",
            lambda *_a, **_k: ArchiveResult(path=tmp_path / "nothing.gz", rows=0, sha256=""),
        )

        report = await build(store, tmp_path).run_once()

        assert report is not None
        assert report.archived_days == 0
        assert report.compacted_rows == 0
        assert any("raw_text" in str(e.payload) for e in store.read_all())
