"""Migrating a flat raw store to one file per publication month (spec §6.3.1).

The three stores were collected before sharding existed and hold 71 MB between them. This is an
**explicit command** rather than something `RawStore.open` does on the way past, for the reason
`maintenance` gives about every irreversible step: 71 MB of collected article text must not be
reorganised as a side effect of being read, and an operator should be able to choose the moment.

The flat file is **renamed, never deleted.** Collection took hours, and for CoinDesk it cannot be
repeated without a signed-in session — so the original stays beside the shards as
`<source>.jsonl.premigration`, and a migration that went wrong is undone by renaming it back.
Deleting it would make this the one irreversible act in the archive, which is a status it has not
earned.

It must also *move*: `RawStore.open` refuses while a flat file is present, because shards written
beside it would leave resume blind to every row it holds.

Failure semantics: a store that is already sharded reports nothing to do rather than raising — a
migration that is unsafe to re-run is one nobody can resume after an interruption. An unparseable
flat file raises `ConfigError` naming the line, exactly as every other read does, and writes
nothing: the rows are loaded and validated in full before the first shard is created.
"""

from __future__ import annotations

import json
from pathlib import Path

from decision_lab.archive.store import SUFFIX, RawArticle, RawStore
from tradebot.core.errors import ConfigError
from tradebot.core.logging import get_logger
from tradebot.core.schema import DomainModel

logger = get_logger(__name__)

#: What the flat file becomes. Kept beside the shards rather than removed.
KEPT_SUFFIX = ".premigration"


class ReshardReport(DomainModel):
    """What one migration did. `migrated` is zero when there was nothing flat to move."""

    migrated: int = 0
    shards: int = 0
    kept: str = ""


def reshard(directory: Path, *, source_id: str) -> ReshardReport:
    """Split `<source>.jsonl` into `<source>/YYYY-MM.jsonl`, keeping the original."""
    flat = directory / f"{source_id}{SUFFIX}"
    if not flat.exists():
        logger.info("archive already sharded", extra={"source": source_id})
        return ReshardReport()

    # Read and validate everything before writing anything: a half-migrated store whose flat file
    # has already moved is the one state from which neither layout is complete.
    rows = _read_flat(flat)
    target = directory / source_id
    target.mkdir(parents=True, exist_ok=True)

    grouped: dict[str, list[RawArticle]] = {}
    for article in rows:
        grouped.setdefault(RawStore.month_of(article), []).append(article)
    for month, shard_rows in grouped.items():
        (target / f"{month}{SUFFIX}").write_text(
            "".join(r.model_dump_json() + "\n" for r in shard_rows), encoding="utf-8"
        )

    kept = flat.with_suffix(f"{SUFFIX}{KEPT_SUFFIX}")
    flat.replace(kept)
    logger.info(
        "archive resharded",
        extra={
            "source": source_id,
            "migrated": len(rows),
            "shards": len(grouped),
            "kept": str(kept),
        },
    )
    return ReshardReport(migrated=len(rows), shards=len(grouped), kept=str(kept))


def _read_flat(path: Path) -> tuple[RawArticle, ...]:
    """The flat file's rows, refusing the whole file rather than skipping a bad line."""
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(RawArticle.model_validate(json.loads(line)))
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"{path}:{number} is not a readable raw article: {exc}") from exc
    return tuple(rows)
