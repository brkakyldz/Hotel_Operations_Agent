"""The explicit demo reset CLI.

Every test works on isolated temporary files; none can reach the user's demo database.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from hotel_operations.clock import utcnow
from hotel_operations.reset import ResetRefused, main, reset_demo
from hotel_operations.services import sessions
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import DemoSession, Reservation, RunRecord
from tests.support.fingerprint import business_fingerprint


@pytest.fixture
def demo(template_db: Path, tmp_path: Path) -> tuple[Path, Path]:
    hotel = tmp_path / "demo" / "hotel.sqlite"
    hotel.parent.mkdir()
    shutil.copy(template_db, hotel)
    conversations = tmp_path / "demo" / "conversations.sqlite"
    sqlite3.connect(conversations).close()
    return hotel, conversations


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dirty(hotel: Path) -> None:
    """Make the demo unrepeatable: extend a checkout and open a session."""
    db = Database(hotel)
    with db.write() as s:
        rsv = s.get(Reservation, "rsv_1042")
        assert rsv is not None
        rsv.scheduled_checkout = rsv.scheduled_checkout + timedelta(hours=2)
        rsv.version += 1
        sessions.create_session(s, "G-001")
    db.dispose()


def test_refuses_without_confirmation(demo: tuple[Path, Path]) -> None:
    hotel, conversations = demo
    before = _digest(hotel)
    with pytest.raises(ResetRefused) as exc:
        reset_demo(hotel, conversations, confirm=False)
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    assert _digest(hotel) == before
    code = main(["--hotel-db", str(hotel), "--conversations-db", str(conversations)])
    assert code == 2 and _digest(hotel) == before


def test_restores_fixture_v1_in_both_databases(demo: tuple[Path, Path], template_db: Path) -> None:
    hotel, conversations = demo
    _dirty(hotel)
    fresh = Database(template_db)
    expected = business_fingerprint(fresh)
    fresh.dispose()
    code = main(["--confirm", "--hotel-db", str(hotel), "--conversations-db", str(conversations)])
    assert code == 0
    assert not conversations.exists()  # recreated empty by the app on next start
    db = Database(hotel)
    assert business_fingerprint(db) == expected
    with db.read() as s:
        assert (s.scalar(select(func.count()).select_from(DemoSession)) or 0) == 0
    db.dispose()
    assert not list(hotel.parent.glob("*.reset-backup"))


def test_refuses_while_a_run_is_active(demo: tuple[Path, Path]) -> None:
    hotel, conversations = demo
    db = Database(hotel)
    with db.write() as s:
        _, demo_session = sessions.create_session(s, "G-001")
        s.add(
            RunRecord(
                id="run_active",
                session_id=demo_session.id,
                client_request_id="00000000-0000-0000-0000-000000000001",
                input_hash="0" * 64,
                user_message="hi",
                status="running",
                conversation_key="conv",
                created_at=utcnow(),
            )
        )
    db.dispose()
    before = _digest(hotel)
    with pytest.raises(ResetRefused) as exc:
        reset_demo(hotel, conversations, confirm=True)
    assert exc.value.code == "ACTIVE_RUNS"
    assert _digest(hotel) == before


def test_refuses_while_the_database_is_held_open(demo: tuple[Path, Path]) -> None:
    hotel, conversations = demo
    app_like = Database(hotel)  # a running app keeps pooled connections open
    with app_like.read() as s:
        s.get(Reservation, "rsv_1042")
    try:
        with pytest.raises(ResetRefused) as exc:
            reset_demo(hotel, conversations, confirm=True)
        assert exc.value.code == "DATABASE_IN_USE"
        with app_like.read() as s:
            rsv = s.get(Reservation, "rsv_1042")
            assert rsv is not None and rsv.version == 1  # untouched
    finally:
        app_like.dispose()
    assert not list(hotel.parent.glob("*.reset-backup"))


def test_never_touches_a_database_it_was_not_pointed_at(
    demo: tuple[Path, Path], template_db: Path, tmp_path: Path
) -> None:
    hotel, conversations = demo
    other = tmp_path / "other" / "hotel.sqlite"
    other.parent.mkdir()
    shutil.copy(template_db, other)
    _dirty(other)
    other_before = _digest(other)
    reset_demo(hotel, conversations, confirm=True)
    assert _digest(other) == other_before


def test_refuses_a_file_that_is_not_a_demo_database(tmp_path: Path) -> None:
    stranger = tmp_path / "notes.sqlite"
    conn = sqlite3.connect(stranger)
    conn.execute("CREATE TABLE notes (x TEXT)")
    conn.commit()
    conn.close()
    before = _digest(stranger)
    with pytest.raises(ResetRefused) as exc:
        reset_demo(stranger, tmp_path / "conversations.sqlite", confirm=True)
    assert exc.value.code == "NOT_A_DEMO_DATABASE"
    assert _digest(stranger) == before


def test_builds_a_fresh_demo_when_none_exists(tmp_path: Path) -> None:
    hotel = tmp_path / "new" / "hotel.sqlite"
    result = reset_demo(hotel, tmp_path / "new" / "conversations.sqlite", confirm=True)
    assert result.fixture_version == "fixture-v1"
    db = Database(hotel)
    with db.read() as s:
        assert s.get(Reservation, "rsv_2001") is not None
    db.dispose()


def test_reset_invalidates_approvals(demo: tuple[Path, Path]) -> None:
    from hotel_operations.fixtures import DEMO_NOW
    from hotel_operations.services import approvals
    from hotel_operations.storage.models import Approval

    hotel, conversations = demo
    db = Database(hotel)
    with db.write() as s:
        approvals.create_or_reuse(
            s,
            hotel_id="hotel_demo",
            guest_id="G-001",
            reservation_id="rsv_1042",
            room_id="room_305",
            tz_name="Europe/Istanbul",
            requested=DEMO_NOW + timedelta(hours=5),
            guest_reason=None,
            current_versions={"reservation": 1},
            run_id="run_x",
            session_id="ses_x",
            demo_now=DEMO_NOW,
        )
    with db.read() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 1
    db.dispose()
    reset_demo(hotel, conversations, confirm=True)
    db = Database(hotel)
    with db.read() as s:
        assert s.scalar(select(func.count()).select_from(Approval)) == 0
    db.dispose()
