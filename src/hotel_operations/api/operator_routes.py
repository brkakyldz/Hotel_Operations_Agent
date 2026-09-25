"""Operator routes.

The human's side of the hotel: review decisions, simulated staff tasks and the audit
trail. A command commits first; then the hotel updates its gate recorded start as
agent turns in the guests' conversations, so a decision never waits on a model.
There is no credential: every running copy has one user on loopback.
The separation is structural. No model tool reaches these routes, a guest session
capability is never read here, and each command is attributed to the one demo
operator in the audit trail.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from hotel_operations import telemetry
from hotel_operations.api.routes import Services
from hotel_operations.fixtures import HOTEL_ID
from hotel_operations.services import events as event_service
from hotel_operations.services import operator as operator_service
from hotel_operations.services import tasks as task_service

OPERATOR_ID = "demo_operator"

router = APIRouter(prefix="/api/operator")


def operator_identity() -> str:
    """The acting operator recorded on every command (audit actor ``operator``)."""
    return OPERATOR_ID


Operator = Annotated[str, Depends(operator_identity)]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    expected_version: int = Field(ge=1, le=1_000_000)
    client_request_id: uuid.UUID


@router.get("/approvals")
def list_approvals(
    operator: Operator,
    services: Services,
    status: Annotated[str, Query(max_length=20)] = "pending",
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    with services.db.read() as s:
        return operator_service.list_approvals(s, HOTEL_ID, status, cursor)


class TaskTransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "complete", "cancel"]
    expected_version: int = Field(ge=1, le=1_000_000)


@router.get("/tasks")
def list_tasks(
    operator: Operator,
    services: Services,
    status: Annotated[str, Query(max_length=20)] = "open",
) -> dict[str, Any]:
    """Housekeeping and maintenance tasks for simulated staff."""
    with services.db.read() as s:
        return task_service.list_tasks(s, HOTEL_ID, status)


@router.post("/tasks/{kind}/{task_id}/transition")
async def transition_task(
    kind: Literal["housekeeping", "maintenance"],
    task_id: str,
    body: TaskTransitionBody,
    operator: Operator,
    services: Services,
) -> dict[str, Any]:
    if len(task_id) > 40:
        raise task_service.not_found()

    def command() -> dict[str, Any]:
        with services.db.write() as s:
            return task_service.transition(
                s,
                hotel_id=HOTEL_ID,
                operator_id=operator,
                kind=kind,
                task_id=task_id,
                action=body.action,
                expected_version=body.expected_version,
            )

    task = await asyncio.to_thread(command)
    telemetry.emit("operator_task", kind=kind, action=body.action, status=task["status"])
    await services.run_manager.deliver_hotel_updates()
    return {"task": task}


@router.get("/events")
def list_events(
    operator: Operator,
    services: Services,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> dict[str, Any]:
    """Sanitized audit trail (allow-listed fields only) for inspecting what happened."""
    with services.db.read() as s:
        return event_service.list_events(s, HOTEL_ID, before_id, limit)


@router.post("/approvals/{approval_id}/decision")
async def decide(
    approval_id: str, body: DecisionBody, operator: Operator, services: Services
) -> dict[str, Any]:
    if len(approval_id) > 40:
        raise operator_service.not_found()

    def command() -> dict[str, Any]:
        with services.db.write() as s:
            return operator_service.decide(
                s,
                hotel_id=HOTEL_ID,
                operator_id=operator,
                approval_id=approval_id,
                decision=body.decision,
                expected_version=body.expected_version,
                client_request_id=str(body.client_request_id),
            )

    result = await asyncio.to_thread(command)
    await services.run_manager.deliver_hotel_updates()
    telemetry.emit(
        "operator_decision",
        approval_id=approval_id,
        decision=body.decision,
        outcome=result["outcome"],
        applied=result["applied"],
        replayed=result["replayed"],
    )
    return result
