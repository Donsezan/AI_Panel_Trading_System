"""The one operation that can destroy records that cannot be recreated.

`run_migrations` runs on *every* process start — every `serve`, every `run`, every CLI invocation
— so the cost of the check matters as much as the protection it gives: one revision read when
nothing will change, and a copy only when something will (spec §4.5).

Two conditions decide it, and the second is the one worth reading twice. A copy is taken when the
upgrade will *move* the revision **and** the database already holds something. A file this very
call created has no rows to lose, and backing it up would put the free-space guard between a fresh
install and its first start. A database holding tables but no `alembic_version` is the ambiguous
case, and it is copied: it cannot be told apart from a real ledger whose version table was lost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

from tradebot.core.clock import ManualClock
from tradebot.maintenance.backup import BackupError, backup_destination
from tradebot.persistence.database import _PROJECT_ROOT, create_database, run_migrations

AT = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)

#: The revision before head. A database left here is one real schema change out of date, which is
#: the state this whole mechanism exists for.
BEHIND = "0006"


def database_at(path: Path, revision: str) -> Engine:
    """A real file database genuinely upgraded to `revision` and no further.

    Genuinely upgraded rather than having `alembic_version` rewritten: a rewound version number
    over a head schema would make the following upgrade fail on a column that already exists, and
    the test would then be asserting on alembic's error rather than on the backup.
    """
    engine = create_engine(f"sqlite:///{path}", future=True)
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
    return engine


def revision_of(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar())


def _copies(path: Path) -> list[str]:
    destination = backup_destination(path)
    return sorted(copy.name for copy in destination.glob("*.db")) if destination.exists() else []


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise BackupError("no room")


class TestDestination:
    def test_it_defaults_beside_the_database_under_its_mode(self, tmp_path: Path) -> None:
        assert backup_destination(tmp_path / "sim.db") == tmp_path / "backups" / "sim"

    def test_an_environment_override_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADEBOT_BACKUP_DIR", str(tmp_path / "elsewhere"))

        assert backup_destination(tmp_path / "sim.db") == tmp_path / "elsewhere" / "sim"

    def test_one_override_still_keeps_the_modes_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shared vault must not let a live copy land in the same directory as a sim one.

        The mode is the last segment, not the first, so pointing every mode at one drive still
        produces `<vault>/live/` and `<vault>/sim/` (PLAN §2.4's rule, applied to the copies).
        """
        monkeypatch.setenv("TRADEBOT_BACKUP_DIR", str(tmp_path / "vault"))

        assert backup_destination(Path("data/live.db")) == tmp_path / "vault" / "live"
        assert backup_destination(Path("data/sim.db")) == tmp_path / "vault" / "sim"


class TestNothingToLose:
    """The cases that must cost nothing, because they are almost every start."""

    def test_a_database_already_at_head_is_not_copied(self, tmp_path: Path) -> None:
        """The 99% case. A backup on every start would be a backup nobody reads."""
        path = tmp_path / "sim.db"
        create_database(path)

        create_database(path)

        assert _copies(path) == []

    def test_a_brand_new_database_is_not_copied(self, tmp_path: Path) -> None:
        """A file this call created has no rows to lose.

        Copying it would also put the 200 MB free-space guard between a fresh install and its
        first start, refusing to boot over a database that is empty by definition.
        """
        path = tmp_path / "sim.db"

        create_database(path)

        assert _copies(path) == []

    def test_an_in_memory_database_is_skipped_by_construction(self) -> None:
        """The entire test suite runs on this path and must never touch the filesystem."""
        run_migrations(create_database(None))


class TestSomethingToLose:
    def test_a_database_behind_head_is_copied_and_named_for_the_revision_it_leaves(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "sim.db"
        engine = database_at(path, BEHIND)

        run_migrations(engine, backup=backup_destination(path), mode="sim", clock=ManualClock(AT))

        assert _copies(path) == [f"sim-pre-{BEHIND}-20260820T040000Z.db"]

    def test_the_copy_is_of_the_schema_being_left_behind_not_the_one_arrived_at(
        self, tmp_path: Path
    ) -> None:
        """The whole point of the ordering: a copy taken after the upgrade is not a rollback point.

        Asserted on the copy's own `alembic_version` rather than on when the file appeared, which
        is the only reading that cannot pass by accident.
        """
        path = tmp_path / "sim.db"
        engine = database_at(path, BEHIND)

        run_migrations(engine, backup=backup_destination(path), mode="sim", clock=ManualClock(AT))

        (copy,) = backup_destination(path).glob("*.db")
        assert revision_of(create_engine(f"sqlite:///{copy}", future=True)) == BEHIND
        assert revision_of(engine) != BEHIND

    def test_a_database_holding_tables_but_no_version_is_copied_rather_than_read_as_empty(
        self, tmp_path: Path
    ) -> None:
        """The ambiguous case the rule exists for.

        Tables with no `alembic_version` cannot be told apart from a real ledger whose version
        table was lost. Reading that as "fresh, nothing to lose" would migrate it with no rollback
        point. The upgrade behind it then fails on its own — correctly, and with the copy already
        on disk, which is the difference between a bad morning and an unrecoverable one.
        """
        path = tmp_path / "sim.db"
        engine = database_at(path, BEHIND)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE alembic_version"))

        with pytest.raises(DatabaseError):
            run_migrations(
                engine, backup=backup_destination(path), mode="sim", clock=ManualClock(AT)
            )

        assert _copies(path) == ["sim-pre-base-20260820T040000Z.db"]


class TestFailClosed:
    """An un-backed-up upgrade of a ledger must not proceed (spec §4.5 step 4)."""

    def test_a_failing_backup_stops_the_migration(self, tmp_path: Path) -> None:
        path = tmp_path / "sim.db"
        engine = database_at(path, BEHIND)

        with pytest.raises(BackupError):
            run_migrations(
                engine,
                backup=backup_destination(path),
                mode="sim",
                clock=ManualClock(AT),
                take=_refuse,
            )

    def test_a_blocked_migration_leaves_the_schema_untouched(self, tmp_path: Path) -> None:
        path = tmp_path / "sim.db"
        engine = database_at(path, BEHIND)

        with pytest.raises(BackupError):
            run_migrations(
                engine,
                backup=backup_destination(path),
                mode="sim",
                clock=ManualClock(AT),
                take=_refuse,
            )

        assert revision_of(engine) == BEHIND
        columns = {column["name"] for column in inspect(engine).get_columns("alert_cursor")}
        assert "stale_streak" not in columns
