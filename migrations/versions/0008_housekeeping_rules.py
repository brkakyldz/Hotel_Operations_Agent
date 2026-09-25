"""housekeeping policy v1

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_COLUMNS = (
    ("room_cleaning_from_local", sa.String(length=5)),
    ("room_cleaning_until_local", sa.String(length=5)),
    ("towels_per_stay", sa.Integer()),
    ("pillows_per_stay", sa.Integer()),
)


def upgrade() -> None:
    with op.batch_alter_table("hotel_policies", schema=None) as batch_op:
        for name, type_ in POLICY_COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))
    with op.batch_alter_table("housekeeping_tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("status_reason", sa.String(length=40), nullable=True))

    # Data step: an existing demo database gets the fixture's housekeeping policy; a fresh one
    # gets it from the seed. New values only, so the policy version is not bumped (a bump would
    # make pending checkout reviews stale for a change that does not affect them).
    op.get_bind().execute(
        sa.text(
            "UPDATE hotel_policies SET room_cleaning_from_local = '09:00', "
            "room_cleaning_until_local = '16:00', towels_per_stay = 8, pillows_per_stay = 4 "
            "WHERE hotel_id = 'hotel_demo'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("housekeeping_tasks", schema=None) as batch_op:
        batch_op.drop_column("status_reason")
    with op.batch_alter_table("hotel_policies", schema=None) as batch_op:
        for name, _ in reversed(POLICY_COLUMNS):
            batch_op.drop_column(name)
