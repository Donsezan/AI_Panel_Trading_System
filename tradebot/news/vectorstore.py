"""`VectorStore` over SQLite, using the deterministic local embedding.

Honest about what it is: a **linear scan** with cosine similarity computed in Python, bounded by
a recency window. At the volumes a research bot ingests — a few thousand headlines — that is
sub-millisecond, and it buys exactness (`Decimal` weights, no float round-trip through storage)
and zero new infrastructure. When the corpus outgrows it, the seam is already here: `sqlite-vec`
or Chroma replaces this class and nothing else changes (DESIGN §6.4).

**Point-in-time is enforced in SQL, not in Python.** `observed_before` becomes a `WHERE` clause,
so a replayed query cannot see a document that arrived later even if the caller forgets to
filter. That is the look-ahead guard, and it belongs where it cannot be skipped.

Embedding is CPU-bound, so it runs in a thread rather than on the trading event loop — the loops
share one loop and must never stall behind text processing (DESIGN §6.4, REVIEW C8).

Failure semantics: the store is an *optimization*, never a correctness dependency. Its absence
degrades dedup to URL-hash matching and retrieval to nothing; it must never block a decision.
Writes go through the single writer, so they serialize with every other write (PLAN §2.6).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Connection, Engine, select

from tradebot.core.clock import Clock
from tradebot.core.schema import canonical_json
from tradebot.interfaces.vectorstore import SimilarDocument, StoredDocument
from tradebot.news.embedding import Vector, dumps, embed, loads, similarity
from tradebot.persistence.database import SingleWriter
from tradebot.persistence.schema import news_vectors, upsert

#: How far back a similarity scan looks. Dedup only needs the recent past — a story republished
#: a month later is news again — and the window is what keeps the scan bounded.
DEFAULT_LOOKBACK = timedelta(days=7)

#: Hard cap on rows pulled into one scan, so a busy corpus cannot turn a query into a stall.
DEFAULT_SCAN_LIMIT = 5_000


class SqliteVectorStore:
    """Embedding store for news dedup and historical-context retrieval."""

    store_id = "sqlite"

    def __init__(
        self,
        engine: Engine,
        writer: SingleWriter,
        clock: Clock,
        *,
        lookback: timedelta = DEFAULT_LOOKBACK,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
    ) -> None:
        self._engine = engine
        self._writer = writer
        self._clock = clock
        self._lookback = lookback
        self._scan_limit = scan_limit

    async def add(
        self, documents: tuple[StoredDocument, ...], vectors: tuple[Vector, ...] | None = None
    ) -> None:
        """Embed and store. Re-adding a `doc_id` overwrites it rather than duplicating it.

        `vectors` lets a caller that has already embedded these documents — the dedup pass, which
        must embed before it can decide — hand them over instead of paying for it twice.
        """
        if not documents:
            return
        vectors = vectors or await self.embed_many(tuple(document.text for document in documents))
        rows = [
            {
                "doc_id": document.doc_id,
                "text": document.text,
                "metadata_json": canonical_json(document.metadata),
                "vector_json": dumps(vector),
                "observed_at": document.observed_at,
            }
            for document, vector in zip(documents, vectors, strict=True)
        ]

        def write(connection: Connection) -> None:
            for row in rows:
                upsert(connection, news_vectors, row, ["doc_id"])

        await self._writer.run(write)

    async def query(
        self, text: str, limit: int, observed_before: datetime | None = None
    ) -> tuple[SimilarDocument, ...]:
        """Nearest neighbours to `text`, restricted to what was known before a moment."""
        vector = (await self.embed_many((text,)))[0]
        return await self.nearest(vector, limit, observed_before)

    async def nearest(
        self, vector: Vector, limit: int, observed_before: datetime | None = None
    ) -> tuple[SimilarDocument, ...]:
        """Nearest neighbours to an already-computed vector."""
        return (await self.nearest_many((vector,), limit, observed_before))[0]

    async def nearest_many(
        self,
        vectors: tuple[Vector, ...],
        limit: int,
        observed_before: datetime | None = None,
    ) -> tuple[tuple[SimilarDocument, ...], ...]:
        """Neighbours for several vectors against **one** scan of the corpus.

        The dedup pass asks about every candidate in a batch. Scanning once and scoring many is
        the difference between one bounded query and fifty.
        """
        if not vectors:
            return ()
        rows = await asyncio.to_thread(self._candidates, observed_before)
        return tuple(_rank(vector, rows, limit) for vector in vectors)

    async def embed_many(self, texts: tuple[str, ...]) -> tuple[dict[int, Decimal], ...]:
        """Embed a batch off the event loop. CPU-bound work never runs on the trading loop."""
        return await asyncio.to_thread(lambda: tuple(embed(text) for text in texts))

    def _candidates(self, observed_before: datetime | None) -> list[tuple[StoredDocument, Vector]]:
        cutoff = observed_before or self._clock.now()
        query = (
            select(news_vectors)
            .where(
                news_vectors.c.observed_at <= cutoff,
                news_vectors.c.observed_at >= cutoff - self._lookback,
            )
            .order_by(news_vectors.c.observed_at.desc())
            .limit(self._scan_limit)
        )
        with self._engine.connect() as connection:
            return [
                (
                    StoredDocument(
                        doc_id=row.doc_id,
                        text=row.text,
                        metadata=json.loads(row.metadata_json or "{}"),
                        observed_at=row.observed_at,
                    ),
                    loads(row.vector_json),
                )
                for row in connection.execute(query)
            ]


def _rank(
    vector: Vector, rows: list[tuple[StoredDocument, Vector]], limit: int
) -> tuple[SimilarDocument, ...]:
    """Top-`limit` matches for one vector. `doc_id` breaks ties so the order is deterministic."""
    if not vector or limit < 1:
        return ()
    scored = [
        SimilarDocument(document=document, similarity=score)
        for document, stored in rows
        if (score := similarity(vector, stored)) > Decimal(0)
    ]
    scored.sort(key=lambda found: (found.similarity, found.document.doc_id), reverse=True)
    return tuple(scored[:limit])
