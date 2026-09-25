"""Hotel updates: what the hotel did that one guest should hear about.

Deterministic code decides whether to speak, the model chooses the words, tools fetch the
facts. This module is the first part, the gate. It is called inside the transaction of a
change the operator side or the hotel clock makes, and writes at most one row per
``(reservation, kind, ref)`` into the ``hotel_updates`` outbox, so a rolled-back change
leaves no update and a replayed command adds none. Delivery, a read-only agent turn, is
``services.hotel_update_runs``.

What the gate reports:

- ``review_closed``: a late-checkout review was decided or expired (not by the guest's own
  turn: ``approvals.release_invalid_pending`` runs while the guest's agent is answering).
- ``task_closed``: staff completed or cancelled a housekeeping or maintenance request, or
  the end of cleaning hours cancelled a room cleaning nobody started.
- ``room_out_of_service``: the guest's own room was taken out of service.
- ``review_at_risk``: a hotel change means a waiting review's time is no longer possible.
- ``checkout_now_possible``: a hotel change makes a time the guest was refused possible.

A deliberate gap: a world event bumps its room's operational version, so a
manager's approve after any change to that room finds its review stale, even when the rules'
answer is unchanged. That is reported when it happens (a ``review_closed`` with the stale
reason), not predicted at the event. A change to another room leaves the review fresh.

``facts`` carries enums, ids and times only. A guest's notes, a fault description and any
other free text never enter it; the agent re-reads the guest's own records with its tools.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.domain import checkout as policy
from hotel_operations.ids import new_id
from hotel_operations.services import approvals, checkout
from hotel_operations.storage.models import (
    Approval,
    AuditEvent,
    Hotel,
    HotelUpdate,
    HousekeepingTask,
    MaintenanceTask,
    Reservation,
    Room,
)

FEASIBLE = ("auto_allowed", "approval_required")


def _enqueue(
    session: Session,
    hotel: Hotel,
    *,
    reservation_id: str,
    kind: str,
    ref: str,
    facts: dict[str, Any],
) -> None:
    """Record the update once; a second report of the same thing is dropped, not an error,
    so it can never roll back the change it reports."""
    session.execute(
        sqlite_insert(HotelUpdate)
        .values(
            id=new_id("hup"),
            hotel_id=hotel.id,
            reservation_id=reservation_id,
            kind=kind,
            ref=ref,
            facts=facts,
            status="pending",
            created_at=utcnow(),
            demo_time=hotel.demo_now,
        )
        .on_conflict_do_nothing(index_elements=["reservation_id", "kind", "ref"])
    )


def review_closed(session: Session, hotel: Hotel, approval: Approval) -> None:
    """A review reached a terminal status by the manager's decision or the hotel clock."""
    _enqueue(
        session,
        hotel,
        reservation_id=approval.reservation_id,
        kind="review_closed",
        ref=approval.id,
        facts={
            "approval_id": approval.id,
            "status": approval.status,
            "status_reason": approval.status_reason,
            "requested_checkout_local": approvals.requested_local(approval, hotel.timezone),
        },
    )


def task_closed(session: Session, kind: str, task: HousekeepingTask | MaintenanceTask) -> None:
    """Staff (or the end of cleaning hours) closed one of the guest's requests."""
    if task.status not in ("completed", "cancelled"):
        return
    hotel = session.get(Hotel, task.hotel_id)
    if hotel is None:
        return
    facts: dict[str, Any] = {"task_id": task.id, "task_kind": kind, "status": task.status}
    if isinstance(task, HousekeepingTask):
        facts |= {"item": task.item, "quantity": task.quantity, "reason": task.status_reason}
    else:
        facts |= {"category": task.category, "reason": None}
    _enqueue(
        session, hotel, reservation_id=task.reservation_id, kind="task_closed", ref=task.id,
        facts=facts,
    )  # fmt: skip


def room_out_of_service(session: Session, hotel: Hotel, room: Room) -> None:
    """The room of a guest who is staying in it was taken out of service."""
    for reservation in session.scalars(
        select(Reservation).where(
            Reservation.room_id == room.id, Reservation.status == "checked_in"
        )
    ):
        _enqueue(
            session, hotel, reservation_id=reservation.id, kind="room_out_of_service",
            ref=room.id, facts={"room_number": room.number},
        )  # fmt: skip


# --- how an update is shown -------------------------------------------------------------------


def _hhmm(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else "the requested time"


_WHAT = {
    "towels": "Towels",
    "pillows": "Pillows",
    "room_cleaning": "Room cleaning",
    "hvac": "Air conditioning or heating report",
    "plumbing": "Plumbing report",
    "electrical": "Electrical report",
    "other": "Maintenance report",
}


def _review_title(f: dict[str, Any]) -> str:
    at = _hhmm(f.get("requested_checkout_local"))
    return {
        "executed": f"Late checkout until {at} approved",
        "rejected": f"Late checkout until {at} declined",
        "expired": f"Late checkout review for {at} expired",
        "stale": f"Late checkout until {at} could not be applied",
    }.get(str(f.get("status")), f"Late checkout review for {at} closed")


def _task_title(f: dict[str, Any]) -> str:
    what = _WHAT.get(str(f.get("item") or f.get("category")), "Request")
    if f.get("status") == "completed":
        return f"{what} marked completed"
    if f.get("reason") == "SERVICE_WINDOW_CLOSED":
        return f"{what} cancelled: cleaning hours ended"
    return f"{what} cancelled by staff"


TITLES: dict[str, Callable[[dict[str, Any]], str]] = {
    "review_closed": _review_title,
    "task_closed": _task_title,
    "room_out_of_service": lambda f: f"Room {f.get('room_number')} taken out of service",
    "review_at_risk": lambda f: (
        f"Late checkout until {_hhmm(f.get('requested_checkout_local'))} no longer possible"
    ),
    "checkout_now_possible": lambda f: (
        f"Checkout at {_hhmm(f.get('requested_checkout_local'))} may now be possible"
    ),
}


def title(update: HotelUpdate) -> str:
    """The short line the guest's transcript shows for an update (never the model input)."""
    return TITLES[update.kind](update.facts or {})


# --- world events: what the checkout rule said before and after ------------------------------


@dataclass(frozen=True)
class _Scope:
    hotel_id: str
    guest_id: str
    reservation_id: str
    room_id: str


@dataclass(frozen=True)
class Watch:
    """A guest's checkout wish the gate compares across one world event."""

    kind: str  # the update it can raise: review_at_risk | checkout_now_possible
    reservation: Reservation
    ref: str  # the approval id, or the refused request's audit event
    requested: datetime
    before: str | None  # the checkout rule's decision before the event; None if unreadable


def _decision(session: Session, hotel: Hotel, reservation: Reservation, when: datetime) -> Any:
    scope = _Scope(hotel.id, reservation.guest_id, reservation.id, reservation.room_id)
    try:
        ctx = checkout.load_context(session, scope)
        return policy.evaluate(when, ctx.rules, ctx.facts)
    except errors.DomainError:
        return None  # nothing the rule can say now; the gate stays silent


def _last_refusal(
    session: Session, hotel: Hotel, reservation: Reservation
) -> tuple[AuditEvent, datetime] | None:
    """The guest's latest checkout request, if it was refused and could still happen."""
    event = session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.hotel_id == hotel.id,
            AuditEvent.event_type.in_(("checkout_evaluated", "checkout_changed")),
            AuditEvent.entity_id == reservation.id,
        )
        .order_by(AuditEvent.id.desc())
        .limit(1)
    )
    if event is None or (event.event_type, event.outcome) != ("checkout_evaluated", "denied"):
        return None  # never refused, or the checkout changed since
    try:
        requested = datetime.fromisoformat(str(event.payload["requested_checkout_local"]))
    except (KeyError, TypeError, ValueError):
        return None
    later = session.scalar(
        select(Approval.id).where(
            Approval.reservation_id == reservation.id, Approval.created_at > event.wall_time
        )
    )
    if later is not None:  # the guest asked again since; that request is the current one
        return None
    if requested <= hotel.demo_now or reservation.scheduled_checkout >= requested:
        return None
    return event, requested


@dataclass(frozen=True)
class Snapshot:
    """What the gate compares across one world event."""

    watches: list[Watch]
    rooms_in_service: frozenset[str]


def before_event(session: Session, hotel: Hotel) -> Snapshot:
    """Before a world event: each staying guest's waiting review or last refused request,
    with what the checkout rule says about it now, and which rooms are in service."""
    rooms = frozenset(
        session.scalars(
            select(Room.id).where(Room.hotel_id == hotel.id, Room.out_of_service.is_(False))
        )
    )
    watched: list[Watch] = []
    for reservation in session.scalars(
        select(Reservation)
        .where(Reservation.hotel_id == hotel.id, Reservation.status == "checked_in")
        .order_by(Reservation.id)
    ):
        pending = [
            a
            for a in approvals.pending_for(session, reservation.id)
            if not approvals.lapsed(a, hotel.demo_now)
        ]
        if pending:
            approval = pending[0]
            when = approvals.requested_time(approval)
            kind, ref = "review_at_risk", approval.id
        elif (refusal := _last_refusal(session, hotel, reservation)) is not None:
            event, when = refusal
            kind, ref = "checkout_now_possible", f"aud_{event.id}"
        else:
            continue
        decided = _decision(session, hotel, reservation, when)
        watched.append(
            Watch(kind, reservation, ref, when, None if decided is None else decided.decision)
        )
    return Snapshot(watched, rooms)


def after_event(session: Session, hotel: Hotel, event_key: str, before: Snapshot) -> None:
    """After a world event: report a room taken out of service under its guest, a waiting
    review the change made impossible, and a refused time it made possible. Anything the
    checkout rule cannot read now stays silent."""
    for room in session.scalars(
        select(Room).where(Room.hotel_id == hotel.id, Room.out_of_service.is_(True))
    ):
        if room.id in before.rooms_in_service:
            room_out_of_service(session, hotel, room)
    for w in before.watches:
        decided = _decision(session, hotel, w.reservation, w.requested)
        if decided is None:
            continue
        requested_local = iso(to_local(w.requested, hotel.timezone))
        if w.kind == "review_at_risk" and w.before in FEASIBLE and decided.decision == "denied":
            facts = {
                "approval_id": w.ref,
                "requested_checkout_local": requested_local,
                "reason_codes": list(decided.reason_codes),
                "cause": event_key,
            }
        elif (
            w.kind == "checkout_now_possible"
            and w.before == "denied"
            and decided.decision in FEASIBLE
        ):
            facts = {
                "requested_checkout_local": requested_local,
                "decision_now": decided.decision,
                "cause": event_key,
            }
        else:
            continue
        _enqueue(
            session, hotel, reservation_id=w.reservation.id, kind=w.kind, ref=w.ref, facts=facts
        )
