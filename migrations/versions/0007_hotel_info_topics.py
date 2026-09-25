"""guest information topics

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-25
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from hotel_operations.hotel_info_v1 import HOTEL_INFO_V1

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hotel_info_topics",
        sa.Column("hotel_id", sa.String(length=40), nullable=False),
        sa.Column("topic", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["hotel_id"], ["hotels.id"], name=op.f("fk_hotel_info_topics_hotel_id_hotels")
        ),
        sa.PrimaryKeyConstraint("hotel_id", "topic", name=op.f("pk_hotel_info_topics")),
    )

    # Data step: an existing demo database gets the frozen v1 content; a fresh one gets it
    # from the seed. Nothing else changes, so no policy or operational version is bumped.
    conn = op.get_bind()
    if not conn.execute(sa.text("SELECT 1 FROM hotels WHERE id = 'hotel_demo'")).first():
        return
    for topic, content in HOTEL_INFO_V1.items():
        conn.execute(
            sa.text(
                "INSERT INTO hotel_info_topics (hotel_id, topic, version, content) "
                "VALUES ('hotel_demo', :topic, 1, :content)"
            ),
            {"topic": topic, "content": json.dumps(content)},
        )


def downgrade() -> None:
    op.drop_table("hotel_info_topics")
