"""phase 13 piece C: the notifications projection

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-22

The dashboard's bell reads a table, not the log: the alerts `ops/rules.py` already produces become
one row each, dismissed or not (spec §5.5). Nothing new decides what an operator should be told —
this is a read model over the rules that already existed.

A true projection, listed in `PROJECTION_TABLES`, so a rebuild replays `NOTIFICATION_RAISED` and
`ALERT_DISMISSED` back into it and lands on the same state. That is what lets dismissal be an
audited act rather than a column somebody wrote in place: the log can answer "who cleared the
reconciliation-mismatch notice, and when" (D6).

`alert_id` is the primary key and is deterministic — `"{event_seq}:{kind}"`, or `"summary:{day}"`
for the daily line — which is what makes recording idempotent. A retry folds onto the row that is
already there, and the projector inserts-or-ignores rather than upserting, so a re-record at 03:20
cannot resurrect a notice dismissed at 03:12.

`events.type` is a plain `String(48)` with no database-level enum, so the two new event types need
no migration of their own.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tradebot.persistence.schema  # custom column types render fully qualified

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("alert_id", sa.String(length=96), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("dismissed_at", tradebot.persistence.schema.UtcText(), nullable=True),
        sa.Column("dismissed_by", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("alert_id"),
    )
    op.create_index("ix_notifications_open", "notifications", ["dismissed_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_notifications_open", table_name="notifications")
    op.drop_table("notifications")
