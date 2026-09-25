"""Append-only audit events (business evidence), written through services only."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from hotel_operations.clock import utcnow
from hotel_operations.storage.models import AuditEvent, Hotel


def append(
    session: Session,
    *,
    hotel_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str | None = None,
    session_id: str | None = None,
    run_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    outcome: str | None = None,
    reason_codes: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    wall_time: datetime | None = None,
) -> AuditEvent:
    hotel = session.get(Hotel, hotel_id)
    event = AuditEvent(
        hotel_id=hotel_id,
        session_id=session_id,
        run_id=run_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        outcome=outcome,
        reason_codes=reason_codes or [],
        payload=payload or {},
        wall_time=wall_time or utcnow(),
        demo_time=hotel.demo_now if hotel else None,
    )
    session.add(event)
    session.flush()
    return event
