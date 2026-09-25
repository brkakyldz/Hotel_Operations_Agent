"""early check-in policy columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_IN_COLUMNS = (
    ("standard_check_in_local", sa.String(length=5)),
    ("early_check_in_from_local", sa.String(length=5)),
)


def upgrade() -> None:
    with op.batch_alter_table("hotel_policies", schema=None) as batch_op:
        for name, type_ in CHECK_IN_COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))

    # Data step: an existing demo database gets the fixture's early check-in policy; a fresh
    # one gets it from the seed. New values only, so the policy version is not bumped (a bump
    # would make pending checkout reviews stale for a change that does not affect them).
    op.get_bind().execute(
        sa.text(
            "UPDATE hotel_policies SET standard_check_in_local = '15:00', "
            "early_check_in_from_local = '12:00' WHERE hotel_id = 'hotel_demo'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("hotel_policies", schema=None) as batch_op:
        for name, _ in reversed(CHECK_IN_COLUMNS):
            batch_op.drop_column(name)
