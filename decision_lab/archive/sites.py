"""Sitemap-addressed publishers, behind one profile (spec §6.3).

CoinDesk is addressed by a dated year listing; these are addressed by Yoast sitemaps, and the
difference is real enough to be its own module rather than a branch inside `coindesk.py`.

Three rules this file exists to hold:

* **A sitemap `lastmod` is not a publication date.** Sampling "2025" chunks on 2026-09-17 returned
  articles published 2024-12-31 and 2023-11-17 — an old piece re-edited later carries a recent
  `lastmod`. So chunk dates choose *which chunks to walk* and nothing else; a row is dated by its
  own `ld+json`, exactly as §6.7 demands. Anything fetched is kept, because the archive is the
  point and the fetch is already paid for.
* **Neither publisher puts `articleBody` in its `ld+json`**, so the body is scoped to a per-site
  container. Unscoped `<p>` extraction picks up navigation and the legal disclaimer, both of which
  are real paragraphs.
* **Both publish `wordCount`**, which is what makes extraction checkable rather than plausible:
  measured against the live sites, the scoped body matched the declared count on both.

Failure semantics: an article with no `datePublished` raises `ConfigError` and is never stored,
identically to every other source. Fail closed — a row that cannot be placed in time cannot be
filtered point-in-time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Final

from decision_lab.archive.coindesk import _LD_JSON, _first_author, _instant, _text
from decision_lab.archive.store import RawArticle
from tradebot.core.errors import ConfigError

_WHITESPACE: Final = re.compile(r"\s+")
_ENTRY: Final = re.compile(r"<loc>\s*(.*?)\s*</loc>(?:\s*<lastmod>\s*(.*?)\s*</lastmod>)?", re.S)

#: The publisher's own summary when its ld+json carries none. CryptoSlate emits no `description`
#: on the article node but does emit this, and it is the same sentence.
_META_DESCRIPTION: Final = re.compile(
    r'<meta[^>]+(?:name="description"|property="og:description")[^>]+content="([^"]*)"',
    re.I,
)


@dataclass(frozen=True, slots=True)
class SiteProfile:
    """What differs between two publishers that are otherwise read identically."""

    source_id: str
    sitemap_index: str
    #: Substring of the `class` attribute on the element wrapping the article text.
    body_container: str
    #: Only chunks whose loc contains this are article chunks; the index also lists categories,
    #: tags and authors, and walking those would fetch thousands of non-articles.
    chunk_marker: str = "post-sitemap"


PROFILES: Final[dict[str, SiteProfile]] = {
    "cryptoslate": SiteProfile(
        source_id="cryptoslate",
        sitemap_index="https://cryptoslate.com/sitemap_index.xml",
        body_container="post-box__content-flow",
    ),
    "bitcoincom": SiteProfile(
        source_id="bitcoincom",
        sitemap_index="https://news.bitcoin.com/sitemap_index.xml",
        body_container="article__body",
    ),
}


#: Node types that count as the article. Yoast emits `NewsArticle`; some templates use `Article`
#: or `BlogPosting` for the same thing.
ARTICLE_TYPES: Final = ("NewsArticle", "Article", "BlogPosting")


def article_record(page: str) -> dict[str, Any]:
    """The article node from the page's ld+json, **searched recursively**.

    Yoast wraps every node in `@graph`, so a scan of only the top level finds nothing and the
    source refuses every row it is given. Found against the live site, where a flat-ld+json fixture
    passed while 100% of real CryptoSlate articles were refused.

    Prefers a node that actually carries `datePublished`: a graph can hold several article-ish
    nodes, and the one that can be dated is the one worth having.
    """
    best: dict[str, Any] = {}
    for block in _LD_JSON.findall(page):
        try:
            loaded = json.loads(block)
        except ValueError:
            continue
        stack: list[Any] = [loaded]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                types = current.get("@type") or ""
                names = types if isinstance(types, list) else [types]
                if any(str(t) in ARTICLE_TYPES for t in names):
                    if current.get("datePublished"):
                        return current
                    best = best or current
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    return best


def meta_description(page: str) -> str:
    """`<meta name="description">`, unescaped. The abstract of last resort."""
    found = _META_DESCRIPTION.search(page)
    return _text(unescape(found.group(1))) if found else ""


def _as_tuple(value: object) -> tuple[str, ...]:
    """A schema.org field that is sometimes a string and sometimes a list of them."""
    if value is None:
        return ()
    items = value if isinstance(value, list) else [value]
    return tuple(_text(item) for item in items if _text(item))


def parse_sitemap(document: str) -> tuple[tuple[str, str], ...]:
    """Every `<loc>` in a sitemap or sitemap index, with its `<lastmod>` (or "" when absent)."""
    return tuple((loc, mod or "") for loc, mod in _ENTRY.findall(document))


class _Scoped(HTMLParser):
    """Character data inside the first element whose class names the article container."""

    VOID: Final = frozenset({"br", "img", "hr", "meta", "link", "input", "source", "wbr"})

    def __init__(self, marker: str) -> None:
        super().__init__(convert_charrefs=True)
        self._marker = marker
        self.blocks: list[str] = []
        self._depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            return
        if self._depth:
            self._depth += 1
            return
        if self._marker in unescape(dict(attrs).get("class") or ""):
            self._depth = 1
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID or not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            self.blocks.append("".join(self._parts))

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._parts.append(data)


def body_in(page: str, container: str) -> str:
    """Article text, scoped to `container` and joined across however many blocks it spans."""
    stripped = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    extractor = _Scoped(container)
    extractor.feed(stripped)
    extractor.close()
    joined = " ".join(block for block in extractor.blocks if block.strip())
    return _WHITESPACE.sub(" ", joined).strip()


def parse_site_article(
    page: str, *, url: str, fetched_at: datetime, profile: SiteProfile
) -> RawArticle:
    """One article page to one raw row, refusing anything that cannot be placed in time."""
    record = article_record(page)
    published = record.get("datePublished")
    if not published:
        raise ConfigError(
            f"{url}: no datePublished in the page's ld+json, so the article cannot be placed in "
            "time. A sitemap `lastmod` is not a substitute — it moves when an old article is "
            "edited, and dating a row by it would misplace it by years (spec §6.7)."
        )
    modified = record.get("dateModified")
    # `articleSection` is a list on CryptoSlate and is the only topic signal those rows carry,
    # so it doubles as keywords when the publisher emits none.
    sections = _as_tuple(record.get("articleSection"))
    return RawArticle(
        source_id=profile.source_id,
        url=url,
        title=_text(record.get("headline")),
        body=body_in(page, profile.body_container),
        abstract=_text(record.get("description") or record.get("abstract"))
        or meta_description(page),
        keywords=_as_tuple(record.get("keywords")) or sections,
        author=_first_author(record.get("author")),
        section=sections[0] if sections else "",
        published_at=_instant(str(published), url=url),
        modified_at=_instant(str(modified), url=url) if modified else None,
        fetched_at=fetched_at,
    )
