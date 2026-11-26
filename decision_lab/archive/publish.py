# decision_lab/archive/publish.py
"""Staging rows → the §6.6 archive a replay reads. The one place §6.4's boundary is crossed.

It is crossed in the only direction that is allowed: data is dropped. This is the only function in
the package that reads `RawArticle.body`, and it reads it **solely** to decide a row is
publishable — never to copy. `ArchiveItem` has no `body` field, so a mistake here is a refusal at
validation rather than a leak onto disk.

`observed_at` is derived here rather than at collection, so the lag can be revised without
re-crawling (§6.7). It is never `published_at` itself: that grants the panel the headline at the
instant of publication, which is optimistic in the one direction that inflates a score.

An unsummarised row is skipped and counted. Publishing it blank would put a title-only item into
one of eight slots and then fail to collapse it, having no cluster key — and a store with nothing
summarised is refused outright, because an archive of zero rows is a news-blind corpus wearing a
digest. §6.9 is the honest way to run without news, and it says so on the report.

Rows are written in `observed_at` order. Shard order is month order and a month's rows are in
*crawl* order, which is not time order once a crawl has been resumed.

Failure semantics: `ConfigError` for a store that cannot be published, naming the command that
fixes it. The write is `PublishedArchive.write`, which replaces the file whole — a partial archive
is not a smaller archive, it is one whose digest describes rows it does not hold.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from decision_lab.archive.config import ArchivePolicy
from decision_lab.archive.published import (
    ARCHIVE_DIR,
    ARCHIVE_FILE,
    ArchiveHeader,
    ArchiveItem,
    PublishedArchive,
)
from decision_lab.archive.store import RawArticle, RawStore
from decision_lab.archive.summarize import digest_for
from tradebot.core.clock import Clock
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel
from tradebot.news.normalize import canonical_url, url_hash

logger = get_logger(__name__)


class PublishReport(DomainModel):
    """What one publish produced. `unsummarised` says the summarising pass is unfinished."""

    rows: int = 0
    published: int = 0
    unsummarised: int = 0
    archive_digest: str = ""
    path: str = ""


def archive_path(data_dir: Path, source_id: str) -> Path:
    """`<data>/news/<source>/news.jsonl`. Beside the dataset, never in the workspace."""
    return data_dir / ARCHIVE_DIR / source_id / ARCHIVE_FILE


def publish_archive(
    store: RawStore,
    policy: ArchivePolicy,
    clock: Clock,
    *,
    out_dir: Path,
    capture: str,
) -> PublishReport:
    """Project one summarised staging store into its travelling archive."""
    rows = store.read_all()
    ready = tuple(a for a in rows if a.is_summarised)
    if not ready:
        raise ConfigError(
            f"{store.path} holds {len(rows)} rows and none is summarised. Run `archive "
            "summarise --data <dir> --source <id>` first: an archive of zero rows is a "
            "news-blind corpus wearing a digest, and §6.9 is the honest way to run without news"
        )

    provenance = _agreed_provenance(ready, policy)
    items = tuple(sorted((_item(a, policy) for a in ready), key=lambda i: (i.observed_at, i.url)))
    header = ArchiveHeader(
        source=store.path.name,
        capture=capture,
        captured_at=clock.now(),
        observed_at_policy=policy.observed_at_policy,
        summarizer_provider=provenance[0],
        summarizer_model=provenance[1],
        prompt_digest=provenance[2],
        news_window_seconds=policy.news_window_seconds,
        cluster_floor=policy.cluster_floor,
        rows=len(items),
    )
    path = out_dir / ARCHIVE_DIR / store.path.name / ARCHIVE_FILE
    PublishedArchive.write(path, header, items)
    published = PublishedArchive.read(path)

    report = PublishReport(
        rows=len(rows),
        published=len(items),
        unsummarised=len(rows) - len(ready),
        archive_digest=published.header.archive_digest,
        path=str(path),
    )
    logger.info("archive published", extra=report.model_dump(mode="json"))
    return report


def _agreed_provenance(
    ready: tuple[RawArticle, ...], policy: ArchivePolicy
) -> tuple[str, str, str]:
    """The one (provider, model, prompt digest) that wrote every row, and the policy's own.

    The header is stamped from what actually summarised the rows. A store mixing two models, or
    one the policy no longer describes, would publish a digest that vouches for output it did not
    produce - and `archive_digest` feeds `corpus_id`, so the corpus would carry the wrong identity.
    """
    in_force = (policy.provider_id, policy.model, digest_for(policy))
    seen = Counter(
        (a.summary_provider or "?", a.summary_model or "?", a.summary_prompt_digest or "?")
        for a in ready
    )
    fix = (
        "Run `archive summarise --data <dir> --source <id> --resummarise-stale` to redo the rows "
        "under the policy in force, or restore the policy the rows were made under"
    )
    if len(seen) > 1:
        ranked = seen.most_common()
        parts = ", ".join(
            f"{n} rows summarised by {p}/{m}" if i == 0 else f"{n} by {p}/{m}"
            for i, ((p, m, _d), n) in enumerate(ranked)
        )
        if len({(p, m) for (p, m, _d) in seen}) == 1:
            parts += " (the prompt differs between them)"
        raise ConfigError(f"the store mixes summarisers: {parts}. {fix}; nothing was written")
    ((agreed, count),) = seen.items()
    if agreed != in_force:
        raise ConfigError(
            f"{count} rows were summarised by {agreed[0]}/{agreed[1]} (prompt {agreed[2][:8]}) "
            f"but the policy in force names {in_force[0]}/{in_force[1]} "
            f"(prompt {in_force[2][:8]}). "
            f"{fix}; nothing was written"
        )
    return agreed


def _item(article: RawArticle, policy: ArchivePolicy) -> ArchiveItem:
    """One staging row as the archive carries it. The body is read nowhere in this function.

    `canonical_url` and `url_hash` are the bot's own (§2.4). A second canonicalisation would key
    one story two ways, and a cross-source duplicate would stop being recognisable as one.
    """
    assert article.event_type is not None and article.modality is not None  # is_summarised
    url = canonical_url(article.url)
    return ArchiveItem(
        source_id=article.source_id,
        url=url,
        url_hash=url_hash(url),
        title=article.title,
        summary=article.summary,
        event_type=article.event_type,
        entities=article.entities,
        assets=article.assets,
        magnitudes=article.magnitudes,
        modality=article.modality,
        published_at=article.published_at,
        observed_at=article.published_at + policy.lag,
    )
