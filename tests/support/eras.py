"""Seed a database the way an earlier build did, to test that migrations upgrade it."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import Session

from hotel_operations import fixtures
from hotel_operations.storage.db import Database
from hotel_operations.storage.migrate import upgrade_to_head

# Columns an earlier schema requires that the current fixture no longer writes, with the
# value that build wrote (the operational version has since moved from the hotel to its rooms).
ERA_VALUES: dict[tuple[str, str], Any] = {("hotels", "operational_version"): 1}


def _current_fixture_rows(tables: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Fixture v1's first-revision rows, written by today's seed into a scratch database at head."""
    saved = (
        fixtures.ensure_policy,
        fixtures.ensure_info_topics,
        fixtures.NEXT_ARRIVALS,
        fixtures.ROOM_DAY_PLANS,
    )
    fixtures.ensure_policy = lambda session: False  # type: ignore[assignment]
    fixtures.ensure_info_topics = lambda session: False  # type: ignore[assignment]
    fixtures.NEXT_ARRIVALS, fixtures.ROOM_DAY_PLANS = (), ()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "head.sqlite"
        upgrade_to_head(path)
        db = Database(path)
        try:
            with db.write() as s:
                fixtures.seed_fixture(s)
            head = MetaData()
            with db.engine.connect() as conn:
                head.reflect(conn, only=tables)
                return {
                    name: [dict(row._mapping) for row in conn.execute(head.tables[name].select())]
                    for name in tables
                }
        finally:
            (
                fixtures.ensure_policy,
                fixtures.ensure_info_topics,
                fixtures.NEXT_ARRIVALS,
                fixtures.ROOM_DAY_PLANS,
            ) = saved  # type: ignore[assignment]
            db.dispose()


def seed_core_schema_era(session: Session) -> None:
    """Seed only the first revision's tables (no policy, info topics, next arrivals or
    room-day plans), in the columns the database actually has, as that build would have."""
    era = MetaData()
    era.reflect(session.connection(), only=["hotels", "guests", "rooms", "reservations"])
    rows = _current_fixture_rows([t.name for t in era.sorted_tables])
    for table in era.sorted_tables:
        for row in rows[table.name]:
            values = {c.name: row[c.name] for c in table.columns if c.name in row}
            for column in table.columns:
                if column.name not in values:
                    values[column.name] = ERA_VALUES[(table.name, column.name)]
            session.execute(table.insert().values(**values))
    session.flush()
