"""Demo selector sessions: immutable server-owned guest scope behind a capability.

Selecting a fictional guest is an explicit demo capability, not identity
verification. The backend resolves the hotel/guest/reservation/room binding
itself, stores only the SHA-256 of a random 256-bit token and returns the token
once.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import to_local, utcnow
from hotel_operations.ids import new_id
from hotel_operations.services import audit
from hotel_operations.storage.models import DemoSession, Guest, Hotel, Reservation, Room

ACTIVE_RESERVATION_STATUSES = ("confirmed", "checked_in")


class ReservationScope(Protocol):
    """The trusted hotel/guest/reservation/room identity a service acts within."""

    @property
    def hotel_id(self) -> str: ...
    @property
    def guest_id(self) -> str: ...
    @property
    def reservation_id(self) -> str: ...
    @property
    def room_id(self) -> str: ...


@dataclass(frozen=True)
class SessionBinding:
    """Trusted, immutable scope resolved from a session capability."""

    session_id: str
    hotel_id: str
    guest_id: str
    reservation_id: str
    room_id: str
    conversation_key: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_STATUS_LABELS = {
    "checked_in": "Checked in",
    "checked_out": "Checked out",
    "cancelled": "Cancelled",
}


def _active_reservation(session: Session, guest: Guest) -> tuple[Reservation, Room] | None:
    row = session.execute(
        select(Reservation, Room)
        .join(Room, Room.id == Reservation.room_id)
        .where(
            Reservation.guest_id == guest.id,
            Reservation.hotel_id == guest.hotel_id,
            Reservation.status.in_(ACTIVE_RESERVATION_STATUSES),
        )
        .order_by(Reservation.scheduled_check_in)
    ).first()
    return None if row is None else (row[0], row[1])


def display_status(reservation: Reservation, hotel: Hotel) -> str:
    """Guest-facing status label; a confirmed arrival on the demo day is "Arriving today"."""
    if reservation.status == "confirmed":
        arrival_day = to_local(reservation.scheduled_check_in, hotel.timezone).date()
        today = to_local(hotel.demo_now, hotel.timezone).date()
        return "Arriving today" if arrival_day == today else "Confirmed (not yet arrived)"
    return _STATUS_LABELS[reservation.status]


def list_selectable_guests(session: Session) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for guest in session.scalars(
        select(Guest).where(Guest.selectable.is_(True)).order_by(Guest.id)
    ):
        found = _active_reservation(session, guest)
        if found is None:
            continue
        reservation, room = found
        hotel = session.get(Hotel, guest.hotel_id)
        assert hotel is not None
        out.append(
            {
                "guest_id": guest.id,
                "display_name": guest.display_name,
                "room_number": room.number,
                "reservation_reference": reservation.reference,
                "display_status": display_status(reservation, hotel),
            }
        )
    return out


def safe_context(session: Session, binding: SessionBinding) -> dict[str, Any]:
    """Selector-level summary for the UI; not full reservation dates."""
    guest = session.get(Guest, binding.guest_id)
    reservation = session.get(Reservation, binding.reservation_id)
    room = session.get(Room, binding.room_id)
    hotel = session.get(Hotel, binding.hotel_id)
    if guest is None or reservation is None or room is None or hotel is None:
        raise errors.session_scope_stale()
    return {
        "guest_id": guest.id,
        "guest_name": guest.display_name,
        "room_number": room.number,
        "reservation_reference": reservation.reference,
        "display_status": display_status(reservation, hotel),
        "hotel_name": hotel.display_name,
        "timezone": hotel.timezone,
        "demo_time": to_local(hotel.demo_now, hotel.timezone).isoformat(),
    }


def create_session(session: Session, guest_id: str) -> tuple[str, DemoSession]:
    """Create a scoped session for an allowlisted selectable fixture guest.

    It has no wall-clock lifetime: the hotel clock is the only time in the demo,
    and a start over discards every session with the world it belonged to.
    """
    guest = session.get(Guest, guest_id)
    if guest is None or not guest.selectable:
        raise errors.DomainError("GUEST_NOT_SELECTABLE", "Unknown demo guest.", http_status=404)
    found = _active_reservation(session, guest)
    if found is None:
        raise errors.DomainError(
            "RESERVATION_NOT_FOUND", "This demo guest has no active reservation.", http_status=404
        )
    reservation, room = found
    token = secrets.token_urlsafe(32)  # 256 bits
    now = utcnow()
    demo_session = DemoSession(
        id=new_id("ses"),
        capability_hash=hash_token(token),
        hotel_id=guest.hotel_id,
        guest_id=guest.id,
        reservation_id=reservation.id,
        room_id=room.id,
        conversation_key=new_id("conv"),
        created_at=now,
        accepted_turn_count=0,
    )
    session.add(demo_session)
    session.flush()
    audit.append(
        session,
        hotel_id=guest.hotel_id,
        event_type="session_created",
        actor_type="guest",
        actor_id=guest.id,
        session_id=demo_session.id,
        entity_type="reservation",
        entity_id=reservation.id,
        outcome="created",
    )
    return token, demo_session


def binding_of(demo_session: DemoSession) -> SessionBinding:
    return SessionBinding(
        session_id=demo_session.id,
        hotel_id=demo_session.hotel_id,
        guest_id=demo_session.guest_id,
        reservation_id=demo_session.reservation_id,
        room_id=demo_session.room_id,
        conversation_key=demo_session.conversation_key,
    )


def resolve(session: Session, token: str | None) -> DemoSession:
    """Resolve a bearer capability. An unknown or closed one maps to 401."""
    if not token or len(token) > 200:
        raise errors.invalid_session()
    demo_session = session.scalar(
        select(DemoSession).where(DemoSession.capability_hash == hash_token(token))
    )
    if demo_session is None or demo_session.closed_at is not None:
        raise errors.invalid_session()
    return demo_session


def revalidate_binding(session: Session, binding: ReservationScope) -> Reservation:
    """The bound reservation must still belong to the same hotel/guest/room.

    Never retargets: any drift is SESSION_SCOPE_STALE.
    """
    reservation = session.get(Reservation, binding.reservation_id)
    if reservation is None:
        raise errors.DomainError(
            "RESERVATION_NOT_FOUND", "The bound reservation no longer exists.", http_status=409
        )
    if (
        reservation.hotel_id != binding.hotel_id
        or reservation.guest_id != binding.guest_id
        or reservation.room_id != binding.room_id
        or reservation.status == "cancelled"
    ):
        raise errors.session_scope_stale()
    return reservation


def check_session_active(session: Session, binding: SessionBinding) -> None:
    """Re-check the session record itself on every tool call (a start over removes it)."""
    demo_session = session.get(DemoSession, binding.session_id)
    if demo_session is None or demo_session.closed_at is not None:
        raise errors.invalid_session()
