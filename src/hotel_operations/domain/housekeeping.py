"""Housekeeping policy v1 as a pure function.

No database, clock or model access: the write service loads current facts inside its
transaction and this module decides, exactly as late checkout does. A request is always
decided again from current state, never from an earlier answer in the conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

Decision = Literal["accepted", "denied", "already_requested"]

# Reason codes (explanatory; the decision is the authority).
OUTSIDE_CLEANING_HOURS = "OUTSIDE_CLEANING_HOURS"
STAY_LIMIT_REACHED = "STAY_LIMIT_REACHED"
ALREADY_REQUESTED = "ALREADY_REQUESTED"
# Set on a room-cleaning task the system cancels when the cleaning hours end.
SERVICE_WINDOW_CLOSED = "SERVICE_WINDOW_CLOSED"


@dataclass(frozen=True)
class HousekeepingRules:
    cleaning_from: time  # room cleaning is accepted from this hotel-local time...
    cleaning_until: time  # ...until just before this one (half-open window)
    towels_per_stay: int
    pillows_per_stay: int


@dataclass(frozen=True)
class HousekeepingFacts:
    now_local: datetime  # the hotel clock, hotel-local
    open_cleaning_task_id: str | None  # a pending or in-progress room cleaning, if any
    towels_this_stay: int  # requested so far, cancelled requests excluded
    pillows_this_stay: int


@dataclass(frozen=True)
class HousekeepingDecision:
    decision: Decision
    reason_codes: tuple[str, ...] = ()
    next_available_local: datetime | None = None  # when room cleaning can be asked for again
    remaining_this_stay: int | None = None  # towels or pillows still available, after this one
    existing_task_id: str | None = None


def cleaning_open(rules: HousekeepingRules, at: datetime) -> bool:
    return rules.cleaning_from <= at.time() < rules.cleaning_until


def next_cleaning_start(rules: HousekeepingRules, now_local: datetime) -> datetime:
    """The next moment room cleaning is accepted: today's opening, or tomorrow's."""
    today = now_local.replace(
        hour=rules.cleaning_from.hour, minute=rules.cleaning_from.minute, second=0, microsecond=0
    )
    return today if now_local < today else today + timedelta(days=1)


def decide(
    item: str, quantity: int, rules: HousekeepingRules, facts: HousekeepingFacts
) -> HousekeepingDecision:
    if item == "room_cleaning":
        if facts.open_cleaning_task_id is not None:
            return HousekeepingDecision(
                "already_requested",
                (ALREADY_REQUESTED,),
                existing_task_id=facts.open_cleaning_task_id,
            )
        if not cleaning_open(rules, facts.now_local):
            return HousekeepingDecision(
                "denied",
                (OUTSIDE_CLEANING_HOURS,),
                next_available_local=next_cleaning_start(rules, facts.now_local),
            )
        return HousekeepingDecision("accepted")
    limit, used = (
        (rules.towels_per_stay, facts.towels_this_stay)
        if item == "towels"
        else (rules.pillows_per_stay, facts.pillows_this_stay)
    )
    remaining = max(0, limit - used)
    if quantity > remaining:
        return HousekeepingDecision("denied", (STAY_LIMIT_REACHED,), remaining_this_stay=remaining)
    return HousekeepingDecision("accepted", remaining_this_stay=remaining - quantity)
