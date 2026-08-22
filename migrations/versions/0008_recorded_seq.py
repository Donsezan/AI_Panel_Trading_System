"""phase 13 piece C: recording's own cursor, beside delivery's

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-22

The dispatcher now tails the log whether or not a sink is configured: on a machine with no
webhook — the sim and paper case — the rules never evaluated at all, so anything fed by them was
permanently empty (spec §5.1). `enabled` comes to gate delivery only.

Recording and delivering fail differently, so they cannot share one position. `last_seq` keeps
its meaning and still advances only after a sink has taken the alert; `recorded_seq` advances
once the notification has been appended, so a dead webhook stalls delivery without withholding
what the dashboard could already show.

The data step is the part autogenerate cannot see: an existing row must start at `last_seq`, not
at 0. Defaulting to 0 would re-record every alert the log has ever justified on the first poll
after the upgrade — a bell that opens full of resolved incidents is a bell an operator learns to
ignore, which is ADR 0019's whole reason for anchoring a fresh tail at the log's end.

`last_seq` itself changes meaning here — it now indexes `NOTIFICATION_RAISED` rather than the
source events — and needs no data migration for it: no event of that type exists below the
upgrade point, so the first read after `last_seq` returns only new rows whatever it holds.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "alert_cursor",
        sa.Column("recorded_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    # Where alerting has already run, recording starts where delivery got to.
    op.execute(sa.text("UPDATE alert_cursor SET recorded_seq = last_seq"))


def downgrade() -> None:
    op.drop_column("alert_cursor", "recorded_seq")
