"""Late-checkout evaluation and requests, with a durable human review above 14:00.

Both entry points load the current reservation, room, policy, room-day plan and
next arrival and decide with the same pure function. The write reloads and
re-decides inside its ``BEGIN IMMEDIATE`` transaction; an earlier evaluation or
anything the model says is never execution authority. Another guest's identity
or reservation never appears in a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.domain import checkout as policy
from hotel_operations.services import approvals, audit
from hotel_operations.services.sessions import (
    ReservationScope,
    SessionBinding,
    revalidate_binding,
)
from hotel_operations.services.writes import (
    WriteScope,
    find_replay,
    normalize_text,
    record_receipt,
    replayed_result,
)
from hotel_operations.storage.models import Hotel, HotelPolicy, Reservation, Room, RoomDayPlan

TOOL_NAME = "request_late_checkout"
MAX_REASON = 300
ACTIVE_STATUSES = ("confirmed", "checked_in")

OUTCOME_BY_DECISION = {
    "auto_allowed": "applied",
    # A durable, immutable Approval is created or reused; the checkout is unchanged.
    "approval_required": "pending_approval",
    "denied": "denied",
    "no_change": "no_change",
}

# Templates filled from the stored policy, never from hard-coded thresholds.
REASON_TEXT = {
    policy.ALREADY_GRANTED: "Your checkout is already at or after the requested time.",
    policy.WITHIN_AUTOMATIC_WINDOW: "Within the automatic extension window (until {auto}).",
    policy.REQUIRES_MANAGER_REVIEW: (
        "Between {auto} and {review} an extension needs manager review."
    ),
    policy.AFTER_LATEST_EXTENSION: "Checkout cannot be extended beyond {review}.",
    policy.ROOM_OUT_OF_SERVICE: "The room is currently out of service.",
    policy.CLEANING_WINDOW_EXCEEDED: "Cleaning could not finish within today's cleaning window.",
    policy.NEXT_ARRIVAL_CONFLICT: "The room must be cleaned and ready for its next arrival.",
}


@dataclass(frozen=True)
class CheckoutContext:
    hotel: Hotel
    reservation: Reservation
    room: Room
    policy_row: HotelPolicy
    rules: policy.CheckoutRules
    facts: policy.CheckoutFacts
    plan: RoomDayPlan | None
    tz: ZoneInfo

    def versions(self) -> dict[str, int | None]:
        return {
            "reservation": self.reservation.version,
            "room": self.room.version,
            "policy": self.policy_row.version,
            "room_day_plan": None if self.plan is None else self.plan.version,
            "operational": self.room.operational_version,
        }


def _rules(row: HotelPolicy | None) -> policy.CheckoutRules:
    fields = (
        None
        if row is None
        else (
            row.standard_checkout_local,
            row.auto_extension_until_local,
            row.reviewed_extension_until_local,
            row.turnaround_minutes,
            row.default_housekeeping_finish_local,
        )
    )
    if fields is None or any(v is None for v in fields):
        raise policy.operational_data_unavailable("The checkout policy is unavailable.")
    standard, auto_until, review_until, turnaround, finish = fields
    assert standard and auto_until and review_until and finish and turnaround is not None
    return policy.CheckoutRules(
        standard=policy.parse_local_time(standard),
        auto_until=policy.parse_local_time(auto_until),
        review_until=policy.parse_local_time(review_until),
        turnaround=timedelta(minutes=turnaround),
        default_finish_by=policy.parse_local_time(finish),
    )


def _next_arrival(session: Session, reservation: Reservation) -> datetime | None:
    """Nearest non-cancelled later reservation for the same room; overlaps fail closed."""
    others = list(
        session.scalars(
            select(Reservation)
            .where(
                Reservation.hotel_id == reservation.hotel_id,
                Reservation.room_id == reservation.room_id,
                Reservation.id != reservation.id,
                Reservation.status.in_(ACTIVE_STATUSES),
                Reservation.scheduled_check_in >= reservation.scheduled_check_in,
            )
            .order_by(Reservation.scheduled_check_in, Reservation.id)
        )
    )
    for other in others:
        if (
            other.status == "checked_in"
            or other.scheduled_check_in < reservation.scheduled_checkout
        ):
            raise policy.operational_data_unavailable(
                "The room's upcoming reservations are inconsistent; checkout cannot be evaluated."
            )
    return others[0].scheduled_check_in if others else None


def load_context(session: Session, binding: ReservationScope) -> CheckoutContext:
    reservation = revalidate_binding(session, binding)
    hotel = session.get(Hotel, binding.hotel_id)
    room = session.get(Room, binding.room_id)
    if hotel is None or room is None:
        raise errors.data_unavailable()
    row = session.get(HotelPolicy, binding.hotel_id)
    rules = _rules(row)
    assert row is not None
    tz = ZoneInfo(hotel.timezone)
    departure_day = to_local(reservation.scheduled_checkout, hotel.timezone).date()
    plan = session.scalar(
        select(RoomDayPlan).where(
            RoomDayPlan.room_id == room.id,
            RoomDayPlan.hotel_id == hotel.id,
            RoomDayPlan.local_date == departure_day.isoformat(),
        )
    )
    facts = policy.CheckoutFacts(
        tz=tz,
        demo_now=hotel.demo_now,
        reservation_status=reservation.status,
        current_checkout=reservation.scheduled_checkout,
        room_in_service=not room.out_of_service,
        ready_after=None if plan is None else plan.housekeeping_ready_after,
        finish_by=None if plan is None else plan.housekeeping_finish_by,
        next_arrival=_next_arrival(session, reservation)
        if reservation.status == "checked_in"
        else None,
    )
    return CheckoutContext(hotel, reservation, room, row, rules, facts, plan, tz)


def _local(value: datetime | None, tz: ZoneInfo) -> str | None:
    return None if value is None else value.astimezone(tz).isoformat()


def _decision_view(ctx: CheckoutContext, decided: policy.CheckoutDecision) -> dict[str, Any]:
    """Privacy-safe projection: times and codes only, never the next guest or reservation."""
    return {
        "requested_checkout_local": _local(decided.requested, ctx.tz),
        "decision": decided.decision,
        "reason_codes": list(decided.reason_codes),
        "reasons": [
            REASON_TEXT[c].format(
                auto=ctx.policy_row.auto_extension_until_local,
                review=ctx.policy_row.reviewed_extension_until_local,
            )
            for c in decided.reason_codes
        ],
        "room_ready_deadline_local": _local(decided.room_ready_deadline, ctx.tz),
        "latest_feasible_checkout_local": _local(decided.latest_feasible, ctx.tz),
        "policy": {
            "standard_checkout_local": ctx.policy_row.standard_checkout_local,
            "automatic_extension_until_local": ctx.policy_row.auto_extension_until_local,
            "reviewed_extension_until_local": ctx.policy_row.reviewed_extension_until_local,
            "turnaround_minutes": ctx.policy_row.turnaround_minutes,
        },
    }


def _meta(ctx: CheckoutContext) -> dict[str, Any]:
    return {
        "observed_at": iso(utcnow()),
        "demo_time": iso(to_local(ctx.hotel.demo_now, ctx.hotel.timezone)),
        "versions": ctx.versions(),
    }


def evaluate_late_checkout(
    session: Session, binding: SessionBinding, requested_checkout_local: str
) -> dict[str, Any]:
    """Advisory evaluation: an observation, never a token authorizing a later write."""
    ctx = load_context(session, binding)
    requested = policy.parse_requested(requested_checkout_local, ctx.tz)
    decided = policy.evaluate(requested, ctx.rules, ctx.facts)
    data = {
        **_decision_view(ctx, decided),
        "current_checkout_local": _local(ctx.reservation.scheduled_checkout, ctx.tz),
        "advisory": True,
    }
    return {"data": data, "meta": _meta(ctx)}


def request_payload(requested: datetime, reason: str | None) -> dict[str, object]:
    """Canonical write payload: UTC-equivalent timestamp and normalized reason."""
    return {
        "requested_checkout_utc": requested.astimezone(UTC).isoformat(),
        "reason": normalize_text(reason),
    }


def apply_checkout_change(
    session: Session,
    ctx: CheckoutContext,
    requested: datetime,
    *,
    actor_type: str,
    actor_id: str,
    reason_codes: list[str],
    session_id: str | None,
    run_id: str | None,
    extra: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Extend the checkout with version bumps and audit; returns ``(before, after)`` local."""
    reservation = ctx.reservation
    previous = reservation.scheduled_checkout
    reservation.scheduled_checkout = requested.astimezone(UTC)
    reservation.version += 1
    ctx.room.operational_version += 1  # this room's availability changed
    session.flush()
    before, after = _local(previous, ctx.tz), _local(reservation.scheduled_checkout, ctx.tz)
    audit.append(
        session,
        hotel_id=ctx.hotel.id,
        event_type="checkout_changed",
        actor_type=actor_type,
        actor_id=actor_id,
        session_id=session_id,
        run_id=run_id,
        entity_type="reservation",
        entity_id=reservation.id,
        tool_name=TOOL_NAME if actor_type == "agent" else None,
        outcome="applied",
        reason_codes=reason_codes,
        payload={
            "before": before,
            "after": after,
            "reservation_version": reservation.version,
            **(extra or {}),
        },
    )
    return before, after


def request_late_checkout(
    session: Session,
    binding: SessionBinding,
    scope: WriteScope,
    *,
    requested_checkout_local: str,
    reason: str | None,
) -> dict[str, Any]:
    clean_reason = normalize_text(reason)
    if clean_reason is not None and len(clean_reason) > MAX_REASON:
        raise errors.DomainError(
            "INVALID_ARGUMENT", "The reason must be at most 300 characters.", http_status=422
        )
    # The hotel timezone is needed to validate the offset before the operation key exists.
    hotel = session.get(Hotel, binding.hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    requested = policy.parse_requested(requested_checkout_local, ZoneInfo(hotel.timezone))
    key, digest, existing = find_replay(
        session, scope, TOOL_NAME, request_payload(requested, clean_reason)
    )
    if existing is not None:
        return replayed_result(existing)

    ctx = load_context(session, binding)
    decided = policy.evaluate(requested, ctx.rules, ctx.facts)
    outcome = OUTCOME_BY_DECISION[decided.decision]
    reservation = ctx.reservation
    result: dict[str, Any] = {
        **_decision_view(ctx, decided),
        "outcome": outcome,
        "approval_id": None,
    }
    if outcome == "pending_approval":
        approval, reused = approvals.create_or_reuse(
            session,
            hotel_id=binding.hotel_id,
            guest_id=binding.guest_id,
            reservation_id=reservation.id,
            room_id=binding.room_id,
            tz_name=ctx.hotel.timezone,
            requested=requested,
            guest_reason=clean_reason,
            current_versions=ctx.versions(),
            run_id=scope.run_id,
            session_id=scope.session_id,
            demo_now=ctx.hotel.demo_now,
        )
        result.update(
            approval_id=approval.id,
            approval_status="pending",
            approval_reused=reused,
            approval_expires_at=iso(approval.expires_at),
            current_checkout_local=_local(reservation.scheduled_checkout, ctx.tz),
            note=(
                "A manager must review this request. It is pending, not approved; the checkout "
                "is unchanged unless a manager approves it before that time arrives on the "
                "hotel clock."
            ),
        )
        receipt = record_receipt(
            session,
            key=key,
            digest=digest,
            scope=scope,
            tool_name=TOOL_NAME,
            result=result,
            artifact_type="approval",
            artifact_id=approval.id,
        )
        return {**result, "receipt_id": receipt.id, "replayed": False}

    if outcome != "applied":
        audit.append(
            session,
            hotel_id=binding.hotel_id,
            event_type="checkout_evaluated",
            actor_type="agent",
            actor_id=binding.guest_id,
            session_id=scope.session_id,
            run_id=scope.run_id,
            entity_type="reservation",
            entity_id=reservation.id,
            tool_name=TOOL_NAME,
            outcome=outcome,
            reason_codes=list(decided.reason_codes),
            payload={"requested_checkout_local": result["requested_checkout_local"]},
        )
        result["current_checkout_local"] = _local(reservation.scheduled_checkout, ctx.tz)
        return {**result, "receipt_id": None, "replayed": False}

    # Auto-allowed: extend atomically with version bumps, audit and receipt.
    before, after = apply_checkout_change(
        session,
        ctx,
        requested,
        actor_type="agent",
        actor_id=binding.guest_id,
        reason_codes=list(decided.reason_codes),
        session_id=scope.session_id,
        run_id=scope.run_id,
        extra={"reason_provided": clean_reason is not None},
    )
    result["previous_checkout_local"] = before
    result["current_checkout_local"] = after
    result["reservation_version"] = reservation.version
    receipt = record_receipt(
        session,
        key=key,
        digest=digest,
        scope=scope,
        tool_name=TOOL_NAME,
        result=result,
        artifact_type="checkout_change",
        artifact_id=reservation.id,
    )
    return {**result, "receipt_id": receipt.id, "replayed": False}
