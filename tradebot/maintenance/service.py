"""One housekeeping pass a day: back up, archive, compact, then delete what has aged out.

The order **is** the design. A backup that fails stops everything destructive behind it, and
nothing is compacted for a day whose archive did not verify — so the payload a row is about to
lose always exists in a file first. Deletion runs last and only over the archive directory.

Every filesystem step runs in a worker thread. `VACUUM INTO` on a multi-gigabyte database is
seconds to minutes of blocking I/O, and gzipping a day of transcripts is the same class of work;
this task shares its event loop with the supervisor, the execution monitor and the dashboard's
socket, none of which may stall behind housekeeping (spec §6.3). Compaction is already off the
loop — it goes through `SingleWriter`'s executor.

Failure semantics: `run_once` never raises. It returns a report whose `failure` is empty on
success and appends one `MAINTENANCE_RAN` event either way, because a maintenance run nobody can
audit is worse than one that did not happen. `run` wraps it in a loop that survives any exception,
because a maintenance defect must never be what stops the bot trading.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from tradebot.core.clock import Clock
from tradebot.core.config import MaintenancePolicy
from tradebot.core.errors import TradebotError
from tradebot.core.events import Event, EventType
from tradebot.core.logging import get_logger
from tradebot.maintenance.archive import archive_day, delete_aged
from tradebot.maintenance.backup import take_backup
from tradebot.maintenance.compaction import compact_day, pending_days
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.store import EventStore

logger = get_logger(__name__)

#: How often the loop asks whether a pass is due. Far finer than the daily boundary it watches, so
#: a process started at any hour begins its first pass promptly rather than at the next midnight.
DEFAULT_POLL_SECONDS = 300.0

#: The aggregate every pass is recorded against. One id, so `read_types` finds them all.
AGGREGATE = "maintenance"


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    """What one pass did. Rendered into the event, and in Piece C into a notification."""

    backup: Path | None = None
    archived_days: int = 0
    compacted_rows: int = 0
    deleted_archives: int = 0
    failure: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure


class MaintenanceService:
    """The daily pass, and the loop that notices it is due."""

    def __init__(
        self,
        *,
        store: EventStore,
        writer: SingleWriter,
        clock: Clock,
        mode: str,
        archive_root: Path,
        backup_dir: Path,
        policy: Callable[[], MaintenancePolicy],
        take: Callable[..., object] = take_backup,
    ) -> None:
        self.store = store
        self.writer = writer
        self._clock = clock
        self._mode = mode
        self._archive_root = archive_root
        self._backup_dir = backup_dir
        #: Read fresh at every pass, never captured — an edit takes effect at the next tick
        #: without a restart (ADR 0021's rule, applied here).
        self._policy = policy
        #: The one injected seam, so a test can starve the backup without a full disk.
        self._take = take

    async def run(self, *, poll_seconds: float = DEFAULT_POLL_SECONDS) -> None:
        """Poll until cancelled. Started alongside the supervisor by `run` and `serve`.

        Paced on the injected `Clock` rather than `asyncio.sleep`, unlike the WebSocket tail's
        deliberate departure: this is **domain** time. A pass is due on a calendar boundary and
        ages files by day, so a backtest stepping its clock a month forward must not trigger
        thirty backups. That also means only a real-clock process may start this — under a
        `ManualClock`, whose `sleep` returns immediately, the loop would spin.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            # `run_once` already records its own failures; this is the belt to that braces. A
            # maintenance defect must not end the loop or stop trading.
            except Exception:
                logger.exception("maintenance pass failed; the loop continues")
            await self._clock.sleep(poll_seconds)

    async def run_once(
        self, *, force: bool = False, override: MaintenancePolicy | None = None
    ) -> MaintenanceReport | None:
        """One pass, or `None` when one has already run today.

        A pass that *failed* still counts as today's run. It raises a HIGH notification and a
        human is now the next step; retrying every five minutes against a full disk would only
        repeat the alarm.

        `force` and `override` exist for `tradebot maintenance compact` and are never used by the
        tick. Dueness is the *tick's* rule — a human who typed the command meant it — and an
        override is recorded on the event as such, so "why did that get deleted" stays answerable
        for a pass that did not run under the published policy (spec §7).
        """
        now = self._clock.now()
        if not force and not self._is_due(now):
            return None

        policy = override or self._policy()
        try:
            report = await self._pass(now, policy)
        except Exception as exc:
            logger.exception("unclassified failure in the maintenance pass")
            report = MaintenanceReport(failure=f"unclassified: {exc}")
        await self._record(now, policy, report, overridden=override is not None)
        return report

    async def _pass(self, now: datetime, policy: MaintenancePolicy) -> MaintenanceReport:
        """Back up, archive, compact, delete — in that order, stopping at the first failure."""
        try:
            backup = await asyncio.to_thread(
                self._take,
                self.store.engine,
                self._backup_dir,
                mode=self._mode,
                clock=self._clock,
            )
        except (TradebotError, OSError) as exc:
            # Nothing destructive has run yet, and nothing will: compacting without a fresh
            # backup is the one ordering that can lose data (spec §6.4).
            return MaintenanceReport(failure=f"backup: {exc}")

        backup_path = getattr(backup, "path", None)
        archived = 0
        compacted = 0
        horizon = now.date() - timedelta(days=policy.compact_after_days)
        try:
            for day in pending_days(self.store.engine, before=horizon):
                archived, compacted = await self._archive_then_compact(
                    day, now, archived, compacted
                )
        except (TradebotError, OSError) as exc:
            return MaintenanceReport(
                backup=backup_path,
                archived_days=archived,
                compacted_rows=compacted,
                failure=f"archive: {exc}",
            )

        removed, failures = await asyncio.to_thread(
            delete_aged,
            self._archive_root,
            self._mode,
            before=now.date() - timedelta(days=policy.archive_keep_days),
        )
        return MaintenanceReport(
            backup=backup_path,
            archived_days=archived,
            compacted_rows=compacted,
            deleted_archives=len(removed),
            failure="; ".join(failures),
        )

    async def _archive_then_compact(
        self, day: date, now: datetime, archived: int, compacted: int
    ) -> tuple[int, int]:
        """One day, in the only order that is safe. Returns the running totals."""
        result = await asyncio.to_thread(
            archive_day, self.store.engine, self._archive_root, mode=self._mode, day=day
        )
        if not result.written:
            return archived, compacted
        rewritten = await compact_day(self.writer, day=day, archive=result, at=now)
        return archived + 1, compacted + rewritten

    def _is_due(self, now: datetime) -> bool:
        """Due when the newest recorded pass is not today (spec §6.2)."""
        recorded = self.store.read_types(EventType.MAINTENANCE_RAN)
        return not recorded or recorded[-1].ts.date() != now.date()

    async def _record(
        self,
        now: datetime,
        policy: MaintenancePolicy,
        report: MaintenanceReport,
        *,
        overridden: bool = False,
    ) -> None:
        """One event per pass — the audit line, the dueness marker, and Piece C's alert source."""
        await self.store.append(
            Event(
                ts=now,
                type=EventType.MAINTENANCE_RAN,
                aggregate_id=AGGREGATE,
                payload={
                    "mode": self._mode,
                    "outcome": "ok" if report.ok else "failed",
                    "detail": report.failure,
                    "backup": str(report.backup) if report.backup else "",
                    "archived_days": report.archived_days,
                    "compacted_rows": report.compacted_rows,
                    "deleted_archives": report.deleted_archives,
                    "compact_after_days": policy.compact_after_days,
                    "archive_keep_days": policy.archive_keep_days,
                    #: Whether these windows came from a one-off CLI flag rather than the
                    #: published document — otherwise the log would attribute a deletion to a
                    #: policy that was never in force (spec §7).
                    "overridden": overridden,
                },
            )
        )
