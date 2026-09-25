"""hotel updates outbox and who started a run

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
    "review_closed",
    "task_closed",
    "room_out_of_service",
    "review_at_risk",
    "checkout_now_possible",
)


def upgrade() -> None:
    op.create_table(
        "hotel_updates",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("hotel_id", sa.String(length=40), nullable=False),
        sa.Column("reservation_id", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("ref", sa.String(length=40), nullable=False),
        sa.Column("facts", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("skip_reason", sa.String(length=40), nullable=True),
        sa.Column("run_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("demo_time", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in KINDS) + ")",
            name=op.f("ck_hotel_updates_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'skipped')", name=op.f("ck_hotel_updates_status")
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"], ["hotels.id"], name=op.f("fk_hotel_updates_hotel_id_hotels")
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["reservations.id"],
            name=op.f("fk_hotel_updates_reservation_id_reservations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_hotel_updates")),
        sa.UniqueConstraint("reservation_id", "kind", "ref", name="uq_hotel_updates_once"),
    )
    with op.batch_alter_table("hotel_updates", schema=None) as batch_op:
        batch_op.create_index("ix_hotel_updates_status", ["status", "created_at"], unique=False)
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "initiated_by",
                sa.String(length=10),
                server_default=sa.text("'guest'"),
                nullable=False,
            )  # fmt: skip
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("initiated_by")
    with op.batch_alter_table("hotel_updates", schema=None) as batch_op:
        batch_op.drop_index("ix_hotel_updates_status")
    op.drop_table("hotel_updates")
