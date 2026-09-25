"""Operator-controlled world events and demo clock.

The fictional hotel is otherwise still: nothing changes unless a guest asks.
These are a closed set of labelled, deterministic simulator changes an operator
can trigger so the freshness, conflict and expiry engineering can be seen in a
short demo. They are operator actions, never model tools. Each one commits with
an audit event in one ``BEGIN IMMEDIATE`` transaction and bumps exactly the
version components the checkout freshness check already compares. Nothing here
marks an approval stale directly: a pending review is re-checked when it is
decided, exactly as before. The functions take the caller's Session and hold no
state of their own.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local
from hotel_operations.domain import checkout as policy
from hotel_operations.services import approvals, audit, checkout, hotel_updates, housekeeping
from hotel_operations.services.writes import payload_hash
from hotel_operations.storage.models import (
    HOTEL_UPDATE_NEWEST_FIRST,
    Approval,
    AuditEvent,
    Guest,
    Hotel,
    HotelPolicy,
    HotelUpdate,
    HousekeepingTask,
    MaintenanceTask,
    Reservation,
    Room,
    RoomDayPlan,
)

EVENT_APPLIED = "world_event_applied"
CLOCK_ADVANCED = "demo_clock_advanced"
WORLD_EVENT_TYPES = (EVENT_APPLIED, CLOCK_ADVANCED)
CLOCK_STEPS_MINUTES = (30, 60, 120)
CLOCK_LIMIT_LOCAL = time(18, 0)  # the demo day ends here; only a reset rewinds the clock
SOURCE_LABEL = "Simulated world event (fictional)"


@dataclass(frozen=True)
class WorldEvent:
    key: str
    title: str
    effect: str
    components: tuple[str, ...]  # checkout freshness components the event changes
    check: Callable[[Session, Hotel], str | None]  # why it cannot apply now, or None
    apply: Callable[[Session, Hotel], tuple[str, str]]  # (before, after) display values
    # Where the hotel board offers it: a room number (or None for hotel-wide) and the fact it
    # changes. ``fact=None`` keeps an event in the catalog (evaluation, tests) but off the board.
    room: str | None = None
    fact: str | None = None
    action: str | None = None  # the board button's words


def _bump_room(session: Session, room_id: str) -> None:
    """A stay or the cleaning plan of one room changed: only that room's reviews go stale."""
    room = session.get(Room, room_id)
    assert room is not None
    room.operational_version += 1


def _tz(hotel: Hotel) -> ZoneInfo:
    return ZoneInfo(hotel.timezone)


def _today_at(hotel: Hotel, clock: time) -> datetime:
    day = to_local(hotel.demo_now, hotel.timezone).date()
    return datetime.combine(day, clock, tzinfo=_tz(hotel))


def _hhmm(value: datetime, hotel: Hotel) -> str:
    return to_local(value, hotel.timezone).strftime("%H:%M")


def _occupant(session: Session, hotel: Hotel, room_id: str) -> Reservation | None:
    return session.scalar(
        select(Reservation).where(
            Reservation.hotel_id == hotel.id,
            Reservation.room_id == room_id,
            Reservation.status == "checked_in",
        )
    )


def _turnaround(session: Session, hotel: Hotel) -> timedelta:
    row = session.get(HotelPolicy, hotel.id)
    minutes = row.turnaround_minutes if row is not None else None
    return timedelta(minutes=minutes if minutes is not None else 60)


# --- the catalog ---------------------------------------------------------------------------

EARLY_ARRIVAL_RESERVATION = "rsv_2001"  # room 305's next (internal) arrival
EARLY_ARRIVAL_LOCAL = time(15, 30)
CANCELLED_ARRIVAL_RESERVATION = "rsv_2002"  # room 412's next (internal) arrival
SHORTENED_PLAN = "rdp_305_20260922"
SHORTENED_FINISH_LOCAL = time(15, 30)
OUT_OF_SERVICE_ROOM = "room_305"


def _early_arrival_check(session: Session, hotel: Hotel) -> str | None:
    arrival = session.get(Reservation, EARLY_ARRIVAL_RESERVATION)
    if arrival is None or arrival.status != "confirmed":
        return "Room 305 has no upcoming arrival to move."
    new_check_in = _today_at(hotel, EARLY_ARRIVAL_LOCAL)
    if arrival.scheduled_check_in <= new_check_in:
        return "Room 305's next arrival is already at or before 15:30."
    occupant = _occupant(session, hotel, arrival.room_id)
    if (
        occupant is not None
        and occupant.scheduled_checkout + _turnaround(session, hotel) > new_check_in
    ):
        # Would create an overlapping, uncleanable stay; the checkout service fails closed on it.
        return "Room 305's current checkout is too late for a 15:30 arrival."
    return None


def _early_arrival_apply(session: Session, hotel: Hotel) -> tuple[str, str]:
    arrival = session.get(Reservation, EARLY_ARRIVAL_RESERVATION)
    assert arrival is not None
    before = _hhmm(arrival.scheduled_check_in, hotel)
    arrival.scheduled_check_in = _today_at(hotel, EARLY_ARRIVAL_LOCAL)
    arrival.version += 1
    _bump_room(session, arrival.room_id)
    return f"next arrival {before}", f"next arrival {_hhmm(arrival.scheduled_check_in, hotel)}"


def _cancel_check(session: Session, hotel: Hotel) -> str | None:
    arrival = session.get(Reservation, CANCELLED_ARRIVAL_RESERVATION)
    if arrival is None or arrival.status != "confirmed":
        return "Room 412 has no upcoming arrival to cancel."
    return None


def _cancel_apply(session: Session, hotel: Hotel) -> tuple[str, str]:
    arrival = session.get(Reservation, CANCELLED_ARRIVAL_RESERVATION)
    assert arrival is not None
    before = f"next arrival {_hhmm(arrival.scheduled_check_in, hotel)}"
    arrival.status = "cancelled"
    arrival.version += 1
    _bump_room(session, arrival.room_id)
    return before, "no next arrival"


def _out_of_service_check(session: Session, hotel: Hotel) -> str | None:
    room = session.get(Room, OUT_OF_SERVICE_ROOM)
    if room is None or room.out_of_service:
        return "Room 305 is already out of service."
    return None


def _out_of_service_apply(session: Session, hotel: Hotel) -> tuple[str, str]:
    room = session.get(Room, OUT_OF_SERVICE_ROOM)
    assert room is not None
    room.out_of_service = True
    room.version += 1
    room.operational_version += 1  # the room's own state
    return "in service", "out of service"


def _shorten_check(session: Session, hotel: Hotel) -> str | None:
    plan = session.get(RoomDayPlan, SHORTENED_PLAN)
    if plan is None:
        return "Room 305 has no cleaning plan for today."
    if plan.housekeeping_finish_by <= _today_at(hotel, SHORTENED_FINISH_LOCAL):
        return "Room 305's cleaning window already ends by 15:30."
    return None


def _shorten_apply(session: Session, hotel: Hotel) -> tuple[str, str]:
    plan = session.get(RoomDayPlan, SHORTENED_PLAN)
    assert plan is not None
    before = f"cleaning by {_hhmm(plan.housekeeping_finish_by, hotel)}"
    plan.housekeeping_finish_by = _today_at(hotel, SHORTENED_FINISH_LOCAL)
    plan.version += 1
    _bump_room(session, plan.room_id)
    return before, f"cleaning by {_hhmm(plan.housekeeping_finish_by, hotel)}"


# Titles and effects are read by people, not code: say what the reviewer will see, in the
# hotel's words. Every event also bumps its room's ``operational`` version, so a
# review already waiting for that room will not apply if approved; the guest has to ask
# again. Reviews for the other rooms are untouched.
_WAITING = (
    " A review already waiting for this room would no longer apply; other rooms are unaffected."
)

CATALOG: tuple[WorldEvent, ...] = (
    WorldEvent(
        "arrival_305_early",
        "Room 305's next guest arrives early (15:30 instead of 18:00)",
        "Emma's room must now be ready by 15:30, so a 15:00 checkout is refused and 14:30 is "
        "the latest she can get." + _WAITING,
        ("operational",),
        _early_arrival_check,
        _early_arrival_apply,
        room="305",
        fact="next_arrival",
        action="Arrives early (15:30)",
    ),
    WorldEvent(
        "arrival_412_cancelled",
        "Room 412's next arrival is cancelled",
        "Daniel's room no longer has to be ready for a 15:00 arrival, so the 14:30 checkout he "
        "was refused can now go to a manager." + _WAITING,
        ("operational",),
        _cancel_check,
        _cancel_apply,
        room="412",
        fact="next_arrival",
        action="Cancel this arrival",
    ),
    WorldEvent(
        "room_305_out_of_service",
        "Room 305 is taken out of service",
        "Any late checkout for room 305 is refused while it is out of service." + _WAITING,
        ("operational", "room"),
        _out_of_service_check,
        _out_of_service_apply,
        room="305",
        fact="in_service",
        action="Take out of service",
    ),
    WorldEvent(
        "cleaning_305_shortened",
        "Room 305's cleaning must finish by 15:30",
        "Housekeeping needs the room earlier, so a 15:00 checkout is refused and 14:30 is the "
        "latest possible." + _WAITING,
        ("operational", "room_day_plan"),
        _shorten_check,
        _shorten_apply,
        # Room 305's, but off the board (no ``fact``): for Emma it has the same effect as the
        # early arrival, so offering both reads as two buttons for one change. The
        # evaluation's W cases still use it.
        room="305",
    ),
)
# The checkout policy is fixed: 14:00 automatic, 16:00 with a manager. No event moves the
# checkout window: a moving window added confusion, not insight.
BY_KEY = {e.key: e for e in CATALOG}


# --- reads ---------------------------------------------------------------------------------


def _hotel(session: Session, hotel_id: str) -> Hotel:
    hotel = session.get(Hotel, hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    return hotel


def _applied(session: Session, hotel_id: str) -> dict[str, AuditEvent]:
    rows = session.scalars(
        select(AuditEvent).where(
            AuditEvent.hotel_id == hotel_id, AuditEvent.event_type == EVENT_APPLIED
        )
    )
    return {str(e.entity_id): e for e in rows}


def clock_limit(hotel: Hotel) -> datetime:
    return _today_at(hotel, CLOCK_LIMIT_LOCAL)


def world_state(session: Session, hotel_id: str) -> dict[str, Any]:
    hotel = _hotel(session, hotel_id)
    applied = _applied(session, hotel_id)
    events = []
    for event in CATALOG:
        done = applied.get(event.key)
        blocked = None if done is not None else event.check(session, hotel)
        events.append(
            {
                "key": event.key,
                "title": event.title,
                "effect": event.effect,
                "components": list(event.components),
                "room": event.room,
                "fact": event.fact,
                "action": event.action,
                "applied": done is not None,
                "applied_at": None if done is None else iso(done.wall_time),
                "available": done is None and blocked is None,
                "unavailable_reason": blocked,
            }
        )
    limit = clock_limit(hotel)
    now = hotel.demo_now
    return {
        "source": SOURCE_LABEL,
        "demo_time": iso(to_local(now, hotel.timezone)),
        "timezone": hotel.timezone,
        "clock_limit": iso(to_local(limit, hotel.timezone)),
        "clock_steps": [m for m in CLOCK_STEPS_MINUTES if now + timedelta(minutes=m) <= limit],
        "events": events,
        "rooms": _board_rooms(session, hotel),
        "policy": _board_policy(session, hotel),
    }


# --- the hotel board ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Scope:
    """A demo guest's reservation, so the board reads facts through the checkout service."""

    hotel_id: str
    guest_id: str
    reservation_id: str
    room_id: str


def _local_iso(value: datetime | None, hotel: Hotel) -> str | None:
    return None if value is None else iso(to_local(value, hotel.timezone))


def _board_room(session: Session, hotel: Hotel, guest: Guest) -> dict[str, Any] | None:
    row = session.execute(
        select(Reservation, Room)
        .join(Room, Room.id == Reservation.room_id)
        .where(
            Reservation.guest_id == guest.id,
            Reservation.hotel_id == hotel.id,
            Reservation.status.in_(checkout.ACTIVE_STATUSES),
        )
        .order_by(Reservation.scheduled_check_in)
    ).first()
    if row is None:
        return None
    reservation, room = row
    out: dict[str, Any] = {
        "room_number": room.number,
        "guest_id": guest.id,
        "guest_name": guest.display_name,
        "status": reservation.status,
        "check_in": _local_iso(reservation.scheduled_check_in, hotel),
        "checkout": _local_iso(reservation.scheduled_checkout, hotel),
        "in_service": not room.out_of_service,
        "housekeeping_state": room.housekeeping_state,
        "open_tasks": _open_tasks(session, reservation.id),
        "pending_review": None,
        "latest_update": _latest_update(session, hotel, reservation.id),
        "next_arrival": None,
        "latest_checkout": None,
        "latest_needs_review": False,
        "latest_blocked_by": None,
    }
    if reservation.status != "checked_in":
        return out
    ctx: checkout.CheckoutContext | None
    try:
        # The same facts and the same pure rule a request would be decided on right now.
        ctx = checkout.load_context(session, _Scope(hotel.id, guest.id, reservation.id, room.id))
    except errors.DomainError as exc:
        ctx = None
        out["latest_blocked_by"] = exc.code
    out["pending_review"] = _pending_review(session, hotel, reservation.id, ctx)
    if ctx is None:
        return out
    try:
        latest = policy.latest_checkout(ctx.rules, ctx.facts)
    except errors.DomainError as exc:
        out["latest_blocked_by"] = exc.code
        return out
    out["next_arrival"] = _local_iso(ctx.facts.next_arrival, hotel)
    out["latest_checkout"] = _local_iso(latest.latest, hotel)
    out["latest_needs_review"] = latest.needs_review
    out["latest_blocked_by"] = latest.blocked_by
    return out


def _open_tasks(session: Session, reservation_id: str) -> list[dict[str, Any]]:
    """The stay's open housekeeping and maintenance requests, oldest first. The labels are
    typed fields only; a guest's own words (notes, descriptions) never reach the board."""
    open_statuses = ("pending", "in_progress")
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for hk in session.scalars(
        select(HousekeepingTask).where(
            HousekeepingTask.reservation_id == reservation_id,
            HousekeepingTask.status.in_(open_statuses),
        )
    ):
        what = "room_cleaning" if hk.item == "room_cleaning" else f"{hk.quantity} {hk.item}"
        rows.append((hk.created_at, {"kind": "housekeeping", "task_id": hk.id, "what": what,
                                     "status": hk.status}))  # fmt: skip
    for mt in session.scalars(
        select(MaintenanceTask).where(
            MaintenanceTask.reservation_id == reservation_id,
            MaintenanceTask.status.in_(open_statuses),
        )
    ):
        rows.append((mt.created_at, {"kind": "maintenance", "task_id": mt.id, "what": mt.category,
                                     "status": mt.status}))  # fmt: skip
    return [item for _, item in sorted(rows, key=lambda r: (r[0], r[1]["task_id"]))]


def _latest_update(session: Session, hotel: Hotel, reservation_id: str) -> dict[str, Any] | None:
    """What the hotel last had to tell this guest, and whether the agent told them."""
    update = session.scalar(
        select(HotelUpdate)
        .where(HotelUpdate.reservation_id == reservation_id)
        .order_by(*HOTEL_UPDATE_NEWEST_FIRST)
        .limit(1)
    )
    if update is None:
        return None
    return {
        "kind": update.kind,
        "title": hotel_updates.title(update),
        "demo_time": _local_iso(update.demo_time, hotel),
        "status": update.status,
        "skip_reason": update.skip_reason,
    }


def _pending_review(
    session: Session, hotel: Hotel, reservation_id: str, ctx: checkout.CheckoutContext | None
) -> dict[str, Any] | None:
    """The stay's waiting review, with what an approve would find now (a preview only)."""
    for approval in session.scalars(
        select(Approval).where(
            Approval.reservation_id == reservation_id, Approval.status == "pending"
        )
    ):
        if approvals.effective_status(approval, hotel.demo_now) != "pending":
            continue
        return {
            "approval_id": approval.id,
            "requested_checkout": approvals.requested_local(approval, hotel.timezone),
            "blocking_reason": approvals.approve_blocker(approval, ctx),
        }
    return None


def _board_rooms(session: Session, hotel: Hotel) -> list[dict[str, Any]]:
    """Each demo guest's room as the rules see it now. Another arrival's identity never
    appears: only when the room must be ready."""
    guests = session.scalars(
        select(Guest).where(Guest.hotel_id == hotel.id, Guest.selectable.is_(True))
    )
    rows = [r for g in guests if (r := _board_room(session, hotel, g)) is not None]
    return sorted(rows, key=lambda r: r["room_number"])


def _board_policy(session: Session, hotel: Hotel) -> dict[str, Any] | None:
    row = session.get(HotelPolicy, hotel.id)
    if row is None:
        return None
    return {
        "version": row.version,
        "standard_checkout": row.standard_checkout_local,
        "automatic_until": row.auto_extension_until_local,
        "manager_until": row.reviewed_extension_until_local,
    }


def changes_since(
    session: Session, hotel_id: str, since: datetime, room_number: str | None = None
) -> list[dict[str, Any]]:
    """World events and clock moves recorded at or after ``since`` (real time), oldest first.

    With ``room_number``, an event that belongs to another room is left out: it cannot affect
    this room's review, and listing it under "what changed since the guest asked" would suggest
    it did. Clock moves concern every room and always stay.
    """
    hotel = _hotel(session, hotel_id)
    rows = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.hotel_id == hotel_id,
            AuditEvent.event_type.in_(WORLD_EVENT_TYPES),
            AuditEvent.wall_time >= since,
        )
        .order_by(AuditEvent.id)
    )
    out = []
    for e in rows:
        payload = e.payload or {}
        known = BY_KEY.get(str(e.entity_id))
        if room_number and known and known.room and known.room != room_number:
            continue
        out.append(
            {
                "kind": "event" if e.event_type == EVENT_APPLIED else "clock",
                "key": e.entity_id,
                "title": known.title if known else payload.get("title"),
                "components": list(known.components) if known else [],
                "before": payload.get("before"),
                "after": payload.get("after"),
                "wall_time": iso(e.wall_time),
                "demo_time": iso(to_local(e.demo_time, hotel.timezone)) if e.demo_time else None,
            }
        )
    return out


# --- writes --------------------------------------------------------------------------------


def _replay(
    session: Session, hotel_id: str, operator_id: str, client_request_id: str, body_hash: str
) -> dict[str, Any] | None:
    prior = session.scalar(
        select(AuditEvent).where(
            AuditEvent.hotel_id == hotel_id,
            AuditEvent.event_type.in_(WORLD_EVENT_TYPES),
            AuditEvent.actor_id == operator_id,
            func.json_extract(AuditEvent.payload, "$.client_request_id") == client_request_id,
        )
    )
    if prior is None:
        return None
    if (prior.payload or {}).get("body_hash") != body_hash:
        raise errors.DomainError(
            "IDEMPOTENCY_CONFLICT",
            "This client_request_id was already used for a different world change.",
            http_status=409,
        )
    return {**prior.payload["result"], "replayed": True}


def _record(
    session: Session,
    hotel: Hotel,
    *,
    event_type: str,
    key: str,
    operator_id: str,
    client_request_id: str,
    body_hash: str,
    result: dict[str, Any],
) -> None:
    audit.append(
        session,
        hotel_id=hotel.id,
        event_type=event_type,
        actor_type="operator",
        actor_id=operator_id,
        entity_type="world",
        entity_id=key,
        outcome="applied",
        payload={
            "before": result["before"],
            "after": result["after"],
            "client_request_id": client_request_id,
            "body_hash": body_hash,
            "result": result,
        },
    )


def apply_event(
    session: Session, *, hotel_id: str, operator_id: str, key: str, client_request_id: str
) -> dict[str, Any]:
    body_hash = payload_hash({"world_event": key})
    replayed = _replay(session, hotel_id, operator_id, client_request_id, body_hash)
    if replayed is not None:
        return replayed
    event = BY_KEY.get(key)
    if event is None:
        raise errors.DomainError("WORLD_EVENT_UNKNOWN", "No such world event.", http_status=404)
    hotel = _hotel(session, hotel_id)
    if key in _applied(session, hotel_id):
        raise errors.DomainError(
            "WORLD_EVENT_ALREADY_APPLIED",
            "This world event already happened; reset the demo to replay it.",
            http_status=409,
        )
    if (reason := event.check(session, hotel)) is not None:
        raise errors.DomainError("WORLD_EVENT_NOT_APPLICABLE", reason, http_status=409)
    snapshot = hotel_updates.before_event(session, hotel)
    before, after = event.apply(session, hotel)
    session.flush()
    hotel_updates.after_event(session, hotel, key, snapshot)
    result: dict[str, Any] = {
        "kind": "event",
        "key": key,
        "title": event.title,
        "components": list(event.components),
        "before": before,
        "after": after,
        "demo_time": iso(to_local(hotel.demo_now, hotel.timezone)),
        "replayed": False,
    }
    _record(
        session, hotel, event_type=EVENT_APPLIED, key=key, operator_id=operator_id,
        client_request_id=client_request_id, body_hash=body_hash, result=result,
    )  # fmt: skip
    return result


def advance_clock(
    session: Session, *, hotel_id: str, operator_id: str, minutes: int, client_request_id: str
) -> dict[str, Any]:
    """Move the frozen business clock forward; it never moves back except by a reset.

    Deliberately no version bump: no availability fact
    changed, and every time-dependent rule is re-decided from ``demo_now`` itself.

    The move has two recorded consequences, in the same transaction: every pending
    review whose requested time the clock reached is persisted as expired, and when the clock
    passes the end of cleaning hours, room cleanings nobody started are cancelled. Both are
    listed in ``side_effects``, so a replay returns them unchanged.
    """
    body_hash = payload_hash({"advance_minutes": minutes})
    replayed = _replay(session, hotel_id, operator_id, client_request_id, body_hash)
    if replayed is not None:
        return replayed
    if minutes not in CLOCK_STEPS_MINUTES:
        raise errors.DomainError(
            "INVALID_ARGUMENT",
            "The demo clock moves in steps of "
            + ", ".join(map(str, CLOCK_STEPS_MINUTES))
            + " minutes.",
            http_status=422,
        )
    hotel = _hotel(session, hotel_id)
    target = hotel.demo_now + timedelta(minutes=minutes)
    limit = clock_limit(hotel)
    if target > limit:
        raise errors.DomainError(
            "DEMO_CLOCK_LIMIT",
            f"The demo day ends at {_hhmm(limit, hotel)}; reset the demo to start over.",
            http_status=409,
        )
    before = _hhmm(hotel.demo_now, hotel)
    was = hotel.demo_now
    hotel.demo_now = target
    session.flush()
    expired = approvals.expire_lapsed(session, hotel.id, target)
    for approval in expired:
        hotel_updates.review_closed(session, hotel, approval)
    cancelled = housekeeping.close_cleaning_window(session, hotel, was, target)
    key = f"advance_{minutes}"
    result: dict[str, Any] = {
        "kind": "clock",
        "key": key,
        "title": f"Demo clock advanced {minutes} minutes",
        "components": [],
        "before": before,
        "after": _hhmm(target, hotel),
        "demo_time": iso(to_local(target, hotel.timezone)),
        "side_effects": {
            "expired_reviews": [a.id for a in expired],
            "cancelled_tasks": [t.id for t in cancelled],
        },
        "replayed": False,
    }
    _record(
        session, hotel, event_type=CLOCK_ADVANCED, key=key, operator_id=operator_id,
        client_request_id=client_request_id, body_hash=body_hash, result=result,
    )  # fmt: skip
    return result
