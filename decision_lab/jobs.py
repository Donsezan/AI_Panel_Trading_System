"""One writer at a time, and runs that *are* the CLI rather than a copy of it (spec §12.4).

A start spawns `python -m decision_lab <argv>` as a child process. The page therefore cannot
diverge from the command — every refusal already has an exit code (3 dataset, 4 candidate, 5
budget, 6 gate, 7 busy) — a six-month `calibrate long` driving `BacktestHarness` never runs on the
event loop serving the page, and Stop is a real termination rather than a cooperative cancel
through code that offers none.

The lock is an **OS advisory lock** (`msvcrt` on Windows, `fcntl` on POSIX) rather than a pid file
with a staleness rule: the operating system releases it when the holder dies, and there is no
portable way to ask whether a pid is alive — on Windows `os.kill(pid, 0)` *terminates* the
process. The sidecar beside it carries argv and pid so a refusal can name the holder; it is read
for the message and never trusted for the decision.

Failure semantics: an absent workspace reads as free. A job whose record says it never finished
and which no longer holds the lock is reported as ended without being recorded — the dashboard
was restarted while it ran — never as still running, because a page claiming a sweep is running
is a page an operator waits on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any, Final

from decision_lab import params
from decision_lab.params import HOLDER_FILE, JOBS_DIR, LOCK_FILE
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel, UtcDatetime

logger = get_logger("decision_lab.jobs")

#: Where the launcher runs its children: the repo root, so a relative `--data data\history` on a
#: form means what it means in a terminal.
REPO_ROOT: Final = Path(__file__).resolve().parents[1]

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - one branch per platform
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - one branch per platform
    msvcrt = None  # type: ignore[assignment]


class Busy(ConfigError):  # noqa: N818 — `jobs.Busy` is the public name the interface contract uses
    """The workspace is held by another run. `cli.main` maps this to exit code 7."""


class Holder(DomainModel):
    """Who holds the lock. For the refusal message; never for the decision."""

    pid: int
    argv: tuple[str, ...]
    started_at: UtcDatetime


class JobRecord(DomainModel):
    """One run the dashboard launched, and how it ended."""

    job_id: str
    argv: tuple[str, ...]
    label: str = ""
    pid: int = 0
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    exit_code: int | None = None


#: Children this process started, so `stop` and `refresh` act on the process itself rather than
#: on a pid — killing by pid is the one thing that is not portable, and a recycled pid is the
#: failure it produces.
_RUNNING: dict[str, subprocess.Popen[bytes]] = {}


def _root(workspace: Path | None) -> Path:
    # `Path(...)` rather than a bare pass-through: the subprocess test constructs a `WorkspaceLock`
    # from a string baked into a script literal, which is a path in substance and a `str` in type.
    return Path(workspace) if workspace else params.workspace_root()


def _jobs_dir(workspace: Path | None) -> Path:
    return _root(workspace) / JOBS_DIR


def _take(handle: IO[Any]) -> bool:
    """Try to take the lock without blocking. True when this process now holds it."""
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _drop(handle: IO[Any]) -> None:
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:  # Already gone: the process is exiting and the OS has released it.
        pass


class WorkspaceLock:
    """The one writer's claim on a workspace, held for the life of a command."""

    def __init__(self, *, workspace: Path | None = None) -> None:
        self._workspace = workspace
        self._path = _root(workspace) / LOCK_FILE
        self._sidecar = _root(workspace) / HOLDER_FILE
        self._handle: IO[Any] | None = None

    def acquire(self, argv: Sequence[str], *, clock: Clock) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        if not _take(handle):
            handle.close()
            raise Busy(_busy_message(holder(workspace=self._workspace)))
        # `_take` succeeded, so the OS lock is live from this line on: everything after it must
        # release that lock on its own way out, not just the `_take`-failure branch above. An
        # in-process caller — a dashboard route, a test harness — outlives a CLI subprocess, so a
        # write that fails here (disk full, a permission fault, a bad `Holder`) must not strand the
        # lock for the rest of that process's life.
        try:
            self._sidecar.write_text(
                Holder(pid=os.getpid(), argv=tuple(argv), started_at=clock.now()).model_dump_json(),
                encoding="utf-8",
            )
        except BaseException:
            _drop(handle)
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        self._sidecar.unlink(missing_ok=True)
        _drop(self._handle)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> WorkspaceLock:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _busy_message(found: Holder | None) -> str:
    if found is None:
        # The lock was taken and released between the refusal and the read. Rare, and the honest
        # message is the one that does not invent a holder.
        return (
            "another run holds this workspace. Wait for it to finish, or stop it from the "
            "dashboard's Runs page"
        )
    return (
        f"another run holds this workspace: pid {found.pid} running "
        f"`{' '.join(found.argv)}` since {found.started_at.isoformat()}. Two writers over one "
        "workspace would interleave registry rows and sweep files (§12.4)"
    )


def holder(*, workspace: Path | None = None) -> Holder | None:
    """Who holds the workspace, or `None` when it is free.

    Probes the lock rather than trusting the sidecar: a sidecar left behind by a killed process is
    stale, and the OS lock never is.
    """
    path = _root(workspace) / LOCK_FILE
    if not path.is_file():
        return None
    with path.open("a+b") as handle:
        if _take(handle):
            _drop(handle)
            return None
    sidecar = _root(workspace) / HOLDER_FILE
    if not sidecar.is_file():
        return Holder(pid=0, argv=(), started_at=_epoch())
    return Holder.model_validate_json(sidecar.read_text(encoding="utf-8"))


def _epoch() -> Any:
    from datetime import UTC, datetime

    return datetime(1970, 1, 1, tzinfo=UTC)


def record_path(job_id: str, *, workspace: Path | None = None) -> Path:
    return _jobs_dir(workspace) / f"{job_id}.json"


def log_path(job_id: str, *, workspace: Path | None = None) -> Path:
    return _jobs_dir(workspace) / f"{job_id}.log"


def start(
    argv: Sequence[str],
    *,
    label: str,
    clock: Clock,
    workspace: Path | None = None,
) -> JobRecord:
    """Spawn one run of this tool's own CLI. Refuses while the workspace is held.

    The child takes the lock itself, which is the real guarantee; this check is so the operator
    is refused *on the page* rather than shown a job that exited 7 a second after they started it.
    """
    if (found := holder(workspace=workspace)) is not None:
        raise Busy(_busy_message(found))

    directory = _jobs_dir(workspace)
    directory.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log = log_path(job_id, workspace=workspace)
    environment = dict(os.environ)
    if workspace is not None:
        # The child is a separate process, so a test workspace has to reach it as configuration.
        environment["DECISION_LAB_WORKSPACE"] = str(workspace)
    with log.open("wb") as sink:
        process = subprocess.Popen(  # noqa: S603 — argv is built here, never from a request
            [sys.executable, "-m", "decision_lab", *argv],
            cwd=REPO_ROOT,
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=environment,
        )
    record = JobRecord(
        job_id=job_id,
        argv=tuple(argv),
        label=label,
        pid=process.pid,
        started_at=clock.now(),
    )
    _RUNNING[job_id] = process
    _write(record, workspace=workspace)
    logger.info("job started", extra={"job_id": job_id, "argv": list(argv), "label": label})
    return record


def _write(record: JobRecord, *, workspace: Path | None) -> None:
    record_path(record.job_id, workspace=workspace).write_text(
        record.model_dump_json(), encoding="utf-8"
    )


def refresh(record: JobRecord, *, clock: Clock, workspace: Path | None = None) -> JobRecord:
    """Update a record from the process itself. A job we did not start is returned unchanged."""
    process = _RUNNING.get(record.job_id)
    if process is None or record.exit_code is not None:
        return record
    code = process.poll()
    if code is None:
        return record
    updated = record.model_copy(update={"exit_code": code, "finished_at": clock.now()})
    _RUNNING.pop(record.job_id, None)
    _write(updated, workspace=workspace)
    return updated


def stop(job_id: str, *, workspace: Path | None = None) -> bool:  # noqa: ARG001
    """Terminate a running child. True when there was one to terminate.

    Only jobs this process started can be stopped, deliberately: killing by a pid read from a file
    is how a recycled pid gets terminated instead, and the alternative — leaving a stale process
    running — is visible on the page rather than silent. `workspace` is unused here — `_RUNNING`
    already holds the live `Popen`, keyed by `job_id` alone — and kept for symmetry with every
    other function in this module's interface, all of which do read the workspace on disk.
    """
    process = _RUNNING.get(job_id)
    if process is None:
        return False
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - a child ignoring SIGTERM
        process.kill()
        process.wait(timeout=10)
    logger.info("job stopped", extra={"job_id": job_id})
    return True


def history(*, limit: int = 20, workspace: Path | None = None) -> tuple[JobRecord, ...]:
    directory = _jobs_dir(workspace)
    if not directory.is_dir():
        return ()
    found = [
        JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in directory.glob("*.json")
    ]
    return tuple(sorted(found, key=lambda one: one.started_at, reverse=True))[:limit]


def status_of(record: JobRecord, *, workspace: Path | None = None) -> str:
    """`running`, `exit <code>`, or `ended without being recorded`.

    The last is what a dashboard restart looks like from here, and it is not `running`: a page
    claiming a sweep is still going is a page an operator waits on.
    """
    if record.exit_code is not None:
        return f"exit {record.exit_code}"
    process = _RUNNING.get(record.job_id)
    if process is not None and process.poll() is None:
        return "running"
    found = holder(workspace=workspace)
    if found is not None and found.pid == record.pid:
        return "running"
    return "ended without being recorded"


def log_tail(job_id: str, *, lines: int = 200, workspace: Path | None = None) -> str:
    """The end of a job's output. An absent log is an empty string, never an error."""
    path = log_path(job_id, workspace=workspace)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])
