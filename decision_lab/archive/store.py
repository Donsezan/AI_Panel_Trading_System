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
import os
import tempfile
from pathlib import Path
from typing import IO, Any, Final, Self

from decision_lab.jobs import Busy, _drop, _take
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
    """Staging for one source, sharded one file per publication month.

    One file per source reached 22-28 MB, and `read_all` parses all of it on every call - every
    backfill batch, every `held` check, and every point-in-time read a replay makes. A cycle
    deciding on 2024-03-15 has no use for December.

    The rule is unconditional: `published_at[:7]` is the filename, always. No threshold, no
    "sparse months go elsewhere", no target year the store must know. A reader computes the path
    instead of consulting a rule, which is what lets `ArchiveNewsFeed` ask by date and nothing
    else. The price is a tail of near-empty shards where a sitemap chunk straddled a year, and it
    is paid deliberately (spec section 6.3.1).

    `published_at` is the key because it is already the only thing that dates a row (section 6.7);
    `fetched_at` would scatter one month across every shard the crawl happened to run in.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    @classmethod
    def open(cls, directory: Path, *, source_id: str) -> Self:
        legacy = directory / f"{source_id}{SUFFIX}"
        if legacy.exists():
            raise ConfigError(
                f"{legacy} is a flat store from before month sharding. Run `archive reshard "
                f"--data <dir> --source {source_id}` first: writing shards beside it would leave "
                "resume blind to every row it holds, and the next crawl would ask the publisher "
                "for all of them again."
            )
        own = directory / source_id
        own.mkdir(parents=True, exist_ok=True)
        return cls(own)

    @property
    def path(self) -> Path:
        """The source's own directory. Named `path` because that is what the log line reports."""
        return self._dir

    @staticmethod
    def month_of(article: RawArticle) -> str:
        return f"{article.published_at:%Y-%m}"

    def shard(self, month: str) -> Path:
        return self._dir / f"{month}{SUFFIX}"

    def months(self) -> tuple[str, ...]:
        """Every month this store holds, chronological."""
        return tuple(sorted(p.stem for p in self._dir.glob(f"*{SUFFIX}")))

    def append(self, article: RawArticle) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self.shard(self.month_of(article)).open("a", encoding="utf-8") as handle:
            handle.write(article.model_dump_json() + "\n")

    def rewrite(self, articles: tuple[RawArticle, ...]) -> None:
        """Replace the store with `articles`, touching only the shards whose contents changed.

        This is what sharding buys: a backfill batch that filled March rewrites 1.8 MB rather than
        the whole 22 MB store. A shard whose rendered text is byte-identical is skipped entirely,
        so its mtime does not move and a backup or sync sees the truth about what changed.

        A month that ends up empty has its shard **deleted**, not left behind: a stale shard would
        be read straight back by the next `read_all`.
        """
        grouped: dict[str, list[RawArticle]] = {}
        for article in articles:
            grouped.setdefault(self.month_of(article), []).append(article)

        self._dir.mkdir(parents=True, exist_ok=True)
        for month, rows in grouped.items():
            payload = "".join(row.model_dump_json() + "\n" for row in rows)
            target = self.shard(month)
            if target.exists() and target.read_text(encoding="utf-8") == payload:
                continue
            _atomic_write(target, payload)
        for stale in set(self.months()) - set(grouped):
            self.shard(stale).unlink(missing_ok=True)

    @property
    def lock_path(self) -> Path:
        """The advisory lock for this source. Beside the directory, so it is never a shard."""
        return self._dir.with_suffix(".lock")

    @staticmethod
    def identity(url: str) -> str:
        """The key a crawl resumes on. The bot's own canonicalisation, never a second one (2.4).

        A listing link and a share link differ only by tracking parameters, so keying on the raw
        URL fetches one story twice and stores it twice.
        """
        return canonical_url(url)

    def held(self) -> frozenset[str]:
        """Identities already collected, across every shard, so a restart asks only for the rest."""
        return frozenset(self.identity(row.url) for row in self.read_all())

    def read_all(self) -> tuple[RawArticle, ...]:
        """Every row, chronological by shard. Month order, never directory order."""
        return tuple(row for month in self.months() for row in self.read_month(month))

    def read_months(self, start: str, end: str) -> tuple[RawArticle, ...]:
        """The rows in `start`..`end` inclusive, as `YYYY-MM`. The read a replay actually makes."""
        return tuple(
            row
            for month in self.months()
            if start <= month <= end
            for row in self.read_month(month)
        )

    def read_month(self, month: str) -> tuple[RawArticle, ...]:
        path = self.shard(month)
        if not path.exists():
            return ()
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(RawArticle.model_validate(json.loads(line)))
            except (ValueError, TypeError) as exc:
                raise ConfigError(f"{path}:{number} is not a readable raw article: {exc}") from exc
        return tuple(rows)


def _atomic_write(path: Path, payload: str) -> None:
    """Write via a temporary file in the same directory, swapped in with `os.replace`.

    Atomic on Windows and POSIX alike, so a process dying mid-write leaves the shard `read_month`
    already trusts untouched rather than truncated on its final line. Same filesystem by
    construction, which is what makes the replace a rename - the idiom `registry.record` uses.
    """
    descriptor, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class StoreLock:
    """One writer at a time over one raw store, for the life of a pass.

    `rewrite` replaces the file whole from rows read at the start of a pass, so two backfills over
    one store lose data silently: the second reads the same pre-pass state and puts those bodyless
    rows back over the first's gains, both reporting success. This is the lost-update hazard
    `jobs.WorkspaceLock` exists for, one directory across.

    It cannot *be* the workspace lock: `archive` is deliberately absent from `cli.LOCKED`, because
    a multi-hour crawl holding the workspace would block every sweep and report for its duration.
    So the scope is the one file that gets rewritten, and two publishers never serialise on each
    other.

    An OS advisory lock rather than a pid file, for the reason `jobs.py` gives at length: the OS
    releases it when the holder dies, and there is no portable liveness check — `os.kill(pid, 0)`
    *terminates* the process on Windows.
    """

    def __init__(self, store: RawStore) -> None:
        self._store = store
        self._handle: IO[Any] | None = None

    def acquire(self) -> None:
        path = self._store.lock_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        if not _take(handle):
            handle.close()
            raise Busy(
                f"another pass holds {self._store.path.name}. Two backfills over one store would "
                "rewrite each other's rows and lose the bodies already collected, reporting "
                "success from both (§6.3.1). Wait for it to finish."
            )
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        _drop(self._handle)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> StoreLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
