"""Seed fixture v1 — invented data for reproducible demos and tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hotel_operations.domain.hotel_info import validated
from hotel_operations.hotel_info_v1 import HOTEL_INFO_V1
from hotel_operations.storage.models import (
    Guest,
    Hotel,
    HotelInfoTopic,
    HotelPolicy,
    Reservation,
    Room,
    RoomDayPlan,
)

FIXTURE_VERSION = "fixture-v1"
HOTEL_ID = "hotel_demo"
HOTEL_TZ = "Europe/Istanbul"
HOTEL_DISPLAY_NAME = "Demo Hotel (fictional)"
_TZ = ZoneInfo(HOTEL_TZ)


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=_TZ)


DEMO_NOW = local(2026, 9, 22, 10, 0)


@dataclass(frozen=True)
class GuestFixture:
    guest_id: str
    name: str
    reservation_id: str
    reference: str
    room_id: str
    room_number: str
    status: str
    check_in: datetime
    checkout: datetime
    actual_check_in: datetime | None
    housekeeping_state: str


GUESTS: tuple[GuestFixture, ...] = (
    GuestFixture(
        "G-001", "Emma Wilson", "rsv_1042", "R-1042", "room_305", "305", "checked_in",
        local(2026, 9, 20, 15), local(2026, 9, 22, 12), local(2026, 9, 20, 16), "dirty",
    ),
    GuestFixture(
        "G-002", "Daniel Kim", "rsv_1088", "R-1088", "room_412", "412", "checked_in",
        local(2026, 9, 21, 15), local(2026, 9, 22, 12), local(2026, 9, 21, 16), "dirty",
    ),
    GuestFixture(
        "G-003", "Sofia Rossi", "rsv_1101", "R-1101", "room_218", "218", "confirmed",
        local(2026, 9, 22, 15), local(2026, 9, 24, 12), None, "inspected",
    ),
)  # fmt: skip


# Hotel policy fixture. Invented values, not hotel facts.
POLICY_V1: dict[str, object] = {
    "breakfast_start_local": "07:00",
    "breakfast_end_local": "10:30",
    "housekeeping_items": ["towels", "pillows", "room_cleaning"],
    "housekeeping_max_quantity": 6,
    "maintenance_categories": ["hvac", "plumbing", "electrical", "other"],
    # Late-checkout policy v1. Fictional demo choices.
    "standard_checkout_local": "12:00",
    "auto_extension_until_local": "14:00",
    "reviewed_extension_until_local": "16:00",
    "turnaround_minutes": 60,
    "default_housekeeping_finish_local": "17:30",
    # Early check-in policy v1. Fictional demo choices.
    "standard_check_in_local": "15:00",
    "early_check_in_from_local": "12:00",
    # Housekeeping policy v1. Fictional demo choices.
    "room_cleaning_from_local": "09:00",
    "room_cleaning_until_local": "16:00",
    "towels_per_stay": 8,
    "pillows_per_stay": 4,
}


@dataclass(frozen=True)
class NextArrival:
    """Internal, nonselectable next reservation for a room. Never exposed to guests."""

    guest_id: str
    reservation_id: str
    reference: str
    room_id: str
    check_in: datetime
    checkout: datetime


NEXT_ARRIVALS: tuple[NextArrival, ...] = (
    NextArrival("G-901", "rsv_2001", "R-2001", "room_305", local(2026, 9, 22, 18),
                local(2026, 9, 24, 12)),
    NextArrival("G-902", "rsv_2002", "R-2002", "room_412", local(2026, 9, 22, 15),
                local(2026, 9, 23, 12)),
)  # fmt: skip
INTERNAL_GUEST_NAME = "Internal arrival (fictional, not selectable)"

# Room-day plans: cleaning may start at 12:00 and must finish by 17:30.
ROOM_DAY_PLANS: tuple[tuple[str, str, datetime, datetime], ...] = (
    ("rdp_305_20260922", "room_305", local(2026, 9, 22, 12), local(2026, 9, 22, 17, 30)),
    ("rdp_412_20260922", "room_412", local(2026, 9, 22, 12), local(2026, 9, 22, 17, 30)),
)


def ensure_policy(session: Session) -> bool:
    """Insert the policy row for the demo hotel if it is missing."""
    if session.get(Hotel, HOTEL_ID) is None or session.get(HotelPolicy, HOTEL_ID) is not None:
        return False
    session.add(HotelPolicy(hotel_id=HOTEL_ID, version=1, **POLICY_V1))
    session.flush()
    return True


def ensure_info_topics(session: Session) -> bool:
    """Insert the v1 guest information documents for the demo hotel if missing."""
    if session.get(Hotel, HOTEL_ID) is None:
        return False
    inserted = False
    for topic, content in HOTEL_INFO_V1.items():
        if session.get(HotelInfoTopic, (HOTEL_ID, topic)) is not None:
            continue
        document = validated(topic, content)
        assert document is not None, f"fixture info topic {topic!r} is not a valid document"
        session.add(HotelInfoTopic(hotel_id=HOTEL_ID, topic=topic, version=1, content=document))
        inserted = True
    session.flush()
    return inserted


def is_empty(session: Session) -> bool:
    return (session.scalar(select(func.count()).select_from(Hotel)) or 0) == 0


def seed_fixture(session: Session) -> bool:
    """Insert fixture v1 into an empty database. Returns False if data already exists.

    Never resets an existing demo; a destructive reset is a separate explicit command.
    """
    if not is_empty(session):
        return False
    session.add(
        Hotel(
            id=HOTEL_ID,
            display_name=HOTEL_DISPLAY_NAME,
            timezone=HOTEL_TZ,
            demo_now=DEMO_NOW,
        )
    )
    session.flush()
    for g in GUESTS:
        session.add(Guest(id=g.guest_id, hotel_id=HOTEL_ID, display_name=g.name, selectable=True))
        session.add(
            Room(
                id=g.room_id,
                hotel_id=HOTEL_ID,
                number=g.room_number,
                housekeeping_state=g.housekeeping_state,
                out_of_service=False,
                version=1,
                operational_version=1,
            )
        )
    session.flush()
    for g in GUESTS:
        session.add(
            Reservation(
                id=g.reservation_id,
                reference=g.reference,
                hotel_id=HOTEL_ID,
                guest_id=g.guest_id,
                room_id=g.room_id,
                status=g.status,
                scheduled_check_in=g.check_in,
                scheduled_checkout=g.checkout,
                actual_check_in=g.actual_check_in,
                actual_checkout=None,
                version=1,
            )
        )
    session.flush()
    ensure_policy(session)
    ensure_info_topics(session)
    for arrival in NEXT_ARRIVALS:
        session.add(
            Guest(
                id=arrival.guest_id,
                hotel_id=HOTEL_ID,
                display_name=INTERNAL_GUEST_NAME,
                selectable=False,
            )
        )
    session.flush()
    for arrival in NEXT_ARRIVALS:
        session.add(
            Reservation(
                id=arrival.reservation_id,
                reference=arrival.reference,
                hotel_id=HOTEL_ID,
                guest_id=arrival.guest_id,
                room_id=arrival.room_id,
                status="confirmed",
                scheduled_check_in=arrival.check_in,
                scheduled_checkout=arrival.checkout,
                actual_check_in=None,
                actual_checkout=None,
                version=1,
            )
        )
    for plan_id, room_id, ready_after, finish_by in ROOM_DAY_PLANS:
        session.add(
            RoomDayPlan(
                id=plan_id,
                hotel_id=HOTEL_ID,
                room_id=room_id,
                local_date=ready_after.date().isoformat(),
                housekeeping_ready_after=ready_after,
                housekeeping_finish_by=finish_by,
                version=1,
            )
        )
    session.flush()
    return True
