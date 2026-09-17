"""Sitemap-addressed sources: CryptoSlate and Bitcoin.com News (spec §6.3).

Structure verified against both live sites on 2026-09-17:

* Both are Yoast-style. `sitemap_index.xml` lists numbered `post-sitemapN.xml` chunks, each with a
  `lastmod`; each chunk lists up to 1 000 article URLs, each with its own `lastmod`.
* **A sitemap `lastmod` is not a publication date.** Two of four articles sampled from
  "2025" chunks were published 2024-12-31 and 2023-11-17 — an old piece re-edited later carries a
  recent `lastmod`. So a chunk's dates select *which chunks to walk*; they never date a row. The
  row's date comes from the article's own `ld+json`, exactly as §6.7 demands.
* Neither publishes `articleBody`, so the body is scoped to a per-site container:
  `post-box__content-flow` for CryptoSlate, `article__body` for Bitcoin.com. Both publish
  `wordCount`, which is what makes extraction *checkable* rather than merely plausible.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from decision_lab.archive.sites import PROFILES, parse_site_article, parse_sitemap
from tradebot.core.errors import ConfigError

FETCHED = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)

LD = """{"@context":"https://schema.org","@type":"NewsArticle",
 "headline":"Ethereum Researcher Proposes 100x Gas Limit Hike",
 "description":"A proposal would raise the block gas limit dramatically.",
 "keywords":["ethereum","gas-limit"],
 "articleSection":"Tech",
 "author":{"@type":"Person","name":"Liam Wright"},
 "wordCount":363,
 "datePublished":"2025-06-30T09:36:28+00:00",
 "dateModified":"2025-07-01T10:00:00+00:00"}"""


def page(container: str, ld: str = LD) -> str:
    """An article page shaped like the real ones: chrome, the body container, then boilerplate."""
    return f"""<html><head>
<style>.x{{content:"junk"}}</style>
<script type="application/ld+json">{ld}</script>
</head><body>
<nav><p>NewsMarketsPricesAboutContactSubscribeNewsletterEventsResearch</p></nav>
<div class="{container}">
  <p>The proposal would raise the block gas limit by two orders of magnitude.</p>
  <p>Validators would need substantially more bandwidth to keep up with larger blocks.</p>
</div>
<div class="site-footer__disclosure">
  <p>Disclaimer: this article is provided for informational purposes only and is not advice.</p>
</div>
</body></html>"""


SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://cryptoslate.com/post-sitemap19.xml</loc><lastmod>2025-02-11</lastmod></sitemap>
<sitemap><loc>https://cryptoslate.com/post-sitemap20.xml</loc><lastmod>2025-06-04</lastmod></sitemap>
<sitemap><loc>https://cryptoslate.com/category-sitemap.xml</loc><lastmod>2026-01-01</lastmod></sitemap>
</sitemapindex>"""


def test_a_sitemap_yields_its_locations_with_their_dates() -> None:
    entries = parse_sitemap(SITEMAP_INDEX)

    assert entries == (
        ("https://cryptoslate.com/post-sitemap19.xml", "2025-02-11"),
        ("https://cryptoslate.com/post-sitemap20.xml", "2025-06-04"),
        ("https://cryptoslate.com/category-sitemap.xml", "2026-01-01"),
    )


@pytest.mark.parametrize("source_id", ["cryptoslate", "bitcoincom"])
def test_the_body_is_scoped_to_the_article_container(source_id: str) -> None:
    """Site chrome and the legal disclaimer are not article text — and both sit in <p> tags."""
    profile = PROFILES[source_id]

    article = parse_site_article(
        page(profile.body_container), url="https://x/y", fetched_at=FETCHED, profile=profile
    )

    assert "two orders of magnitude" in article.body
    assert "substantially more bandwidth" in article.body
    assert "informational purposes" not in article.body, "the disclaimer is boilerplate"
    assert "NewsMarketsPrices" not in article.body, "navigation is not article text"


def test_the_publishers_own_description_becomes_the_abstract() -> None:
    profile = PROFILES["cryptoslate"]

    article = parse_site_article(
        page(profile.body_container), url="https://x/y", fetched_at=FETCHED, profile=profile
    )

    assert article.abstract == "A proposal would raise the block gas limit dramatically."
    assert article.keywords == ("ethereum", "gas-limit")
    assert article.author == "Liam Wright"
    assert article.published_at == datetime(2025, 6, 30, 9, 36, 28, tzinfo=UTC)
    assert article.summary == ""


def test_an_article_without_a_publication_instant_is_refused() -> None:
    """Same rule as every other source: a row that cannot be placed in time is not stored."""
    profile = PROFILES["cryptoslate"]
    undated = LD.replace('"datePublished":"2025-06-30T09:36:28+00:00",', "")

    with pytest.raises(ConfigError, match="datePublished"):
        parse_site_article(
            page(profile.body_container, undated),
            url="https://x/y",
            fetched_at=FETCHED,
            profile=profile,
        )


GRAPH_LD = """{"@context":"https://schema.org","@graph":[
 {"@type":"WebSite","name":"CryptoSlate"},
 {"@type":"WebPage","url":"https://x/y"},
 {"@type":"NewsArticle",
  "headline":"Ethereum Foundation Introduces New Leadership Model",
  "description":"The foundation restructures around a management team.",
  "keywords":["ethereum","governance"],
  "author":{"@type":"Person","name":"Liam Wright"},
  "datePublished":"2025-03-01T08:00:00+00:00"}]}"""


def test_an_article_nested_in_a_yoast_graph_is_found() -> None:
    """Yoast wraps every node in `@graph`, so a top-level-only scan finds nothing and refuses.

    Caught against the live site: every CryptoSlate article uses this shape, so the source was
    refusing 100% of its rows while the fixture with a flat ld+json passed.
    """
    profile = PROFILES["cryptoslate"]

    article = parse_site_article(
        page(profile.body_container, GRAPH_LD),
        url="https://x/y",
        fetched_at=FETCHED,
        profile=profile,
    )

    assert article.published_at == datetime(2025, 3, 1, 8, 0, tzinfo=UTC)
    assert article.title == "Ethereum Foundation Introduces New Leadership Model"
    assert article.abstract == "The foundation restructures around a management team."


CRYPTOSLATE_LD = """{"@context":"https://schema.org","@graph":[
 {"@type":"NewsArticle",
  "headline":"Fed Policy Favours Big Banks",
  "articleSection":["Banking","Featured","Regulation","Stablecoins"],
  "author":{"@type":"Person","name":"Liam Wright"},
  "wordCount":588,
  "datePublished":"2025-04-28T14:00:00+00:00"}]}"""


def cryptoslate_page() -> str:
    """The real CryptoSlate shape: no description or keywords in ld+json, both in <meta>."""
    body = page(PROFILES["cryptoslate"].body_container, CRYPTOSLATE_LD)
    meta = (
        '<meta name="description" content="Long argued that the Fed&#039;s unchanged '
        'policy creates an unfair advantage."/>'
    )
    return body.replace("</head>", meta + "</head>", 1)


def test_the_abstract_falls_back_to_the_meta_description() -> None:
    """CryptoSlate carries no `description` in its ld+json, so the node alone yields no abstract.

    Measured live: bodies and dates parsed perfectly while every abstract came back empty. The
    publisher does emit one -- in `<meta name="description">` -- and that is the same text.
    """
    article = parse_site_article(
        cryptoslate_page(),
        url="https://x/y",
        fetched_at=FETCHED,
        profile=PROFILES["cryptoslate"],
    )

    assert article.abstract.startswith("Long argued that the Fed's unchanged policy")


def test_a_list_valued_article_section_becomes_section_and_keywords() -> None:
    """`articleSection` is a list on CryptoSlate, and it is the only topic signal those rows carry.

    Passed through `str()` it would render as a Python repr -- "['Banking', 'Featured', ...]" --
    which is neither a section nor a usable tag set.
    """
    article = parse_site_article(
        cryptoslate_page(),
        url="https://x/y",
        fetched_at=FETCHED,
        profile=PROFILES["cryptoslate"],
    )

    assert article.section == "Banking"
    assert article.keywords == ("Banking", "Featured", "Regulation", "Stablecoins")
