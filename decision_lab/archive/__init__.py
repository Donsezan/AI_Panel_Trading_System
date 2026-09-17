"""Collecting recorded news, so a past decision can be scored on what was knowable at the time.

Two stores, two rules, and the split is the point (spec §6.3.1, §6.4):

* `store.py` is **staging** — raw articles as crawled, bodies included, gitignored and never
  distributed. It exists only so the summarizer can run later instead of inline.
* The §6.6 archive that travels keeps §6.4's rule and never gains a body field.

Failure semantics: every fetch goes through `tradebot.news.http.FeedFetcher`, so a `robots.txt`
denial or a 403 raises `SourceDisallowedError` and we stop asking, a 429 raises `RateLimitedError`
carrying the publisher's `Retry-After`, and a timeout or 5xx raises `VenueError`. A crawl that
stops is resumable: the staging store is keyed by canonical URL, so restarting re-reads what is
already held and asks only for the rest.
"""

from __future__ import annotations
