"""Early check-in policy v1 as a pure function.

Mirrors the late-checkout module: callers load current facts (inside their
transaction for writes) and this function decides. v1 has no review path: an
early check-in is either applied automatically, denied, or needs no change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from hotel_operations import errors
from hotel_operations.domain.checkout import invalid_time

Decision = Literal["auto_allowed", "denied", "no_change"]

# Reason codes (explanatory; the decision is the authority).
ALREADY_ALLOWED = "ALREADY_ALLOWED"
EARLY_CHECK_IN_AVAILABLE = "EARLY_CHECK_IN_AVAILABLE"
BEFORE_EARLIEST_CHECK_IN = "BEFORE_EARLIEST_CHECK_IN"
ROOM_OUT_OF_SERVICE = "ROOM_OUT_OF_SERVICE"
ROOM_NOT_READY = "ROOM_NOT_READY"
ROOM_STILL_OCCUPIED = "ROOM_STILL_OCCUPIED"

READY_STATES = ("clean", "inspected")


def not_arriving() -> errors.DomainError:
    return errors.DomainError(
        "NOT_ARRIVING",
        "Early check-in is available only for a confirmed reservation that has not checked in.",
        http_status=409,
    )


@dataclass(frozen=True)
class CheckInRules:
    standard: time
    earliest: time


@dataclass(frozen=True)
class CheckInFacts:
    tz: ZoneInfo
    demo_now: datetime
    reservation_status: str
    current_check_in: datetime
    room_in_service: bool
    housekeeping_state: str
    # Scheduled checkout of a checked-in stay still in the room, if any (another guest's
    # data: used for the decision only and never returned).
    occupied_until: datetime | None


@dataclass(frozen=True)
class CheckInDecision:
    decision: Decision
    reason_codes: tuple[str, ...]
    requested: datetime
    earliest_possible: datetime | None = None


def _at(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)


def evaluate(requested: datetime, rules: CheckInRules, facts: CheckInFacts) -> CheckInDecision:
    """Decide an early check-in request from current facts. Raises for invalid input."""
    tz = facts.tz
    if facts.reservation_status != "confirmed":
        raise not_arriving()
    arrival_day = facts.current_check_in.astimezone(tz).date()
    today = facts.demo_now.astimezone(tz).date()
    if arrival_day != today:
        raise invalid_time(
            f"Early check-in can be arranged only on the arrival day ({arrival_day})."
        )
    if requested.astimezone(tz).date() != arrival_day:
        raise invalid_time(f"The requested time must be on the arrival date ({arrival_day}).")
    if requested < facts.demo_now:
        raise invalid_time("The requested time has already passed at the current demo time.")
    if requested >= facts.current_check_in:
        # Never move a check-in later and never "grant" what is already allowed.
        return CheckInDecision("no_change", (ALREADY_ALLOWED,), requested)

    earliest_rule = _at(arrival_day, rules.earliest, tz)
    room_ready = facts.room_in_service and facts.housekeeping_state in READY_STATES
    free_from = facts.occupied_until
    codes: list[str] = []
    if requested < earliest_rule:
        codes.append(BEFORE_EARLIEST_CHECK_IN)
    if not facts.room_in_service:
        codes.append(ROOM_OUT_OF_SERVICE)
    elif facts.housekeeping_state not in READY_STATES:
        codes.append(ROOM_NOT_READY)
    if free_from is not None and requested < free_from:
        codes.append(ROOM_STILL_OCCUPIED)

    earliest_possible: datetime | None = None
    if room_ready:
        candidate = max(earliest_rule, facts.demo_now, free_from or earliest_rule)
        if candidate < facts.current_check_in:
            earliest_possible = candidate
    if codes:
        return CheckInDecision("denied", tuple(codes), requested, earliest_possible)
    return CheckInDecision(
        "auto_allowed", (EARLY_CHECK_IN_AVAILABLE,), requested, earliest_possible
    )
