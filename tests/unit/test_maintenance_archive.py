"""The archive is the copy compaction relies on, so it is verified before anything is dropped.

A day file is written once and never rewritten: a day only becomes eligible when it is entirely
past the horizon, which is what makes a whole-file hash a meaningful check (spec §3.4). There is
no partial-rewrite path here to get wrong, and that is by construction rather than by care.

Failure semantics under test: anything that leaves the file absent, unreadable, or not matching
what was just written raises `ArchiveError`, and the caller compacts nothing for that day. The
database still holds every payload, so a failed archive costs a retry, never data.
"""

from __future__ import annotations

import gzip
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tradebot.core.events import Event, EventType
from tradebot.maintenance.archive import (
    ArchiveError,
    archive_day,
    archive_path,
    delete_aged,
    inventory,
    read_archive,
)
from tradebot.persistence.store import EventStore

DAY = date(2026, 7, 19)
NOON = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _scratch_files(root: Path) -> list[Path]:
    """Sync helper: globbing inside an `async def` is blocking I/O on the event loop."""
    return list(root.rglob("*.tmp"))


def seat_event(at: datetime, text: str = "raw completion text") -> Event:
    return Event(
        ts=at,
        type=EventType.SEAT_RESPONDED,
        aggregate_id="cycle-1",
        cycle_id="cycle-1",
        payload={"response": {"seat_id": "technical", "raw_text": text, "cost_usd": "0"}},
    )


class TestPath:
    def test_a_day_file_is_grouped_by_month_under_its_mode(self, tmp_path: Path) -> None:
        assert archive_path(tmp_path, "sim", DAY) == (
            tmp_path / "sim" / "2026-07" / "2026-07-19.jsonl.gz"
        )

    def test_each_mode_keeps_its_own_tree(self, tmp_path: Path) -> None:
        """A live transcript must never be filed under sim (PLAN §2.4)."""
        assert archive_path(tmp_path, "live", DAY) != archive_path(tmp_path, "sim", DAY)


class TestArchiveDay:
    async def test_it_writes_every_event_of_that_day(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON), seat_event(NOON + timedelta(hours=1)))

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert result.rows == 2
        assert result.path.exists()
        assert result.sha256

    async def test_the_archived_payload_is_identical_to_the_stored_one(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Identical, because that is what makes the archive a *copy* rather than a summary."""
        (stored,) = await store.append(seat_event(NOON))

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        (line,) = read_archive(result.path)
        assert line["payload"] == stored.payload
        assert line["seq"] == stored.seq
        assert line["event_id"] == stored.event_id
        assert line["type"] == "SEAT_RESPONDED"

    async def test_the_raw_text_compaction_will_drop_is_in_the_file(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """The whole point: after compaction this file is the only copy until it is deleted."""
        await store.append(seat_event(NOON, text="the model said this exactly"))

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        (line,) = read_archive(result.path)
        assert line["payload"]["response"]["raw_text"] == "the model said this exactly"

    async def test_a_neighbouring_day_is_not_swept_in(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON), seat_event(NOON + timedelta(days=1)))

        assert archive_day(store.engine, tmp_path, mode="sim", day=DAY).rows == 1

    async def test_the_day_boundary_is_utc_midnight_inclusive_of_its_first_instant(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Half-open [midnight, next midnight): the first instant is in, the last is not."""
        await store.append(
            seat_event(datetime(2026, 7, 19, 0, 0, tzinfo=UTC)),
            seat_event(datetime(2026, 7, 19, 23, 59, 59, tzinfo=UTC)),
            seat_event(datetime(2026, 7, 20, 0, 0, tzinfo=UTC)),
        )

        assert archive_day(store.engine, tmp_path, mode="sim", day=DAY).rows == 2

    async def test_every_type_is_archived_not_only_the_compactable_ones(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """The archive is a copy of the day, not of the part compaction happens to touch."""
        await store.append(
            seat_event(NOON),
            Event(
                ts=NOON,
                type=EventType.CYCLE_COMPLETED,
                aggregate_id="cycle-1",
                cycle_id="cycle-1",
                payload={"outcome": "no_trade", "cost_usd": "0"},
            ),
        )

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert {line["type"] for line in read_archive(result.path)} == {
            "SEAT_RESPONDED",
            "CYCLE_COMPLETED",
        }

    async def test_a_day_with_nothing_in_it_writes_no_file(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert result.rows == 0
        assert not result.path.exists()

    async def test_no_temporary_file_survives_a_completed_write(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON))

        archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert _scratch_files(tmp_path) == []

    async def test_an_existing_verified_file_is_left_alone(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """A day is immutable once past the horizon, so a re-run must not rewrite it."""
        await store.append(seat_event(NOON))
        first = archive_day(store.engine, tmp_path, mode="sim", day=DAY)
        written_at = first.path.stat().st_mtime_ns

        second = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert second.sha256 == first.sha256
        assert second.path.stat().st_mtime_ns == written_at


class TestVerification:
    """Nothing is compacted against an archive that did not verify (spec §3.5)."""

    async def test_a_corrupted_archive_is_refused_rather_than_trusted(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        await store.append(seat_event(NOON))
        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)
        result.path.write_bytes(b"not gzip at all")

        with pytest.raises(ArchiveError, match=r"readable archive|could not be re-read"):
            archive_day(store.engine, tmp_path, mode="sim", day=DAY)

    async def test_a_truncated_archive_is_refused_on_its_row_count(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Readable but short: gzip alone cannot catch a file missing its last events."""
        await store.append(seat_event(NOON), seat_event(NOON + timedelta(hours=1)))
        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)
        with gzip.open(result.path, "wt", encoding="utf-8") as handle:
            handle.write('{"seq": 1}\n')

        with pytest.raises(ArchiveError, match="holds 1 rows; the day has 2"):
            archive_day(store.engine, tmp_path, mode="sim", day=DAY)

    async def test_the_hash_is_of_the_file_on_disk(self, store: EventStore, tmp_path: Path) -> None:
        """Hashing what was written in memory would verify nothing about what landed."""
        import hashlib

        await store.append(seat_event(NOON))

        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()


class TestDeleteAged:
    """The only irreversible act in this piece (spec D1a), so its aim is asserted narrowly."""

    def _day_file(self, root: Path, day: date, mode: str = "sim") -> Path:
        path = archive_path(root, mode, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_it_deletes_only_files_older_than_the_cutoff(self, tmp_path: Path) -> None:
        old = self._day_file(tmp_path, date(2026, 4, 1))
        recent = self._day_file(tmp_path, date(2026, 7, 19))

        removed, failures = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == [old]
        assert failures == []
        assert recent.exists()

    def test_the_cutoff_day_itself_is_kept(self, tmp_path: Path) -> None:
        """`before` is exclusive: a window of N days keeps N days, not N minus one."""
        edge = self._day_file(tmp_path, date(2026, 5, 1))

        removed, _ = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == []
        assert edge.exists()

    def test_a_partial_write_is_never_matched(self, tmp_path: Path) -> None:
        """A `.tmp` is not a day file, and deletion must not guess."""
        base = archive_path(tmp_path, "sim", date(2026, 4, 1))
        base.parent.mkdir(parents=True, exist_ok=True)
        partial = base.with_name(base.name + ".tmp")
        partial.write_bytes(b"x")

        removed, _ = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == []
        assert partial.exists()

    def test_a_file_that_does_not_name_a_day_is_left_alone(self, tmp_path: Path) -> None:
        """Deletion parses the name; anything it cannot parse is somebody else's file."""
        stray = archive_path(tmp_path, "sim", date(2026, 4, 1)).with_name("notes.jsonl.gz")
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"x")

        removed, _ = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == []
        assert stray.exists()

    def test_another_mode_is_never_touched(self, tmp_path: Path) -> None:
        """A sim retention window must not delete live's records (PLAN §2.4)."""
        other = self._day_file(tmp_path, date(2026, 4, 1), mode="live")

        delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert other.exists()

    def test_the_database_and_the_backups_are_never_considered(self, tmp_path: Path) -> None:
        """Deletion runs over the archive directory and nothing else (spec §3.5)."""
        database = tmp_path / "sim.db"
        database.write_bytes(b"db")
        backup = tmp_path / "backups" / "sim" / "sim-20260401T000000Z.db"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"backup")
        self._day_file(tmp_path, date(2026, 4, 1))

        delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert database.exists()
        assert backup.exists()

    def test_an_absent_archive_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """A system that has never archived is not a system with a retention failure."""
        assert delete_aged(tmp_path / "nothing", "sim", before=date(2026, 5, 1)) == ([], [])

    def test_an_undeletable_file_is_reported_and_the_rest_still_go(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One locked file must not stop a pass; the next pass tries again (spec §6.4)."""
        locked = self._day_file(tmp_path, date(2026, 3, 1))
        removable = self._day_file(tmp_path, date(2026, 4, 1))
        real_unlink = Path.unlink

        def refuse_one(self: Path, **kwargs: object) -> None:
            if self == locked:
                raise OSError("locked by another process")
            real_unlink(self)

        monkeypatch.setattr(Path, "unlink", refuse_one)

        removed, failures = delete_aged(tmp_path, "sim", before=date(2026, 5, 1))

        assert removed == [removable]
        assert len(failures) == 1
        assert "locked by another process" in failures[0]
        assert locked.exists()


class TestInventory:
    """What the archive directory holds — the only place to see what deletion has taken."""

    def _day_file(self, root: Path, day: date, mode: str = "sim", size: int = 8) -> Path:
        path = archive_path(root, mode, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_an_empty_directory_reports_nothing_rather_than_raising(self, tmp_path: Path) -> None:
        """A database that has never archived is a normal state, not a fault."""
        found = inventory(tmp_path, "sim")

        assert (found.files, found.oldest, found.newest, found.total_bytes) == (0, None, None, 0)

    def test_it_counts_and_spans_this_mode_s_day_files(self, tmp_path: Path) -> None:
        self._day_file(tmp_path, date(2026, 4, 1))
        self._day_file(tmp_path, date(2026, 7, 19))

        found = inventory(tmp_path, "sim")

        assert found.files == 2
        assert (found.oldest, found.newest) == (date(2026, 4, 1), date(2026, 7, 19))
        assert found.total_bytes == 16

    def test_another_mode_is_never_counted(self, tmp_path: Path) -> None:
        """Narrow like every other thing in this module: one mode's directory, never more."""
        self._day_file(tmp_path, date(2026, 4, 1), mode="live")

        assert inventory(tmp_path, "sim").files == 0

    def test_a_partial_write_is_not_a_day_file(self, tmp_path: Path) -> None:
        """The same rule `delete_aged` decides by: a `.tmp` is an interrupted write, not a day."""
        base = archive_path(tmp_path, "sim", date(2026, 4, 1))
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(base.suffix + ".tmp").write_bytes(b"x")

        assert inventory(tmp_path, "sim").files == 0


class TestArchiveFailuresFailClosed:
    """Every way the filesystem can refuse. None may escape as a raw `OSError`.

    The caller compacts a day only when `archive_day` returned, so an unclassified error escaping
    here would either stop the whole pass with a traceback or — worse, if anyone ever caught it
    loosely — license compacting against a file that is not there.
    """

    async def test_a_write_that_fails_is_refused_and_leaves_no_scratch_file(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await store.append(seat_event(NOON))

        def refuse(self: Path, target: Path) -> None:
            raise OSError("read-only file system")

        monkeypatch.setattr(Path, "replace", refuse)

        with pytest.raises(ArchiveError, match="could not write"):
            archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        assert _scratch_files(tmp_path) == []

    async def test_an_archive_that_cannot_be_read_back_is_refused(
        self, store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification is a real re-read; a file it cannot open is not a verified archive."""
        await store.append(seat_event(NOON))
        archive_day(store.engine, tmp_path, mode="sim", day=DAY)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("locked by another process")

        monkeypatch.setattr("tradebot.maintenance.archive.read_archive", refuse)

        with pytest.raises(ArchiveError, match="could not be re-read"):
            archive_day(store.engine, tmp_path, mode="sim", day=DAY)

    async def test_a_line_that_is_not_json_is_refused(
        self, store: EventStore, tmp_path: Path
    ) -> None:
        """Gzip's CRC passes on a well-compressed file full of nonsense."""
        await store.append(seat_event(NOON))
        result = archive_day(store.engine, tmp_path, mode="sim", day=DAY)
        with gzip.open(result.path, "wt", encoding="utf-8") as handle:
            handle.write("not json at all\n")

        with pytest.raises(ArchiveError, match="not a readable archive"):
            archive_day(store.engine, tmp_path, mode="sim", day=DAY)
