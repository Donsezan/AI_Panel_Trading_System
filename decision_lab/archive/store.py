"""The raw staging store — what a crawl collected, before anything summarises it (spec §6.3.1).

The deliberate, scoped exception to §6.4. That section holds an article body in memory for the
summarizer call and discards it, which works only while summarising happens inline; deferring the
summarizer means the body has to survive between the two steps. It survives *here*, in a store that
is gitignored, never distributed, never read by a report, and never an input to `archive_digest` —
while the §6.6 archive that travels keeps §6.4's rule and has no body field at all.

A raw row records **facts, never policy**: `published_at` is the publisher's claim and `fetched_at`
is when we retrieved it. §6.7's derived `observed_at` is computed when the archive is built, so the
lag can be revised without re-crawling.

Failure semantics: the store is a file. An unreadable or truncated line raises `ConfigError` naming
the file and the line, rather than being skipped — a staging store that silently dropped rows would
under-report coverage in a way no later step could detect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Self

from tradebot.core.errors import ConfigError
from tradebot.core.schema import DomainModel, UtcDatetime
from tradebot.news.normalize import canonical_url

#: One file per source, so two sources are never interleaved in one resume scan.
SUFFIX: Final = ".jsonl"

#: Staging lives beside the dataset, in its own directory, and is gitignored. Never the
#: workspace: the workspace is scratch a corpus id owns, and this outlives any one corpus.
RAW_DIR: Final = "news-raw"


class RawArticle(DomainModel):
    """One crawled article, exactly as collected.

    `body` is the field §6.4 forbids in the travelling archive and permits here; see the module
    docstring for why the two stores differ.

    `abstract` is the **publisher's** own one-line summary, and it is kept beside `body` rather
    than instead of it: it cannot contain hindsight — it was written at publication — so it is the
    baseline any later summarizing pass is compared against.

    `summary` is that later pass's output and is empty at collection time. It is a field rather
    than a second file so one row carries one article's whole record.
    """

    source_id: str
    url: str
    title: str
    body: str
    abstract: str = ""
    keywords: tuple[str, ...] = ()
    author: str = ""
    section: str = ""
    published_at: UtcDatetime
    modified_at: UtcDatetime | None = None
    fetched_at: UtcDatetime
    summary: str = ""


class RawStore:
    """Append-only staging for one source."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def open(cls, directory: Path, *, source_id: str) -> Self:
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory / f"{source_id}{SUFFIX}")

    @property
    def path(self) -> Path:
        return self._path

    def append(self, article: RawArticle) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(article.model_dump_json() + "\n")

    @staticmethod
    def identity(url: str) -> str:
        """The key a crawl resumes on. The bot's own canonicalisation, never a second one (§2.4).

        A listing link and a share link differ only by tracking parameters, so keying on the raw
        URL fetches one story twice and stores it twice.
        """
        return canonical_url(url)

    def held(self) -> frozenset[str]:
        """Identities already collected, so a restarted crawl asks only for the rest."""
        return frozenset(self.identity(row.url) for row in self.read_all())

    def read_all(self) -> tuple[RawArticle, ...]:
        if not self._path.exists():
            return ()
        rows = []
        for number, line in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(RawArticle.model_validate(json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise ConfigError(
                    f"{self._path}:{number} is not a readable raw article: {exc}"
                ) from exc
        return tuple(rows)
