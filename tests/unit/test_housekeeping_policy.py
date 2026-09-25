"""Housekeeping policy v1 as a pure function."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from hotel_operations.domain import housekeeping as hk

TZ = ZoneInfo("Europe/Istanbul")
RULES = hk.HousekeepingRules(
    cleaning_from=time(9, 0), cleaning_until=time(16, 0), towels_per_stay=8, pillows_per_stay=4
)


def facts(hhmm: str = "10:00", **kw: object) -> hk.HousekeepingFacts:
    hour, minute = map(int, hhmm.split(":"))
    base: dict[str, object] = {
        "now_local": datetime(2026, 9, 22, hour, minute, tzinfo=TZ),
        "open_cleaning_task_id": None,
        "towels_this_stay": 0,
        "pillows_this_stay": 0,
    }
    base.update(kw)
    return hk.HousekeepingFacts(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("at", "decision", "next_local"),
    [
        ("08:59", "denied", "2026-09-22 09:00"),
        ("09:00", "accepted", None),
        ("15:59", "accepted", None),
        ("16:00", "denied", "2026-09-23 09:00"),
        ("17:30", "denied", "2026-09-23 09:00"),
    ],
)
def test_room_cleaning_hours_are_half_open(at: str, decision: str, next_local: str | None) -> None:
    result = hk.decide("room_cleaning", 1, RULES, facts(at))
    assert result.decision == decision
    if next_local is None:
        assert result.next_available_local is None and result.reason_codes == ()
    else:
        assert result.reason_codes == (hk.OUTSIDE_CLEANING_HOURS,)
        assert result.next_available_local is not None
        assert result.next_available_local.strftime("%Y-%m-%d %H:%M") == next_local


def test_an_open_cleaning_is_reported_before_the_hours() -> None:
    result = hk.decide("room_cleaning", 1, RULES, facts("17:00", open_cleaning_task_id="hk_1"))
    assert (result.decision, result.existing_task_id) == ("already_requested", "hk_1")
    assert result.reason_codes == (hk.ALREADY_REQUESTED,)


@pytest.mark.parametrize(
    ("item", "used", "asked", "decision", "remaining"),
    [
        ("towels", 6, 2, "accepted", 0),
        ("towels", 8, 1, "denied", 0),
        ("towels", 5, 4, "denied", 3),
        ("pillows", 0, 4, "accepted", 0),
        ("pillows", 2, 3, "denied", 2),
    ],
)
def test_stay_limits(item: str, used: int, asked: int, decision: str, remaining: int) -> None:
    result = hk.decide(item, asked, RULES, facts(**{f"{item}_this_stay": used}))
    assert (result.decision, result.remaining_this_stay) == (decision, remaining)
    if decision == "denied":
        assert result.reason_codes == (hk.STAY_LIMIT_REACHED,)


def test_towels_are_not_bound_to_cleaning_hours() -> None:
    assert hk.decide("towels", 2, RULES, facts("22:00")).decision == "accepted"
