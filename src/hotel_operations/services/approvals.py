"""Durable human-review requests for late checkout.

An Approval is created or reused inside the guest's request transaction; no
reservation changes. Before the one-pending rule is enforced for a new intent,
expired or version-invalid pending approvals are transitioned (with audit) so the
slot is released without a scheduler. Expiry is the hotel clock: a review
lapses once ``Hotel.demo_now`` reaches the checkout time it asks for, because it can
never execute after that. Real elapsed time expires nothing. Reads report the
effective status without writing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, utcnow
from hotel_operations.domain import checkout as policy
from hotel_operations.ids import new_id
from hotel_operations.services import audit
from hotel_operations.services.writes import payload_hash
from hotel_operations.storage.models import Approval

if TYPE_CHECKING:  # the checkout service imports this module
    from hotel_operations.services.checkout import CheckoutContext

ACTION_LATE_CHECKOUT = "late_checkout"


def approval_payload(requested: datetime) -> dict[str, Any]:
    """The immutable, hashed payload: what would be executed, nothing else."""
    return {
        "action_type": ACTION_LATE_CHECKOUT,
        "requested_checkout_utc": requested.astimezone(UTC).isoformat(),
    }


def requested_time(approval: Approval) -> datetime:
    return datetime.fromisoformat(str(approval.immutable_payload["requested_checkout_utc"]))


def requested_local(approval: Approval, tz_name: str) -> str | None:
    """Display value; None when a stored payload is unreadable (integrity checks fail it)."""
    from hotel_operations.clock import to_local

    try:
        return iso(to_local(requested_time(approval), tz_name))
    except (KeyError, TypeError, ValueError):
        return None


def lapsed(approval: Approval, demo_now: datetime) -> bool:
    """The hotel clock has reached the requested time: this review can never execute."""
    return demo_now >= approval.expires_at


def effective_status(approval: Approval, demo_now: datetime) -> str:
    """A pending approval the hotel clock has overtaken is expired, even before it is persisted."""
    if approval.status == "pending" and lapsed(approval, demo_now):
        return "expired"
    return approval.status


def changed_versions(observed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    keys = sorted(set(observed) | set(current))
    return [k for k in keys if observed.get(k) != current.get(k)]


def transition(
    session: Session,
    approval: Approval,
    status: str,
    *,
    reason: str | None,
    actor_type: str,
    actor_id: str | None,
    event_type: str,
    decision: str | None = None,
    now: datetime | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Move a pending approval to a terminal status; never overwrites a human decision."""
    assert approval.status == "pending", "only pending approvals transition"
    approval.status = status
    approval.status_reason = reason
    approval.version += 1
    if decision is not None:
        approval.human_decision = decision
        approval.decided_by = actor_id
        approval.decided_at = now or utcnow()
    audit.append(
        session,
        hotel_id=approval.hotel_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        session_id=session_id,
        run_id=run_id,
        entity_type="approval",
        entity_id=approval.id,
        outcome=status,
        reason_codes=None if reason is None else [reason],
        payload={"approval_version": approval.version, "human_decision": approval.human_decision},
    )


def pending_for(session: Session, reservation_id: str) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval).where(
                Approval.reservation_id == reservation_id, Approval.status == "pending"
            )
        )
    )


def release_invalid_pending(
    session: Session,
    reservation_id: str,
    current_versions: dict[str, Any],
    *,
    demo_now: datetime,
    actor_id: str,
    run_id: str,
    session_id: str,
) -> None:
    """Persist expiry/staleness of pending approvals before enforcing one-pending.

    ``demo_now`` is the hotel business clock: once it has reached a pending request's
    time, that request can never execute, so it no longer holds the slot.
    """
    for approval in pending_for(session, reservation_id):
        if lapsed(approval, demo_now):
            transition(
                session, approval, "expired", reason="EXPIRED_BEFORE_DECISION",
                actor_type="system", actor_id=actor_id, event_type="approval_expired",
                run_id=run_id, session_id=session_id,
            )  # fmt: skip
        elif changed := changed_versions(approval.observed_versions, current_versions):
            transition(
                session, approval, "stale", reason="CHANGED:" + ",".join(changed),
                actor_type="system", actor_id=actor_id, event_type="approval_stale",
                run_id=run_id, session_id=session_id,
            )  # fmt: skip
    session.flush()


def pending_conflict(existing: Approval, tz_name: str) -> errors.DomainError:
    from hotel_operations.clock import to_local

    when = to_local(requested_time(existing), tz_name).strftime("%H:%M")
    return errors.DomainError(
        "PENDING_APPROVAL_CONFLICT",
        f"A different late-checkout request (until {when}) is already waiting for manager "
        "review. It must be decided or expire before a different time can be requested.",
        http_status=409,
    )


def create_or_reuse(
    session: Session,
    *,
    hotel_id: str,
    guest_id: str,
    reservation_id: str,
    room_id: str,
    tz_name: str,
    requested: datetime,
    guest_reason: str | None,
    current_versions: dict[str, Any],
    run_id: str,
    session_id: str,
    demo_now: datetime,
) -> tuple[Approval, bool]:
    """Return ``(approval, reused)``. Raises PENDING_APPROVAL_CONFLICT for a different payload."""
    now = utcnow()
    release_invalid_pending(
        session, reservation_id, current_versions,
        demo_now=demo_now, actor_id=guest_id, run_id=run_id, session_id=session_id,
    )  # fmt: skip
    payload = approval_payload(requested)
    digest = payload_hash(payload)
    for existing in pending_for(session, reservation_id):
        if existing.payload_hash == digest:
            return existing, True
        raise pending_conflict(existing, tz_name)
    approval = Approval(
        id=new_id("apr"),
        hotel_id=hotel_id,
        guest_id=guest_id,
        reservation_id=reservation_id,
        room_id=room_id,
        action_type=ACTION_LATE_CHECKOUT,
        immutable_payload=payload,
        payload_hash=digest,
        observed_versions=dict(current_versions),
        guest_reason=guest_reason,
        status="pending",
        version=1,
        originating_run_id=run_id,
        created_at=now,
        expires_at=requested,  # on the hotel clock
    )
    session.add(approval)
    session.flush()
    audit.append(
        session,
        hotel_id=hotel_id,
        event_type="approval_requested",
        actor_type="agent",
        actor_id=guest_id,
        session_id=session_id,
        run_id=run_id,
        entity_type="approval",
        entity_id=approval.id,
        tool_name="request_late_checkout",
        outcome="pending",
        payload={"requested_checkout_utc": payload["requested_checkout_utc"]},
    )
    return approval, False


def guest_view(approval: Approval, tz_name: str, demo_now: datetime) -> dict[str, Any]:
    from hotel_operations.clock import to_local

    status = effective_status(approval, demo_now)
    return {
        "approval_id": approval.id,
        "status": status,
        "requested_checkout_local": requested_local(approval, tz_name),
        "created_at": iso(approval.created_at),  # real UTC time
        # Hotel-local: the hotel clock time at which the review lapses.
        "expires_at": iso(to_local(approval.expires_at, tz_name)),
        "human_decision": approval.human_decision,
        "decided_at": iso(approval.decided_at),
    }


def approve_blocker(approval: Approval, ctx: CheckoutContext | None) -> str | None:
    """Why an approve attempt must not execute now, or None when it may.

    The one check an approve performs, shared by the decision itself and every read-only
    preview of it (the review queue, the hotel board). It decides nothing on its own.
    """
    if ctx is None:
        return "SCOPE_OR_DATA_UNAVAILABLE"
    if payload_hash(approval.immutable_payload) != approval.payload_hash:
        return "PAYLOAD_INTEGRITY"
    changed = changed_versions(approval.observed_versions, ctx.versions())
    if changed:
        return "CHANGED:" + ",".join(changed)
    try:
        decided = policy.evaluate(requested_time(approval), ctx.rules, ctx.facts)
    except errors.DomainError as exc:
        # E.g. the demo clock moved past the requested time (INVALID_TIME): never executable,
        # and a 4xx here would roll back and leave the review pending forever.
        return "NO_LONGER_ELIGIBLE:" + exc.code
    if decided.decision not in ("approval_required", "auto_allowed"):
        return "NO_LONGER_ELIGIBLE:" + ",".join(decided.reason_codes)
    return None


def expire_lapsed(session: Session, hotel_id: str, demo_now: datetime) -> list[Approval]:
    """The hotel clock's own move persists every pending review it has overtaken.

    Reads already report such a review as expired (``effective_status``); this makes it a
    recorded fact at the moment it happens, so the guest can be told. Actor ``system``.
    """
    expired: list[Approval] = []
    for approval in session.scalars(
        select(Approval)
        .where(Approval.hotel_id == hotel_id, Approval.status == "pending")
        .order_by(Approval.created_at, Approval.id)
    ):
        if lapsed(approval, demo_now):
            transition(
                session, approval, "expired", reason="EXPIRED_BEFORE_DECISION",
                actor_type="system", actor_id=None, event_type="approval_expired",
            )  # fmt: skip
            expired.append(approval)
    session.flush()
    return expired
