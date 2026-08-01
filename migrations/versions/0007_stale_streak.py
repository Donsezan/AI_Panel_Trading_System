"""phase 8: the market-data staleness streak, persisted beside the alert cursor

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-01

A run of cycles that refused their own market data is now an alert, counted the same way repeated
provider failure is (`ops/rules.py`). It is persisted for the same reason: a streak counted in
memory is a streak a restart forgives, and a process that restarts every time the feed dies would
never reach the third cycle that tells anyone.

`server_default="0"` matters — this table already has its row on any database that has alerted,
and a NULL there would read as a broken counter rather than as a fresh one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alert_cursor",
        sa.Column("stale_streak", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("alert_cursor", "stale_streak")
