"""Operator task operations: simulated staff move tasks along.

Housekeeping and maintenance tasks are created PENDING by the agent's tools. Only the
operator routes (the Manager tab) can move them on:

    pending -> in_progress -> completed
    pending | in_progress -> cancelled

A move is one ``BEGIN IMMEDIATE`` transaction: an optimistic ``expected_version`` check,
the status change, a version bump and an audit event. No model tool can reach this
module, and nothing here contacts real staff; "completed" means completed in the
simulator.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, utcnow
from hotel_operations.services import audit, hotel_updates
from hotel_operations.storage.models import (
    Guest,
    HousekeepingTask,
    MaintenanceTask,
    Reservation,
    Room,
)

Task = HousekeepingTask | MaintenanceTask
KINDS = ("housekeeping", "maintenance")
ALLOWED_FROM: dict[str, tuple[str, ...]] = {
    "start": ("pending",),
    "complete": ("in_progress",),
    "cancel": ("pending", "in_progress"),
}
TARGET: dict[str, str] = {"start": "in_progress", "complete": "completed", "cancel": "cancelled"}
OPEN_STATUSES = ("pending", "in_progress")
STATUS_FILTERS = ("open", "all", "pending", "in_progress", "completed", "cancelled")
LIST_LIMIT = 100


def not_found() -> errors.DomainError:
    return errors.DomainError("TASK_NOT_FOUND", "No such task.", http_status=404)


def _get(session: Session, kind: str, task_id: str) -> Task | None:
    if kind == "housekeeping":
        return session.get(HousekeepingTask, task_id)
    if kind == "maintenance":
        return session.get(MaintenanceTask, task_id)
    return None


def _all(session: Session, kind: str, hotel_id: str, status: str) -> list[Task]:
    def statuses() -> tuple[str, ...] | None:
        if status == "open":
            return OPEN_STATUSES
        return None if status == "all" else (status,)

    wanted = statuses()
    rows: list[Task] = []
    if kind == "housekeeping":
        hk = select(HousekeepingTask).where(HousekeepingTask.hotel_id == hotel_id)
        if wanted is not None:
            hk = hk.where(HousekeepingTask.status.in_(wanted))
        rows.extend(session.scalars(hk))
    else:
        mt = select(MaintenanceTask).where(MaintenanceTask.hotel_id == hotel_id)
        if wanted is not None:
            mt = mt.where(MaintenanceTask.status.in_(wanted))
        rows.extend(session.scalars(mt))
    return rows


def _details(kind: str, task: Task) -> dict[str, Any]:
    if isinstance(task, HousekeepingTask):
        return {"item": task.item, "quantity": task.quantity, "notes": task.notes}
    # Guest free text: shown to staff as data (rendered as plain text by the UI).
    return {"category": task.category, "guest_description": task.description}


def actions_for(status: str) -> list[str]:
    return [action for action, sources in ALLOWED_FROM.items() if status in sources]


def task_view(session: Session, kind: str, task: Task) -> dict[str, Any]:
    guest = session.get(Guest, task.guest_id)
    room = session.get(Room, task.room_id)
    reservation = session.get(Reservation, task.reservation_id)
    return {
        "kind": kind,
        "task_id": task.id,
        "status": task.status,
        "version": task.version,
        "guest_name": guest.display_name if guest else None,
        "room_number": room.number if room else None,
        "reservation_reference": reservation.reference if reservation else None,
        "details": _details(kind, task),
        "created_at": iso(task.created_at),  # real UTC record time
        "actions": actions_for(task.status),
    }


def list_tasks(session: Session, hotel_id: str, status: str) -> dict[str, Any]:
    if status not in STATUS_FILTERS:
        raise errors.DomainError("INVALID_ARGUMENT", "Unknown status filter.", http_status=422)
    rows: list[tuple[datetime, str, str, Task]] = []
    for kind in KINDS:
        for task in _all(session, kind, hotel_id, status):
            rows.append((task.created_at, task.id, kind, task))
    rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
    return {
        "tasks": [task_view(session, kind, task) for _, _, kind, task in rows[:LIST_LIMIT]],
        "truncated": len(rows) > LIST_LIMIT,
        "observed_at": iso(utcnow()),
    }


def transition(
    session: Session,
    *,
    hotel_id: str,
    operator_id: str,
    kind: str,
    task_id: str,
    action: str,
    expected_version: int,
) -> dict[str, Any]:
    if kind not in KINDS or action not in TARGET:
        raise errors.DomainError(
            "INVALID_ARGUMENT", "Unknown task kind or action.", http_status=422
        )
    task = _get(session, kind, task_id)
    if task is None or task.hotel_id != hotel_id:
        raise not_found()
    if task.version != expected_version:
        raise errors.DomainError(
            "VERSION_CONFLICT",
            f"The task changed (now version {task.version}); reload and try again.",
            http_status=409,
        )
    if task.status not in ALLOWED_FROM[action]:
        raise errors.DomainError(
            "INVALID_TRANSITION",
            f"A {task.status} task cannot be moved with '{action}'.",
            http_status=409,
        )
    before = task.status
    task.status = TARGET[action]
    task.version += 1
    session.flush()
    audit.append(
        session,
        hotel_id=hotel_id,
        event_type="task_status_changed",
        actor_type="operator",
        actor_id=operator_id,
        entity_type=f"{kind}_task",
        entity_id=task.id,
        outcome=task.status,
        payload={"before": before, "after": task.status, "task_version": task.version},
    )
    hotel_updates.task_closed(session, kind, task)  # the guest hears about it
    return task_view(session, kind, task)


def system_cancel(session: Session, task: HousekeepingTask, reason: str) -> None:
    """A rule, not a person, cancels an open task: audited as actor ``system``,
    with the reason kept on the task so the guest's request list can say why."""
    assert task.status in OPEN_STATUSES, "only open tasks are cancelled"
    before = task.status
    task.status = "cancelled"
    task.status_reason = reason
    task.version += 1
    session.flush()
    audit.append(
        session,
        hotel_id=task.hotel_id,
        event_type="task_status_changed",
        actor_type="system",
        entity_type="housekeeping_task",
        entity_id=task.id,
        outcome=task.status,
        reason_codes=[reason],
        payload={"before": before, "after": task.status, "task_version": task.version},
    )
    hotel_updates.task_closed(session, "housekeeping", task)
