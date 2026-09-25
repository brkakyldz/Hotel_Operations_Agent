"""Schema, fixture seed and SQLite mechanics."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from hotel_operations.fixtures import DEMO_NOW, seed_fixture
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import Base, Hotel, Reservation, business_tables


def test_tzdata_resolves_istanbul_without_host_database() -> None:
    assert ZoneInfo("Europe/Istanbul").utcoffset(DEMO_NOW.replace(tzinfo=None)) is not None
    assert DEMO_NOW.utcoffset().total_seconds() == 3 * 3600  # type: ignore[union-attr]


def test_migrations_create_exactly_the_orm_schema(db: Database) -> None:
    with db.read() as s:
        names = {r[0] for r in s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert set(Base.metadata.tables) <= names
    assert "alembic_version" in names


def test_seed_fixture_v1_values(db: Database) -> None:
    with db.read() as s:
        hotel = s.get(Hotel, "hotel_demo")
        assert hotel is not None
        assert hotel.display_name == "Demo Hotel (fictional)"
        assert hotel.demo_now == DEMO_NOW
        rows = {r.reference: r for r in s.scalars(select(Reservation))}
    tz = ZoneInfo("Europe/Istanbul")
    # Internal, nonselectable next arrivals for rooms 305 and 412.
    assert set(rows) == {"R-1042", "R-1088", "R-1101", "R-2001", "R-2002"}
    assert rows["R-2001"].room_id == "room_305" and rows["R-2002"].room_id == "room_412"
    assert (
        rows["R-2001"].scheduled_check_in.astimezone(ZoneInfo("Europe/Istanbul")).strftime("%H:%M")
        == "18:00"
    )
    assert rows["R-1042"].status == "checked_in"
    assert rows["R-1042"].scheduled_checkout.astimezone(tz).strftime("%Y-%m-%d %H:%M") == (
        "2026-09-22 12:00"
    )
    assert rows["R-1088"].actual_check_in.astimezone(tz).strftime("%H:%M") == "16:00"  # type: ignore[union-attr]
    assert rows["R-1101"].status == "confirmed"
    assert rows["R-1101"].actual_check_in is None


def test_seed_is_idempotent_and_never_resets(db: Database) -> None:
    with db.write() as s:
        s.get(Reservation, "rsv_1042").version = 7  # type: ignore[union-attr]
    with db.write() as s:
        assert seed_fixture(s) is False
    with db.read() as s:
        assert s.get(Reservation, "rsv_1042").version == 7  # type: ignore[union-attr]


def test_every_connection_sets_pragmas(db: Database) -> None:
    for _ in range(3):
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 500
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        with db.write() as s:
            s.execute(
                text(
                    "INSERT INTO guests (id, hotel_id, display_name, selectable) "
                    "VALUES ('G-X', 'no-such-hotel', 'X', 0)"
                )
            )


def test_second_immediate_writer_is_refused_busy(db_path: Path) -> None:
    """While one connection holds BEGIN IMMEDIATE, a second immediate begin fails busy.

    This is the property that makes check-then-write admission safe: two writers
    cannot both read the same state and both insert.
    """
    first = Database(db_path)
    second = Database(db_path)
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with first.write() as s:
            s.execute(text("SELECT count(*) FROM runs")).scalar()
            holding.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert holding.wait(5)
    started = time.monotonic()
    try:
        with pytest.raises(OperationalError, match="locked"):
            with second.write() as s:
                s.execute(text("SELECT count(*) FROM runs")).scalar()
        waited = time.monotonic() - started
        assert 0.3 <= waited < 3.0, waited  # the 500 ms busy timeout, not an instant pass
        # A plain deferred read is still allowed concurrently in WAL mode.
        with second.read() as s:
            assert s.execute(text("SELECT count(*) FROM runs")).scalar() == 0
    finally:
        release.set()
        t.join()
        first.dispose()
        second.dispose()


def test_business_tables_come_from_metadata() -> None:
    tables = business_tables()
    assert {"hotels", "guests", "rooms", "reservations"} <= set(tables)
    assert not {"demo_sessions", "runs", "audit_events"} & set(tables)
