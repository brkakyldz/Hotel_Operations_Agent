"""Guest and demo routes. JSON bodies reject extra fields."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from hotel_operations import errors, telemetry
from hotel_operations.agent.run_service import admit_run
from hotel_operations.api.state import AppServices, get_services
from hotel_operations.clock import iso
from hotel_operations.config import SIMULATION_LABEL
from hotel_operations.fixtures import HOTEL_ID
from hotel_operations.services import policy as policy_service
from hotel_operations.services import runs as run_views
from hotel_operations.services import sessions as session_service
from hotel_operations.services.requests import list_my_requests
from hotel_operations.services.reservations import read_my_reservation
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.storage.models import DemoSession

router = APIRouter(prefix="/api")

Services = Annotated[AppServices, Depends(get_services)]


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


def current_binding(
    services: Services, token: Annotated[str | None, Depends(bearer_token)]
) -> SessionBinding:
    with services.db.read() as s:
        demo_session = session_service.resolve(s, token)
        return session_service.binding_of(demo_session)


Binding = Annotated[SessionBinding, Depends(current_binding)]


class CreateSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_id: str = Field(min_length=1, max_length=40)


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: uuid.UUID
    message: str = Field(min_length=1, max_length=4000)


@router.get("/health")
def health(services: Services) -> dict[str, Any]:
    return {
        "status": "ok",
        "simulation_label": SIMULATION_LABEL,
        "provider_configured": services.provider_configured(),
    }


@router.get("/demo/guests")
def demo_guests(services: Services) -> dict[str, Any]:
    with services.db.read() as s:
        return {"guests": session_service.list_selectable_guests(s)}


@router.get("/hotel/policy")
def hotel_policy(services: Services) -> dict[str, Any]:
    """The hotel handbook: every policy topic exactly as ``get_hotel_policy`` returns it.

    Hotel-wide information, the same for every guest, so it needs no guest capability.
    """
    with services.db.read() as s:
        return policy_service.handbook(s, HOTEL_ID)


@router.post("/demo/sessions", status_code=201)
def create_session(body: CreateSessionBody, services: Services) -> dict[str, Any]:
    with services.db.write() as s:
        token, demo_session = session_service.create_session(s, body.guest_id)
        binding = session_service.binding_of(demo_session)
        context = session_service.safe_context(s, binding)
    return {
        "session_id": demo_session.id,
        "token": token,
        "context": context,
        "demo_time": context["demo_time"],
        "timezone": context["timezone"],
        "simulation_label": SIMULATION_LABEL,
    }


@router.get("/session")
def read_session(binding: Binding, services: Services) -> dict[str, Any]:
    with services.db.read() as s:
        demo_session = s.get(DemoSession, binding.session_id)
        if demo_session is None:
            raise errors.invalid_session()
        session_service.revalidate_binding(s, binding)
        context = session_service.safe_context(s, binding)
        return {
            "session_id": demo_session.id,
            "accepted_turn_count": demo_session.accepted_turn_count,
            "context_restarted_at": iso(demo_session.context_restarted_at),
            "context": context,
            "demo_time": context["demo_time"],
            "timezone": context["timezone"],
            "simulation_label": SIMULATION_LABEL,
            "run_ids": run_views.session_run_ids(s, demo_session.id),
        }


@router.post("/chat", status_code=202)
async def chat(
    body: ChatBody, request: Request, response: Response, binding: Binding, services: Services
) -> dict[str, Any]:
    if not services.provider_configured():
        raise errors.DomainError(
            "PROVIDER_NOT_CONFIGURED",
            "The language model is not configured (OPENAI_API_KEY missing). No reply can be "
            "generated; this demo never substitutes canned answers.",
            http_status=503,
        )
    run, created = await asyncio.to_thread(
        admit_run,
        services.db,
        services.settings,
        binding,
        str(body.client_request_id),
        body.message,
    )
    if created:
        services.run_manager.schedule(run.id)
    telemetry.emit("run_admitted", run_id=run.id, replayed=not created)
    return {
        "run_id": run.id,
        "status": run.status,
        "poll": f"/api/runs/{run.id}",
        "replayed": not created,
    }


@router.get("/me/requests")
def my_requests(
    binding: Binding,
    services: Services,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> dict[str, Any]:
    """Same reservation-scoped projection as get_my_requests; no scope overrides accepted."""
    with services.db.read() as s:
        data: dict[str, Any] = list_my_requests(s, binding, cursor)["data"]
        return data


@router.get("/me/reservation")
def my_reservation(binding: Binding, services: Services) -> dict[str, Any]:
    """Context-panel refresh through the same scoped read service as get_my_reservation.

    A refresh here is not model tool activity and is not recorded as such.
    """
    with services.db.read() as s:
        read = read_my_reservation(s, binding)
        return {"reservation": read["data"], "meta": read["meta"]}


@router.get("/runs/{run_id}")
def read_run(run_id: str, binding: Binding, services: Services) -> dict[str, Any]:
    if len(run_id) > 64:
        raise errors.not_found("Run")
    with services.db.read() as s:
        run = run_views.get_own_run(s, binding, run_id)
        return run_views.project_run(s, run)


# Seconds between keep-alive comments while a run is quiet, and the coalescing pause that
# folds a burst of text deltas into one snapshot.
SSE_KEEPALIVE_SECONDS = 15.0
SSE_COALESCE_SECONDS = 0.05


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str, request: Request, binding: Binding, services: Services
) -> StreamingResponse:
    """Server-Sent Events: provisional reply text and activity hints for an own run.

    Snapshots are hints; the persisted run stays authoritative. A run that is not executing
    in this process gets a single ``done`` event, and the client reads the persisted run.
    The capability travels in the Authorization header, never in the URL.
    """
    if len(run_id) > 64:
        raise errors.not_found("Run")

    def owned() -> None:
        with services.db.read() as s:
            run_views.get_own_run(s, binding, run_id)

    await asyncio.to_thread(owned)
    channel = services.run_manager.events.get(run_id)

    async def stream() -> AsyncIterator[str]:
        if channel is None:
            yield _sse("done", {"live": False})
            return
        seen = -1
        while True:
            if await request.is_disconnected():
                return
            if channel.version != seen:
                seen = channel.version
                yield _sse("snapshot", channel.snapshot())
                if channel.done:
                    yield _sse("done", {"live": True})
                    return
                await asyncio.sleep(SSE_COALESCE_SECONDS)
                continue
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(SSE_KEEPALIVE_SECONDS):
                    await channel.wait_past(seen)
            if channel.version == seen:
                yield ": keep-alive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
