"""Hotel-initiated agent turns: delivering what the gate recorded.

The gate (``services.hotel_updates``) decided that a guest should hear about something.
Here that becomes one agent run in the guest's conversation, admitted like a guest's
message but started by the hotel:

- Admission happens in one short write right after an operator, task, world or clock
  command commits, and inside every finished run's terminal write, when a session or the
  global capacity frees up. There is no dispatcher and nothing polls.
- All of a reservation's pending updates go into one run, in its newest open session.
  The run's model input is a developer-role message built from the update rows (enums,
  ids and times); the transcript shows only a short title.
- The run gets the read tools only, and the gateway refuses a write anyway.
- One attempt: an admitted update is ``sent`` for good. A failed run shows its own error
  and is not retried. Updates still pending when the server starts are skipped, never
  replayed.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.agent.admission import admission_blocker, input_hash
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.config import Settings
from hotel_operations.ids import new_id
from hotel_operations.services import audit
from hotel_operations.services.hotel_updates import title
from hotel_operations.services.sessions import binding_of, revalidate_binding
from hotel_operations.storage.models import (
    HOTEL_UPDATE_RECORDED_ORDER,
    DemoSession,
    Hotel,
    HotelUpdate,
    RunRecord,
)

# Skip reasons: the update is recorded, but no agent turn will carry it.
NO_PROVIDER = "NO_PROVIDER"
NO_CONVERSATION = "NO_CONVERSATION"
SERVER_RESTARTED = "SERVER_RESTARTED"


def developer_text(updates: list[HotelUpdate], hotel: Hotel) -> str:
    """The model input for a hotel turn: what happened, as recorded, and nothing else."""
    events = [
        {
            "kind": u.kind,
            "hotel_time": iso(to_local(u.demo_time, hotel.timezone)),
            "facts": u.facts,
        }
        for u in updates
    ]
    return (
        "HOTEL UPDATE from the hotel system (the guest did not write anything). "
        "Follow your Hotel updates instructions.\n" + json.dumps(events, ensure_ascii=False)
    )


def _target(session: Session, reservation_id: str) -> DemoSession | None:
    """The reservation's newest open session: the conversation the guest is looking at."""
    return session.scalar(
        select(DemoSession)
        .where(
            DemoSession.reservation_id == reservation_id,
            DemoSession.closed_at.is_(None),
        )
        .order_by(DemoSession.created_at.desc(), DemoSession.id.desc())
        .limit(1)
    )


def _skip(updates: list[HotelUpdate], reason: str) -> None:
    for u in updates:
        u.status, u.skip_reason = "skipped", reason


def deliver_pending(session: Session, settings: Settings, *, provider_available: bool) -> list[str]:
    """Admit one hotel run per reservation with pending updates, where possible now.

    Called inside a write transaction; returns the admitted run ids for the caller to
    schedule after commit. A session that is busy (or a full demo) keeps its updates
    pending for the next terminal write.
    """
    groups: dict[str, list[HotelUpdate]] = {}
    for update in session.scalars(
        select(HotelUpdate)
        .where(HotelUpdate.status == "pending")
        .order_by(*HOTEL_UPDATE_RECORDED_ORDER)
    ):
        groups.setdefault(update.reservation_id, []).append(update)
    admitted: list[str] = []
    for reservation_id, updates in groups.items():
        if not provider_available:
            _skip(updates, NO_PROVIDER)
            continue
        demo_session = _target(session, reservation_id)
        if demo_session is None:
            _skip(updates, NO_CONVERSATION)
            continue
        try:
            revalidate_binding(session, binding_of(demo_session))
        except errors.DomainError:
            _skip(updates, NO_CONVERSATION)
            continue
        if admission_blocker(session, settings, demo_session) is not None:
            continue  # a run is still going; the next terminal write starts this turn
        admitted.append(_admit(session, demo_session, updates))
    session.flush()
    return admitted


def _admit(session: Session, demo_session: DemoSession, updates: list[HotelUpdate]) -> str:
    shown = "; ".join(title(u) for u in updates)
    run = RunRecord(
        id=new_id("run"),
        session_id=demo_session.id,
        client_request_id=updates[0].id,  # unique per update, 36 characters
        input_hash=input_hash(shown),
        user_message=shown,
        status="queued",
        conversation_key=demo_session.conversation_key,
        created_at=utcnow(),
        artifact_refs=[],
        initiated_by="hotel",
    )
    session.add(run)
    demo_session.accepted_turn_count += 1
    session.flush()
    for u in updates:
        u.status, u.run_id = "sent", run.id
    audit.append(
        session,
        hotel_id=demo_session.hotel_id,
        event_type="request_accepted",
        actor_type="system",
        session_id=demo_session.id,
        run_id=run.id,
        outcome="queued",
        payload={"initiated_by": "hotel", "updates": [u.kind for u in updates]},
    )
    return run.id


def skip_undelivered(session: Session) -> int:
    """Startup: an update the previous process never delivered is not replayed now."""
    rows = list(session.scalars(select(HotelUpdate).where(HotelUpdate.status == "pending")))
    _skip(rows, SERVER_RESTARTED)
    return len(rows)


def run_updates(session: Session, run_id: str) -> list[HotelUpdate]:
    return list(
        session.scalars(
            select(HotelUpdate)
            .where(HotelUpdate.run_id == run_id)
            .order_by(*HOTEL_UPDATE_RECORDED_ORDER)
        )
    )
