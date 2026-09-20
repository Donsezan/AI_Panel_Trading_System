"""One backfill at a time over one raw store (spec §6.3.1, §12.4's rule one directory across).

`RawStore.rewrite` replaces the file whole from rows read at the *start* of a pass. Two backfills
over one store therefore lose data in the quietest possible way: the second reads the same
pre-batch state, and its rewrite puts those bodyless rows back over the first's gains. Both passes
report `gained: N` and exit 0, and hours of collected article text are gone with no error anywhere
and no way to tell from the store that it happened.

That is the lost-update hazard `WorkspaceLock` exists for, one directory across — and the
workspace lock does not reach it, because `archive` is deliberately absent from `LOCKED`: a
multi-hour crawl must not block every sweep and report for its duration. So the archive takes its
own lock, scoped to the one file it rewrites.

An OS advisory lock rather than a pid file, for the reason `jobs.py` already gives: the operating
system releases it when the holder dies, and `os.kill(pid, 0)` *terminates* the process on Windows.

Failure semantics: refusing is `jobs.Busy`, which `cli.main` already maps to exit 7 — the same
code, and the same meaning, a second writer gets anywhere else in this tool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from decision_lab.archive.backfill import backfill
from decision_lab.archive.store import RawStore, StoreLock
from decision_lab.jobs import Busy
from decision_lab.tests.test_archive_backfill import URL, stored
from decision_lab.tests.test_archive_coindesk import article_page
from decision_lab.tests.test_archive_collect import FakeFetcher
from tradebot.core.clock import ManualClock

NOW = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)


def test_a_second_claim_on_one_store_is_refused(tmp_path: Path) -> None:
    store = RawStore.open(tmp_path, source_id="coindesk")

    with StoreLock(store), pytest.raises(Busy, match="coindesk"), StoreLock(store):
        pass  # pragma: no cover - the claim above raises


def test_two_sources_are_locked_independently(tmp_path: Path) -> None:
    """One crawl per publisher is the normal case and must not serialise on the other."""
    first = RawStore.open(tmp_path, source_id="coindesk")
    second = RawStore.open(tmp_path, source_id="cryptoslate")

    with StoreLock(first), StoreLock(second):
        pass  # both claims held at once


def test_a_released_claim_can_be_retaken(tmp_path: Path) -> None:
    """Batches are sequential invocations over one store; the second must not find it held."""
    store = RawStore.open(tmp_path, source_id="coindesk")

    with StoreLock(store):
        pass
    with StoreLock(store):
        pass


async def test_a_backfill_refuses_while_another_holds_the_store(tmp_path: Path) -> None:
    """The property that matters: the *pass* is what must not double, not just the lock."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored())
    fetcher = FakeFetcher({URL: article_page()})

    with StoreLock(store), pytest.raises(Busy):
        await backfill(fetcher, store, ManualClock(NOW), pause=None)

    assert fetcher.asked == [], "a refused pass must not have spent a request first"


async def test_a_backfill_releases_the_store_when_it_finishes(tmp_path: Path) -> None:
    """A pass that held the lock past its own end would refuse every later batch."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored())

    await backfill(FakeFetcher({URL: article_page()}), store, ManualClock(NOW), pause=None)

    with StoreLock(store):
        pass  # free again


async def test_a_backfill_that_raises_still_releases_the_store(tmp_path: Path) -> None:
    """A crash mid-pass must not strand the lock for the life of the process."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored())

    class Exploding:
        """A transport that fails the way `_fetch` does not absorb — not a `VenueError`."""

        async def fetch(self, url: str) -> str | None:
            raise RuntimeError("the transport fell over")

        async def close(self) -> None:
            return None

    with pytest.raises(RuntimeError):
        await backfill(Exploding(), store, ManualClock(NOW), pause=None)

    with StoreLock(store):
        pass  # free again
