"""Reservation read service: a fresh, scoped read of the bound reservation."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.services.sessions import SessionBinding, display_status, revalidate_binding
from hotel_operations.storage.models import Guest, Hotel, Room


def read_my_reservation(session: Session, binding: SessionBinding) -> dict[str, Any]:
    """Return the bound reservation's current safe projection plus observation metadata."""
    reservation = revalidate_binding(session, binding)
    guest = session.get(Guest, binding.guest_id)
    room = session.get(Room, binding.room_id)
    hotel = session.get(Hotel, binding.hotel_id)
    if guest is None or room is None or hotel is None:
        raise errors.data_unavailable()
    tz = hotel.timezone

    def local(value: Any) -> str | None:
        return None if value is None else iso(to_local(value, tz))

    data = {
        "reservation_reference": reservation.reference,
        "guest_name": guest.display_name,
        "room_number": room.number,
        "status": reservation.status,
        "display_status": display_status(reservation, hotel),
        "scheduled_check_in": local(reservation.scheduled_check_in),
        "scheduled_checkout": local(reservation.scheduled_checkout),
        "actual_check_in": local(reservation.actual_check_in),
        "actual_checkout": local(reservation.actual_checkout),
        "hotel_timezone": tz,
        # Re-readable after a hotel update: whether the guest's room is in service.
        "room_in_service": not room.out_of_service,
        "reservation_version": reservation.version,
    }
    meta = {
        "observed_at": iso(utcnow()),
        "demo_time": iso(to_local(hotel.demo_now, tz)),
        "versions": {"reservation": reservation.version, "room": room.version},
    }
    return {"data": data, "meta": meta, "entity": ("reservation", reservation.id)}
