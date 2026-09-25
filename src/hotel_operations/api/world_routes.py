"""Operator world-control routes.

The same operator side as the review queue (no credential, one local user).
No model or provider call happens in a command, and the agent has no tool that reaches
these services. After a command commits, the hotel updates it caused start as agent
turns.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from hotel_operations import telemetry
from hotel_operations.api.operator_routes import Operator
from hotel_operations.api.routes import Services
from hotel_operations.fixtures import HOTEL_ID
from hotel_operations.services import world

router = APIRouter(prefix="/api/operator/world")


class EventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Annotated[str, Field(min_length=1, max_length=40)]
    client_request_id: uuid.UUID


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True]


class ClockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    advance_minutes: int = Field(ge=1, le=600)
    client_request_id: uuid.UUID


@router.get("")
def world_state(operator: Operator, services: Services) -> dict[str, Any]:
    with services.db.read() as s:
        return world.world_state(s, HOTEL_ID)


@router.post("/events")
async def apply_event(body: EventBody, operator: Operator, services: Services) -> dict[str, Any]:
    def command() -> dict[str, Any]:
        with services.db.write() as s:
            return world.apply_event(
                s,
                hotel_id=HOTEL_ID,
                operator_id=operator,
                key=body.event,
                client_request_id=str(body.client_request_id),
            )

    result = await asyncio.to_thread(command)
    telemetry.emit("world_event", key=result["key"], replayed=result["replayed"])
    await services.run_manager.deliver_hotel_updates()
    return result


@router.post("/clock")
async def advance_clock(body: ClockBody, operator: Operator, services: Services) -> dict[str, Any]:
    def command() -> dict[str, Any]:
        with services.db.write() as s:
            return world.advance_clock(
                s,
                hotel_id=HOTEL_ID,
                operator_id=operator,
                minutes=body.advance_minutes,
                client_request_id=str(body.client_request_id),
            )

    result = await asyncio.to_thread(command)
    telemetry.emit("world_clock", key=result["key"], replayed=result["replayed"])
    await services.run_manager.deliver_hotel_updates()
    return result


@router.post("/reset")
async def reset_world(body: ResetBody, operator: Operator, services: Services) -> dict[str, Any]:
    """Start over from fixture v1: the guided scenarios' "Start over".

    Destructive, like the CLI reset it runs: every conversation,
    request, decision, world event and clock move is discarded, runs in flight end as
    interrupted, and every guest session token stops working.
    """
    await services.reset_world()
    with services.db.read() as s:
        return world.world_state(s, HOTEL_ID)
