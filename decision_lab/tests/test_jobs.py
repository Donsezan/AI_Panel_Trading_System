"""§12.4 — one writer at a time, and a run that is the CLI rather than a copy of it.

The lock is an OS advisory lock, so a killed process releases it: a lock whose staleness has to
be *detected* is one that eventually strands the workspace, and there is no portable way to ask
whether a pid is alive without risking terminating it on Windows.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest

from decision_lab import jobs
from tradebot.core.clock import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    from datetime import UTC, datetime

    return ManualClock(datetime(2026, 9, 6, 12, 0, tzinfo=UTC))


def test_a_free_workspace_has_no_holder(tmp_path: Path) -> None:
    assert jobs.holder(workspace=tmp_path) is None


def test_the_lock_names_its_holder_and_refuses_a_second_taker(
    tmp_path: Path, clock: ManualClock
) -> None:
    first = jobs.WorkspaceLock(workspace=tmp_path)
    first.acquire(("sweep", "--corpus", "abc"), clock=clock)
    try:
        found = jobs.holder(workspace=tmp_path)
        assert found is not None
        assert found.argv == ("sweep", "--corpus", "abc")

        with pytest.raises(jobs.Busy, match="sweep"):
            jobs.WorkspaceLock(workspace=tmp_path).acquire(("report",), clock=clock)
    finally:
        first.release()

    assert jobs.holder(workspace=tmp_path) is None, "releasing frees it"


def test_a_lock_held_by_another_process_is_seen_and_released_when_it_exits(
    tmp_path: Path, clock: ManualClock
) -> None:
    """The property the OS gives us for free, asserted because the whole design leans on it."""
    import subprocess
    import textwrap

    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(Path.cwd())!r})
        from datetime import UTC, datetime
        from decision_lab import jobs
        from tradebot.core.clock import ManualClock
        lock = jobs.WorkspaceLock(workspace={str(tmp_path)!r})
        lock.acquire(("sweep",), clock=ManualClock(datetime(2026, 9, 6, tzinfo=UTC)))
        sys.stdout.write("held\\n")
        sys.stdout.flush()
        time.sleep(30)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        assert jobs.holder(workspace=tmp_path) is not None
    finally:
        child.kill()
        child.wait(timeout=10)
        # Closed explicitly rather than left for the garbage collector: a killed child can leave
        # its end of the pipe in a state where finalizing the handle later — GC's timing is not
        # ours to pick, so it can land inside a *later* test — raises an ignored `OSError` that
        # `filterwarnings = ["error"]` promotes into a failure of whatever test happened to be
        # running when the collector got to it.
        if child.stdout is not None:
            with contextlib.suppress(OSError):
                child.stdout.close()

    # `TerminateProcess` returning (what `wait` waits on) and NTFS releasing the byte-range lock
    # are not one atomic step on Windows, so the release can trail `wait()` by a beat. A short
    # poll asserts the property the design leans on — the OS releases it, unassisted — without
    # asserting a timing guarantee nothing in `jobs.py` makes.
    deadline = time.monotonic() + 2.0
    while jobs.holder(workspace=tmp_path) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert jobs.holder(workspace=tmp_path) is None, "the OS released it when the process died"


def test_start_records_the_job_and_stop_ends_it(tmp_path: Path, clock: ManualClock) -> None:
    record = jobs.start(
        ["dataset", "verify", "--data", str(tmp_path / "nothing")],
        label="verify",
        clock=clock,
        workspace=tmp_path,
    )

    assert record.job_id and record.pid
    assert jobs.history(workspace=tmp_path)[0].job_id == record.job_id

    jobs.stop(record.job_id, workspace=tmp_path)
    ended = jobs.refresh(record, clock=clock, workspace=tmp_path)
    assert ended.exit_code is not None, "a stopped job records how it ended"
    assert jobs.status_of(ended, workspace=tmp_path) != "running"


def test_acquire_drops_the_lock_if_setup_fails_after_taking_it(
    tmp_path: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure between `_take` succeeding and `acquire` returning must not strand the OS lock.

    The `_take`-failure branch (tested above, via `Busy`) already releases correctly; this is the
    other branch — a fault writing the sidecar (disk full, permissions, a bad `Holder`) — which
    must `_drop` the lock and close the handle just the same, or an in-process caller (unlike a CLI
    subprocess, which death eventually releases for) blocks every later `acquire` on this workspace
    for the rest of its life.
    """
    lock = jobs.WorkspaceLock(workspace=tmp_path)
    original = jobs.Holder.model_dump_json
    calls = {"n": 0}

    def _fails_once(self: jobs.Holder) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return original(self)

    monkeypatch.setattr(jobs.Holder, "model_dump_json", _fails_once)

    with pytest.raises(OSError):
        lock.acquire(("sweep",), clock=clock)
    assert lock._handle is None, "the failed acquire must not leave a handle behind"

    # The proof the OS lock was actually released, not merely forgotten about in Python: a second
    # `WorkspaceLock` over the same workspace can take it.
    second = jobs.WorkspaceLock(workspace=tmp_path)
    second.acquire(("sweep",), clock=clock)
    try:
        assert jobs.holder(workspace=tmp_path) is not None
    finally:
        second.release()


def test_start_refuses_while_the_workspace_is_held(tmp_path: Path, clock: ManualClock) -> None:
    lock = jobs.WorkspaceLock(workspace=tmp_path)
    lock.acquire(("sweep",), clock=clock)
    try:
        with pytest.raises(jobs.Busy, match="sweep"):
            jobs.start(["report", "--corpus", "x"], label="r", clock=clock, workspace=tmp_path)
    finally:
        lock.release()


def test_the_cli_refuses_a_locked_workspace_with_exit_seven(
    tmp_path: Path, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decision_lab import cli, params

    monkeypatch.setenv("DECISION_LAB_WORKSPACE", str(tmp_path))
    lock = jobs.WorkspaceLock(workspace=tmp_path)
    lock.acquire(("sweep", "--corpus", "abc"), clock=clock)
    try:
        assert params.workspace_root() == tmp_path
        assert cli.main(["report", "--corpus", "abc"]) == cli.EXIT_BUSY
    finally:
        lock.release()
