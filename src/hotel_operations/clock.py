"""Wall-clock and demo-clock helpers.

Real UTC wall time governs run deadlines and session expiry. The frozen hotel
business clock (Hotel.demo_now) governs hotel-local decisions, including when a
pending manager review lapses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_local(value: datetime, timezone: str) -> datetime:
    return value.astimezone(ZoneInfo(timezone))


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
