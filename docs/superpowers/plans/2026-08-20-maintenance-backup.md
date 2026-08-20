# Maintenance Piece A — Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take a consistent copy of a mode's database on demand and before any schema migration, refusing rather than filling the disk.

**Architecture:** A new `tradebot/maintenance/` package whose only member in this piece is `backup.py`. It runs SQLite's `VACUUM INTO` on its own autocommit connection, so the copy is a single defragmented file containing everything in the WAL, taken without blocking a cycle. `run_migrations` gains a hook that compares the stored Alembic revision with head and takes a backup first when they differ — and refuses to upgrade if that backup fails.

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 Core, Alembic, SQLite 3.45 (WAL), pytest, ruff, mypy.

**Spec:** [docs/superpowers/specs/2026-08-20-retention-backup-and-notifications-design.md](../specs/2026-08-20-retention-backup-and-notifications-design.md) — §4 in full, plus §2 D2, D3, D4.

## Global Constraints

- **Money is `Decimal`, never `float`.** No money appears in this piece; **disk sizes are `int` bytes and all arithmetic is integer** — never introduce a float ratio for the headroom multiplier.
- **Time is UTC-aware `datetime` from an injected `Clock`.** Never call `datetime.now()` in library code. `SystemClock` is the default only where no clock can be threaded (see Task 3).
- **Errors are classified.** A backup that cannot be taken raises `BackupError(FailClosedError)`. A bare `except: pass` is a defect.
- **Nothing this piece writes is ever deleted by the system** (spec D4). No rotation, no pruning, not even of failed partial files beyond the atomic-rename discipline.
- **Line length 100**, ruff format, full type annotations, `from __future__ import annotations` at the top of every module.
- **Docstrings state failure semantics at module level** — what happens when this module's dependency is unavailable.
- Verification command for every task: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_backup.py -q`, and `.\check.ps1` before the final commit of the piece.

---

### Task 1: `take_backup` — a consistent copy via `VACUUM INTO`

**Files:**
- Create: `tradebot/maintenance/__init__.py`
- Create: `tradebot/maintenance/backup.py`
- Test: `tests/unit/test_maintenance_backup.py`

**Interfaces:**
- Consumes: `tradebot.core.clock.Clock`, `tradebot.core.errors.FailClosedError`, `tradebot.core.logging.get_logger`, `tradebot.persistence.database.create_database`.
- Produces:
  - `HEADROOM_BYTES: Final[int]`
  - `class BackupError(FailClosedError)`
  - `@dataclass(frozen=True, slots=True) class BackupResult: path: Path; size_bytes: int`
  - `def backup_name(mode: str, at: datetime, *, revision: str = "") -> str`
  - `def free_bytes(directory: Path) -> int`
  - `def take_backup(engine: Engine, destination: Path, *, mode: str, clock: Clock, revision: str = "", probe: Callable[[Path], int] = free_bytes) -> BackupResult`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_maintenance_backup.py`:

```python
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
from tradebot.maintenance.backup import BackupError, backup_name, take_backup
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
    await store.append(
        Event(
            ts=AT,
            type=EventType.RISK_EVENT,
            aggregate_id="portfolio",
            payload={"rule": "test", "action": "recorded"},
        )
    )


class TestName:
    def test_a_daily_backup_is_named_for_its_mode_and_instant(self) -> None:
        assert backup_name("sim", AT) == "sim-20260820T040000Z.db"

    def test_a_pre_migration_backup_names_the_revision_it_leaves(self) -> None:
        assert backup_name("sim", AT, revision="a1b2c3") == "sim-pre-a1b2c3-20260820T040000Z.db"


class TestTakeBackup:
    async def test_the_copy_holds_what_the_source_held(self, database: Engine, tmp_path: Path) -> None:
        await _write_one_event(database)

        result = take_backup(
            database, tmp_path / "backups", mode="sim", clock=ManualClock(AT)
        )

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
            take_backup(
                create_database(None), tmp_path, mode="sim", clock=ManualClock(AT)
            )

    def test_a_second_backup_at_the_same_instant_is_refused_not_overwritten(
        self, database: Engine, tmp_path: Path
    ) -> None:
        """Nothing this module writes is ever destroyed by it, including by collision (D4)."""
        take_backup(database, tmp_path, mode="sim", clock=ManualClock(AT))

        with pytest.raises(BackupError):
            take_backup(database, tmp_path, mode="sim", clock=ManualClock(AT))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_backup.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradebot.maintenance'`

- [ ] **Step 3: Write minimal implementation**

Create `tradebot/maintenance/__init__.py`:

```python
"""Housekeeping that outlives a cycle: backups now, retention and compaction next.

Deliberately not part of `ops/`, which reads and never writes anything but its own cursor. This
package writes: it copies the database and, in a later piece, rewrites payloads in the event log.
"""

from __future__ import annotations
```

Create `tradebot/maintenance/backup.py`:

```python
"""Consistent database copies, taken while the bot runs.

`VACUUM INTO` produces one defragmented file that already contains everything in the WAL, holding
only a read transaction — a plain file copy would miss the WAL and a `.backup` loop would need its
own retry policy. The output is an ordinary SQLite database: recovery is copying it back over
`data/<mode>.db` with the process stopped (spec §4.1).

Failure semantics: anything that prevents a complete copy raises `BackupError`, and no caller
swallows it. The pre-migration hook refuses to upgrade; the daily tick reports a maintenance
failure and does not go on to compact anything.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from sqlalchemy import Engine

from tradebot.core.clock import Clock
from tradebot.core.errors import FailClosedError
from tradebot.core.logging import get_logger

logger = get_logger(__name__)

#: Spare room demanded beyond the copy itself. A full volume stops the event log accepting an
#: order intent (PLAN §1.4), so the backup must never be the write that fills it. Integer bytes:
#: no float ever enters this arithmetic.
HEADROOM_BYTES: Final[int] = 200 * 1024 * 1024


class BackupError(FailClosedError):
    """A backup that could not be taken. Refused upward, never logged and forgotten."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Where the copy landed and how big it is — what a report and a notification quote."""

    path: Path
    size_bytes: int


def backup_name(mode: str, at: datetime, *, revision: str = "") -> str:
    """`sim-20260820T040000Z.db`, or `sim-pre-<revision>-...` when a migration is about to run."""
    marker = f"-pre-{revision}" if revision else ""
    return f"{mode}{marker}-{at.strftime('%Y%m%dT%H%M%SZ')}.db"


def free_bytes(directory: Path) -> int:
    """Free space on the volume holding `directory`. A seam, so a test can starve it."""
    return shutil.disk_usage(directory).free


def source_path(engine: Engine) -> Path:
    """The file behind an engine. Raises for the in-memory database the test suite runs on."""
    database = engine.url.database
    if not database or database == ":memory:":
        raise BackupError("an in-memory database has nothing to back up")
    return Path(database)


def take_backup(
    engine: Engine,
    destination: Path,
    *,
    mode: str,
    clock: Clock,
    revision: str = "",
    probe: Callable[[Path], int] = free_bytes,
) -> BackupResult:
    """Copy the database into `destination`, or refuse and leave the volume as it was."""
    source = source_path(engine)
    destination.mkdir(parents=True, exist_ok=True)

    required = required_bytes(source)
    available = probe(destination)
    if available < required:
        raise BackupError(
            f"{destination} has {available} bytes free; a backup of {source.name} needs "
            f"{required}. Nothing was written."
        )

    target = destination / backup_name(mode, clock.now(), revision=revision)
    if target.exists():
        raise BackupError(f"{target} already exists; refusing to overwrite a backup")

    # VACUUM cannot run inside a transaction, and SQLAlchemy opens one implicitly. The literal is
    # escaped rather than bound because SQLite takes no parameter in this position.
    literal = str(target).replace("'", "''")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        try:
            connection.exec_driver_sql(f"VACUUM INTO '{literal}'")
        except Exception as exc:  # noqa: BLE001 — re-raised classified, never swallowed
            raise BackupError(f"could not write {target}: {exc}") from exc

    result = BackupResult(path=target, size_bytes=target.stat().st_size)
    logger.info(
        "database backed up",
        extra={"path": str(target), "bytes": result.size_bytes, "revision": revision},
    )
    return result


def required_bytes(source: Path) -> int:
    """The copy, a fifth again, and the headroom — all integer arithmetic (spec §4.4).

    Public because it is the one piece of this module worth asserting on directly: whether the
    WAL is counted cannot be observed from the outside without corrupting a live database.
    """
    live = source.stat().st_size
    wal = source.with_name(source.name + "-wal")
    if wal.exists():
        live += wal.stat().st_size
    return live + live // 5 + HEADROOM_BYTES
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_backup.py -q`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add tradebot/maintenance/ tests/unit/test_maintenance_backup.py
git commit -m "feat(maintenance): consistent database backups via VACUUM INTO"
```

---

### Task 2: The free-space guard refuses rather than fills

**Files:**
- Modify: `tests/unit/test_maintenance_backup.py` (add a class)

**Interfaces:**
- Consumes: `take_backup(..., probe=...)` from Task 1 — the `probe` parameter exists precisely so this test needs no real full disk.
- Produces: nothing new. This task proves the guard Task 1 wrote.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_maintenance_backup.py`:

```python
class TestFreeSpaceGuard:
    """Because nothing rotates (spec D4), the backup must not be what fills the volume."""

    def test_it_refuses_when_the_volume_is_too_full(
        self, database: Engine, tmp_path: Path
    ) -> None:
        with pytest.raises(BackupError, match="bytes free"):
            take_backup(
                database,
                tmp_path / "backups",
                mode="sim",
                clock=ManualClock(AT),
                probe=lambda _: 1024,
            )

    def test_a_refused_backup_writes_nothing_at_all(
        self, database: Engine, tmp_path: Path
    ) -> None:
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
```

Extend the import from `tradebot.maintenance.backup` to include `HEADROOM_BYTES` and
`required_bytes`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_backup.py::TestFreeSpaceGuard -q`
Expected: FAIL — `ImportError: cannot import name 'HEADROOM_BYTES'` if the import was not extended; otherwise PASS, since Task 1 implemented the guard. **If all three pass immediately, that is the correct outcome** — this task is a guard-rail test, and you record that in the commit message rather than inventing a failure.

- [ ] **Step 3: Extend the import**

```python
from tradebot.maintenance.backup import (
    HEADROOM_BYTES,
    BackupError,
    backup_name,
    required_bytes,
    take_backup,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_maintenance_backup.py -q`
Expected: PASS — 10 passed

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_maintenance_backup.py
git commit -m "test(maintenance): the free-space guard refuses and writes nothing"
```

---

### Task 3: Back up before a migration that will move the revision

**Files:**
- Modify: `tradebot/persistence/database.py:39-90` (`create_database`, `run_migrations`)
- Create: `tests/unit/test_migration_backup.py`

**Interfaces:**
- Consumes: `take_backup`, `BackupError` from Task 1.
- Produces:
  - `def backup_destination(path: Path) -> Path` in `tradebot/maintenance/backup.py`
  - `run_migrations(engine: Engine, *, backup: Path | None = None, mode: str = "", clock: Clock | None = None, take: Callable[..., object] = take_backup) -> None` — every new keyword defaults to "do exactly what this did before", so existing callers and the whole test suite are unaffected. `take` is a seam of the same kind as `probe` in Task 1: it lets a test starve the backup without a full disk, and it is the *only* injected parameter — there is no test-only branch inside the function.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_migration_backup.py`:

```python
"""The one operation that can destroy records that cannot be recreated.

`run_migrations` runs on *every* process start, so the cost of the check matters as much as the
protection: it is one revision read when nothing will change, and a copy only when something will
(spec §4.5).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from tradebot.core.clock import ManualClock
from tradebot.maintenance.backup import BackupError, backup_destination
from tradebot.persistence.database import create_database, run_migrations
from tests.unit.test_maintenance_backup import AT


class TestDestination:
    def test_it_defaults_beside_the_database_under_its_mode(self, tmp_path: Path) -> None:
        assert backup_destination(tmp_path / "sim.db") == tmp_path / "backups" / "sim"

    def test_an_environment_override_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADEBOT_BACKUP_DIR", str(tmp_path / "elsewhere"))

        assert backup_destination(tmp_path / "sim.db") == tmp_path / "elsewhere" / "sim"


class TestPreMigrationBackup:
    def test_a_database_already_at_head_is_not_copied(self, tmp_path: Path) -> None:
        """The 99% case. A backup on every start would be a backup nobody reads."""
        path = tmp_path / "sim.db"
        create_database(path)  # first call migrates base -> head
        destination = backup_destination(path)
        for stale in destination.glob("*.db"):
            stale.unlink()

        create_database(path)  # second call: nothing to do

        assert list(destination.glob("*.db")) == []

    def test_a_fresh_database_is_backed_up_before_its_first_upgrade(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "sim.db"

        create_database(path)

        copies = list(backup_destination(path).glob("*.db"))
        assert len(copies) == 1
        assert "-pre-base-" in copies[0].name

    def test_a_failing_backup_stops_the_migration(self, tmp_path: Path) -> None:
        """Fail closed: an un-backed-up upgrade of a ledger must not proceed.

        Built with `create_engine` rather than `create_database`, so the database is genuinely
        un-migrated and the revision genuinely differs from head — no override, and therefore no
        test-only branch in the production path.
        """
        from sqlalchemy import create_engine

        path = tmp_path / "sim.db"
        engine = create_engine(f"sqlite:///{path}", future=True)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise BackupError("no room")

        with pytest.raises(BackupError):
            run_migrations(
                engine,
                backup=backup_destination(path),
                mode="sim",
                clock=ManualClock(AT),
                take=refuse,
            )

    def test_a_blocked_migration_leaves_the_schema_untouched(self, tmp_path: Path) -> None:
        from sqlalchemy import create_engine, inspect

        path = tmp_path / "sim.db"
        engine = create_engine(f"sqlite:///{path}", future=True)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise BackupError("no room")

        with pytest.raises(BackupError):
            run_migrations(
                engine, backup=tmp_path / "b", mode="sim", clock=ManualClock(AT), take=refuse
            )

        assert "events" not in inspect(engine).get_table_names()

    def test_an_in_memory_database_is_skipped_by_construction(self) -> None:
        """The entire test suite runs on this path and must never touch the filesystem."""
        run_migrations(create_database(None))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_migration_backup.py -q`
Expected: FAIL — `ImportError: cannot import name 'backup_destination'`

- [ ] **Step 3: Write the implementation**

Add to `tradebot/maintenance/backup.py`:

```python
import os

#: Where a backup lands when nothing says otherwise. An operator is encouraged in OPERATIONS.md to
#: point this at another volume: a copy beside the database survives a bad migration, not a bad
#: disk.
BACKUP_DIR_ENV: Final = "TRADEBOT_BACKUP_DIR"


def backup_destination(path: Path) -> Path:
    """The directory backups of `path` belong in, honouring the environment override."""
    override = os.environ.get(BACKUP_DIR_ENV)
    root = Path(override) if override else path.parent / "backups"
    return root / path.stem
```

Replace `run_migrations` in `tradebot/persistence/database.py` and adjust its caller:

```python
def create_database(path: Path | None) -> Engine:
    ...  # unchanged body through the `_configure` listener
    if path is None:
        run_migrations(engine)
    else:
        run_migrations(
            engine, backup=backup_destination(path), mode=path.stem, clock=SystemClock()
        )
    return engine


def run_migrations(
    engine: Engine,
    *,
    backup: Path | None = None,
    mode: str = "",
    clock: Clock | None = None,
    take: Callable[..., object] = take_backup,
) -> None:
    """Bring the database to the head revision, copying it first if that will change anything.

    Alembic is the single source of schema truth from day one, including for a fresh database.
    `create_all` would work today and then leave the first schema change with no upgrade path —
    unacceptable for a database that holds financial records that cannot be recreated. For the
    same reason the upgrade is preceded by a backup whenever it will actually move the revision,
    and a backup that cannot be taken stops the upgrade (spec §4.5).

    The migration runs on the engine's *own* connection: an in-memory database lives inside its
    connection, so opening a second one would migrate a different database.
    """
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))

    if backup is not None:
        head = ScriptDirectory.from_config(config).get_current_head()
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        if current != head:
            take(
                engine,
                backup,
                mode=mode,
                clock=clock or SystemClock(),
                revision=current or "base",
            )

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
```

New imports for `database.py`:

```python
from collections.abc import Callable

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from tradebot.core.clock import Clock, SystemClock
from tradebot.maintenance.backup import backup_destination, take_backup
```

The revision check runs on its **own** connection and completes before `engine.begin()` opens the
migration transaction — `VACUUM INTO` cannot run inside one, and nesting the two would deadlock a
file database against itself.

- [ ] **Step 4: Run the new tests, then the whole suite**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_migration_backup.py -q`
Expected: PASS — 6 passed

Run: `.venv\Scripts\python.exe -m pytest tests/unit -q`
Expected: PASS — no regressions. Every existing caller passes `backup=None` implicitly, so no test
gains a filesystem dependency.

- [ ] **Step 5: Commit**

```bash
git add tradebot/persistence/database.py tradebot/maintenance/backup.py tests/unit/test_migration_backup.py
git commit -m "feat(persistence): back up before a migration that moves the revision"
```

---

### Task 4: `tradebot maintenance backup|status`

**Files:**
- Modify: `tradebot/__main__.py` (add the subparser near the `config` parser at :259, and a command function beside `risk_command` at :741)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: `take_backup`, `backup_destination`, `BackupResult`, `BackupError` from Tasks 1 and 3; `tradebot.app.database_path`.
- Produces: `async def maintenance_command(args: argparse.Namespace) -> int` — exit 0 on success, **exit 5 on a refused backup**, matching `report promotion`'s convention that a refusal is a distinct non-zero code rather than a crash.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_cli.py`, following the existing invocation style in that file:

```python
class TestMaintenanceBackup:
    async def test_it_writes_a_copy_and_reports_where(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        create_database(tmp_path / "sim.db")

        code = await main(["maintenance", "backup", "--mode", "sim", "--data-dir", str(tmp_path)])

        assert code == 0
        assert list((tmp_path / "backups" / "sim").glob("*.db"))
        assert "backed up" in capsys.readouterr().out

    async def test_a_refusal_exits_five_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        create_database(tmp_path / "sim.db")
        monkeypatch.setenv("TRADEBOT_BACKUP_DIR", str(tmp_path / "full"))
        monkeypatch.setattr("tradebot.maintenance.backup.free_bytes", lambda _: 1024)

        code = await main(["maintenance", "backup", "--mode", "sim", "--data-dir", str(tmp_path)])

        assert code == 5

    async def test_status_reports_the_inventory_without_writing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        create_database(tmp_path / "sim.db")
        await main(["maintenance", "backup", "--mode", "sim", "--data-dir", str(tmp_path)])

        code = await main(["maintenance", "status", "--mode", "sim", "--data-dir", str(tmp_path)])

        assert code == 0
        out = capsys.readouterr().out
        assert "backups: 1" in out
        assert "free:" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -k Maintenance -q`
Expected: FAIL — `argparse` error, "invalid choice: 'maintenance'"

- [ ] **Step 3: Write the implementation**

Subparser, beside the others:

```python
    maintenance = subparsers.add_parser("maintenance", help="backups and housekeeping")
    maintenance.add_argument("action", choices=("backup", "status"))
    maintenance.add_argument(
        "--backup-dir", type=Path, default=None, help="overrides TRADEBOT_BACKUP_DIR"
    )
```

Command function:

```python
async def maintenance_command(args: argparse.Namespace) -> int:
    """Housekeeping against one mode's database. Reads and copies; never trades.

    Deliberately does not build an `Application`: a backup must be takeable while the bot is
    running, and wiring a second one would open a second writer against the same file.
    """
    path = database_path(args.mode, root=args.data_dir)
    if not path.exists():
        print(f"no database at {path}")
        return 1

    destination = args.backup_dir or backup_destination(path)
    engine = create_database(path)
    if args.action == "status":
        copies = sorted(destination.glob("*.db")) if destination.exists() else []
        newest = copies[-1].name if copies else "none"
        free = free_bytes(destination) if destination.exists() else free_bytes(path.parent)
        print(f"database: {path} ({path.stat().st_size} bytes)")
        print(f"backups: {len(copies)} in {destination}, newest {newest}")
        print(f"free: {free} bytes")
        return 0

    try:
        result = take_backup(
            engine, destination, mode=args.mode.value, clock=SystemClock()
        )
    except BackupError as exc:
        print(f"backup refused: {exc}")
        return 5
    print(f"backed up to {result.path} ({result.size_bytes} bytes)")
    return 0
```

Register it wherever the other `*_command` functions are dispatched from `main`, following the
existing pattern in that function exactly.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/unit/test_cli.py -k Maintenance -q`
Expected: PASS — 3 passed

- [ ] **Step 5: Run the full gate and commit**

Run: `.\check.ps1`
Expected: format, lint, mypy, tests and coverage gates all pass. `tradebot/maintenance/` is outside
the 95% packages, so its gate is 80%; `backup.py` should land above 95% on these tests alone.

```bash
git add tradebot/__main__.py tests/unit/test_cli.py
git commit -m "feat(cli): tradebot maintenance backup|status"
```

---

### Task 5: Document the restore drill

**Files:**
- Modify: `docs/OPERATIONS.md` (new section beside the arming procedure; precondition 17 at :231)
- Modify: `CLAUDE.md` (Commands section, after the validation ladder)

**Interfaces:**
- Consumes: the CLI from Task 4.
- Produces: no code.

- [ ] **Step 1: Write the restore procedure**

Add to `docs/OPERATIONS.md`. A backup nobody has restored is a hypothesis, so the drill is written
as steps an operator performs, not as prose about the feature:

```markdown
## Restoring from a backup

1. **Stop the process.** `Ctrl+C` the `serve`/`run` session and confirm nothing is cycling —
   a restore under a live writer produces a database that is neither copy.
2. **Identify the copy.** `python -m tradebot maintenance status --mode <mode>` lists the
   inventory and the newest file. A `-pre-<revision>-` name is the copy taken immediately before
   that schema change.
3. **Move the current database aside** rather than deleting it:
   `Move-Item data\<mode>.db data\<mode>.db.broken` (and the `-wal` / `-shm` beside it).
4. **Copy the backup into place**: `Copy-Item <backup>.db data\<mode>.db`. The file is an
   ordinary SQLite database; there is nothing to import.
5. **Start and reconcile.** `python -m tradebot risk status --mode <mode>` first, then start
   normally. The startup sequence reconciles against the venue, which is what makes any gap
   between the copy and reality visible rather than silent.
6. **Read what the gap cost.** Cycles, orders and fills between the backup and now are gone from
   the log. The venue still holds the truth; reconciliation will classify the difference.
```

Change precondition 17's "never" column, since it is now answerable: retention is set in the
`maintenance` config document (Piece B) and backups are taken daily.

- [ ] **Step 2: Add the commands to CLAUDE.md**

```markdown
Backups are taken daily by a running process, before any schema migration, and on demand. Nothing
is ever auto-deleted; pruning is a human act ([ADR 0028](docs/adr/0028-...)):

​```powershell
.venv\Scripts\python.exe -m tradebot maintenance backup --mode sim
.venv\Scripts\python.exe -m tradebot maintenance status --mode sim
​```
```

- [ ] **Step 3: Verify the drill by performing it**

Run, against a scratch copy rather than `data/sim.db`:

```powershell
Copy-Item data\sim.db $env:TEMP\drill.db
.venv\Scripts\python.exe -m tradebot maintenance backup --mode sim --data-dir $env:TEMP
```

Expected: a `.db` under `$env:TEMP\backups\sim\`, openable, and `maintenance status` counting it.
**Do not perform steps 3-4 of the drill against `data/sim.db` itself.**

- [ ] **Step 4: Commit**

```bash
git add docs/OPERATIONS.md CLAUDE.md
git commit -m "docs: restore drill and the maintenance commands"
```

---

## What this piece deliberately does not do

- **No daily tick.** `MaintenanceService`, `MAINTENANCE_RAN`, and the supervisor wiring arrive in
  Piece B, where they schedule compaction as well. Until then a backup is taken on demand and
  before migrations only.
- **No notification.** A refused backup exits 5 and logs; making it visible on the dashboard is
  Piece C. This ordering is deliberate — the safety net ships before the destructive feature it
  protects, and before the thing that merely reports on it.
- **No rotation, ever** (D4).
