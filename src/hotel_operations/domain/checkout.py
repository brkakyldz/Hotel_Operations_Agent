"""Late-checkout policy v1 as a pure function.

No database, clock or model access: callers load current facts (inside their
transaction for writes) and this module decides. The same function serves the
advisory evaluation and the write, so a request is always re-decided from
current state rather than from an earlier evaluation or model output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from hotel_operations import errors

Decision = Literal["auto_allowed", "approval_required", "denied", "no_change"]

# Reason codes (explanatory; the decision is the authority).
ALREADY_GRANTED = "ALREADY_GRANTED"
WITHIN_AUTOMATIC_WINDOW = "WITHIN_AUTOMATIC_WINDOW"
REQUIRES_MANAGER_REVIEW = "REQUIRES_MANAGER_REVIEW"
AFTER_LATEST_EXTENSION = "AFTER_LATEST_EXTENSION"
ROOM_OUT_OF_SERVICE = "ROOM_OUT_OF_SERVICE"
CLEANING_WINDOW_EXCEEDED = "CLEANING_WINDOW_EXCEEDED"
NEXT_ARRIVAL_CONFLICT = "NEXT_ARRIVAL_CONFLICT"

MAX_TIME_TEXT = 40


def invalid_time(message: str) -> errors.DomainError:
    return errors.DomainError("INVALID_TIME", message, http_status=422)


def not_checked_in() -> errors.DomainError:
    return errors.DomainError(
        "NOT_CHECKED_IN", "Checkout changes are available only after check-in.", http_status=409
    )


def operational_data_unavailable(message: str) -> errors.DomainError:
    return errors.DomainError(
        "OPERATIONAL_DATA_UNAVAILABLE", message, http_status=503, retryable=False
    )


@dataclass(frozen=True)
class CheckoutRules:
    standard: time
    auto_until: time
    review_until: time
    turnaround: timedelta
    default_finish_by: time


@dataclass(frozen=True)
class CheckoutFacts:
    tz: ZoneInfo
    demo_now: datetime
    reservation_status: str
    current_checkout: datetime
    room_in_service: bool
    ready_after: datetime | None
    finish_by: datetime | None
    next_arrival: datetime | None


@dataclass(frozen=True)
class CheckoutDecision:
    decision: Decision
    reason_codes: tuple[str, ...]
    requested: datetime
    cleaning_finish: datetime | None = None
    room_ready_deadline: datetime | None = None
    latest_feasible: datetime | None = None


def parse_local_time(value: str) -> time:
    """Parse a stored policy "HH:MM" value."""
    return time.fromisoformat(value)


def parse_requested(value: str, tz: ZoneInfo, what: str = "checkout") -> datetime:
    """Accept only a complete ISO 8601 date-time whose offset is the hotel's own offset.

    UTC-only or foreign-offset input is rejected rather than silently reinterpreted as
    hotel-local time; a bare time or a naive date-time is ambiguous and also rejected.
    """
    text = value.strip()
    if not text or len(text) > MAX_TIME_TEXT:
        raise invalid_time(f"Give the {what} time as a full date and time.")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise invalid_time(
            "Use a complete ISO 8601 date-time with offset, e.g. 2026-09-22T14:00:00+03:00."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise invalid_time("The date-time must include the hotel's UTC offset.")
    local = parsed.astimezone(tz)
    if parsed.utcoffset() != local.utcoffset():
        raise invalid_time(
            f"The offset must be the hotel's local offset ({_offset_text(local)}) for that date."
        )
    return parsed


def _offset_text(value: datetime) -> str:
    offset = value.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _at(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)


@dataclass(frozen=True)
class _Window:
    finish_by: datetime
    deadline: datetime  # the room must be clean by then
    latest: datetime
    feasible: bool


def _window(rules: CheckoutRules, facts: CheckoutFacts, departure_day: date) -> _Window:
    """The departure day's cleaning deadline and the latest checkout that still meets it."""
    if facts.ready_after is None or facts.finish_by is None:
        raise operational_data_unavailable("The room's cleaning plan for today is unavailable.")
    tz = facts.tz
    finish_by = min(facts.finish_by, _at(departure_day, rules.default_finish_by, tz))
    deadline = finish_by if facts.next_arrival is None else min(finish_by, facts.next_arrival)
    latest = min(deadline - rules.turnaround, _at(departure_day, rules.review_until, tz))
    feasible = (
        facts.room_in_service
        and facts.ready_after + rules.turnaround <= deadline
        and latest > facts.current_checkout
    )
    return _Window(finish_by, deadline, latest, feasible)


# Why no later checkout can be granted now (the hotel board's reading, not a request decision).
NOT_DEPARTING_TODAY = "NOT_DEPARTING_TODAY"
NO_LATER_TIME = "NO_LATER_TIME"
TIME_PASSED = "TIME_PASSED"


@dataclass(frozen=True)
class LatestCheckout:
    latest: datetime | None
    needs_review: bool  # the latest time is after the automatic window
    blocked_by: str | None  # a reason code when ``latest`` is None


def latest_checkout(rules: CheckoutRules, facts: CheckoutFacts) -> LatestCheckout:
    """The latest checkout a request made now could get, from the same arithmetic as
    :func:`evaluate`'s ``latest_feasible``, plus the clock: a passed time cannot be asked for.
    Raises like :func:`evaluate` for a guest who is not checked in or missing data."""
    if facts.reservation_status != "checked_in":
        raise not_checked_in()
    tz = facts.tz
    departure_day = facts.current_checkout.astimezone(tz).date()
    if departure_day != facts.demo_now.astimezone(tz).date():
        return LatestCheckout(None, False, NOT_DEPARTING_TODAY)
    window = _window(rules, facts, departure_day)
    if not facts.room_in_service:
        return LatestCheckout(None, False, ROOM_OUT_OF_SERVICE)
    if not window.feasible:
        return LatestCheckout(None, False, NO_LATER_TIME)
    if window.latest <= facts.demo_now:
        return LatestCheckout(None, False, TIME_PASSED)
    auto_until = _at(departure_day, rules.auto_until, tz)
    return LatestCheckout(window.latest, window.latest > auto_until, None)


def evaluate(requested: datetime, rules: CheckoutRules, facts: CheckoutFacts) -> CheckoutDecision:
    """Decide a late-checkout request from current facts. Raises for invalid/unavailable input."""
    tz = facts.tz
    if facts.reservation_status != "checked_in":
        raise not_checked_in()
    departure_day = facts.current_checkout.astimezone(tz).date()
    today = facts.demo_now.astimezone(tz).date()
    local = requested.astimezone(tz)
    if departure_day != today:
        raise invalid_time(
            f"Late checkout can be arranged only on the departure day ({departure_day})."
        )
    if local.date() != departure_day:
        raise invalid_time(f"The requested time must be on the departure date ({departure_day}).")
    if requested <= facts.demo_now:
        raise invalid_time("The requested time has already passed at the current demo time.")
    if requested <= facts.current_checkout:
        # Never shorten an already granted checkout and never ask for approval of it.
        return CheckoutDecision("no_change", (ALREADY_GRANTED,), requested)

    if requested > _at(departure_day, rules.review_until, tz):
        # The hard ceiling needs no operational data and no human can override it.
        return CheckoutDecision("denied", (AFTER_LATEST_EXTENSION,), requested)

    window = _window(rules, facts, departure_day)
    assert facts.ready_after is not None  # _window checked it
    finish_by, deadline = window.finish_by, window.deadline
    cleaning_finish = max(requested, facts.ready_after) + rules.turnaround

    codes: list[str] = []
    if not facts.room_in_service:
        codes.append(ROOM_OUT_OF_SERVICE)
    if cleaning_finish > finish_by:
        codes.append(CLEANING_WINDOW_EXCEEDED)
    if facts.next_arrival is not None and cleaning_finish > facts.next_arrival:
        codes.append(NEXT_ARRIVAL_CONFLICT)
    latest_feasible = window.latest if window.feasible else None

    def decided(decision: Decision, reasons: tuple[str, ...]) -> CheckoutDecision:
        return CheckoutDecision(
            decision, reasons, requested, cleaning_finish, deadline, latest_feasible
        )

    if codes:
        return decided("denied", tuple(codes))
    if requested <= _at(departure_day, rules.auto_until, tz):
        return decided("auto_allowed", (WITHIN_AUTOMATIC_WINDOW,))
    return decided("approval_required", (REQUIRES_MANAGER_REVIEW,))
