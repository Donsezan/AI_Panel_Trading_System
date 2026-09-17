"""Walking a publisher's archive and collecting what is inside the window (spec §6.3, §6.4).

The crawl is long — one year is ~5 700 articles — so every property here is about *not* redoing
work and *not* being rude:

* A page walk stops once the listing has gone past the window. The listing is reverse-chronological,
  so continuing costs one request per 500 rows for nothing.
* An article already in the store is never re-fetched. Resume is the whole reason the store is
  append-only, and a crawl that restarted from zero would re-ask the publisher for thousands of
  pages it already has.
* An article that cannot be placed in time is **counted and skipped**, never fatal and never
  stored. One malformed page must not end a two-hour crawl, and it must not enter the archive with
  a guessed timestamp either (§6.7).

Nothing here touches the network: the fetcher is injected, and the fake asserts on the URLs asked
for, which is also how "we never requested a disallowed path" is checked.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from decision_lab.archive.collect import CollectReport, collect
from decision_lab.archive.store import RawStore
from decision_lab.tests.test_archive_coindesk import LD_JSON, article_page
from tradebot.core.clock import ManualClock
from tradebot.core.errors import ConfigError, SourceDisallowedError, VenueError

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def listing(*rows: tuple[str, str, str]) -> str:
    """An archive listing page carrying `rows` of (url, title, listed date)."""
    anchors = "".join(
        f'<a href="{url}"><span>{title}</span><span>{listed}</span></a>'
        for url, title, listed in rows
    )
    return f"<html><body><div>{anchors}</div></body></html>"


def url_for(day: str, slug: str) -> str:
    return f"https://www.coindesk.com/markets/{day.replace('-', '/')}/{slug}"


class FakeFetcher:
    """Serves canned pages and records every URL asked for."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.asked: list[str] = []
        self.closed = False

    async def fetch(self, url: str) -> str | None:
        self.asked.append(url)
        return self.pages.get(url)

    async def close(self) -> None:
        self.closed = True


def one_page_year(rows: tuple[tuple[str, str, str], ...]) -> dict[str, str]:
    """A single listing page plus an article page behind every row."""
    pages = {"https://www.coindesk.com/sitemap/archive/2024": listing(*rows)}
    for url, _title, listed in rows:
        instant = f"{listed}T09:30:00.000Z"
        pages[url] = article_page(LD_JSON.replace("2024-07-16T15:32:00.326Z", instant))
    return pages


ROWS = (
    (url_for("2024-07-10", "after-the-window"), "After", "2024-07-10"),
    (url_for("2024-06-20", "inside-the-window"), "Inside", "2024-06-20"),
    (url_for("2024-05-02", "also-inside"), "Also inside", "2024-05-02"),
)


async def test_only_articles_inside_the_window_are_fetched(tmp_path: Path) -> None:
    fetcher = FakeFetcher(one_page_year(ROWS))
    store = RawStore.open(tmp_path, source_id="coindesk")

    report = await collect(
        fetcher,
        store,
        ManualClock(NOW),
        year=2024,
        since=date(2024, 5, 1),
        until=date(2024, 7, 1),
        pause=timedelta(),
        backoff=timedelta(),
    )

    assert {row.url for row in store.read_all()} == {
        url_for("2024-06-20", "inside-the-window"),
        url_for("2024-05-02", "also-inside"),
    }
    assert url_for("2024-07-10", "after-the-window") not in fetcher.asked
    assert report.collected == 2


async def test_an_article_already_held_is_never_fetched_again(tmp_path: Path) -> None:
    """Resume: the second run asks the publisher for nothing it already has."""
    fetcher = FakeFetcher(one_page_year(ROWS))
    store = RawStore.open(tmp_path, source_id="coindesk")

    async def run() -> CollectReport:
        return await collect(
            fetcher,
            store,
            ManualClock(NOW),
            year=2024,
            since=date(2024, 5, 1),
            until=date(2024, 7, 1),
            pause=timedelta(),
            backoff=timedelta(),
        )

    await run()
    fetcher.asked.clear()
    report = await run()

    assert [u for u in fetcher.asked if "/markets/" in u] == [], "re-fetched a held article"
    assert report.collected == 0
    assert report.already_held == 2
    assert len(store.read_all()) == 2, "resume must not duplicate rows"


async def test_an_article_that_cannot_be_placed_in_time_is_counted_not_stored(
    tmp_path: Path,
) -> None:
    """One malformed page must not end the crawl, and must not enter the archive either."""
    pages = one_page_year(ROWS)
    undated = LD_JSON.replace('"datePublished":"2024-07-16T15:32:00.326Z",', "")
    pages[url_for("2024-06-20", "inside-the-window")] = article_page(undated)
    store = RawStore.open(tmp_path, source_id="coindesk")

    report = await collect(
        FakeFetcher(pages),
        store,
        ManualClock(NOW),
        year=2024,
        since=date(2024, 5, 1),
        until=date(2024, 7, 1),
        pause=timedelta(),
        backoff=timedelta(),
    )

    assert report.refused == 1
    assert report.collected == 1
    assert {row.url for row in store.read_all()} == {url_for("2024-05-02", "also-inside")}


class FlakyBodyFetcher(FakeFetcher):
    """Serves a bodiless variant of one article until `good_on` attempts have been made.

    Not a contrivance: the publisher returns the same URL as ~1.62MB with no body or ~1.71MB with
    one, varying per request and with `no-store` and no `ETag`, so a re-fetch is a genuinely fresh
    answer rather than a cached repeat. Measured 2026-09-16 — 17 of 20 rows in a live one-day crawl
    came back bodiless, and re-fetching them individually returned bodies.
    """

    def __init__(self, pages: dict[str, str], *, flaky: str, good_on: int) -> None:
        super().__init__(pages)
        self.flaky = flaky
        self.good_on = good_on
        self.attempts = 0

    async def fetch(self, url: str) -> str | None:
        if url == self.flaky:
            self.attempts += 1
            if self.attempts < self.good_on:
                self.asked.append(url)
                # Faithful to the real variant: the ld+json record survives, the body
                # blocks do not. That is why a bodiless row still has an abstract.
                return (
                    '<html><head><script type="application/ld+json">'
                    + LD_JSON
                    + "</script></head><body><nav><p>chrome</p></nav></body></html>"
                )
        return await super().fetch(url)


async def test_a_bodiless_response_is_refetched_before_being_accepted(tmp_path: Path) -> None:
    """A body is the point of the raw store, and the publisher drops it non-deterministically."""
    inside = url_for("2024-06-20", "inside-the-window")
    fetcher = FlakyBodyFetcher(one_page_year(ROWS), flaky=inside, good_on=3)
    store = RawStore.open(tmp_path, source_id="coindesk")

    report = await collect(
        fetcher,
        store,
        ManualClock(NOW),
        year=2024,
        since=date(2024, 5, 1),
        until=date(2024, 7, 1),
        pause=timedelta(),
        backoff=timedelta(),
        body_retries=2,
    )

    stored = {row.url: row for row in store.read_all()}
    assert stored[inside].body, "a retry should have recovered the body"
    assert fetcher.attempts == 3, "should retry until the body arrives, not once and give up"
    assert report.bodyless == 0


async def test_an_article_that_never_yields_a_body_is_stored_and_counted(tmp_path: Path) -> None:
    """Some posts genuinely have no body.

    Keep the row — the abstract, keywords and timestamp are real evidence — and report the
    count, so thin body coverage is a number rather than a silent gap.
    """
    inside = url_for("2024-06-20", "inside-the-window")
    fetcher = FlakyBodyFetcher(one_page_year(ROWS), flaky=inside, good_on=99)
    store = RawStore.open(tmp_path, source_id="coindesk")

    report = await collect(
        fetcher,
        store,
        ManualClock(NOW),
        year=2024,
        since=date(2024, 5, 1),
        until=date(2024, 7, 1),
        pause=timedelta(),
        backoff=timedelta(),
        body_retries=2,
    )

    assert report.bodyless == 1
    assert report.collected == 2, "a bodiless row is still collected, not dropped"


class FlakyTransport(FakeFetcher):
    """Raises a transient transport error `failures` times before answering."""

    def __init__(self, pages: dict[str, str], *, failures: int, forever: bool = False) -> None:
        super().__init__(pages)
        self.failures = failures
        self.forever = forever
        self.raised = 0

    async def fetch(self, url: str) -> str | None:
        if self.forever or self.raised < self.failures:
            self.raised += 1
            raise VenueError(f"transport failure: {url}")
        return await super().fetch(url)


async def test_a_transient_transport_failure_is_retried_not_fatal(tmp_path: Path) -> None:
    """`VenueError` is a `RetryableError` \u2014 the class is the handling instruction (CLAUDE.md).

    Treating it as fatal cost a real 3-hour crawl at the 1 430th row, on one timeout.
    """
    fetcher = FlakyTransport(one_page_year(ROWS), failures=2)
    store = RawStore.open(tmp_path, source_id="coindesk")

    report = await collect(
        fetcher,
        store,
        ManualClock(NOW),
        year=2024,
        since=date(2024, 5, 1),
        until=date(2024, 7, 1),
        pause=timedelta(),
        backoff=timedelta(),
    )

    assert fetcher.raised == 2, "both failures should have been absorbed"
    assert report.collected == 2, "the crawl should have completed the window"


async def test_a_transport_that_never_recovers_stops_the_crawl(tmp_path: Path) -> None:
    """A retry *budget*, not infinite patience.

    What is already stored survives the raise, so the next run resumes from it.
    """
    fetcher = FlakyTransport(one_page_year(ROWS), failures=0, forever=True)
    store = RawStore.open(tmp_path, source_id="coindesk")

    with pytest.raises(VenueError):
        await collect(
            fetcher,
            store,
            ManualClock(NOW),
            year=2024,
            since=date(2024, 5, 1),
            until=date(2024, 7, 1),
            pause=timedelta(),
            backoff=timedelta(),
        )


async def test_a_publisher_refusal_is_never_retried(tmp_path: Path) -> None:
    """`SourceDisallowedError` is fail-closed: being told to go away is an answer, not a blip."""

    class Refusing(FakeFetcher):
        async def fetch(self, url: str) -> str | None:
            self.asked.append(url)
            raise SourceDisallowedError(f"robots.txt disallows fetching {url}")

    fetcher = Refusing(one_page_year(ROWS))

    with pytest.raises(SourceDisallowedError):
        await collect(
            fetcher,
            RawStore.open(tmp_path, source_id="coindesk"),
            ManualClock(NOW),
            year=2024,
            since=date(2024, 5, 1),
            until=date(2024, 7, 1),
            pause=timedelta(),
            backoff=timedelta(),
        )

    assert len(fetcher.asked) == 1, "a refusal must stop on the spot, not retry"


class EndsAtPage(FakeFetcher):
    """Serves listing pages up to `last`, then 404s as the real archive does past its end."""

    def __init__(self, pages: dict[str, str], *, last: int) -> None:
        super().__init__(pages)
        self.last = last

    async def fetch(self, url: str) -> str | None:
        if "/sitemap/archive/" in url:
            # Page 1 is the bare year (".../archive/2024"); later pages append their number.
            tail = url.rsplit("/", 1)[-1]
            page = 1 if tail == "2024" else int(tail)
            if page > self.last:
                self.asked.append(url)
                raise ConfigError(f"{url} returned HTTP 404; the feed URL looks wrong")
        return await super().fetch(url)


async def test_running_past_the_last_listing_page_ends_the_walk(tmp_path: Path) -> None:
    """A 404 past the final page is how the end of pagination is discovered, not a failure.

    `FeedFetcher` classifies any 4xx as `ConfigError` — right for a misconfigured feed URL, wrong
    for the sentinel request that finds the end of a paginated archive. Unhandled it killed a
    complete 2024 crawl on its 5 687th row, after all the work was already done.
    """
    fetcher = EndsAtPage(one_page_year(ROWS), last=1)
    store = RawStore.open(tmp_path, source_id="coindesk")

    report = await collect(
        fetcher,
        store,
        ManualClock(NOW),
        year=2024,
        since=date(2024, 5, 1),
        until=date(2024, 7, 1),
        pause=timedelta(),
        backoff=timedelta(),
    )

    assert report.collected == 2, "the pages that existed should still have been collected"
    assert report.pages == 1
