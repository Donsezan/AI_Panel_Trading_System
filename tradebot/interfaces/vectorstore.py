"""Embedding store, used for news dedup and historical-context retrieval.

Failure semantics: the store is an *optimization*, not a dependency of correctness. If it is
unavailable, dedup degrades to URL-hash matching and retrieval returns nothing; the cycle
continues with a snapshot that records the reduced coverage. It must never block a trade
decision, and it must never stall the event loop — embedding is CPU-bound and runs in a thread
executor (REVIEW C8).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from tradebot.core.schema import DomainModel, Money, UtcDatetime


class StoredDocument(DomainModel):
    doc_id: str
    text: str
    metadata: dict[str, str] = Field(default_factory=dict)
    observed_at: UtcDatetime


class SimilarDocument(DomainModel):
    document: StoredDocument
    similarity: Money


@runtime_checkable
class VectorStore(Protocol):
    """Implementations: ChromaDB (v1), sqlite-vec (swap candidate)."""

    store_id: str

    async def add(self, documents: tuple[StoredDocument, ...]) -> None: ...

    async def query(
        self, text: str, limit: int, observed_before: UtcDatetime | None = None
    ) -> tuple[SimilarDocument, ...]:
        """Nearest neighbours, optionally restricted to what was known before a moment.

        `observed_before` is the point-in-time guard: in replay it is what stops tomorrow's
        news being retrieved into yesterday's decision (DESIGN §6.4).
        """
        ...
