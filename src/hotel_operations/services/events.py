"""Sanitized audit-event projection for the operator surface.

The audit log is append-only evidence of what happened. This view exposes event
metadata and an allow-listed subset of payload keys, so it cannot leak guest free
text, tokens, capability hashes or prompts even if a payload ever carried them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local
from hotel_operations.storage.models import AuditEvent, Hotel

PAGE_SIZE = 50
SAFE_PAYLOAD_KEYS = frozenset(
    {
        "after", "approval_version", "before", "category", "description_length",
        "duration_ms", "human_decision", "item", "latency_ms", "prompt_version",
        "proposal_number", "quantity", "reason_provided", "requested_check_in_local",
        "requested_checkout_local", "requested_checkout_utc", "reservation_version",
        "task_version", "topic", "total_tokens",
    }
)  # fmt: skip
SAFE_SUMMARY_KEYS = frozenset(
    {
        "category", "decision", "item", "outcome", "quantity", "reason_codes",
        "requested_check_in_local", "requested_checkout_local", "replayed",
        "reservation_version", "status", "task_id", "topic",
    }
)  # fmt: skip


def _safe_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if key in SAFE_PAYLOAD_KEYS and isinstance(value, str | int | float | bool | None):
            out[key] = value
        elif key == "summary" and isinstance(value, dict):
            out["summary"] = {
                k: v
                for k, v in value.items()
                if k in SAFE_SUMMARY_KEYS
                and (
                    isinstance(v, str | int | float | bool | None)
                    or (
                        isinstance(v, list)
                        and all(isinstance(x, str | int | float | bool) for x in v)
                    )
                )
            }
    return out


def list_events(
    session: Session, hotel_id: str, before_id: int | None, limit: int = PAGE_SIZE
) -> dict[str, Any]:
    if not 1 <= limit <= PAGE_SIZE:
        raise errors.DomainError("INVALID_ARGUMENT", "limit must be 1..50.", http_status=422)
    hotel = session.get(Hotel, hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    query = select(AuditEvent).where(AuditEvent.hotel_id == hotel_id)
    if before_id is not None:
        query = query.where(AuditEvent.id < before_id)
    rows = list(session.scalars(query.order_by(AuditEvent.id.desc()).limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    return {
        "events": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "actor_type": e.actor_type,
                "run_id": e.run_id,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "tool_name": e.tool_name,
                "outcome": e.outcome,
                "reason_codes": list(e.reason_codes or []),
                "wall_time": iso(e.wall_time),
                "demo_time": iso(to_local(e.demo_time, hotel.timezone)) if e.demo_time else None,
                "payload": _safe_payload(e.payload),
            }
            for e in page
        ],
        "next_before_id": page[-1].id if more and page else None,
    }
