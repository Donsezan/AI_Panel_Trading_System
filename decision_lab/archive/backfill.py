"""Filling the body into rows a metered crawl stored without one (spec §6.3.1).

`archive build` cannot do this job. It is keyed by canonical URL so that an interrupted crawl
resumes rather than restarting, which means every row the 2024 pass stored is `held` and a re-run
collects nothing. The 5 678 rows that arrived without text — an *anonymous* crawl is metered at
three articles per session — are complete in every respect but one, and this is the pass that
completes them. A signed-in session is not metered that way: measured 2026-09-18, an authenticated
pass returned a body for every row it asked for.

Five rules, each of which is a way the obvious implementation loses data:

* **Only bodyless rows are requested.** Re-asking for a row that already has its text is rude to
  the publisher and buys nothing.
* **A row that still arrives bodyless is kept as it was.** The meter may still be biting. Dropping
  those rows would trade a complete archive for a smaller one holding only what came through.
* **Only `body` and `fetched_at` move.** Every other field was accepted at collection; re-parsing
  a since-edited page would silently rewrite the archive's record of what the publisher published.
* **`summary` survives.** Summarising is a separate pass over the stored bodies and may have run
  already, so replacing rows wholesale would wipe its output.
* **One pass at a time**, through `StoreLock`. Two would each rewrite the store from their own
  starting snapshot, so the later one puts bodyless rows back over the earlier one's gains — with
  both reporting `gained: N` and exiting 0, and nothing in the file to say it happened.

The pass is **not** resumable the way a crawl is: it holds the rows in memory and rewrites the
file once, at the end. That is deliberate at this size — 5 687 rows is a few megabytes — and it is
what makes a partial pass safe, because the store is only ever replaced by a complete document.
The cost is that a long pass risks everything it has collected, so a large job is run as a
sequence of `--limit` batches, each of which commits.

Failure semantics: identical to `collect`. Transport failures are absorbed within a budget by
`_fetch`; `SourceDisallowedError` propagates, because a 401 is the publisher answering that this
session is not entitled to the text. A page that will not parse is counted and the stored row is
kept untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from decision_lab.archive.coindesk import parse_article
from decision_lab.archive.collect import (
    DEFAULT_BACKOFF,
    DEFAULT_PAUSE,
    DEFAULT_TRANSPORT_RETRIES,
    Fetcher,
    _fetch,
)
from decision_lab.archive.store import RawArticle, RawStore, StoreLock
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel

logger = get_logger(__name__)


class BackfillReport(DomainModel):
    """What one pass did. `gained` against `refetched` is the answer the probe asks for."""

    rows: int = 0
    bodyless: int = 0
    refetched: int = 0
    gained: int = 0
    still_bodyless: int = 0
    refused: int = 0
    missing: int = 0

    def added(self, **counts: int) -> BackfillReport:
        return self.model_copy(update={k: getattr(self, k) + v for k, v in counts.items()})


async def backfill(
    fetcher: Fetcher,
    store: RawStore,
    clock: Clock,
    *,
    limit: int | None = None,
    pause: timedelta | None = DEFAULT_PAUSE,
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
    backoff: timedelta = DEFAULT_BACKOFF,
    parse: Callable[[str, str, datetime], RawArticle] = lambda page, url, at: parse_article(
        page, url=url, fetched_at=at
    ),
) -> BackfillReport:
    """Re-fetch every stored row that has no body, and rewrite the store once with the result.

    `limit` caps how many rows are *requested*, which is what makes the cheap probe cheap: five
    requests answer "does this session get bodies at all", where a full pass would spend 5 678
    before reporting that every one came back metered.
    """
    # Claimed before the first read, and released however this returns. Two passes over one store
    # would each rewrite it from their own starting snapshot, so the later one puts bodyless rows
    # back over the earlier one's gains — both reporting `gained: N` and exiting 0.
    with StoreLock(store):
        rows = store.read_all()
        report = BackfillReport(rows=len(rows))
        updated: list[RawArticle] = []

        for row in rows:
            if row.body:
                updated.append(row)
                continue
            report = report.added(bodyless=1)
            if limit is not None and report.refetched >= limit:
                updated.append(row)
                continue
            report = report.added(refetched=1)
            fresh, counts = await _body_for(
                fetcher,
                row,
                clock,
                pause=pause,
                transport_retries=transport_retries,
                backoff=backoff,
                parse=parse,
            )
            report = report.added(**counts)
            updated.append(fresh)

        store.rewrite(tuple(updated))
    return report


async def _body_for(
    fetcher: Fetcher,
    row: RawArticle,
    clock: Clock,
    *,
    pause: timedelta | None,
    transport_retries: int,
    backoff: timedelta,
    parse: Callable[[str, str, datetime], RawArticle],
) -> tuple[RawArticle, dict[str, int]]:
    """One row's body, or the row unchanged and the reason it stayed that way.

    The stored row is the base and only `body`/`fetched_at` are taken from the fresh page, so a
    headline edited since collection cannot rewrite the archive, and a `summary` an earlier pass
    wrote is carried through untouched.
    """
    if pause:
        await asyncio.sleep(pause.total_seconds())
    document = await _fetch(fetcher, row.url, retries=transport_retries, backoff=backoff)
    if not document:
        logger.warning("backfill article unavailable", extra={"url": row.url})
        return row, {"missing": 1}
    try:
        fresh = parse(document, row.url, clock.now())
    except ConfigError as exc:
        # A row already in the store was dateable when it was collected, so a refusal here is the
        # page having changed shape rather than §6.7 biting. Counted, and the row kept as it was.
        logger.warning("backfill article refused", extra={"url": row.url, "reason": str(exc)})
        return row, {"refused": 1}
    if not fresh.body:
        logger.debug("backfill article still metered", extra={"url": row.url})
        return row, {"still_bodyless": 1}
    return row.model_copy(update={"body": fresh.body, "fetched_at": fresh.fetched_at}), {
        "gained": 1
    }
