"""phase 6: versioned configuration and per-cycle version pinning

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-30

Two changes, and they are one idea. Configuration stops being an argument passed to the
composition root and becomes versioned rows; a cycle then records *which* versions it ran on, so
a decision can be re-read against the limits that produced it rather than against today's.

`config_versions` is not a projection. A rebuild replays the log into the read model, and the log
refers to config versions by number — truncating the table those numbers resolve against would
erase the meaning of the log it was rebuilding from.

`cycles.config_versions_json` defaults to `{}` so cycles recorded before this revision stay
readable: an empty pin set is honest about a cycle that ran before pinning existed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tradebot.persistence.schema  # custom column types render fully qualified

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        # Integer rather than boolean, for the same reason as `live_arming.armed`: SQLite has no
        # native boolean and a dialect-dependent coercion is not wanted on a retirement flag.
        sa.Column("retired", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actor", sa.String(length=64), nullable=True, server_default=""),
        sa.Column("note", sa.Text(), nullable=True, server_default=""),
        sa.Column("created_at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.PrimaryKeyConstraint("kind", "config_id", "version"),
    )
    op.add_column(
        "cycles", sa.Column("config_versions_json", sa.Text(), nullable=True, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("cycles", "config_versions_json")
    op.drop_table("config_versions")
