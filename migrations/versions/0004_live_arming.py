"""phase 5: the live-arming row

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30

One row, four columns, and the reason it is a table rather than a config flag: the other live
preconditions (a required CLI mode, a typed phrase) are transient by design, and this one must
not be. A file or an env var left in place arms a machine after a reboot nobody authorised,
whereas a row lives in *this mode's* database — paper and live never share one — and shows up in
`tradebot risk status` next to the kill switch.

Not a projection: nothing replays into it. It records a human decision, so a projection rebuild
must leave it alone (PLAN §2.4).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import tradebot.persistence.schema  # custom column types render fully qualified

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_arming",
        sa.Column("scope", sa.String(length=32), nullable=False),
        # Integer rather than boolean: SQLite has no native boolean, and storing the truth of
        # "may this process trade real money" through a dialect-dependent coercion is not a place
        # for cleverness.
        sa.Column("armed", sa.Integer(), nullable=False, server_default="0"),
        # Nullable on purpose. NULL is "no cap was chosen", which live mode refuses to start on —
        # distinct from a cap of zero, which would be a deliberate stop.
        sa.Column("max_live_notional", tradebot.persistence.schema.DecimalText(), nullable=True),
        sa.Column("armed_by", sa.String(length=64), nullable=True, server_default=""),
        sa.Column("note", sa.Text(), nullable=True, server_default=""),
        sa.Column("updated_at", tradebot.persistence.schema.UtcText(), nullable=False),
        sa.PrimaryKeyConstraint("scope"),
    )


def downgrade() -> None:
    op.drop_table("live_arming")
