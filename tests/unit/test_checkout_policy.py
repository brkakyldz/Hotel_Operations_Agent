"""Late-checkout policy v1 as a pure function: exact boundaries and input validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from hotel_operations import errors
from hotel_operations.domain import checkout as policy

TZ = ZoneInfo("Europe/Istanbul")
RULES = policy.CheckoutRules(
    standard=time(12),
    auto_until=time(14),
    review_until=time(16),
    turnaround=timedelta(minutes=60),
    default_finish_by=time(17, 30),
)


def at(hour: int, minute: int = 0, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


EMMA = policy.CheckoutFacts(
    tz=TZ,
    demo_now=at(10),
    reservation_status="checked_in",
    current_checkout=at(12),
    room_in_service=True,
    ready_after=at(12),
    finish_by=at(17, 30),
    next_arrival=at(18),
)
DANIEL = replace(EMMA, next_arrival=at(15))


def decide(value: str, facts: policy.CheckoutFacts = EMMA) -> policy.CheckoutDecision:
    return policy.evaluate(policy.parse_requested(value, TZ), RULES, facts)


def code_of(value: str, facts: policy.CheckoutFacts = EMMA) -> str:
    with pytest.raises(errors.DomainError) as exc:
        decide(value, facts)
    return exc.value.code


@pytest.mark.parametrize(
    ("clock", "decision", "reason"),
    [
        ("11:00", "no_change", policy.ALREADY_GRANTED),  # never shortened
        ("12:00", "no_change", policy.ALREADY_GRANTED),
        ("12:01", "auto_allowed", policy.WITHIN_AUTOMATIC_WINDOW),
        ("14:00", "auto_allowed", policy.WITHIN_AUTOMATIC_WINDOW),  # inclusive
        ("14:01", "approval_required", policy.REQUIRES_MANAGER_REVIEW),
        ("15:00", "approval_required", policy.REQUIRES_MANAGER_REVIEW),
        ("16:00", "approval_required", policy.REQUIRES_MANAGER_REVIEW),  # inclusive
        ("16:01", "denied", policy.AFTER_LATEST_EXTENSION),
        ("17:00", "denied", policy.AFTER_LATEST_EXTENSION),
    ],
)
def test_emma_boundaries(clock: str, decision: str, reason: str) -> None:
    result = decide(f"2026-09-22T{clock}:00+03:00")
    assert (result.decision, result.reason_codes) == (decision, (reason,))


@pytest.mark.parametrize(
    ("clock", "decision", "reasons"),
    [
        ("14:00", "auto_allowed", (policy.WITHIN_AUTOMATIC_WINDOW,)),  # finishes exactly 15:00
        ("14:01", "denied", (policy.NEXT_ARRIVAL_CONFLICT,)),
        ("15:00", "denied", (policy.NEXT_ARRIVAL_CONFLICT,)),
        ("16:01", "denied", (policy.AFTER_LATEST_EXTENSION,)),
    ],
)
def test_daniel_next_arrival(clock: str, decision: str, reasons: tuple[str, ...]) -> None:
    result = decide(f"2026-09-22T{clock}:00+03:00", DANIEL)
    assert (result.decision, result.reason_codes) == (decision, reasons)


def test_denial_reports_the_latest_feasible_time_not_the_next_guest() -> None:
    result = decide("2026-09-22T15:00:00+03:00", DANIEL)
    assert result.room_ready_deadline == at(15)
    assert result.latest_feasible == at(14)
    assert result.cleaning_finish == at(16)


def test_cleaning_window_and_default_finish() -> None:
    tight = replace(EMMA, finish_by=at(15, 30))
    assert decide("2026-09-22T15:00:00+03:00", tight).reason_codes == (
        policy.CLEANING_WINDOW_EXCEEDED,
    )
    # A later per-room window cannot exceed the policy default (17:30); the stricter one applies.
    late_ready = replace(EMMA, finish_by=at(19), ready_after=at(16, 45), next_arrival=None)
    assert decide("2026-09-22T16:00:00+03:00", late_ready).reason_codes == (
        policy.CLEANING_WINDOW_EXCEEDED,
    )


def test_cleaning_starts_no_earlier_than_ready_after() -> None:
    late_ready = replace(EMMA, ready_after=at(13, 30))
    result = decide("2026-09-22T13:00:00+03:00", late_ready)
    assert result.decision == "auto_allowed"
    assert result.cleaning_finish == at(14, 30)


def test_out_of_service_room_is_denied() -> None:
    result = decide("2026-09-22T13:00:00+03:00", replace(EMMA, room_in_service=False))
    assert (result.decision, result.reason_codes) == ("denied", (policy.ROOM_OUT_OF_SERVICE,))
    assert result.latest_feasible is None


def test_missing_operational_data_is_unavailable_not_a_denial() -> None:
    missing = replace(EMMA, ready_after=None, finish_by=None)
    assert code_of("2026-09-22T13:00:00+03:00", missing) == "OPERATIONAL_DATA_UNAVAILABLE"
    # The hard ceiling itself needs no operational data.
    assert decide("2026-09-22T16:30:00+03:00", missing).decision == "denied"


def test_arriving_guest_is_not_checked_in() -> None:
    arriving = replace(EMMA, reservation_status="confirmed")
    assert code_of("2026-09-22T13:00:00+03:00", arriving) == "NOT_CHECKED_IN"


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-22T14:00:00",  # naive: whose clock?
        "2026-09-22T11:00:00Z",  # UTC-only is not reinterpreted as hotel-local
        "2026-09-22T11:00:00+00:00",
        "2026-09-22T14:00:00+04:00",  # wrong offset for the hotel on that date
        "14:00",
        "tomorrow at 3pm",
        "",
        "2026-09-22T14:00:00+03:00" + "0" * 20,  # over-long
    ],
)
def test_malformed_or_foreign_offset_times_are_invalid(value: str) -> None:
    with pytest.raises(errors.DomainError) as exc:
        policy.parse_requested(value, TZ)
    assert exc.value.code == "INVALID_TIME"


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-22T03:00:00+03:00",  # 12-hour slip: 3 AM has already passed
        "2026-09-22T10:00:00+03:00",  # not later than demo_now
        "2026-09-23T11:00:00+03:00",  # future, non-departure date
        "2026-09-21T15:00:00+03:00",  # past date
    ],
)
def test_times_outside_the_departure_day_are_invalid(value: str) -> None:
    assert code_of(value) == "INVALID_TIME"


def test_departure_must_be_the_current_demo_day() -> None:
    staying = replace(EMMA, current_checkout=at(12, day=24))
    assert code_of("2026-09-24T13:00:00+03:00", staying) == "INVALID_TIME"


def test_same_instant_in_hotel_offset_is_canonical() -> None:
    parsed = policy.parse_requested("2026-09-22T14:00:00+03:00", TZ)
    assert parsed == at(14)


# --- the hotel board's reading: the latest checkout a request made now could get ----------


@pytest.mark.parametrize(
    ("facts", "latest", "needs_review", "blocked_by"),
    [
        (EMMA, at(16), True, None),  # the manager ceiling
        (DANIEL, at(14), False, None),  # the next arrival at 15:00, minus the turnaround
        (replace(EMMA, next_arrival=at(15, 30)), at(14, 30), True, None),
        (replace(DANIEL, next_arrival=None), at(16), True, None),
        (replace(EMMA, finish_by=at(15, 30)), at(14, 30), True, None),
        (replace(EMMA, room_in_service=False), None, False, policy.ROOM_OUT_OF_SERVICE),
        (replace(EMMA, current_checkout=at(16)), None, False, policy.NO_LATER_TIME),
        (replace(EMMA, demo_now=at(16)), None, False, policy.TIME_PASSED),
        (replace(EMMA, demo_now=at(10, day=21)), None, False, policy.NOT_DEPARTING_TODAY),
    ],
)
def test_latest_checkout_follows_the_same_rules(
    facts: policy.CheckoutFacts,
    latest: datetime | None,
    needs_review: bool,
    blocked_by: str | None,
) -> None:
    result = policy.latest_checkout(RULES, facts)
    assert (result.latest, result.needs_review, result.blocked_by) == (
        latest,
        needs_review,
        blocked_by,
    )


@pytest.mark.parametrize(
    "facts",
    [EMMA, DANIEL, replace(EMMA, next_arrival=at(15, 30)), replace(EMMA, finish_by=at(15, 30))],
)
def test_the_latest_checkout_is_exactly_what_evaluate_grants(facts: policy.CheckoutFacts) -> None:
    latest = policy.latest_checkout(RULES, facts).latest
    assert latest is not None
    assert policy.evaluate(latest, RULES, facts).decision in {"auto_allowed", "approval_required"}
    assert policy.evaluate(latest + timedelta(minutes=1), RULES, facts).decision == "denied"


def test_latest_checkout_needs_a_checked_in_guest() -> None:
    with pytest.raises(errors.DomainError) as exc:
        policy.latest_checkout(RULES, replace(EMMA, reservation_status="confirmed"))
    assert exc.value.code == "NOT_CHECKED_IN"
