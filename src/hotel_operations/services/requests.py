"""Reservation-scoped request projections: housekeeping, maintenance and review requests.

Scope always comes from the session binding; the opaque cursor only positions
within that scope and cannot widen it.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.services import approvals
from hotel_operations.services.sessions import SessionBinding, revalidate_binding
from hotel_operations.storage.models import (
    Approval,
    Hotel,
    HousekeepingTask,
    MaintenanceTask,
    OperationReceipt,
    Room,
)

PAGE_SIZE = 20
# The model's read of the same list is smaller and clips free text, so its result stays well
# inside the tool projection bound however many requests a stay collects.
TOOL_PAGE_SIZE = 10
TOOL_TEXT_LIMIT = 120

STATUS_MEANINGS = {
    "pending": "Recorded for simulated staff; not yet done.",
    "in_progress": "Simulated staff are working on it.",
    "completed": "Marked completed in the simulator.",
    "cancelled": "Cancelled.",
}

APPROVAL_STATUS_MEANINGS = {
    "pending": "Waiting for manager review; checkout not changed yet.",
    "executed": "Approved by a manager; checkout changed.",
    "rejected": "Declined by a manager; checkout not changed.",
    "expired": "Expired before a decision; checkout not changed.",
    "stale": "Hotel data changed before a decision; checkout not changed. Ask again if needed.",
}

MAINTENANCE_STATUS_MEANINGS = {
    "pending": "Reported to simulated staff; not yet repaired.",
    "in_progress": "Simulated staff are working on it.",
    "completed": "Marked completed in the simulator.",
    "cancelled": "Cancelled.",
}


def invalid_cursor() -> errors.DomainError:
    return errors.DomainError("INVALID_CURSOR", "The cursor is not valid.", http_status=422)


def encode_cursor(created_at: datetime, item_id: str) -> str:
    raw = json.dumps({"t": created_at.isoformat(), "id": item_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    if len(cursor) > 200:
        raise invalid_cursor()
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        created = datetime.fromisoformat(data["t"])
        item_id = str(data["id"])
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise invalid_cursor() from exc
    if created.tzinfo is None or len(item_id) > 40:
        raise invalid_cursor()
    return created, item_id


def _clip(text: str | None, limit: int | None) -> str | None:
    if text is None or limit is None or len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _items(
    session: Session, binding: SessionBinding, text_limit: int | None
) -> list[dict[str, Any]]:
    hotel = session.get(Hotel, binding.hotel_id)
    assert hotel is not None
    rooms = {r.id: r.number for r in session.scalars(select(Room))}
    receipts = {
        r.artifact_id: r.id
        for r in session.scalars(
            select(OperationReceipt).where(
                OperationReceipt.reservation_id == binding.reservation_id
            )
        )
    }
    out: list[dict[str, Any]] = []
    for task in session.scalars(
        select(HousekeepingTask).where(
            HousekeepingTask.reservation_id == binding.reservation_id,
            HousekeepingTask.hotel_id == binding.hotel_id,
        )
    ):
        out.append(
            {
                "kind": "housekeeping",
                "id": task.id,
                "status": task.status,
                "status_meaning": STATUS_MEANINGS[task.status],
                "created_at": iso(task.created_at),  # real UTC record time
                "_sort": (task.created_at, task.id),
                "room_number": rooms.get(task.room_id),
                "receipt_id": receipts.get(task.id),
                "details": {
                    "item": task.item,
                    "quantity": task.quantity,
                    "notes": _clip(task.notes, text_limit),
                },
                # Why the system moved it (e.g. SERVICE_WINDOW_CLOSED); null for staff moves.
                "status_reason": task.status_reason,
            }
        )
    for issue in session.scalars(
        select(MaintenanceTask).where(
            MaintenanceTask.reservation_id == binding.reservation_id,
            MaintenanceTask.hotel_id == binding.hotel_id,
        )
    ):
        out.append(
            {
                "kind": "maintenance",
                "id": issue.id,
                "status": issue.status,
                "status_meaning": MAINTENANCE_STATUS_MEANINGS[issue.status],
                "created_at": iso(issue.created_at),  # real UTC record time
                "_sort": (issue.created_at, issue.id),
                "room_number": rooms.get(issue.room_id),
                "receipt_id": receipts.get(issue.id),
                # The description is the guest's own untrusted text, returned as data.
                "details": {
                    "category": issue.category,
                    "guest_description": _clip(issue.description, text_limit),
                },
            }
        )
    for approval in session.scalars(
        select(Approval).where(
            Approval.reservation_id == binding.reservation_id,
            Approval.hotel_id == binding.hotel_id,
        )
    ):
        view = approvals.guest_view(approval, hotel.timezone, hotel.demo_now)
        out.append(
            {
                "kind": "late_checkout_review",
                "id": approval.id,
                "status": view["status"],  # effective: a lapsed pending one reads as expired
                "status_meaning": APPROVAL_STATUS_MEANINGS[view["status"]],
                "created_at": view["created_at"],
                "_sort": (approval.created_at, approval.id),
                "room_number": rooms.get(approval.room_id),
                "receipt_id": receipts.get(approval.id),
                "details": {
                    "requested_checkout_local": view["requested_checkout_local"],
                    "expires_at": view["expires_at"],
                    "human_decision": view["human_decision"],
                },
                # Guest-safe codes only (e.g. EXPIRED_BEFORE_DECISION, CHANGED:operational).
                "status_reason": approval.status_reason,
            }
        )
    return out


def list_my_requests(
    session: Session,
    binding: SessionBinding,
    cursor: str | None,
    *,
    page_size: int = PAGE_SIZE,
    text_limit: int | None = None,
) -> dict[str, Any]:
    revalidate_binding(session, binding)
    hotel = session.get(Hotel, binding.hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    items = sorted(_items(session, binding, text_limit), key=lambda i: i["_sort"], reverse=True)
    if cursor:
        after = decode_cursor(cursor)
        items = [i for i in items if i["_sort"] < after]
    page, rest = items[:page_size], items[page_size:]
    next_cursor = encode_cursor(*page[-1]["_sort"]) if rest and page else None
    for item in page:
        item.pop("_sort")
    return {
        "data": {"requests": page, "next_cursor": next_cursor},
        "meta": {
            "observed_at": iso(utcnow()),
            "demo_time": iso(to_local(hotel.demo_now, hotel.timezone)),
        },
    }
