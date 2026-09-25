"""Session-scoped run projections for the browser (sanitized, no hidden reasoning)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local
from hotel_operations.services.hotel_updates import title
from hotel_operations.services.receipts import run_artifacts
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.storage.models import (
    HOTEL_UPDATE_RECORDED_ORDER,
    AuditEvent,
    DemoSession,
    Hotel,
    HotelUpdate,
    RunRecord,
)

TOOL_EVENT_TYPES = ("tool_proposed", "tool_completed", "tool_rejected", "tool_failed")


def get_own_run(session: Session, binding: SessionBinding, run_id: str) -> RunRecord:
    """Unknown runs and runs of other sessions are indistinguishable (404)."""
    run = session.get(RunRecord, run_id)
    if run is None or run.session_id != binding.session_id:
        raise errors.not_found("Run")
    return run


def tool_activity(session: Session, run_id: str) -> list[dict[str, Any]]:
    """Actual recorded tool events for a run, merged per tool call, in audit order."""
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for ev in session.scalars(
        select(AuditEvent)
        .where(AuditEvent.run_id == run_id, AuditEvent.event_type.in_(TOOL_EVENT_TYPES))
        .order_by(AuditEvent.id)
    ):
        key = ev.tool_call_id or f"event-{ev.id}"
        if key not in calls:
            order.append(key)
            calls[key] = {
                "tool_call_id": ev.tool_call_id,
                "tool_name": ev.tool_name,
                "status": "proposed",
                "proposed_at": iso(ev.wall_time),
                "finished_at": None,
                "outcome": None,
                "error_code": None,
                "entity_type": None,
                "entity_id": None,
                "summary": {},
                "duration_ms": None,
            }
        entry = calls[key]
        if ev.event_type == "tool_completed":
            entry.update(
                status="completed",
                finished_at=iso(ev.wall_time),
                outcome=ev.outcome,
                entity_type=ev.entity_type,
                entity_id=ev.entity_id,
                summary=ev.payload.get("summary", {}),
                duration_ms=ev.payload.get("duration_ms"),
            )
        elif ev.event_type in ("tool_rejected", "tool_failed"):
            entry.update(
                status="rejected" if ev.event_type == "tool_rejected" else "failed",
                finished_at=iso(ev.wall_time),
                outcome="error",
                error_code=ev.reason_codes[0] if ev.reason_codes else None,
                duration_ms=ev.payload.get("duration_ms"),
            )
    return [calls[k] for k in order]


def _hotel_updates(session: Session, run: RunRecord) -> list[dict[str, Any]]:
    """What a hotel-initiated turn was about: kind, the transcript's title and the
    hotel time it happened. The model's own input is never shown."""
    if run.initiated_by != "hotel":
        return []
    demo_session = session.get(DemoSession, run.session_id)
    hotel = None if demo_session is None else session.get(Hotel, demo_session.hotel_id)
    if hotel is None:
        return []
    rows = session.scalars(
        select(HotelUpdate)
        .where(HotelUpdate.run_id == run.id)
        .order_by(*HOTEL_UPDATE_RECORDED_ORDER)
    )
    return [
        {
            "kind": u.kind,
            "title": title(u),
            "demo_time": iso(to_local(u.demo_time, hotel.timezone)),
        }
        for u in rows
    ]


def project_run(session: Session, run: RunRecord) -> dict[str, Any]:
    artifacts = run_artifacts(session, run.id)
    error = None
    if run.error_code:
        error = {
            "code": run.error_code,
            "message": run.error_message,
            "retryable": run.error_code
            in {
                "PROVIDER_TIMEOUT",
                "PROVIDER_RATE_LIMITED",
                "PROVIDER_UNAVAILABLE",
                "PROVIDER_ERROR",
                "RUN_DEADLINE_EXCEEDED",
                "STORAGE_UNAVAILABLE",
            },
        }
    outcome_detail = None
    if run.status in ("failed", "interrupted") and artifacts:
        outcome_detail = "action_committed_response_failed"
    usage = run.usage or None
    return {
        "run_id": run.id,
        "client_request_id": run.client_request_id,
        "status": run.status,
        # "hotel": the hotel started this turn to tell the guest something; then
        # user_message is the update's title, not words the guest wrote.
        "initiated_by": run.initiated_by,
        "hotel_updates": _hotel_updates(session, run),
        "user_message": run.user_message,
        "final_message": run.final_message,
        "error": error,
        "outcome_detail": outcome_detail,
        "activity": tool_activity(session, run.id),
        "artifacts": artifacts,
        # Whole conversational context restarted (rotation fallback / process death).
        "conversation_restarted": run.conversation_abandoned,
        # Only this failed turn was removed from the agent's memory; earlier turns remain.
        "turn_forgotten": run.status in ("failed", "interrupted")
        and not run.conversation_abandoned,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "model_id": run.model_id,
        "latency_ms": run.latency_ms,
        "usage": None
        if usage is None
        else {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "model_calls": usage.get("recorded_calls"),
            "complete": usage.get("complete"),
        },
    }


def session_run_ids(session: Session, session_id: str) -> list[str]:
    return list(
        session.scalars(
            select(RunRecord.id)
            .where(RunRecord.session_id == session_id)
            .order_by(RunRecord.created_at, RunRecord.id)
        )
    )
