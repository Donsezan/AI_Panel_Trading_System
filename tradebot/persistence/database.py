"""Database connection and the single writer that owns it.

One writer, enforced rather than assumed (PLAN §2.6). Writes are funnelled through a
single-threaded executor, so there is exactly one thread that may mutate the database and its
identity can be asserted. An `asyncio.Lock` preserves submission order on top of that, which
also gives the event log a stable total order.

SQLite runs in WAL mode: readers never block the writer, which is what lets the dashboard query
projections while a cycle is mid-flight.

Failure semantics: a failed unit of work rolls back its whole transaction — an event and the
projection it feeds are written together or not at all, so the log can never describe a state
the projections don't show.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import StaticPool

from tradebot.core.errors import SingleWriterViolationError

T = TypeVar("T")

WRITER_THREAD_NAME = "tradebot-db-writer"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_database(path: Path | None) -> Engine:
    """Open (creating if needed) the database for one mode.

    Each mode uses its own file, so a paper ledger can never be read as a live one (PLAN §2.4).
    `None` gives an in-memory database for tests.
    """
    if path is None:
        # An in-memory database lives *inside its connection*, so every pooled connection would
        # otherwise get its own empty schema. StaticPool keeps one connection for the process.
        engine = create_engine(
            "sqlite:///:memory:",
            future=True,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            f"sqlite:///{path}",
            future=True,
            # The writer runs on its own thread; readers on others. WAL makes that safe, and
            # serialization is enforced by `SingleWriter`, not by pysqlite's thread check.
            connect_args={"check_same_thread": False},
        )

    @event.listens_for(engine, "connect")
    def _configure(connection: DBAPIConnection, _record: object) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")  # an order must survive a power cut
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    run_migrations(engine)
    return engine


def run_migrations(engine: Engine) -> None:
    """Bring the database to the head revision.

    Alembic is the single source of schema truth from day one, including for a fresh database.
    `create_all` would work today and then leave the first schema change with no upgrade path —
    unacceptable for a database that holds financial records that cannot be recreated.

    The migration runs on the engine's *own* connection: an in-memory database lives inside its
    connection, so opening a second one would migrate a different database.
    """
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


class SingleWriter:
    """Serializes every write and guarantees exactly one writing thread."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=WRITER_THREAD_NAME)
        self._lock = asyncio.Lock()

    async def run(self, work: Callable[[Connection], T]) -> T:
        """Execute `work` in the writer thread inside one transaction."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, self._execute, work)

    def _execute(self, work: Callable[[Connection], T]) -> T:
        self._assert_writer_thread()
        with self._engine.begin() as connection:
            return work(connection)

    @staticmethod
    def _assert_writer_thread() -> None:
        name = threading.current_thread().name
        if not name.startswith(WRITER_THREAD_NAME):
            raise SingleWriterViolationError(f"database write attempted from thread {name!r}")

    def close(self) -> None:
        self._executor.shutdown(wait=True)
