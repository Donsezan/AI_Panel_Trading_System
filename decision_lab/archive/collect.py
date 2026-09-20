"""Walking an archive listing and collecting the articles inside a window (spec §6.3, §6.4).

One year is roughly 5 700 articles, so this is a long job and every rule here is about surviving
it rather than about correctness of a single row:

* **The walk stops when the listing passes the window.** Listings are reverse-chronological, so
  once a page's oldest row predates `since` there is nothing further forward to find.
* **A held article is never re-fetched.** The store is append-only and keyed by canonical URL
  precisely so an interrupted crawl resumes instead of restarting — and so a restart does not
  re-ask the publisher for thousands of pages it already has.
* **A refusal is counted, not raised.** An article with no publication instant is skipped and
  tallied; one malformed page must not end a two-hour crawl, and must not enter the archive with a
  guessed timestamp either (§6.7).

Failure semantics: transport errors are the fetcher's and are *not* caught here. A `robots.txt`
denial or a 403 raises `SourceDisallowedError` and the crawl stops, which is the correct response
to being told to go away — the work already done is on disk and the next run resumes from it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Final, Protocol

from decision_lab.archive.coindesk import parse_article, parse_listing
from decision_lab.archive.sites import SiteProfile, parse_site_article, parse_sitemap
from decision_lab.archive.store import RawArticle, RawStore
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError, RateLimitedError, VenueError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel

logger = get_logger(__name__)

#: Gap between article fetches when the caller does not say. A publisher notices a crawler that
#: asks faster than a person reads, and this job has no deadline.
DEFAULT_PAUSE: Final = timedelta(seconds=1.5)

#: Listing pages are 500 rows each; a year is twelve of them. The ceiling is a runaway guard, not
#: a limit anyone should reach.
MAX_PAGES: Final = 60

#: Re-fetches when a page arrives without its article text. **Zero by default, deliberately.**
#: CoinDesk meters the body at three articles per session (`rw_remaining` counts 2→1→0 and
#: `rw_allowed` flips to false exactly as the text disappears), so past the third article a retry
#: cannot succeed — it only triples the requests made to the publisher for nothing. The knob
#: stays because a source that drops a body *transiently* is a different case; this one does not.
DEFAULT_BODY_RETRIES: Final = 0

#: Attempts to absorb a *transient* transport failure before giving up. `VenueError` is a
#: `RetryableError` and the class is the handling instruction (CLAUDE.md), so treating one as
#: fatal is a defect — it cost a real 3-hour crawl at its 1 430th row, on a single timeout.
#: `SourceDisallowedError` is `FailClosedError` and is deliberately **not** retried: being told
#: to go away is an answer, not a blip.
DEFAULT_TRANSPORT_RETRIES: Final = 4

#: First wait after a transient failure; doubled per attempt. A publisher having a bad minute is
#: not helped by being asked again immediately.
DEFAULT_BACKOFF: Final = timedelta(seconds=5)

#: Share of a chunk's URLs that must carry the target year before the chunk is walked, as a
#: percentage. A chunk genuinely covering the year runs at or near 100%; a stale chunk
#: re-stamped by a single edit runs at roughly one URL in a thousand. Anything between is a
#: chunk straddling a year boundary, which is worth walking.
MIN_YEAR_PERCENT: Final = 20

LISTING_ROOT: Final = "https://www.coindesk.com/sitemap/archive"


class Fetcher(Protocol):
    """What the crawl needs of a transport. `FeedFetcher` satisfies it (§2.4)."""

    async def fetch(self, url: str) -> str | None: ...

    async def close(self) -> None: ...


class CollectReport(DomainModel):
    """What one crawl did, so a resumed run can say what it added rather than what exists."""

    pages: int = 0
    seen: int = 0
    collected: int = 0
    already_held: int = 0
    refused: int = 0
    missing: int = 0
    bodyless: int = 0

    def added(self, **counts: int) -> CollectReport:
        return self.model_copy(update={k: getattr(self, k) + v for k, v in counts.items()})


def listing_url(year: int, page: int) -> str:
    """Page 1 is the bare year; later pages carry their number."""
    return f"{LISTING_ROOT}/{year}" if page == 1 else f"{LISTING_ROOT}/{year}/{page}"


async def collect(
    fetcher: Fetcher,
    store: RawStore,
    clock: Clock,
    *,
    year: int,
    since: date,
    until: date,
    pause: timedelta = DEFAULT_PAUSE,
    body_retries: int = DEFAULT_BODY_RETRIES,
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
    backoff: timedelta = DEFAULT_BACKOFF,
) -> CollectReport:
    """Collect every article listed for `year` whose row falls within `[since, until]`."""
    report = CollectReport()
    held = set(store.held())
    for page in range(1, MAX_PAGES + 1):
        url = listing_url(year, page)
        try:
            document = await _fetch(fetcher, url, retries=transport_retries, backoff=backoff)
        except ConfigError:
            # A 404 one page past the end is how the end is found: the archive publishes no page
            # count. `FeedFetcher` classifies every 4xx as `ConfigError`, which is right for a
            # mistyped feed URL and wrong for this sentinel — unhandled, it killed a complete 2024
            # crawl on its 5 687th row, after every page that existed had already been read.
            logger.info("archive listing exhausted", extra={"url": url, "pages": report.pages})
            break
        if not document:
            break
        rows = parse_listing(document)
        if not rows:
            break
        report = report.added(pages=1, seen=len(rows))
        for row in rows:
            if not since <= row.listed_on <= until:
                continue
            identity = store.identity(row.url)
            if identity in held:
                report = report.added(already_held=1)
                continue
            report = report.added(
                **await _one(
                    fetcher,
                    store,
                    clock,
                    row.url,
                    pause=pause,
                    body_retries=body_retries,
                    transport_retries=transport_retries,
                    backoff=backoff,
                )
            )
            held.add(identity)
        if min(row.listed_on for row in rows) < since:
            break
    return report


async def _one(
    fetcher: Fetcher,
    store: RawStore,
    clock: Clock,
    url: str,
    *,
    pause: timedelta,
    body_retries: int,
    transport_retries: int,
    backoff: timedelta,
    parse: Callable[[str, str, datetime], RawArticle] = lambda page, url, at: parse_article(
        page, url=url, fetched_at=at
    ),
) -> dict[str, int]:
    """Fetch, parse and store one article. Never raises for a bad page — only for transport.

    Re-fetches while the body is missing, because the publisher drops it non-deterministically.
    A row that never yields one is still **stored and counted**: the abstract, keywords and
    timestamp are real evidence, and some posts genuinely have no body at all.
    """
    article: RawArticle | None = None
    for attempt in range(body_retries + 1):
        if pause:
            await asyncio.sleep(pause.total_seconds())
        document = await _fetch(fetcher, url, retries=transport_retries, backoff=backoff)
        if not document:
            logger.warning("archive article unavailable", extra={"url": url})
            return {"missing": 1}
        try:
            article = parse(document, url, clock.now())
        except ConfigError as exc:
            logger.warning("archive article refused", extra={"url": url, "reason": str(exc)})
            return {"refused": 1}
        if article.body:
            break
        logger.debug("archive article arrived without a body", extra={"url": url, "try": attempt})
    if article is None:  # unreachable: the loop runs at least once and returns or assigns.
        return {"missing": 1}  # An `assert` would vanish under `-O`, so this is a guard.
    store.append(article)
    return {"collected": 1} if article.body else {"collected": 1, "bodyless": 1}


async def _fetch(fetcher: Fetcher, url: str, *, retries: int, backoff: timedelta) -> str | None:
    """One fetch, absorbing transient transport failures within a budget.

    Only `VenueError` and below are retried. `SourceDisallowedError` is `FailClosedError` and
    propagates on the spot: a `robots.txt` denial or a 403 is a decision by the publisher, and
    asking again is exactly what one must not do. A budget rather than infinite patience, because
    a crawl that never gives up against a dead host is a crawl nobody can reason about — and what
    is already stored survives either way, so the next run resumes.
    """
    for attempt in range(retries + 1):
        try:
            return await fetcher.fetch(url)
        except VenueError as exc:
            if attempt == retries:
                raise
            after = (
                getattr(exc, "retry_after_seconds", None)
                if isinstance(exc, RateLimitedError)
                else None
            )
            wait = after if after is not None else backoff.total_seconds() * (2**attempt)
            logger.warning(
                "archive fetch failed, retrying",
                extra={"url": url, "attempt": attempt + 1, "wait_s": wait, "error": str(exc)},
            )
            if wait:
                await asyncio.sleep(wait)
    return None  # unreachable: the loop returns or raises.


async def collect_site(
    fetcher: Fetcher,
    store: RawStore,
    clock: Clock,
    *,
    profile: SiteProfile,
    year: int,
    pause: timedelta = DEFAULT_PAUSE,
    body_retries: int = DEFAULT_BODY_RETRIES,
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES,
    backoff: timedelta = DEFAULT_BACKOFF,
) -> CollectReport:
    """Walk a Yoast sitemap source, collecting the chunks that cover `year`.

    The selection rule is the opposite of the CoinDesk walk's, and deliberately so. A chunk's
    `lastmod` decides only **whether to open it**; the URLs inside are never filtered by theirs.
    A `lastmod` moves when an old article is edited, so an article published in `year` and touched
    afterwards carries a later one — filtering rows by it would silently drop exactly those, and
    under-collection is the one failure a one-shot archive cannot repair later.

    Everything fetched is kept. A row from an adjacent year is archive material and the request is
    already paid for; the row's own `datePublished` is what dates it (§6.7).
    """
    report = CollectReport()
    held = set(store.held())

    def parse(page: str, url: str, at: datetime) -> RawArticle:
        return parse_site_article(page, url=url, fetched_at=at, profile=profile)

    index = await _fetch(fetcher, profile.sitemap_index, retries=transport_retries, backoff=backoff)
    if not index:
        return report

    # The index's `lastmod` is the **max** over a chunk's articles, so editing one 2017 post
    # re-stamps its whole chunk with today's date. Trusting it collected articles published from
    # 2017 onwards under the banner of a 2025 crawl. A chunk is therefore judged by what is inside
    # it: the share of its URLs whose own `lastmod` falls in the year. That share is only ever used
    # to decide whether to *open* a chunk — rows inside an opened one are never filtered by it,
    # because a `lastmod` does not date an article (§6.7).
    candidates = [loc for loc, _ in parse_sitemap(index) if profile.chunk_marker in loc]
    logger.info(
        "archive chunks to inspect",
        extra={"source": profile.source_id, "year": year, "candidates": len(candidates)},
    )

    # Two passes, because a chunk's *neighbours* decide whether it is walked and that cannot be
    # known while streaming. Every chunk's sitemap is read first, then the selection is made, then
    # the articles are fetched. 49 sitemap reads against several thousand article fetches.
    scanned: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for chunk in candidates:
        document = await _fetch(fetcher, chunk, retries=transport_retries, backoff=backoff)
        if not document:
            continue
        entries = parse_sitemap(document)
        if entries:
            scanned.append((chunk, entries))

    in_year = [sum(1 for _, mod in entries if mod[:4] == str(year)) for _, entries in scanned]
    # Integer arithmetic on purpose: the package admits no `float` (test_discipline.py).
    qualifying = {
        index
        for index, (_, entries) in enumerate(scanned)
        if in_year[index] * 100 >= len(entries) * MIN_YEAR_PERCENT
    }
    # **A chunk beside the qualifying run is opened when it holds *any* of the target year.** A
    # paginated sitemap is chronological, so the year's first articles sit in the tail of the
    # previous chunk, and that chunk is mostly *last* year by construction — so the share test
    # lands it only when the year happens to begin early in it, which is luck rather than a rule.
    # Measured on Bitcoin.com's live index: `post-sitemap34.xml` held 46 URLs from 2025 against
    # 954 from 2024 (4.6%), and skipping it cost 2025-01-01..03 and the whole of December 2024.
    #
    # Adjacency alone is not enough to open one, or a stale chunk of 2017 posts that happens to
    # sit beside the run would be walked for nothing. Holding at least one URL in the year is what
    # separates a boundary chunk from a neighbour: a boundary chunk contains the year, a stale one
    # does not.
    neighbours = {i - 1 for i in qualifying} | {i + 1 for i in qualifying}
    boundary = {
        index
        for index in neighbours & set(range(len(scanned)))
        if index not in qualifying and in_year[index]
    }
    selected = sorted(qualifying | boundary)
    logger.info(
        "archive chunks selected",
        extra={
            "source": profile.source_id,
            "scanned": len(scanned),
            "qualifying": len(qualifying),
            "boundary": len(boundary),
            "selected": len(selected),
        },
    )

    for position in selected:
        chunk, entries = scanned[position]
        urls = [loc for loc, _ in entries]
        report = report.added(pages=1, seen=len(urls))
        for url in urls:
            identity = store.identity(url)
            if identity in held:
                report = report.added(already_held=1)
                continue
            report = report.added(
                **await _one(
                    fetcher,
                    store,
                    clock,
                    url,
                    pause=pause,
                    body_retries=body_retries,
                    transport_retries=transport_retries,
                    backoff=backoff,
                    parse=parse,
                )
            )
            held.add(identity)
    return report
