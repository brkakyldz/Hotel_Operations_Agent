"""Early check-in evaluation and requests.

Mirrors the late-checkout service. Both entry points load the current
reservation, room, policy and any stay still in the room, and decide with the
same pure function. The write reloads and re-decides inside its ``BEGIN
IMMEDIATE`` transaction; an earlier evaluation or anything the model says is never
execution authority. v1 has no review path. Another guest's identity, reservation
or checkout time never appears in a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.domain import checkout as checkout_policy
from hotel_operations.domain import early_check_in as policy
from hotel_operations.services import audit
from hotel_operations.services.sessions import (
    ReservationScope,
    SessionBinding,
    revalidate_binding,
)
from hotel_operations.services.writes import (
    WriteScope,
    find_replay,
    record_receipt,
    replayed_result,
)
from hotel_operations.storage.models import Hotel, HotelPolicy, Reservation, Room

TOOL_NAME = "request_early_check_in"

OUTCOME_BY_DECISION = {"auto_allowed": "applied", "denied": "denied", "no_change": "no_change"}

# Templates filled from the stored policy, never from hard-coded thresholds.
REASON_TEXT = {
    policy.ALREADY_ALLOWED: "Your check-in is already allowed from that time or earlier.",
    policy.EARLY_CHECK_IN_AVAILABLE: (
        "Your room is ready, and early check-in is available from {earliest}."
    ),
    policy.BEFORE_EARLIEST_CHECK_IN: "Early check-in is not available before {earliest}.",
    policy.ROOM_OUT_OF_SERVICE: "The room is currently out of service.",
    policy.ROOM_NOT_READY: "The room is not ready yet.",
    policy.ROOM_STILL_OCCUPIED: "The room is not free yet at that time.",
}


@dataclass(frozen=True)
class CheckInContext:
    hotel: Hotel
    reservation: Reservation
    room: Room
    policy_row: HotelPolicy
    rules: policy.CheckInRules
    facts: policy.CheckInFacts
    tz: ZoneInfo

    def versions(self) -> dict[str, int]:
        return {
            "reservation": self.reservation.version,
            "room": self.room.version,
            "policy": self.policy_row.version,
            "operational": self.room.operational_version,
        }


def _rules(row: HotelPolicy | None) -> policy.CheckInRules:
    standard = None if row is None else row.standard_check_in_local
    earliest = None if row is None else row.early_check_in_from_local
    if standard is None or earliest is None:
        raise checkout_policy.operational_data_unavailable("The check-in policy is unavailable.")
    return policy.CheckInRules(
        standard=checkout_policy.parse_local_time(standard),
        earliest=checkout_policy.parse_local_time(earliest),
    )


def _occupied_until(session: Session, reservation: Reservation) -> datetime | None:
    """Latest scheduled checkout of a stay still checked in to the same room, if any."""
    stays = session.scalars(
        select(Reservation.scheduled_checkout).where(
            Reservation.hotel_id == reservation.hotel_id,
            Reservation.room_id == reservation.room_id,
            Reservation.id != reservation.id,
            Reservation.status == "checked_in",
        )
    )
    return max(stays, default=None)


def load_context(session: Session, binding: ReservationScope) -> CheckInContext:
    reservation = revalidate_binding(session, binding)
    hotel = session.get(Hotel, binding.hotel_id)
    room = session.get(Room, binding.room_id)
    if hotel is None or room is None:
        raise errors.data_unavailable()
    row = session.get(HotelPolicy, binding.hotel_id)
    rules = _rules(row)
    assert row is not None
    facts = policy.CheckInFacts(
        tz=ZoneInfo(hotel.timezone),
        demo_now=hotel.demo_now,
        reservation_status=reservation.status,
        current_check_in=reservation.scheduled_check_in,
        room_in_service=not room.out_of_service,
        housekeeping_state=room.housekeeping_state,
        occupied_until=_occupied_until(session, reservation),
    )
    return CheckInContext(hotel, reservation, room, row, rules, facts, ZoneInfo(hotel.timezone))


def _local(value: datetime | None, tz: ZoneInfo) -> str | None:
    return None if value is None else value.astimezone(tz).isoformat()


def _decision_view(ctx: CheckInContext, decided: policy.CheckInDecision) -> dict[str, Any]:
    """Privacy-safe projection: times and codes only, never another stay in the room."""
    return {
        "requested_check_in_local": _local(decided.requested, ctx.tz),
        "decision": decided.decision,
        "reason_codes": list(decided.reason_codes),
        "reasons": [
            REASON_TEXT[c].format(earliest=ctx.policy_row.early_check_in_from_local)
            for c in decided.reason_codes
        ],
        "earliest_possible_check_in_local": _local(decided.earliest_possible, ctx.tz),
        "policy": {
            "standard_check_in_local": ctx.policy_row.standard_check_in_local,
            "early_check_in_from_local": ctx.policy_row.early_check_in_from_local,
        },
    }


def _meta(ctx: CheckInContext) -> dict[str, Any]:
    return {
        "observed_at": iso(utcnow()),
        "demo_time": iso(to_local(ctx.hotel.demo_now, ctx.hotel.timezone)),
        "versions": ctx.versions(),
    }


def parse_requested(session: Session, binding: ReservationScope, value: str) -> datetime:
    hotel = session.get(Hotel, binding.hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    return checkout_policy.parse_requested(value, ZoneInfo(hotel.timezone), what="check-in")


def evaluate_early_check_in(
    session: Session, binding: SessionBinding, requested_check_in_local: str
) -> dict[str, Any]:
    """Advisory evaluation: an observation, never a token authorizing a later write."""
    requested = parse_requested(session, binding, requested_check_in_local)
    ctx = load_context(session, binding)
    decided = policy.evaluate(requested, ctx.rules, ctx.facts)
    data = {
        **_decision_view(ctx, decided),
        "current_check_in_local": _local(ctx.reservation.scheduled_check_in, ctx.tz),
        "advisory": True,
    }
    return {"data": data, "meta": _meta(ctx)}


def request_payload(requested: datetime) -> dict[str, object]:
    """Canonical write payload: the UTC-equivalent timestamp."""
    return {"requested_check_in_utc": requested.astimezone(UTC).isoformat()}


def request_early_check_in(
    session: Session,
    binding: SessionBinding,
    scope: WriteScope,
    *,
    requested_check_in_local: str,
) -> dict[str, Any]:
    requested = parse_requested(session, binding, requested_check_in_local)
    key, digest, existing = find_replay(session, scope, TOOL_NAME, request_payload(requested))
    if existing is not None:
        return replayed_result(existing)

    ctx = load_context(session, binding)
    decided = policy.evaluate(requested, ctx.rules, ctx.facts)
    outcome = OUTCOME_BY_DECISION[decided.decision]
    reservation = ctx.reservation
    result: dict[str, Any] = {**_decision_view(ctx, decided), "outcome": outcome}

    if outcome != "applied":
        audit.append(
            session,
            hotel_id=binding.hotel_id,
            event_type="check_in_evaluated",
            actor_type="agent",
            actor_id=binding.guest_id,
            session_id=scope.session_id,
            run_id=scope.run_id,
            entity_type="reservation",
            entity_id=reservation.id,
            tool_name=TOOL_NAME,
            outcome=outcome,
            reason_codes=list(decided.reason_codes),
            payload={"requested_check_in_local": result["requested_check_in_local"]},
        )
        result["current_check_in_local"] = _local(reservation.scheduled_check_in, ctx.tz)
        return {**result, "receipt_id": None, "replayed": False}

    # Auto-allowed: move the check-in earlier atomically with version bumps, audit and receipt.
    # The reservation stays "confirmed": arriving at the desk is still a separate step.
    previous = reservation.scheduled_check_in
    reservation.scheduled_check_in = requested.astimezone(UTC)
    reservation.version += 1
    ctx.room.operational_version += 1  # this room's availability changed
    session.flush()
    before, after = _local(previous, ctx.tz), _local(reservation.scheduled_check_in, ctx.tz)
    audit.append(
        session,
        hotel_id=ctx.hotel.id,
        event_type="check_in_changed",
        actor_type="agent",
        actor_id=binding.guest_id,
        session_id=scope.session_id,
        run_id=scope.run_id,
        entity_type="reservation",
        entity_id=reservation.id,
        tool_name=TOOL_NAME,
        outcome="applied",
        reason_codes=list(decided.reason_codes),
        payload={"before": before, "after": after, "reservation_version": reservation.version},
    )
    result.update(
        previous_check_in_local=before,
        current_check_in_local=after,
        reservation_version=reservation.version,
    )
    receipt = record_receipt(
        session,
        key=key,
        digest=digest,
        scope=scope,
        tool_name=TOOL_NAME,
        result=result,
        artifact_type="check_in_change",
        artifact_id=reservation.id,
    )
    return {**result, "receipt_id": receipt.id, "replayed": False}
