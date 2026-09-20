"""Walking a Yoast sitemap source (spec §6.3).

The selection rule is the whole point, and it is the opposite of the CoinDesk walk's:

* **Chunks are chosen by `lastmod`; rows are not filtered by it at all.** A chunk's dates say
  roughly which period it covers, which is enough to decide whether to open it. Filtering the URLs
  inside it by `lastmod` would *under*-collect — an article published in 2025 and edited in 2026
  carries a 2026 `lastmod` and would be dropped — and under-collection is the one failure a
  one-shot archive cannot repair later.
* **Everything fetched is kept.** A row from an adjacent year is archive material, and the fetch
  is already paid for. The year selects chunks; it never rejects an article.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from decision_lab.archive.collect import collect_site
from decision_lab.archive.sites import PROFILES
from decision_lab.archive.store import RawStore
from decision_lab.tests.test_archive_coindesk import LD_JSON
from decision_lab.tests.test_archive_collect import FakeFetcher
from decision_lab.tests.test_archive_sites import page
from tradebot.core.clock import ManualClock

NOW = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
PROFILE = PROFILES["cryptoslate"]


def sitemap(*entries: tuple[str, str]) -> str:
    body = "".join(f"<url><loc>{u}</loc><lastmod>{m}</lastmod></url>" for u, m in entries)
    return f"<?xml version='1.0'?><urlset>{body}</urlset>"


def index(*entries: tuple[str, str]) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc><lastmod>{m}</lastmod></sitemap>" for u, m in entries)
    return f"<?xml version='1.0'?><sitemapindex>{body}</sitemapindex>"


IN_YEAR = "https://cryptoslate.com/a-2025-story"
EDITED_LATER = "https://cryptoslate.com/published-2025-edited-2026"
OTHER_YEAR = "https://cryptoslate.com/a-2023-story"


def article(published: str) -> str:
    return page(PROFILE.body_container, LD_JSON.replace("2024-07-16T15:32:00.326Z", published))


def pages() -> dict[str, str]:
    return {
        PROFILE.sitemap_index: index(
            ("https://cryptoslate.com/post-sitemap20.xml", "2025-06-04"),
            ("https://cryptoslate.com/post-sitemap9.xml", "2023-02-01"),
            ("https://cryptoslate.com/category-sitemap.xml", "2025-08-01"),
        ),
        "https://cryptoslate.com/post-sitemap20.xml": sitemap(
            (IN_YEAR, "2025-06-04"),
            (EDITED_LATER, "2026-03-02"),
        ),
        "https://cryptoslate.com/post-sitemap9.xml": sitemap((OTHER_YEAR, "2023-02-01")),
        IN_YEAR: article("2025-06-04T09:00:00+00:00"),
        EDITED_LATER: article("2025-11-20T09:00:00+00:00"),
        OTHER_YEAR: article("2023-02-01T09:00:00+00:00"),
    }


async def collect(store: RawStore, fetcher: FakeFetcher) -> object:
    return await collect_site(
        fetcher,
        store,
        ManualClock(NOW),
        profile=PROFILE,
        year=2025,
        pause=timedelta(),
        backoff=timedelta(),
    )


async def test_an_article_edited_after_the_year_is_still_collected(tmp_path: Path) -> None:
    """The under-collection trap: its `lastmod` is 2026 but it was published in 2025."""
    store = RawStore.open(tmp_path, source_id=PROFILE.source_id)

    await collect(store, FakeFetcher(pages()))

    held = {row.url for row in store.read_all()}
    assert EDITED_LATER in held, "filtering rows by lastmod drops articles published in the year"
    assert IN_YEAR in held


async def test_articles_in_a_chunk_outside_the_year_are_never_fetched(tmp_path: Path) -> None:
    """Inspecting a chunk costs one request; *walking* it costs a thousand.

    Every chunk is read, because the index's own `lastmod` cannot be trusted to say what a chunk
    contains. What the year decides is whether the articles behind it are fetched — and that is
    where the whole cost of the crawl sits.
    """
    fetcher = FakeFetcher(pages())

    await collect(RawStore.open(tmp_path, source_id=PROFILE.source_id), fetcher)

    assert OTHER_YEAR not in fetcher.asked, "walked a chunk from the wrong period"
    assert IN_YEAR in fetcher.asked


async def test_only_article_chunks_are_walked(tmp_path: Path) -> None:
    """The index also lists categories, tags and authors; those are not articles."""
    fetcher = FakeFetcher(pages())

    await collect(RawStore.open(tmp_path, source_id=PROFILE.source_id), fetcher)

    assert "https://cryptoslate.com/category-sitemap.xml" not in fetcher.asked


BOUNDARY_INDEX = index(
    ("https://cryptoslate.com/post-sitemap20.xml", "2026-01-05"),
    ("https://cryptoslate.com/post-sitemap21.xml", "2026-09-01"),
)


async def test_a_chunk_straddling_the_year_boundary_is_walked(tmp_path: Path) -> None:
    """Both chunks here are stamped 2026; one is half 2025 and must still be opened.

    This is why selection is a *share* rather than a match: the year's last articles sit in a chunk
    whose newest entries are already next year, and an equality test on any single date misses
    them entirely.
    """
    straddling = sitemap(
        *[(f"https://cryptoslate.com/edge-{i}", f"{2025 + (i % 2)}-12-15") for i in range(10)]
    )
    next_year = sitemap(*[(f"https://cryptoslate.com/later-{i}", "2026-07-01") for i in range(10)])
    pages_ = {
        PROFILE.sitemap_index: BOUNDARY_INDEX,
        "https://cryptoslate.com/post-sitemap20.xml": straddling,
        "https://cryptoslate.com/post-sitemap21.xml": next_year,
    }
    for i in range(10):
        pages_[f"https://cryptoslate.com/edge-{i}"] = article("2025-12-15T09:00:00+00:00")
        pages_[f"https://cryptoslate.com/later-{i}"] = article("2026-07-01T09:00:00+00:00")
    fetcher = FakeFetcher(pages_)

    await collect(RawStore.open(tmp_path, source_id=PROFILE.source_id), fetcher)

    assert "https://cryptoslate.com/edge-0" in fetcher.asked, "dropped the boundary chunk"
    assert "https://cryptoslate.com/later-0" not in fetcher.asked, "walked a whole later year"


def dated(n: int, year: int, count: int = 10) -> str:
    """A chunk whose URLs all carry `year`."""
    return sitemap(
        *[
            (f"https://cryptoslate.com/{year}-story-{i}", f"{year}-06-0{i % 9 + 1}")
            for i in range(count)
        ]
    )


STALE_INDEX = index(
    # An OLD chunk of 2017 articles, stamped 2025 because one of them was edited then.
    ("https://cryptoslate.com/post-sitemap3.xml", "2025-03-01"),
    ("https://cryptoslate.com/post-sitemap20.xml", "2025-11-30"),
)


async def test_a_stale_chunk_stamped_with_the_year_is_not_walked(tmp_path: Path) -> None:
    """An index `lastmod` is the *max* over the chunk's articles, so one edit re-stamps the lot.

    Measured on the real run: chunks stamped 2025 held articles published 2017-10-17 onwards, and
    the crawl dutifully collected them. A chunk is judged by what is *inside* it, never by the
    index's claim about it.
    """
    stale = dated(3, 2017)  # 10 urls, all 2017 lastmods
    fresh = dated(20, 2025)  # 10 urls, all 2025 lastmods
    pages_ = {
        PROFILE.sitemap_index: STALE_INDEX,
        "https://cryptoslate.com/post-sitemap3.xml": stale,
        "https://cryptoslate.com/post-sitemap20.xml": fresh,
    }
    for i in range(10):
        pages_[f"https://cryptoslate.com/2017-story-{i}"] = article("2017-06-01T09:00:00+00:00")
        pages_[f"https://cryptoslate.com/2025-story-{i}"] = article("2025-06-01T09:00:00+00:00")
    fetcher = FakeFetcher(pages_)

    await collect(RawStore.open(tmp_path, source_id=PROFILE.source_id), fetcher)

    assert "https://cryptoslate.com/2017-story-0" not in fetcher.asked, "walked a stale chunk"
    assert "https://cryptoslate.com/2025-story-0" in fetcher.asked, "skipped a real 2025 chunk"


BOUNDARY_EARLY = "https://cryptoslate.com/published-2025-01-02"
BOUNDARY_OLD = "https://cryptoslate.com/published-2024-12-30"


def boundary_pages() -> dict[str, str]:
    """A chunk that is overwhelmingly last year, holding the target year's first articles.

    Measured on Bitcoin.com's live index: `post-sitemap34.xml` carried 46 URLs from 2025 against
    954 from 2024 — 4.6%, far under `MIN_YEAR_PERCENT` — and being skipped cost 2025-01-01..03
    *and* all of December 2024. The threshold only lands a boundary chunk when the year happens
    to begin early in it, which is luck rather than a rule.
    """
    return {
        PROFILE.sitemap_index: index(
            ("https://cryptoslate.com/post-sitemap34.xml", "2024-12-30"),
            ("https://cryptoslate.com/post-sitemap35.xml", "2025-06-04"),
        ),
        # 1 of 5 URLs in the target year: 20% would pass, so make it plainly under.
        "https://cryptoslate.com/post-sitemap34.xml": sitemap(
            (BOUNDARY_OLD, "2024-12-30"),
            ("https://cryptoslate.com/old-1", "2024-11-01"),
            ("https://cryptoslate.com/old-2", "2024-11-02"),
            ("https://cryptoslate.com/old-3", "2024-11-03"),
            ("https://cryptoslate.com/old-4", "2024-11-04"),
            ("https://cryptoslate.com/old-5", "2024-11-05"),
            ("https://cryptoslate.com/old-6", "2024-11-06"),
            ("https://cryptoslate.com/old-7", "2024-11-07"),
            ("https://cryptoslate.com/old-8", "2024-11-08"),
            (BOUNDARY_EARLY, "2025-01-02"),
        ),
        "https://cryptoslate.com/post-sitemap35.xml": sitemap((IN_YEAR, "2025-06-04")),
        BOUNDARY_EARLY: article("2025-01-02T09:00:00+00:00"),
        BOUNDARY_OLD: article("2024-12-30T09:00:00+00:00"),
        IN_YEAR: article("2025-06-04T09:00:00+00:00"),
        **{
            f"https://cryptoslate.com/old-{n}": article(f"2024-11-0{n}T09:00:00+00:00")
            for n in range(1, 9)
        },
    }


async def test_the_chunk_before_the_first_kept_one_is_walked(tmp_path: Path) -> None:
    """A year's first articles live in the tail of the previous chunk, under any threshold.

    A paginated sitemap is chronological, so the boundary chunk is mostly *last* year by
    construction. Judging it by its own share of the target year drops the first days of January
    and the whole of the preceding December — which is under-collection, the one failure a
    one-shot archive cannot repair later.
    """
    store = RawStore.open(tmp_path, source_id="cryptoslate")
    fetcher = FakeFetcher(boundary_pages())

    await collect(store, fetcher)

    collected = {row.url for row in store.read_all()}
    assert BOUNDARY_EARLY in collected, "2025-01-02 sat in a chunk that is 10% 2025"
    assert BOUNDARY_OLD in collected, "December of the preceding year comes with it"
