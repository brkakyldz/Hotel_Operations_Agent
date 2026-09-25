"""Housekeeping requests: one atomic task + audit + receipt per operation key.

Runs inside the caller's ``BEGIN IMMEDIATE`` transaction. Scope, current state
and business rules are checked here, inside the transaction, not only in the
tool wrapper. Housekeeping policy v1 (``domain.housekeeping``) decides each
request from current facts: a refusal or an already-open cleaning is recorded
as an audit event with no task and no receipt, as a checkout denial is. A
pending task is not a delivery and does not change the room's housekeeping
state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.domain import housekeeping as rules_v1
from hotel_operations.domain.checkout import parse_local_time
from hotel_operations.ids import new_id
from hotel_operations.services import audit, tasks
from hotel_operations.services.sessions import SessionBinding, revalidate_binding
from hotel_operations.services.writes import (
    WriteScope,
    find_replay,
    normalize_text,
    record_receipt,
    replayed_result,
)
from hotel_operations.storage.models import Hotel, HotelPolicy, HousekeepingTask, Room

TOOL_NAME = "create_housekeeping_task"


def not_checked_in() -> errors.DomainError:
    return errors.DomainError(
        "NOT_CHECKED_IN",
        "In-room requests are available only after check-in.",
        http_status=409,
    )


def invalid_argument(message: str) -> errors.DomainError:
    return errors.DomainError("INVALID_ARGUMENT", message, http_status=422)


def policy_unavailable() -> errors.DomainError:
    return errors.DomainError(
        "POLICY_UNAVAILABLE", "Housekeeping policy is unavailable.", http_status=503
    )


REASON_TEXT = {
    rules_v1.OUTSIDE_CLEANING_HOURS: "room cleaning is only accepted during cleaning hours",
    rules_v1.STAY_LIMIT_REACHED: "that would exceed this stay's limit for the item",
    rules_v1.ALREADY_REQUESTED: "a room cleaning request is already open for this stay",
}


def load_rules(policy: HotelPolicy) -> rules_v1.HousekeepingRules:
    """Policy v1's housekeeping values; missing ones fail closed, never a guessed default."""
    start, end = policy.room_cleaning_from_local, policy.room_cleaning_until_local
    towels, pillows = policy.towels_per_stay, policy.pillows_per_stay
    if start is None or end is None or towels is None or pillows is None:
        raise policy_unavailable()
    return rules_v1.HousekeepingRules(
        cleaning_from=parse_local_time(start),
        cleaning_until=parse_local_time(end),
        towels_per_stay=towels,
        pillows_per_stay=pillows,
    )


def load_facts(session: Session, reservation_id: str, hotel: Hotel) -> rules_v1.HousekeepingFacts:
    """This stay's housekeeping so far, read inside the caller's transaction."""
    open_cleaning = session.scalar(
        select(HousekeepingTask.id)
        .where(
            HousekeepingTask.reservation_id == reservation_id,
            HousekeepingTask.item == "room_cleaning",
            HousekeepingTask.status.in_(("pending", "in_progress")),
        )
        .order_by(HousekeepingTask.created_at)
    )
    used: dict[str, int] = {
        str(item): int(total or 0)
        for item, total in session.execute(
            select(HousekeepingTask.item, func.sum(HousekeepingTask.quantity))
            .where(
                HousekeepingTask.reservation_id == reservation_id,
                HousekeepingTask.status != "cancelled",
            )
            .group_by(HousekeepingTask.item)
        ).all()
    }
    return rules_v1.HousekeepingFacts(
        now_local=to_local(hotel.demo_now, hotel.timezone),
        open_cleaning_task_id=open_cleaning,
        towels_this_stay=used.get("towels", 0),
        pillows_this_stay=used.get("pillows", 0),
    )


def create_housekeeping_task(
    session: Session,
    binding: SessionBinding,
    scope: WriteScope,
    *,
    item: str,
    quantity: int,
    notes: str | None,
) -> dict[str, Any]:
    qty = int(quantity)
    clean_notes = normalize_text(notes)
    payload: dict[str, object] = {"item": item, "quantity": qty, "notes": clean_notes}
    key, digest, existing = find_replay(session, scope, TOOL_NAME, payload)
    if existing is not None:
        return replayed_result(existing)

    reservation = revalidate_binding(session, binding)
    if reservation.status != "checked_in":
        raise not_checked_in()
    policy = session.get(HotelPolicy, binding.hotel_id)
    if policy is None:
        raise policy_unavailable()
    if item not in policy.housekeeping_items:
        raise invalid_argument(f"Unsupported housekeeping item: {item}.")
    if not 1 <= qty <= policy.housekeeping_max_quantity:
        raise invalid_argument("Quantity must be between 1 and 6.")
    if item == "room_cleaning" and qty != 1:
        raise invalid_argument("Room cleaning quantity must be 1.")
    if clean_notes is not None and len(clean_notes) > 300:
        raise invalid_argument("Notes must be at most 300 characters.")

    room = session.get(Room, binding.room_id)
    hotel = session.get(Hotel, binding.hotel_id)
    if room is None or hotel is None:
        raise errors.data_unavailable()
    decision = rules_v1.decide(
        item, qty, load_rules(policy), load_facts(session, binding.reservation_id, hotel)
    )
    if decision.decision != "accepted":
        return _not_created(session, binding, scope, hotel, room, item, qty, decision)
    now = utcnow()
    task = HousekeepingTask(
        id=new_id("hk"),
        hotel_id=binding.hotel_id,
        guest_id=binding.guest_id,
        reservation_id=binding.reservation_id,
        room_id=binding.room_id,
        item=item,
        quantity=qty,
        notes=clean_notes,
        status="pending",
        originating_run_id=scope.run_id,
        version=1,
        created_at=now,
    )
    session.add(task)
    session.flush()
    audit.append(
        session,
        hotel_id=binding.hotel_id,
        event_type="task_created",
        actor_type="agent",
        actor_id=binding.guest_id,
        session_id=scope.session_id,
        run_id=scope.run_id,
        entity_type="housekeeping_task",
        entity_id=task.id,
        tool_name=TOOL_NAME,
        outcome="pending",
        payload={"item": item, "quantity": task.quantity},
    )
    result = {
        "task_id": task.id,
        "kind": "housekeeping",
        "room_number": room.number,
        "item": item,
        "quantity": task.quantity,
        "notes": task.notes,
        "outcome": "pending",
        "status": "pending",
        "status_meaning": "Recorded for simulated staff; not yet delivered.",
        "remaining_this_stay": decision.remaining_this_stay,
        # Real UTC record time; the frozen hotel clock is reported separately.
        "created_at": iso(now),
        "demo_time": iso(to_local(hotel.demo_now, hotel.timezone)),
    }
    receipt = record_receipt(
        session,
        key=key,
        digest=digest,
        scope=scope,
        tool_name=TOOL_NAME,
        result=result,
        artifact_type="housekeeping_task",
        artifact_id=task.id,
    )
    return {**result, "receipt_id": receipt.id, "replayed": False}


def _not_created(
    session: Session,
    binding: SessionBinding,
    scope: WriteScope,
    hotel: Hotel,
    room: Room,
    item: str,
    quantity: int,
    decision: rules_v1.HousekeepingDecision,
) -> dict[str, Any]:
    """A refusal or an already-open cleaning: an audited decision, no task and no receipt."""
    existing = (
        session.get(HousekeepingTask, decision.existing_task_id)
        if decision.existing_task_id
        else None
    )
    audit.append(
        session,
        hotel_id=binding.hotel_id,
        event_type="housekeeping_evaluated",
        actor_type="agent",
        actor_id=binding.guest_id,
        session_id=scope.session_id,
        run_id=scope.run_id,
        entity_type="housekeeping_task" if existing else None,
        entity_id=existing.id if existing else None,
        tool_name=TOOL_NAME,
        outcome=decision.decision,
        reason_codes=list(decision.reason_codes),
        payload={"item": item, "quantity": quantity},
    )
    next_local = decision.next_available_local
    return {
        "kind": "housekeeping",
        "outcome": decision.decision,
        "room_number": room.number,
        "item": item,
        "quantity": quantity,
        "reason_codes": list(decision.reason_codes),
        "reasons": [REASON_TEXT[c] for c in decision.reason_codes],
        "next_available_local": None if next_local is None else iso(next_local),
        "remaining_this_stay": decision.remaining_this_stay,
        "existing_task_id": existing.id if existing else None,
        "existing_status": existing.status if existing else None,
        "nothing_created": True,
        "demo_time": iso(to_local(hotel.demo_now, hotel.timezone)),
        "receipt_id": None,
        "replayed": False,
    }


def close_cleaning_window(
    session: Session, hotel: Hotel, before: datetime, after: datetime
) -> list[HousekeepingTask]:
    """When the hotel clock passes the end of cleaning hours, room cleanings nobody has
    started are cancelled by the system (SERVICE_WINDOW_CLOSED). Started ones and
    towel or pillow requests are left to staff. The clock never crosses a day in the demo."""
    policy = session.get(HotelPolicy, hotel.id)
    if policy is None or policy.room_cleaning_until_local is None:
        return []
    until = parse_local_time(policy.room_cleaning_until_local)
    start, end = to_local(before, hotel.timezone), to_local(after, hotel.timezone)
    if not (start.date() == end.date() and start.time() < until <= end.time()):
        return []
    closed = list(
        session.scalars(
            select(HousekeepingTask)
            .where(
                HousekeepingTask.hotel_id == hotel.id,
                HousekeepingTask.item == "room_cleaning",
                HousekeepingTask.status == "pending",
            )
            .order_by(HousekeepingTask.created_at, HousekeepingTask.id)
        )
    )
    for task in closed:
        tasks.system_cancel(session, task, rules_v1.SERVICE_WINDOW_CLOSED)
    return closed
