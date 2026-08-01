"""phase 7 pass 2: the ops alerting cursor

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-01

Alerting is a log tail, not a hook in `EventStore.append` (ADR 0019): notification must never sit
on the money path, where a slow webhook would delay an order. The cursor is what makes that tail
safe across a restart — it is advanced only after a batch has actually been delivered, so the
guarantee is at-least-once. A duplicate kill-switch alert is an annoyance; a missing one is the
failure this table exists to prevent.

Not a projection. A rebuild replays the log into the read model, and truncating this row would
re-deliver every alert the log has ever justified.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tradebot.persistence.schema  # custom column types render fully qualified

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_cursor",
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_summary_day", sa.String(length=10), nullable=True, server_default=""),
        sa.Column("degraded_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.PrimaryKeyConstraint("scope"),
    )


def downgrade() -> None:
    op.drop_table("alert_cursor")
