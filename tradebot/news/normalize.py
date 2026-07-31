"""Turning a publisher's feed entry into something storable, comparable and legal to keep.

Three jobs, all of them deterministic so a replayed cycle rebuilds byte-identical items:

* **Canonical URLs.** The same article arrives with different tracking parameters from different
  feeds. Canonicalization is what makes the cheap half of dedup — a URL hash — actually work.
* **Text, not markup.** Feed summaries are HTML. The panel reads plain text, and an unescaped
  tag in a prompt is noise at best.
* **Excerpts, not articles.** Policy, not preference: we store title + short excerpt + link and
  nothing more, because retaining full article bodies is a copyright exposure we get no trading
  benefit from (PLAN §3.3).

Failure semantics: this module is pure and cannot fail from the outside. Unparseable input
degrades to empty text rather than raising — a mangled summary should cost us one item's
usefulness, not the cycle.
"""

from __future__ import annotations

import re
from hashlib import blake2s
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query parameters that identify the *referral*, not the article. Stripped so two links to one
#: story hash identically.
TRACKING_PARAMS = re.compile(r"^(utm_|ga_|mc_|pk_|_hs|ref$|refsrc$|fbclid$|gclid$|igshid$|source$)")

DEFAULT_EXCERPT_CHARS = 280

_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z0-9]+")


class _TextExtractor(HTMLParser):
    """Collects character data, dropping markup.

    stdlib rather than a regex: feed HTML contains unclosed tags, CDATA and stray angle
    brackets, and a regex that "works" on those quietly eats sentences.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: str) -> str:
    """Markup-free, whitespace-collapsed text."""
    extractor = _TextExtractor()
    try:
        extractor.feed(value)
        extractor.close()
    except Exception:
        return _WHITESPACE.sub(" ", unescape(value)).strip()
    return _WHITESPACE.sub(" ", "".join(extractor.parts)).strip()


def excerpt(value: str, limit: int = DEFAULT_EXCERPT_CHARS) -> str:
    """Truncate to `limit` characters on a word boundary.

    Retention policy, expressed in code: the excerpt is what we are entitled to keep.
    """
    text = strip_html(value)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > limit // 2 else cut).rstrip() + "…"


def canonical_url(url: str) -> str:
    """Scheme/host lowercased, fragment and tracking parameters removed, path de-slashed."""
    parts = urlsplit(url.strip())
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parts.query) if not TRACKING_PARAMS.match(key)]
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        ((parts.scheme or "https").lower(), (parts.netloc or "").lower(), path, query, "")
    )


def url_hash(url: str) -> str:
    """Stable identity for an article. `blake2s`, not `hash()`, which is salted per process."""
    return blake2s(canonical_url(url).encode(), digest_size=16).hexdigest()


def tokens(value: str) -> frozenset[str]:
    """Lowercase alphanumeric words. The unit both relevance and embedding are built from."""
    return frozenset(_WORD.findall(value.lower()))
