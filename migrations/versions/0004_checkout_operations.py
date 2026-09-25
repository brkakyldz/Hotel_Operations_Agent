"""checkout policy, room-day plans and next arrivals

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKOUT_COLUMNS = (
    ("standard_checkout_local", sa.String(length=5)),
    ("auto_extension_until_local", sa.String(length=5)),
    ("reviewed_extension_until_local", sa.String(length=5)),
    ("turnaround_minutes", sa.Integer()),
    ("default_housekeeping_finish_local", sa.String(length=5)),
)


def upgrade() -> None:
    with op.batch_alter_table("hotel_policies", schema=None) as batch_op:
        for name, type_ in CHECKOUT_COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))

    op.create_table(
        "room_day_plans",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("hotel_id", sa.String(length=40), nullable=False),
        sa.Column("room_id", sa.String(length=40), nullable=False),
        sa.Column("local_date", sa.String(length=10), nullable=False),
        sa.Column("housekeeping_ready_after", sa.DateTime(), nullable=False),
        sa.Column("housekeeping_finish_by", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "housekeeping_finish_by > housekeeping_ready_after",
            name=op.f("ck_room_day_plans_window_order"),
        ),
        sa.ForeignKeyConstraint(
            ["hotel_id"], ["hotels.id"], name=op.f("fk_room_day_plans_hotel_id_hotels")
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["rooms.id"], name=op.f("fk_room_day_plans_room_id_rooms")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_room_day_plans")),
        sa.UniqueConstraint("room_id", "local_date", name="uq_room_day_plans_room_date"),
    )

    # Data step: an existing demo database gets this revision's fixture additions (the
    # internal, nonselectable next reservations); a fresh one gets them from the seed.
    # Times are naive UTC as stored by UTCDateTime (Europe/Istanbul is +03:00 on 2026-09-22).
    conn = op.get_bind()
    if not conn.execute(sa.text("SELECT 1 FROM hotels WHERE id = 'hotel_demo'")).first():
        return
    conn.execute(
        sa.text(
            "UPDATE hotel_policies SET standard_checkout_local = '12:00', "
            "auto_extension_until_local = '14:00', reviewed_extension_until_local = '16:00', "
            "turnaround_minutes = 60, default_housekeeping_finish_local = '17:30' "
            "WHERE hotel_id = 'hotel_demo'"
        )
    )
    arrivals = (
        ("G-901", "rsv_2001", "R-2001", "room_305", "2026-09-22 15:00:00", "2026-09-24 09:00:00"),
        ("G-902", "rsv_2002", "R-2002", "room_412", "2026-09-22 12:00:00", "2026-09-23 09:00:00"),
    )
    for guest_id, rsv_id, ref, room_id, check_in, checkout in arrivals:
        if not conn.execute(
            sa.text("SELECT 1 FROM rooms WHERE id = :room"), {"room": room_id}
        ).first():
            continue
        conn.execute(
            sa.text(
                "INSERT INTO guests (id, hotel_id, display_name, selectable) VALUES "
                "(:id, 'hotel_demo', 'Internal arrival (fictional, not selectable)', 0)"
            ),
            {"id": guest_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO reservations (id, reference, hotel_id, guest_id, room_id, status, "
                "scheduled_check_in, scheduled_checkout, actual_check_in, actual_checkout, "
                "version) VALUES (:id, :ref, 'hotel_demo', :guest, :room, 'confirmed', "
                ":check_in, :checkout, NULL, NULL, 1)"
            ),
            {
                "id": rsv_id,
                "ref": ref,
                "guest": guest_id,
                "room": room_id,
                "check_in": check_in,
                "checkout": checkout,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO room_day_plans (id, hotel_id, room_id, local_date, "
                "housekeeping_ready_after, housekeeping_finish_by, version) VALUES "
                "(:id, 'hotel_demo', :room, '2026-09-22', '2026-09-22 09:00:00', "
                "'2026-09-22 14:30:00', 1)"
            ),
            {"id": f"rdp_{room_id.removeprefix('room_')}_20260922", "room": room_id},
        )
    # New availability-affecting rows: bump the coarse operational version.
    conn.execute(
        sa.text(
            "UPDATE hotels SET operational_version = operational_version + 1 "
            "WHERE id = 'hotel_demo'"
        )
    )


def downgrade() -> None:
    op.drop_table("room_day_plans")
    with op.batch_alter_table("hotel_policies", schema=None) as batch_op:
        for name, _ in reversed(CHECKOUT_COLUMNS):
            batch_op.drop_column(name)
