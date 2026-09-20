"""Parsing one publisher's archive listing and article pages (spec §6.3, §6.7).

The fixtures here are trimmed from real captures, and every structural choice they encode was
verified against the live site on 2026-09-16 rather than guessed:

* A listing row is an `<a>` whose `href` carries `/<section>/YYYY/MM/DD/<slug>`, with the title in
  the first `<span>` and a `YYYY-MM-DD` in the second. Non-article links (sign-in, navigation) sit
  in the same list and must not be mistaken for rows.
* **The listing's date and the article's own date disagree** on about 1% of rows — a recurring
  column keeps its original URL date while the listing shows when it was last touched. Neither is
  the publication instant, which is why §6.7 forbids deriving one from a date.
* An article page carries `application/ld+json` with `datePublished` precise to the millisecond,
  plus the publisher's own `abstract` and `keywords`.
* The body lives in `<div class="document-body …">` — **more than one of them**, because an ad or a
  premium component splits it — and the boilerplate disclosure is the one carrying `font-metadata`.
  Taking the longest block instead of joining all the non-metadata ones silently drops the lede.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from decision_lab.archive.coindesk import parse_article, parse_listing
from tradebot.core.errors import ConfigError

URL = "https://www.coindesk.com/markets/2024/07/16/trump-odds-hit-high"
FETCHED = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

LD_JSON = """{"@context":"https://schema.org","@type":"NewsArticle",
 "headline":"Trump Odds on Polymarket Hit Another All-Time High",
 "abstract":"Traders now see a 72% chance the former president retakes the White House.",
 "keywords":["prediction-markets","polymarket","trump"],
 "articleSection":"Markets",
 "author":[{"@type":"Person","name":"Marc Hochstein"}],
 "datePublished":"2024-07-16T15:32:00.326Z",
 "dateModified":"2024-07-16T15:40:11.469Z"}"""


def article_page(ld_json: str = LD_JSON) -> str:
    """A trimmed article page with the real page's load-bearing structure."""
    return f"""<html><head>
<style>.pullquote:after{{content:url("data:image/svg+xml,");bottom:-8px}}</style>
<script type="application/ld+json">{ld_json}</script>
<script>window.__x = {{"datePublished":"1999-01-01T00:00:00Z"}};</script>
</head><body>
<nav><p>NewsMarketsFinanceTechPolicyFocusBitcoin NewsEthereum NewsPrices</p></nav>
<div class="document-body font-body-lg [&amp;_h1]:clear-both">
  <p>&quot;Yes&quot; shares for Trump were trading at 72 cents mid-morning Tuesday.</p>
</div>
<div class="document-body font-body-lg [&amp;_h1]:clear-both">
  <p>A total of $262 million has been staked on the contract, a record.</p>
</div>
<div class="document-body font-body-lg font-metadata text-subtle">
  <p>Disclosure &amp; Polices: CoinDesk is an award-winning media outlet.</p>
</div>
</body></html>"""


def test_the_publication_instant_comes_from_the_article_not_its_url() -> None:
    """§6.7: a date plus a lag is up to 24h of intra-day look-ahead. Only the instant will do."""
    parsed = parse_article(article_page(), url=URL, fetched_at=FETCHED)

    assert parsed.published_at == datetime(2024, 7, 16, 15, 32, 0, 326000, tzinfo=UTC)


def test_an_article_without_a_publication_instant_is_refused() -> None:
    """Fail closed. A row we cannot place in time cannot be filtered point-in-time (§6.7)."""
    without = LD_JSON.replace('"datePublished":"2024-07-16T15:32:00.326Z",', "")

    with pytest.raises(ConfigError, match="datePublished"):
        parse_article(article_page(without), url=URL, fetched_at=FETCHED)


def test_the_body_joins_every_block_and_excludes_the_chrome() -> None:
    """A split body is the normal case, and the lede is in the *first* block."""
    parsed = parse_article(article_page(), url=URL, fetched_at=FETCHED)

    assert "72 cents mid-morning" in parsed.body, "the lede block was dropped"
    assert "$262 million has been staked" in parsed.body, "the second block was dropped"
    assert "award-winning media outlet" not in parsed.body, "the disclosure is boilerplate"
    assert "NewsMarketsFinance" not in parsed.body, "navigation is not article text"
    assert "pullquote" not in parsed.body, "stylesheet text is not article text"


def test_the_publishers_own_abstract_and_keywords_are_kept() -> None:
    """The abstract is what a later summarizing pass compares against, and may replace it."""
    parsed = parse_article(article_page(), url=URL, fetched_at=FETCHED)

    assert (
        parsed.abstract
        == "Traders now see a 72% chance the former president retakes the White House."
    )
    assert parsed.keywords == ("prediction-markets", "polymarket", "trump")
    assert parsed.author == "Marc Hochstein"
    assert parsed.section == "Markets"
    assert parsed.modified_at == datetime(2024, 7, 16, 15, 40, 11, 469000, tzinfo=UTC)


def test_a_freshly_collected_row_carries_no_summary_yet() -> None:
    """Summarising is a later pass that augments this record; collection never writes one."""
    parsed = parse_article(article_page(), url=URL, fetched_at=FETCHED)

    assert parsed.summary == ""


LISTING = """<html><body><div class="divide-y">
<a class="flex justify-between" href="https://www.coindesk.com/auth/login?returnTo=%2Fsitemap"
  ><span class="truncate pr-4">Sign In</span
  ><span class="whitespace-nowrap">2024-07-16</span></a
><a class="flex justify-between" href="https://www.coindesk.com/markets/2024/07/16/trump-odds"
  ><span class="truncate pr-4">Trump Odds Hit Another All-Time High</span
  ><span class="whitespace-nowrap">2024-07-16</span></a
><a class="flex justify-between" href="https://www.coindesk.com/tech/2024/06/27/protocol-village"
  ><span class="truncate pr-4">Protocol Village: DWF Launches $20M Fund</span
  ><span class="whitespace-nowrap">2024-07-04</span></a
></div></body></html>"""


def test_only_article_links_are_listing_rows() -> None:
    """A sign-in link sits in the same list and has the same shape bar the date in its path.

    `robots.txt` disallows `/auth/`, so a parser that admitted it would also make a request the
    publisher forbade.
    """
    rows = parse_listing(LISTING)

    assert [row.url for row in rows] == [
        "https://www.coindesk.com/markets/2024/07/16/trump-odds",
        "https://www.coindesk.com/tech/2024/06/27/protocol-village",
    ]


def test_a_row_keeps_the_listed_date_not_the_one_in_its_url() -> None:
    """The two disagree on ~1% of rows, and the listing's is the one the page is ordered by.

    Neither is the publication instant — that comes from the article (§6.7) — but page-walking
    has only the listing to decide when it has gone far enough back.
    """
    village = parse_listing(LISTING)[1]

    assert village.listed_on == date(2024, 7, 4), "the URL says 2024/06/27; the listing governs"
    assert village.title == "Protocol Village: DWF Launches $20M Fund"
