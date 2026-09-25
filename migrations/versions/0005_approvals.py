"""approvals and operator commands

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("hotel_id", sa.String(length=40), nullable=False),
        sa.Column("guest_id", sa.String(length=40), nullable=False),
        sa.Column("reservation_id", sa.String(length=40), nullable=False),
        sa.Column("room_id", sa.String(length=40), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("immutable_payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_versions", sa.JSON(), nullable=False),
        sa.Column("guest_reason", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_reason", sa.String(length=200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("originating_run_id", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("human_decision", sa.String(length=10), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("execution_receipt_id", sa.String(length=40), nullable=True),
        sa.CheckConstraint(
            "action_type IN ('late_checkout')", name=op.f("ck_approvals_action_type")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'executed', 'rejected', 'expired', 'stale')",
            name=op.f("ck_approvals_status"),
        ),
        sa.CheckConstraint(
            "human_decision IS NULL OR human_decision IN ('approve', 'reject')",
            name=op.f("ck_approvals_human_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["guest_id"], ["guests.id"], name=op.f("fk_approvals_guest_id_guests")
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"], ["hotels.id"], name=op.f("fk_approvals_hotel_id_hotels")
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name=op.f("fk_approvals_reservation_id_reservations"),
        ),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], name=op.f("fk_approvals_room_id_rooms")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.create_index(
            "ix_approvals_hotel_status", ["hotel_id", "status", "created_at"], unique=False
        )
        batch_op.create_index(
            "uq_approvals_one_pending_per_reservation",
            ["reservation_id"],
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
        )

    op.create_table(
        "operator_commands",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("client_request_id", sa.String(length=36), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=False),
        sa.Column("approval_id", sa.String(length=40), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator_commands")),
        sa.UniqueConstraint(
            "operator_id", "client_request_id", name="uq_operator_commands_operator_request"
        ),
    )


def downgrade() -> None:
    op.drop_table("operator_commands")
    with op.batch_alter_table("approvals", schema=None) as batch_op:
        batch_op.drop_index("uq_approvals_one_pending_per_reservation")
        batch_op.drop_index("ix_approvals_hotel_status")
    op.drop_table("approvals")
