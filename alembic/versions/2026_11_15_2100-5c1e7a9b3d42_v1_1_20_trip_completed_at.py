"""v1.1.20 — trips.completed_at

Settlement used to treat ``trips.updated_at`` as a trip's completion date.
Any edit after completion therefore moved a finished trip into the current
settlement period. The edits that did it include recording a sub's cost
(``record_subcontractor_cost``, v1.1.18), a fare correction, and adding notes.
``completed_at`` is now stamped once by ``core.update_trip_status`` when the
trip enters COMPLETED, and settlement reads it.

Backfill: existing COMPLETED trips get ``completed_at = updated_at``. That's
the best evidence there is, and it's exactly what settlement showed for them
before this change. Trips completed from here on carry the real timestamp.

Revision ID: 5c1e7a9b3d42
Revises: 47830a2466c9
Create Date: 2026-11-15 21:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5c1e7a9b3d42"
down_revision: Union[str, Sequence[str], None] = "47830a2466c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("trips", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))
    # TripStatus is stored by enum NAME (see the baseline: sa.Enum('REQUESTED', ...)).
    op.execute(
        sa.text(
            "UPDATE trips SET completed_at = updated_at "
            "WHERE status = 'COMPLETED' AND completed_at IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("trips", schema=None) as batch_op:
        batch_op.drop_column("completed_at")
