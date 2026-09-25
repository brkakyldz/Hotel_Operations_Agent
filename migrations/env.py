"""Alembic environment for hotel.sqlite (business/session/run/audit store)."""

from __future__ import annotations

from pathlib import Path

from alembic import context

from hotel_operations.config import get_settings
from hotel_operations.storage.db import create_hotel_engine
from hotel_operations.storage.models import Base

config = context.config
target_metadata = Base.metadata


def _db_path() -> Path:
    override = context.get_x_argument(as_dictionary=True).get("db")
    return Path(override) if override else get_settings().hotel_db_path


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        context.configure(
            connection=connectable, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_hotel_engine(_db_path())
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline SQL generation is not supported; run against a database file.")
run_migrations_online()
