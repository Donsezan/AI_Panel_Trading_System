"""The §6.6 archive that travels: header line, item lines, and no body anywhere (spec §6.4).

Two stores, two rules. `store.py` is staging — raw articles as crawled, bodies included,
gitignored, never distributed, and never an input to `archive_digest`. This is the archive a
corpus is built against, and it keeps §6.4's rule structurally: `ArchiveItem` has no `body` field,
so there is nowhere for one to be written. `DomainModel` forbids extra keys, so an upstream step
that starts carrying a body is refused here rather than quietly dropped.

One file per source rather than month shards, deliberately, and the reasoning is the inverse of
`archive reshard`'s. That migration happened because `RawStore.read_all` was re-parsed on every
backfill batch and every `held` check of a multi-hour crawl. This file is read **once per corpus
build** and indexed in memory by `ArchiveNewsFeed` — 11 894 bodyless rows is roughly 7 MB — so
re-parsing never happens, and §6.6's declared format keeps the header on line 1 where the spec
puts it.

`archive_digest` covers the source, the capture mode, the `observed_at` policy, the summariser's
model and prompt digest, the news window, the cluster floor, and the row set. It feeds `corpus_id`
(§5.4), so re-summarising with a different model — or reading the same rows through a different
window or floor — yields a different corpus rather than silently mixing two experiments. It
deliberately ignores `captured_at`: when an archive was written is provenance, what it contains is
identity, and a digest that moved on a clock read would renumber the corpus of an archive whose
rows are byte-identical.

Failure semantics: the archive is a file. An unreadable line, a missing header, or a header that
is not line 1 raises `ConfigError` naming the file and the line. Nothing is skipped — an archive
that silently dropped rows would under-report coverage in a way no later step could detect.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self, TypeVar

from decision_lab.archive.fields import EventType, Modality
from tradebot.core.errors import ConfigError
from tradebot.core.schema import DomainModel, Money, UtcDatetime, canonical_json

#: Beside the dataset, in its own directory, so the travelling archive is never confused with the
#: gitignored staging store `RAW_DIR` holds.
ARCHIVE_DIR: Final = "news"

#: §6.6's filename. One per source directory.
ARCHIVE_FILE: Final = "news.jsonl"

_HEADER: Final = "header"
_ITEM: Final = "item"

#: Module-level rather than PEP 695 (`def f[T]`): the project targets Python 3.11.
T = TypeVar("T", bound=DomainModel)


class ArchiveItem(DomainModel):
    """One archived article as a replay reads it. No body, by construction (§6.4).

    `summary` is §6.5's output and is what reaches `NewsItem.excerpt` and therefore the panel's
    280-character slot. The four extracted fields beside it are not decoration: `event_type`,
    `entities` and `assets` are the §6.8.2.1 cluster key, and `modality` is what stops a
    correction being collapsed behind the claim it corrects.

    `observed_at` is derived (§6.7) and the header declares how. It is the **only** field a
    point-in-time filter may use: filtering on `published_at` would let an item we did not have at
    the time into the decision, which is the look-ahead bug DESIGN [L12] exists to prevent.
    """

    source_id: str
    url: str
    url_hash: str
    title: str
    summary: str
    event_type: EventType
    entities: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()
    magnitudes: tuple[str, ...] = ()
    modality: Modality
    published_at: UtcDatetime
    observed_at: UtcDatetime


class ArchiveHeader(DomainModel):
    """§6.6's provenance line. Everything a reader needs to know how to read the rows below it.

    `archive_digest` is stored here as well as computed, so a reader can state the identity of the
    archive it loaded without recomputing it over every row — and `PublishedArchive.read` checks
    the two agree, which is how a hand-edited archive is caught.
    """

    source: str
    capture: str
    captured_at: UtcDatetime
    observed_at_policy: str
    summarizer_provider: str
    summarizer_model: str
    prompt_digest: str
    news_window_seconds: int
    cluster_floor: Money
    rows: int
    archive_digest: str = ""


def archive_digest(header: ArchiveHeader, items: tuple[ArchiveItem, ...]) -> str:
    """§6.6. Everything that decides what a replay reads, and nothing about when it was written."""
    payload = {
        "source": header.source,
        "capture": header.capture,
        "observed_at_policy": header.observed_at_policy,
        "summarizer_provider": header.summarizer_provider,
        "summarizer_model": header.summarizer_model,
        "prompt_digest": header.prompt_digest,
        "news_window_seconds": header.news_window_seconds,
        "cluster_floor": str(header.cluster_floor),
        "items": [item.model_dump(mode="json") for item in items],
    }
    return hashlib.blake2s(canonical_json(payload).encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True, slots=True)
class PublishedArchive:
    """One source's travelling archive, in memory."""

    header: ArchiveHeader
    items: tuple[ArchiveItem, ...]

    @staticmethod
    def write(path: Path, header: ArchiveHeader, items: tuple[ArchiveItem, ...]) -> None:
        """Write the header line and the item lines, stamping the digest over the rows given."""
        stamped = header.model_copy(
            update={"rows": len(items), "archive_digest": archive_digest(header, items)}
        )
        lines = [json.dumps({"kind": _HEADER} | stamped.model_dump(mode="json"))]
        lines += [json.dumps({"kind": _ITEM} | item.model_dump(mode="json")) for item in items]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Self:
        """Parse the whole file. Refuses a missing header, a bad line or a disagreeing digest."""
        if not path.is_file():
            raise ConfigError(
                f"no published news archive at {path}. Run `archive publish --data <dir> "
                "--source <id>` after `archive summarise`"
            )
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise ConfigError(f"{path} is empty; it carries neither a header nor any rows")

        head = _decode(path, 1, lines[0])
        if head.pop("kind", None) != _HEADER:
            raise ConfigError(
                f"{path}:1 is not a §6.6 header. The header is line 1 and declares the "
                "`observed_at` policy; without it the rows below cannot be read point-in-time"
            )
        header = _validate(path, 1, ArchiveHeader, head)

        items = []
        for number, line in enumerate(lines[1:], 2):
            body = _decode(path, number, line)
            if body.pop("kind", None) != _ITEM:
                raise ConfigError(f"{path}:{number} is not an item row")
            items.append(_validate(path, number, ArchiveItem, body))
        rows = tuple(items)

        # An absent digest is not "nothing to check": it feeds corpus_id, where empty means the
        # news-blind identity, so a stripped field would pass a news-fed build off as blind.
        if not header.archive_digest:
            raise ConfigError(
                f"{path} carries no archive_digest. It was not written by `archive publish`, or "
                "the field was removed; re-publish it. An empty digest is the news-blind "
                "corpus identity (§6.6)"
            )
        recomputed = archive_digest(header, rows)
        if header.archive_digest != recomputed:
            raise ConfigError(
                f"{path} declares archive_digest {header.archive_digest} but its rows hash to "
                f"{recomputed}. The file has been edited since it was published; re-publish it "
                "rather than trusting either number — the digest feeds corpus_id (§6.6)"
            )
        return cls(header=header, items=rows)


def _decode(path: Path, number: int, line: str) -> dict[str, Any]:
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name}:{number} is not readable JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ConfigError(f"{path.name}:{number} is not a JSON object")
    return decoded


def _validate(path: Path, number: int, model: type[T], body: dict[str, Any]) -> T:
    try:
        return model.model_validate(body)
    except ValueError as exc:
        raise ConfigError(
            f"{path.name}:{number} is not a readable {model.__name__}: {exc}"
        ) from exc
