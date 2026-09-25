"""availability version per room; sessions without a wall-clock lifetime

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rooms", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("operational_version", sa.Integer(), nullable=False, server_default="1")
        )
    bind = op.get_bind()
    # Each room starts from the hotel's old value, so a review already waiting keeps the
    # snapshot it was decided on and does not turn stale because of this migration.
    bind.execute(
        sa.text(
            "UPDATE rooms SET operational_version = "
            "(SELECT operational_version FROM hotels WHERE hotels.id = rooms.hotel_id)"
        )
    )
    # SQLite drops a plain column in place; a batch copy would drop the parent table of
    # every foreign key.
    bind.execute(sa.text("ALTER TABLE hotels DROP COLUMN operational_version"))
    bind.execute(sa.text("ALTER TABLE demo_sessions DROP COLUMN expires_at"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "ALTER TABLE demo_sessions ADD COLUMN expires_at DATETIME NOT NULL "
            "DEFAULT '1970-01-01 00:00:00'"
        )
    )
    bind.execute(sa.text("UPDATE demo_sessions SET expires_at = datetime(created_at, '+24 hours')"))
    bind.execute(
        sa.text("ALTER TABLE hotels ADD COLUMN operational_version INTEGER NOT NULL DEFAULT 1")
    )
    bind.execute(
        sa.text(
            "UPDATE hotels SET operational_version = COALESCE("
            "(SELECT MAX(operational_version) FROM rooms WHERE rooms.hotel_id = hotels.id), 1)"
        )
    )
    bind.execute(sa.text("ALTER TABLE rooms DROP COLUMN operational_version"))
