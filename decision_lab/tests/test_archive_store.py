"""The raw staging store: what a crawl collected, before anything summarises it (spec §6.3.1).

This store is the deliberate exception to §6.4's "the article body is never written to disk", and
the exception is scoped rather than general. §6.4 holds a body in memory for the summarizer call
and discards it, which works only while summarising happens *inline*. Deferring the summarizer —
the decision taken on 2026-09-16, because no summarizer key exists yet — means the body has to
survive between the two steps, and there is nowhere to survive but disk.

So there are two stores with two different rules. This one is staging: gitignored, never
distributed, never read by a report, and never an input to `archive_digest`. The §6.6 archive that
travels keeps §6.4's rule intact and has no body field at all.

A raw row records **facts**, never policy. `published_at` is the publisher's claim and `fetched_at`
is when we retrieved it; `observed_at` is §6.7's *derived* field and is computed when the archive is
built, not here. Storing a derived value in a raw store would make the lag un-revisable without
re-crawling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from decision_lab.archive.store import RawArticle, RawStore

PUBLISHED = datetime(2024, 3, 11, 9, 2, tzinfo=UTC)
FETCHED = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def article(url: str = "https://www.coindesk.com/markets/2024/03/11/a-headline") -> RawArticle:
    return RawArticle(
        source_id="coindesk",
        url=url,
        title="A headline",
        body="The body text the summarizer will read later.",
        published_at=PUBLISHED,
        fetched_at=FETCHED,
    )


def test_a_stored_article_is_read_back_whole(tmp_path: Path) -> None:
    store = RawStore.open(tmp_path, source_id="coindesk")
    stored = article()

    store.append(stored)

    assert RawStore.open(tmp_path, source_id="coindesk").read_all() == (stored,)


def test_a_restarted_crawl_knows_what_it_already_holds(tmp_path: Path) -> None:
    """Resume is the whole reason this is append-only: ~2 800 fetches is not a one-sitting job."""
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(article("https://www.coindesk.com/markets/2024/03/11/one"))
    store.append(article("https://www.coindesk.com/markets/2024/03/12/two"))

    held = RawStore.open(tmp_path, source_id="coindesk").held()

    assert held == {
        "https://www.coindesk.com/markets/2024/03/11/one",
        "https://www.coindesk.com/markets/2024/03/12/two",
    }


def test_a_referral_link_to_a_held_story_is_not_fetched_again(tmp_path: Path) -> None:
    """Identity is the canonical URL, so one story reached by two links is fetched once.

    A sitemap listing and a share link differ only by tracking parameters; keyed on the raw URL,
    the same article is re-fetched and stored twice, and the second copy inflates whatever a
    coverage count later reports.
    """
    store = RawStore.open(tmp_path, source_id="coindesk")
    store.append(article("https://www.coindesk.com/markets/2024/03/11/one"))

    held = RawStore.open(tmp_path, source_id="coindesk").held()

    assert store.identity("https://www.coindesk.com/markets/2024/03/11/one?utm_source=x") in held
