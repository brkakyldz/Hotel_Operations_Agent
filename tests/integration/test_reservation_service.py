"""Scoped reservation reads against real migrated SQLite files."""

from __future__ import annotations

from datetime import timedelta

import pytest

from hotel_operations import errors
from hotel_operations.services import sessions
from hotel_operations.services.reservations import read_my_reservation
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import Reservation, Room


def _binding(db: Database, guest_id: str) -> sessions.SessionBinding:
    with db.write() as s:
        _, demo = sessions.create_session(s, guest_id)
        return sessions.binding_of(demo)


@pytest.mark.parametrize(
    ("guest", "ref", "room", "status", "checkout"),
    [
        ("G-001", "R-1042", "305", "checked_in", "2026-09-22T12:00:00+03:00"),
        ("G-002", "R-1088", "412", "checked_in", "2026-09-22T12:00:00+03:00"),
        ("G-003", "R-1101", "218", "confirmed", "2026-09-24T12:00:00+03:00"),
    ],
)
def test_each_fixture_reads_its_own_reservation(
    db: Database, guest: str, ref: str, room: str, status: str, checkout: str
) -> None:
    binding = _binding(db, guest)
    with db.read() as s:
        out = read_my_reservation(s, binding)
    data = out["data"]
    assert data["reservation_reference"] == ref
    assert data["room_number"] == room
    assert data["status"] == status
    assert data["scheduled_checkout"] == checkout
    assert data["hotel_timezone"] == "Europe/Istanbul"
    assert out["meta"]["demo_time"] == "2026-09-22T10:00:00+03:00"
    assert out["meta"]["versions"]["reservation"] == 1


def test_arriving_guest_is_labelled_and_readable(db: Database) -> None:
    binding = _binding(db, "G-003")
    with db.read() as s:
        data = read_my_reservation(s, binding)["data"]
    assert data["display_status"] == "Arriving today"
    assert data["actual_check_in"] is None


def test_read_is_fresh_after_a_controlled_update(db: Database) -> None:
    binding = _binding(db, "G-001")
    with db.write() as s:
        r = s.get(Reservation, "rsv_1042")
        assert r is not None
        r.scheduled_checkout = r.scheduled_checkout + timedelta(hours=2)
        r.version += 1
    with db.read() as s:
        data = read_my_reservation(s, binding)["data"]
    assert data["scheduled_checkout"] == "2026-09-22T14:00:00+03:00"
    assert data["reservation_version"] == 2


def test_room_reassignment_makes_scope_stale_not_retargeted(db: Database) -> None:
    binding = _binding(db, "G-001")
    with db.write() as s:
        s.add(
            Room(
                id="room_999",
                hotel_id="hotel_demo",
                number="999",
                housekeeping_state="clean",
                out_of_service=False,
                version=1,
            )
        )
        s.flush()
        s.get(Reservation, "rsv_1042").room_id = "room_999"  # type: ignore[union-attr]
    with db.read() as s, pytest.raises(errors.DomainError) as exc:
        read_my_reservation(s, binding)
    assert exc.value.code == "SESSION_SCOPE_STALE"


def test_missing_reservation_is_safe_error(db: Database) -> None:
    binding = _binding(db, "G-001")
    ghost = sessions.SessionBinding(**{**binding.__dict__, "reservation_id": "rsv_missing"})
    with db.read() as s, pytest.raises(errors.DomainError) as exc:
        read_my_reservation(s, ghost)
    assert exc.value.code == "RESERVATION_NOT_FOUND"
