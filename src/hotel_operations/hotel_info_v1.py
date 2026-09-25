"""Guest information v1: the invented content of each info topic.

Frozen: migration 0007 inserts exactly this into an existing demo database, and the seed
inserts it into a fresh one, so both reach the same hotel. A later change is a new version
(a new module and migration), never an edit here. Every value is fictional demo content,
not a fact about a real hotel.
"""

from __future__ import annotations

from typing import Any

HOTEL_INFO_V1: dict[str, dict[str, Any]] = {
    "facilities": {
        "venues": [
            {
                "name": "Pool",
                "opens_local": "08:00",
                "closes_local": "20:00",
                "location": "Garden level",
                "notes": "Outdoor and heated. Pool towels are handed out at the pool.",
            },
            {
                "name": "Gym",
                "opens_local": "06:00",
                "closes_local": "23:00",
                "location": "Lower ground floor",
                "notes": None,
            },
            {
                "name": "Spa",
                "opens_local": "10:00",
                "closes_local": "19:00",
                "location": "Lower ground floor",
                "notes": "Treatments by appointment.",
            },
        ],
        "booking": (
            "Spa treatments are booked at the front desk. The concierge chat cannot book or "
            "reserve facilities."
        ),
    },
    "dining": {
        "venues": [
            {
                "name": "Olive Restaurant",
                "opens_local": "19:00",
                "closes_local": "22:30",
                "location": "Ground floor",
                "notes": "Dinner, à la carte.",
            },
            {
                "name": "Lobby Bar",
                "opens_local": "17:00",
                "closes_local": "00:00",
                "location": "Lobby",
                "notes": None,
            },
            {
                "name": "Room service",
                "opens_local": "11:00",
                "closes_local": "23:00",
                "location": None,
                "notes": "Ordered by phone from the room.",
            },
        ],
        "ordering": (
            "Food and drinks are ordered by phone or at the venue. The concierge chat cannot "
            "place orders."
        ),
    },
    "internet": {
        "wifi_network": "DemoHotel-Guest",
        "price": "Free for all guests.",
        "coverage": "All rooms and public areas.",
        "password_location": (
            "Printed on the key-card sleeve. It is not stored in this policy, so the chat "
            "cannot give it."
        ),
    },
    "house_rules": {
        "quiet_hours_start_local": "22:00",
        "quiet_hours_end_local": "08:00",
        "smoking": "Not allowed in rooms or indoors; allowed on the garden terrace.",
        "pets": (
            "Pets up to 8 kg are welcome when announced before arrival. Assistance animals are "
            "always welcome."
        ),
    },
    "parking": {
        "available": True,
        "location": "Underground garage, entrance on the side street.",
        "hours": "Open 24 hours.",
        "ev_chargers": 2,
        "price": "15 EUR per night (fictional), settled at the front desk.",
    },
    "luggage": {
        "location": "Front desk.",
        "before_check_in": "Bags can be left at the front desk on the arrival day.",
        "after_checkout_until_local": "22:00",
        "price": "Free for guests.",
    },
    "room_amenities": {
        "items": [
            "In-room safe",
            "Minibar",
            "Kettle with tea and coffee",
            "Hair dryer",
            "Air conditioning",
        ],
        "notes": "Extra towels, pillows and room cleaning are requested through housekeeping.",
    },
}
