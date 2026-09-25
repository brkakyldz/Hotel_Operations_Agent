"""Maintenance reports: one atomic task + audit + receipt per operation key.

A maintenance issue keeps its own domain meaning (category + free-text
description), not a housekeeping quantity. Reporting never marks anything
repaired and never changes the room (``out_of_service``, housekeeping state).
The description is untrusted guest text: it is stored and displayed as data,
not echoed back to the model as an instruction and never interpreted as policy.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.ids import new_id
from hotel_operations.services import audit
from hotel_operations.services.housekeeping import invalid_argument, not_checked_in
from hotel_operations.services.sessions import SessionBinding, revalidate_binding
from hotel_operations.services.writes import (
    WriteScope,
    find_replay,
    normalize_text,
    record_receipt,
    replayed_result,
)
from hotel_operations.storage.models import Hotel, HotelPolicy, MaintenanceTask, Room

TOOL_NAME = "create_maintenance_task"
MAX_DESCRIPTION = 500


def maintenance_payload(category: str, description: str) -> dict[str, object]:
    """The canonical payload the operation key is computed over."""
    return {"category": category, "description": normalize_text(description)}


def create_maintenance_task(
    session: Session,
    binding: SessionBinding,
    scope: WriteScope,
    *,
    category: str,
    description: str,
) -> dict[str, Any]:
    payload = maintenance_payload(category, description)
    key, digest, existing = find_replay(session, scope, TOOL_NAME, payload)
    if existing is not None:
        return replayed_result(existing)

    reservation = revalidate_binding(session, binding)
    if reservation.status != "checked_in":
        raise not_checked_in()
    policy = session.get(HotelPolicy, binding.hotel_id)
    if policy is None:
        raise errors.DomainError(
            "POLICY_UNAVAILABLE", "Maintenance policy is unavailable.", http_status=503
        )
    if category not in policy.maintenance_categories:
        raise invalid_argument(f"Unsupported maintenance category: {category}.")
    clean = payload["description"]
    if not isinstance(clean, str) or not clean:
        raise invalid_argument("Describe the issue in 1 to 500 characters.")
    if len(clean) > MAX_DESCRIPTION:
        raise invalid_argument("The description must be at most 500 characters.")

    room = session.get(Room, binding.room_id)
    hotel = session.get(Hotel, binding.hotel_id)
    if room is None or hotel is None:
        raise errors.data_unavailable()
    now = utcnow()
    task = MaintenanceTask(
        id=new_id("mt"),
        hotel_id=binding.hotel_id,
        guest_id=binding.guest_id,
        reservation_id=binding.reservation_id,
        room_id=binding.room_id,
        category=category,
        description=clean,
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
        entity_type="maintenance_task",
        entity_id=task.id,
        tool_name=TOOL_NAME,
        outcome="pending",
        # The free text stays in the task row only; audit keeps the typed facts.
        payload={"category": category, "description_length": len(clean)},
    )
    result = {
        "task_id": task.id,
        "kind": "maintenance",
        "room_number": room.number,
        "category": category,
        "status": "pending",
        "status_meaning": "Reported to simulated staff; not yet repaired.",
        "repair_time_promised": False,
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
        artifact_type="maintenance_task",
        artifact_id=task.id,
    )
    return {**result, "receipt_id": receipt.id, "replayed": False}
