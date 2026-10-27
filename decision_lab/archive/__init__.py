"""Collecting recorded news, so a past decision can be scored on what was knowable at the time.

Two stores, two rules, and the split is the point (spec §6.3.1, §6.4):

* `store.py` is **staging** — raw articles as crawled, bodies included, gitignored and never
  distributed. It exists only so the summarizer can run later instead of inline.
* `published.py` is the half that **travels** — the §6.6 archive a replay reads. It keeps §6.4's
  rule and never gains a body field; `publish.py` is the one projection into it.

Failure semantics: every fetch goes through `tradebot.news.http.FeedFetcher`, so a `robots.txt`
denial or a 403 raises `SourceDisallowedError` and we stop asking, a 429 raises `RateLimitedError`
carrying the publisher's `Retry-After`, and a timeout or 5xx raises `VenueError`. A crawl that
stops is resumable: the staging store is keyed by canonical URL, so restarting re-reads what is
already held and asks only for the rest.
"""

from __future__ import annotations

from decision_lab.archive.config import ArchivePolicy, load_policy
from decision_lab.archive.publish import publish_archive
from decision_lab.archive.published import ArchiveHeader, ArchiveItem, PublishedArchive
from decision_lab.archive.summarize import summarize_store

__all__ = [
    "ArchiveHeader",
    "ArchiveItem",
    "ArchivePolicy",
    "PublishedArchive",
    "load_policy",
    "publish_archive",
    "summarize_store",
]
