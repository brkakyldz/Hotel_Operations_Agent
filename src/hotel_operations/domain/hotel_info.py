"""Guest information topics: what the hotel tells guests.

Each topic is a typed document, validated when it is stored and again when it is read, so
the policy tool returns exactly the stored fields and nothing a model could embellish.
These documents hold information the agent only relays. Values that a deterministic rule
computes with (checkout, check-in, housekeeping limits) stay columns on ``HotelPolicy``,
whose version drives the freshness of pending checkout reviews; a change to a document here
has its own version and never makes a review stale.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Venue(_Document):
    """A place with opening hours (hotel-local). ``closes_local`` 00:00 means midnight."""

    name: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    opens_local: HHMM
    closes_local: HHMM
    location: Text | None = None
    notes: Text | None = None


class Facilities(_Document):
    venues: list[Venue] = Field(min_length=1, max_length=8)
    booking: Text


class Dining(_Document):
    venues: list[Venue] = Field(min_length=1, max_length=8)
    ordering: Text


class Internet(_Document):
    wifi_network: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    price: Text
    coverage: Text
    password_location: Text


class HouseRules(_Document):
    quiet_hours_start_local: HHMM
    quiet_hours_end_local: HHMM
    smoking: Text
    pets: Text


class Parking(_Document):
    available: bool
    location: Text
    hours: Text
    ev_chargers: int = Field(ge=0, le=50)
    price: Text


class Luggage(_Document):
    location: Text
    before_check_in: Text
    after_checkout_until_local: HHMM
    price: Text


class RoomAmenities(_Document):
    items: list[Annotated[str, StringConstraints(min_length=1, max_length=60)]] = Field(
        min_length=1, max_length=12
    )
    notes: Text


InfoTopic = Literal[
    "facilities", "dining", "internet", "house_rules", "parking", "luggage", "room_amenities"
]
INFO_DOCUMENTS: dict[str, type[_Document]] = {
    "facilities": Facilities,
    "dining": Dining,
    "internet": Internet,
    "house_rules": HouseRules,
    "parking": Parking,
    "luggage": Luggage,
    "room_amenities": RoomAmenities,
}
INFO_TOPICS: tuple[str, ...] = get_args(InfoTopic)
assert set(INFO_TOPICS) == set(INFO_DOCUMENTS), "every info topic needs exactly one document type"


def validated(topic: str, content: Any) -> dict[str, Any] | None:
    """The stored document for ``topic`` as plain JSON, or None when it is not valid.

    Invalid stored data is treated as missing: the caller reports the topic unavailable
    instead of relaying a partial or malformed policy.
    """
    document = INFO_DOCUMENTS.get(topic)
    if document is None or not isinstance(content, dict):
        return None
    try:
        return document.model_validate(content).model_dump(mode="json")
    except ValidationError:
        return None
