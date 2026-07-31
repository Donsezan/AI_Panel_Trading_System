"""phase 3: normalized news items and their embeddings

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-26

Two tables rather than one, on purpose. `news_items` is the typed, point-in-time record the
ContextBuilder selects from; `news_vectors` implements the generic `VectorStore` seam, so
replacing the local embedding with a real model — or with Chroma — changes one table and leaves
the news rows alone.

Neither is a projection: they are *observations*, not a fold of our own events, so a projection
rebuild must not truncate them. A publisher takes an article down and it is gone; the row we
stored at the time is the only evidence of what the panel actually saw.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tradebot.persistence.schema  # custom column types render fully qualified

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_items",
        sa.Column("item_id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        # Always written, possibly as the empty string: an item with no summary still has a
        # summary field, and a NULL there would mean "unknown", which is a different fact.
        sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("published_at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.Column("observed_at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.PrimaryKeyConstraint("item_id"),
        sa.UniqueConstraint("url_hash"),
    )
    with op.batch_alter_table("news_items", schema=None) as batch_op:
        batch_op.create_index("ix_news_items_observed", ["observed_at"], unique=False)
        batch_op.create_index("ix_news_items_source", ["source_id"], unique=False)

    op.create_table(
        "news_vectors",
        sa.Column("doc_id", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("vector_json", sa.Text(), nullable=False),
        sa.Column("observed_at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.PrimaryKeyConstraint("doc_id"),
    )
    with op.batch_alter_table("news_vectors", schema=None) as batch_op:
        batch_op.create_index("ix_news_vectors_observed", ["observed_at"], unique=False)


def downgrade() -> None:
    """Drops observed news. Not reconstructable from the event log — re-fetching a feed returns
    what the publisher shows *now*, which is not what we saw then. A downgrade therefore loses
    the evidence of a past decision's news context, and only the snapshot digest survives."""
    with op.batch_alter_table("news_vectors", schema=None) as batch_op:
        batch_op.drop_index("ix_news_vectors_observed")
    op.drop_table("news_vectors")

    with op.batch_alter_table("news_items", schema=None) as batch_op:
        batch_op.drop_index("ix_news_items_source")
        batch_op.drop_index("ix_news_items_observed")
    op.drop_table("news_items")
