"""Filling the body into rows a metered crawl stored without one (spec §6.3.1).

CoinDesk meters the article body at three per session, so the 2024 crawl stored 5 687 rows of
which 9 carry a body. The rest are complete in every other respect — title, abstract, keywords,
and a `datePublished` to the millisecond — so re-running `archive build` is the wrong instrument:
`held()` is keyed by canonical URL, every row is held, and the crawl collects nothing.

Four properties, each of which corresponds to a way a naive "just crawl it again" loses data:

* **Only bodyless rows are requested.** 5 678 of 5 687 rows need a body; re-asking for the other
  nine is rude and buys nothing.
* **A row that still arrives without a body is kept exactly as it was.** The meter may still be
  biting; a backfill that dropped those rows would trade a complete archive for a smaller one
  carrying only the articles that happened to come through.
* **Only `body` and `fetched_at` move.** Everything else on a stored row was already accepted, and
  a publisher who edited the headline since would otherwise silently rewrite the archive's record
  of what was published.
* **`summary` survives.** The summarising pass is a separate process run later against the stored
  bodies, so a backfill that replaced rows wholesale would wipe its output on the next run.

Nothing here touches the network: the fetcher is injected, and the fake records every URL asked
for, which is how "a row with a body was never re-requested" is checked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from decision_lab.archive.backfill import backfill
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.tests.test_archive_coindesk import LD_JSON, article_page
from decision_lab.tests.test_archive_collect import FakeFetcher
from tradebot.core.clock import ManualClock

PUBLISHED = datetime(2024, 7, 16, 15, 32, tzinfo=UTC)
FETCHED = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 18, 8, 0, tzinfo=UTC)

URL = "https://www.coindesk.com/markets/2024/07/16/trump-odds-hit-high"


def metered_page(ld_json: str = LD_JSON) -> str:
    """The page a metered request returns: the whole `ld+json`, and no `document-body` block.

    Measured on 2026-09-16: past the third article of a session the response drops from ~1.71 MB
    to ~1.62 MB and the body element is simply absent, while the structured record is served
    intact. That asymmetry is the whole reason a body-only backfill is possible.
    """
    return article_page(ld_json).replace("document-body", "not-the-body")


def stored(*, url: str = URL, body: str = "", summary: str = "") -> RawArticle:
    """A row as the metered 2024 crawl left it: complete but for its body."""
    return RawArticle(
        source_id="coindesk",
        url=url,
        title="Trump Odds on Polymarket Hit Another All-Time High",
        body=body,
        abstract="Traders now see a 72% chance the former president retakes the White House.",
        keywords=("prediction-markets", "polymarket", "trump"),
        author="Marc Hochstein",
        section="Markets",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
        summary=summary,
    )


async def test_a_bodyless_row_gains_the_body_the_publisher_now_serves(tmp_path: Path) -> None:
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored())

    report = await backfill(FakeFetcher({URL: article_page()}), store, ManualClock(NOW), pause=None)

    (row,) = RawStore.open(tmp_path, source_id="coindesk").read_all()
    assert "72 cents mid-morning Tuesday" in row.body
    assert report.gained == 1


async def test_a_row_that_already_has_a_body_is_never_requested_again(tmp_path: Path) -> None:
    """Re-asking for 9 of 5 687 rows buys nothing and is the rudeness the meter is a reaction to."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored(body="The body this row was collected with."))
    fetcher = FakeFetcher({URL: article_page()})

    report = await backfill(fetcher, store, ManualClock(NOW), pause=None)

    assert fetcher.asked == []
    assert report.bodyless == 0


async def test_a_row_still_served_without_a_body_is_kept_exactly_as_it_was(
    tmp_path: Path,
) -> None:
    """The meter may still be biting. A smaller archive is not a better one."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored())

    report = await backfill(FakeFetcher({URL: metered_page()}), store, ManualClock(NOW), pause=None)

    assert RawStore.open(tmp_path, source_id="coindesk").read_all() == (stored(),)
    assert report.still_bodyless == 1


async def test_a_backfilled_row_moves_only_its_body_and_its_fetch_instant(
    tmp_path: Path,
) -> None:
    """A publisher who edited the headline since must not rewrite what the archive recorded."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored())
    edited = LD_JSON.replace("Trump Odds on Polymarket Hit Another All-Time High", "Rewritten")

    await backfill(FakeFetcher({URL: article_page(edited)}), store, ManualClock(NOW), pause=None)

    (row,) = RawStore.open(tmp_path, source_id="coindesk").read_all()
    assert row.title == "Trump Odds on Polymarket Hit Another All-Time High"
    assert row.fetched_at == NOW


async def test_a_summary_written_by_the_later_pass_survives_a_backfill(tmp_path: Path) -> None:
    """Summarising is a separate process over the stored bodies, and may run before this does."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(stored(summary="A summary an earlier LLM pass wrote."))

    await backfill(FakeFetcher({URL: article_page()}), store, ManualClock(NOW), pause=None)

    (row,) = RawStore.open(tmp_path, source_id="coindesk").read_all()
    assert row.summary == "A summary an earlier LLM pass wrote."


async def test_limit_stops_the_pass_after_n_rows(tmp_path: Path) -> None:
    """`--limit 5` is the cheap probe: does an authenticated session lift the meter at all?

    Finding that out must cost five requests, not the 5 678 a full pass would spend before
    reporting that every one of them came back metered.
    """
    store = RawStore.open(tmp_path, source_id="coindesk")
    pages = {}
    for day in range(1, 5):
        url = f"https://www.coindesk.com/markets/2024/07/0{day}/story"
        store.append(stored(url=url))
        pages[url] = article_page()
    fetcher = FakeFetcher(pages)

    report = await backfill(fetcher, store, ManualClock(NOW), pause=None, limit=2)

    assert len(fetcher.asked) == 2
    assert report.refetched == 2


async def test_rows_untouched_by_a_limited_pass_are_still_in_the_store(tmp_path: Path) -> None:
    """The rewrite replaces the file, so a partial pass must carry the rows it never examined."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    first = "https://www.coindesk.com/markets/2024/07/01/one"
    second = "https://www.coindesk.com/markets/2024/07/02/two"
    store.append(stored(url=first))
    store.append(stored(url=second))

    await backfill(
        FakeFetcher({first: article_page()}), store, ManualClock(NOW), pause=None, limit=1
    )

    rows = RawStore.open(tmp_path, source_id="coindesk").read_all()
    assert [row.url for row in rows] == [first, second]
