"""maintenance tasks

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_tasks",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("hotel_id", sa.String(length=40), nullable=False),
        sa.Column("guest_id", sa.String(length=40), nullable=False),
        sa.Column("reservation_id", sa.String(length=40), nullable=False),
        sa.Column("room_id", sa.String(length=40), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("originating_run_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "category IN ('hvac', 'plumbing', 'electrical', 'other')",
            name=op.f("ck_maintenance_tasks_category"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'cancelled')",
            name=op.f("ck_maintenance_tasks_status"),
        ),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 500",
            name=op.f("ck_maintenance_tasks_description"),
        ),
        sa.ForeignKeyConstraint(
            ["guest_id"], ["guests.id"], name=op.f("fk_maintenance_tasks_guest_id_guests")
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"], ["hotels.id"], name=op.f("fk_maintenance_tasks_hotel_id_hotels")
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name=op.f("fk_maintenance_tasks_reservation_id_reservations"),
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name=op.f("fk_maintenance_tasks_room_id_rooms")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_tasks")),
    )
    with op.batch_alter_table("maintenance_tasks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_maintenance_tasks_reservation", ["reservation_id", "created_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("maintenance_tasks", schema=None) as batch_op:
        batch_op.drop_index("ix_maintenance_tasks_reservation")
    op.drop_table("maintenance_tasks")
