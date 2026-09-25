"""Operator review queue and decisions.

Operator commands arrive only through the operator routes, never from a body field,
a guest token or model output. Approve and execute happen in one ``BEGIN
IMMEDIATE`` transaction: there is no approved-but-unexecuted state. A decision
runs without any model/provider call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.ids import new_id
from hotel_operations.services import approvals, checkout, hotel_updates, world
from hotel_operations.services.writes import canonical_json, payload_hash
from hotel_operations.storage.models import (
    Approval,
    Guest,
    Hotel,
    OperationReceipt,
    OperatorCommand,
    Reservation,
    Room,
)

PAGE_SIZE = 20
STATUS_FILTERS = ("pending", "executed", "rejected", "expired", "stale", "all")


@dataclass(frozen=True)
class ApprovalScope:
    """The reservation scope recorded on an approval (satisfies ReservationScope)."""

    hotel_id: str
    guest_id: str
    reservation_id: str
    room_id: str


def _scope(approval: Approval) -> ApprovalScope:
    return ApprovalScope(
        approval.hotel_id, approval.guest_id, approval.reservation_id, approval.room_id
    )


def _hotel(session: Session, approval: Approval) -> Hotel:
    hotel = session.get(Hotel, approval.hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    return hotel


def not_found() -> errors.DomainError:
    return errors.DomainError("APPROVAL_NOT_FOUND", "No such approval.", http_status=404)


def operator_view(session: Session, approval: Approval, now: datetime) -> dict[str, Any]:
    """Staff projection: current snapshot next to the observed one, and whether it is fresh."""
    hotel = session.get(Hotel, approval.hotel_id)
    guest = session.get(Guest, approval.guest_id)
    room = session.get(Room, approval.room_id)
    reservation = session.get(Reservation, approval.reservation_id)
    assert hotel is not None
    tz = hotel.timezone
    status = approvals.effective_status(approval, hotel.demo_now)
    current: dict[str, Any] | None = None
    fresh: bool | None = None
    changed: list[str] | None = None
    blocking: str | None = None
    if status == "pending":
        ctx: checkout.CheckoutContext | None
        try:
            ctx = checkout.load_context(session, _scope(approval))
        except errors.DomainError:
            ctx = None
        if ctx is not None:
            current = ctx.versions()
            changed = approvals.changed_versions(approval.observed_versions, current)
            fresh = not changed
        else:
            fresh = False
        # A read-only preview of the check an approve performs; it decides nothing.
        blocking = _prevented(approval, ctx)
    return {
        "approval_id": approval.id,
        "status": status,
        "version": approval.version,
        "action_type": approval.action_type,
        "guest_name": guest.display_name if guest else None,
        "room_number": room.number if room else None,
        "reservation_reference": reservation.reference if reservation else None,
        "requested_checkout_local": approvals.requested_local(approval, tz),
        "current_checkout_local": (
            iso(to_local(reservation.scheduled_checkout, tz)) if reservation else None
        ),
        "guest_reason": approval.guest_reason,
        "created_at": iso(approval.created_at),
        # The hotel-clock time at which a pending review lapses.
        "expires_at": iso(to_local(approval.expires_at, tz)),
        "observed_versions": approval.observed_versions,
        "current_versions": current,
        "fresh": fresh,
        # Which freshness components changed, what an approve would find, and the
        # simulated world changes recorded since the request.
        "changed_components": changed,
        "blocking_reason": blocking,
        "world_changes": (
            world.changes_since(
                session, approval.hotel_id, approval.created_at, room.number if room else None
            )
            if status in ("pending", "stale")
            else []
        ),
        "human_decision": approval.human_decision,
        "decided_by": approval.decided_by,
        "decided_at": iso(approval.decided_at),
        "status_reason": approval.status_reason,
        "execution_receipt_id": approval.execution_receipt_id,
    }


def list_approvals(
    session: Session, hotel_id: str, status: str, cursor: str | None
) -> dict[str, Any]:
    if status not in STATUS_FILTERS:
        raise errors.DomainError("INVALID_ARGUMENT", "Unknown status filter.", http_status=422)
    from hotel_operations.services.requests import decode_cursor, encode_cursor

    now = utcnow()
    hotel = session.get(Hotel, hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    rows = list(
        session.scalars(
            select(Approval)
            .where(Approval.hotel_id == hotel_id)
            .order_by(Approval.created_at.desc(), Approval.id.desc())
        )
    )
    if status != "all":
        rows = [a for a in rows if approvals.effective_status(a, hotel.demo_now) == status]
    if cursor:
        after = decode_cursor(cursor)
        rows = [a for a in rows if (a.created_at, a.id) < after]
    page, rest = rows[:PAGE_SIZE], rows[PAGE_SIZE:]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if rest and page else None
    return {
        "approvals": [operator_view(session, a, now) for a in page],
        "next_cursor": next_cursor,
        "observed_at": iso(now),
    }


def _body_hash(approval_id: str, decision: str, expected_version: int) -> str:
    return payload_hash(
        {"approval_id": approval_id, "decision": decision, "expected_version": expected_version}
    )


def _execution_receipt(
    session: Session, approval: Approval, operator_id: str, client_request_id: str, result: Any
) -> OperationReceipt:
    key = payload_hash({"operator": operator_id, "request": client_request_id, "id": approval.id})
    receipt = OperationReceipt(
        id=new_id("rcp"),
        operation_key=key,
        hotel_id=approval.hotel_id,
        reservation_id=approval.reservation_id,
        session_id=None,
        run_id=None,
        client_request_id=client_request_id,
        tool_name="operator_approve",
        payload_hash=approval.payload_hash,
        result=result,
        artifact_type="checkout_change",
        artifact_id=approval.reservation_id,
        committed_at=utcnow(),
    )
    session.add(receipt)
    session.flush()
    return receipt


def _prevented(approval: Approval, ctx: checkout.CheckoutContext | None) -> str | None:
    """Why an approve attempt must not execute, or None when it may."""
    return approvals.approve_blocker(approval, ctx)


def decide(
    session: Session,
    *,
    hotel_id: str,
    operator_id: str,
    approval_id: str,
    decision: str,
    expected_version: int,
    client_request_id: str,
) -> dict[str, Any]:
    body_hash = _body_hash(approval_id, decision, expected_version)
    prior = session.scalar(
        select(OperatorCommand).where(
            OperatorCommand.operator_id == operator_id,
            OperatorCommand.client_request_id == client_request_id,
        )
    )
    if prior is not None:
        if prior.body_hash != body_hash:
            raise errors.DomainError(
                "IDEMPOTENCY_CONFLICT",
                "This client_request_id was already used with a different decision.",
                http_status=409,
            )
        return {**prior.result, "replayed": True}

    approval = session.get(Approval, approval_id)
    if approval is None or approval.hotel_id != hotel_id:
        raise not_found()
    if approval.version != expected_version:
        raise errors.DomainError(
            "VERSION_CONFLICT",
            f"The approval changed (now version {approval.version}); reload and decide again.",
            http_status=409,
        )
    if approval.status != "pending":
        raise errors.DomainError(
            "APPROVAL_NOT_PENDING",
            f"This approval is already {approval.status}; it cannot be decided again.",
            http_status=409,
        )

    now = utcnow()
    receipt_id: str | None = None

    def mark(status: str, reason: str | None, event_type: str) -> None:
        approvals.transition(
            session, approval, status, reason=reason, actor_type="operator",
            actor_id=operator_id, event_type=event_type, decision=decision, now=now,
        )  # fmt: skip

    if decision == "reject":
        mark("rejected", None, "approval_decided")
    elif approvals.lapsed(approval, _hotel(session, approval).demo_now):
        mark("expired", "EXPIRED_BEFORE_DECISION", "approval_expired")
    else:
        try:
            ctx: checkout.CheckoutContext | None = checkout.load_context(session, _scope(approval))
        except errors.DomainError:
            ctx = None
        reason = _prevented(approval, ctx)
        if reason is not None or ctx is None:
            mark("stale", reason, "approval_stale")
        else:
            before, after = checkout.apply_checkout_change(
                session,
                ctx,
                approvals.requested_time(approval),
                actor_type="operator",
                actor_id=operator_id,
                reason_codes=["MANAGER_APPROVED"],
                session_id=None,
                run_id=None,
                extra={"approval_id": approval.id},
            )
            mark("executed", None, "approval_decided")
            receipt = _execution_receipt(
                session,
                approval,
                operator_id,
                client_request_id,
                {
                    "approval_id": approval.id,
                    "outcome": "applied",
                    "previous_checkout_local": before,
                    "current_checkout_local": after,
                },
            )
            approval.execution_receipt_id = receipt.id
            receipt_id = receipt.id
    session.flush()
    hotel_updates.review_closed(session, _hotel(session, approval), approval)
    result = {
        "approval": operator_view(session, approval, now),
        "decision": decision,
        "outcome": approval.status,
        "applied": approval.status == "executed",
        "receipt_id": receipt_id,
        "replayed": False,
    }
    # JSON round-trip so the stored command result equals what a replay returns.
    stored = _jsonable(result)
    session.add(
        OperatorCommand(
            id=new_id("opc"),
            operator_id=operator_id,
            client_request_id=client_request_id,
            body_hash=body_hash,
            approval_id=approval.id,
            result=stored,
            created_at=now,
        )
    )
    session.flush()
    return stored


def _jsonable(value: dict[str, Any]) -> dict[str, Any]:
    import json

    loaded: dict[str, Any] = json.loads(canonical_json(value))
    return loaded
