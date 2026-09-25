"""Typed hotel policy reads. Only stored values are returned; nothing is invented.

Two kinds of topic share one tool and one envelope. Rule topics (breakfast, check-in,
checkout, housekeeping, maintenance) read the typed ``HotelPolicy`` row that deterministic
code also computes with. Information topics read a validated document each.
One read may ask for several topics: the source label and the hotel timezone are
stated once for all of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, get_args

from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import iso, to_local, utcnow
from hotel_operations.domain.hotel_info import INFO_TOPICS, validated
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.storage.models import Hotel, HotelInfoTopic, HotelPolicy

SOURCE_LABEL = "Simulated hotel policy"

PolicyTopic = Literal[
    "breakfast",
    "check_in",
    "checkout",
    "housekeeping",
    "maintenance",
    "facilities",
    "dining",
    "internet",
    "house_rules",
    "parking",
    "luggage",
    "room_amenities",
]
ALL_TOPICS: tuple[str, ...] = get_args(PolicyTopic)
assert set(INFO_TOPICS) <= set(ALL_TOPICS), "every information topic must be readable"


def policy_unavailable(topic: str) -> errors.DomainError:
    return errors.DomainError(
        "POLICY_UNAVAILABLE",
        f"No stored hotel policy is available for '{topic}'.",
        http_status=404,
    )


def _hotel(session: Session, hotel_id: str) -> Hotel:
    hotel = session.get(Hotel, hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    return hotel


def _meta(hotel: Hotel, versions: dict[str, int]) -> dict[str, Any]:
    return {
        "observed_at": iso(utcnow()),
        "demo_time": iso(to_local(hotel.demo_now, hotel.timezone)),
        "versions": versions,
    }


def _read_info(session: Session, hotel: Hotel, topic: str) -> tuple[dict[str, Any], dict[str, int]]:
    row = session.get(HotelInfoTopic, (hotel.id, topic))
    document = None if row is None else validated(topic, row.content)
    if row is None or document is None:
        # Missing or malformed: say so; never fill it with plausible hotel facts.
        raise policy_unavailable(topic)
    return {"topic": topic, "policy_version": row.version, **document}, {
        f"info_topic:{topic}": row.version
    }


def read_topic(session: Session, hotel: Hotel, topic: str) -> tuple[dict[str, Any], dict[str, int]]:
    """One topic's projection and the version it was read at; POLICY_UNAVAILABLE if absent."""
    if topic in INFO_TOPICS:
        return _read_info(session, hotel, topic)
    policy = session.get(HotelPolicy, hotel.id)
    if policy is None:
        raise policy_unavailable(topic)
    base: dict[str, Any] = {"topic": topic, "policy_version": policy.version}
    if topic == "breakfast":
        data = {
            **base,
            "breakfast_start_local": policy.breakfast_start_local,
            "breakfast_end_local": policy.breakfast_end_local,
            "served": "daily",
        }
    elif topic == "housekeeping":
        data = {
            **base,
            "supported_items": list(policy.housekeeping_items),
            "max_quantity_per_request": policy.housekeeping_max_quantity,
            "room_cleaning_quantity": 1,
            "eligibility": "checked-in guests only",
            "handling": "recorded as a pending request for simulated staff",
            "delivery_time_promised": False,
            # Housekeeping policy v1: the same values the write service decides with.
            "room_cleaning_from_local": policy.room_cleaning_from_local,
            "room_cleaning_until_local": policy.room_cleaning_until_local,
            "room_cleaning_open_requests_per_stay": 1,
            "towels_per_stay": policy.towels_per_stay,
            "pillows_per_stay": policy.pillows_per_stay,
        }
    elif topic == "maintenance":
        data = {
            **base,
            "supported_categories": list(policy.maintenance_categories),
            "eligibility": "checked-in guests only",
            "handling": "recorded as a pending report for simulated staff",
            "repair_time_promised": False,
            "emergency_service": False,
        }
    elif topic == "check_in":
        check_in = {
            "standard_check_in_local": policy.standard_check_in_local,
            "early_check_in_from_local": policy.early_check_in_from_local,
        }
        if any(value is None for value in check_in.values()):
            raise policy_unavailable(topic)
        data = {
            **base,
            **check_in,
            "notes": (
                "Early check-in depends on the room being ready and free on the arrival day; "
                "there is no manager review. This is general policy, not an individual "
                "eligibility decision."
            ),
        }
    elif topic == "checkout":
        fields = {
            "standard_checkout_local": policy.standard_checkout_local,
            "automatic_extension_until_local": policy.auto_extension_until_local,
            "reviewed_extension_until_local": policy.reviewed_extension_until_local,
            "turnaround_minutes": policy.turnaround_minutes,
        }
        if any(value is None for value in fields.values()):
            # Never fill missing thresholds with plausible hotel facts.
            raise policy_unavailable(topic)
        data = {
            **base,
            **fields,
            "latest_possible_extension_local": policy.reviewed_extension_until_local,
            "notes": (
                "Extensions depend on the room's cleaning window and next arrival; extensions "
                "after the automatic window need manager review. This is general policy, not an "
                "individual eligibility decision."
            ),
        }
    else:
        raise policy_unavailable(topic)
    return data, {"policy": policy.version}


def read_policies(
    session: Session, binding: SessionBinding, topics: Sequence[str]
) -> dict[str, Any]:
    """The policy tool's read: the session's own hotel only, one or more topics at once.

    A requested topic without stored data is listed as unavailable next to the ones that
    were read; only when none of them can be read is the whole read unavailable.
    """
    hotel = _hotel(session, binding.hotel_id)
    read: list[dict[str, Any]] = []
    unavailable: list[str] = []
    versions: dict[str, int] = {}
    for topic in dict.fromkeys(topics):  # a repeated topic is read once, in first order
        try:
            data, version = read_topic(session, hotel, topic)
        except errors.DomainError as exc:
            if exc.code != "POLICY_UNAVAILABLE":
                raise
            unavailable.append(topic)
            continue
        read.append(data)
        versions.update(version)
    if not read:
        raise policy_unavailable(", ".join(unavailable))
    result: dict[str, Any] = {
        "source": SOURCE_LABEL,
        "hotel_timezone": hotel.timezone,
        "topics": read,
    }
    if unavailable:
        result["unavailable_topics"] = unavailable
    return {"data": result, "meta": _meta(hotel, versions)}


def handbook(session: Session, hotel_id: str) -> dict[str, Any]:
    """Every topic exactly as the policy tool would return it, for the on-screen handbook.

    A topic without stored data is listed as unavailable, the same answer the tool gives.
    """
    hotel = _hotel(session, hotel_id)
    topics = []
    for topic in ALL_TOPICS:
        try:
            data: dict[str, Any] | None = read_topic(session, hotel, topic)[0]
        except errors.DomainError as exc:
            if exc.code != "POLICY_UNAVAILABLE":
                raise
            data = None
        topics.append({"topic": topic, "available": data is not None, "data": data})
    return {
        "source": SOURCE_LABEL,
        "hotel_name": hotel.display_name,
        "hotel_timezone": hotel.timezone,
        "demo_time": iso(to_local(hotel.demo_now, hotel.timezone)),
        "topics": topics,
    }
