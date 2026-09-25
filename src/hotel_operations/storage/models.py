"""ORM schema for hotel.sqlite.

Business tables hold the authoritative simulated hotel. Observation tables
(sessions, runs, audit, receipts) record what happened; they are excluded from
"no business mutation" fingerprints by name in ``OBSERVATION_TABLES``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    literal_column,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnClause

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC in SQLite and always returns timezone-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not accepted; pass an aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --- Business tables -------------------------------------------------------


class Hotel(Base):
    __tablename__ = "hotels"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64))
    demo_now: Mapped[datetime] = mapped_column(UTCDateTime())


class Guest(Base):
    __tablename__ = "guests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    display_name: Mapped[str] = mapped_column(String(120))
    selectable: Mapped[bool] = mapped_column(Boolean, default=False)


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("hotel_id", "number", name="uq_rooms_hotel_number"),
        CheckConstraint(
            "housekeeping_state IN ('dirty', 'clean', 'inspected')", name="housekeeping_state"
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    number: Mapped[str] = mapped_column(String(10))
    housekeeping_state: Mapped[str] = mapped_column(String(20))
    out_of_service: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Everything that decides this room's availability: its stays, its state and its cleaning
    # plan. A change to another room never moves it.
    operational_version: Mapped[int] = mapped_column(Integer, default=1)


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        UniqueConstraint("hotel_id", "reference", name="uq_reservations_hotel_reference"),
        CheckConstraint(
            "status IN ('confirmed', 'checked_in', 'checked_out', 'cancelled')", name="status"
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    reference: Mapped[str] = mapped_column(String(20))
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    status: Mapped[str] = mapped_column(String(20))
    scheduled_check_in: Mapped[datetime] = mapped_column(UTCDateTime())
    scheduled_checkout: Mapped[datetime] = mapped_column(UTCDateTime())
    actual_check_in: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    actual_checkout: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)


class HotelPolicy(Base):
    """One current typed policy per hotel."""

    __tablename__ = "hotel_policies"

    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    breakfast_start_local: Mapped[str] = mapped_column(String(5))
    breakfast_end_local: Mapped[str] = mapped_column(String(5))
    housekeeping_items: Mapped[list[str]] = mapped_column(JSON)
    housekeeping_max_quantity: Mapped[int] = mapped_column(Integer)
    maintenance_categories: Mapped[list[str]] = mapped_column(JSON)
    # Checkout policy (hotel-local "HH:MM"). Nullable: a missing value makes checkout
    # evaluation unavailable instead of being filled with a plausible default.
    standard_checkout_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    auto_extension_until_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    reviewed_extension_until_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    turnaround_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_housekeeping_finish_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Early check-in policy v1; nullable for the same reason.
    standard_check_in_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    early_check_in_from_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    # Housekeeping policy v1; nullable: without them requests fail closed.
    room_cleaning_from_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    room_cleaning_until_local: Mapped[str | None] = mapped_column(String(5), nullable=True)
    towels_per_stay: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pillows_per_stay: Mapped[int | None] = mapped_column(Integer, nullable=True)


class HotelInfoTopic(Base):
    """One guest information document per hotel and topic.

    Kept off ``HotelPolicy`` on purpose: that row's version decides whether a pending
    checkout review is still fresh, and relayed information must never make one stale.
    ``content`` is validated against the topic's document type on write and on read.
    """

    __tablename__ = "hotel_info_topics"

    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"), primary_key=True)
    topic: Mapped[str] = mapped_column(String(30), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)


class RoomDayPlan(Base):
    """One operational housekeeping window per room and hotel-local date."""

    __tablename__ = "room_day_plans"
    __table_args__ = (
        UniqueConstraint("room_id", "local_date", name="uq_room_day_plans_room_date"),
        CheckConstraint("housekeeping_finish_by > housekeeping_ready_after", name="window_order"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    local_date: Mapped[str] = mapped_column(String(10))
    housekeeping_ready_after: Mapped[datetime] = mapped_column(UTCDateTime())
    housekeeping_finish_by: Mapped[datetime] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(Integer, default=1)


class HousekeepingTask(Base):
    __tablename__ = "housekeeping_tasks"
    __table_args__ = (
        CheckConstraint("item IN ('towels', 'pillows', 'room_cleaning')", name="item"),
        CheckConstraint("quantity BETWEEN 1 AND 6", name="quantity"),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'cancelled')", name="status"
        ),
        Index("ix_housekeeping_tasks_reservation", "reservation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    item: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    # Why the system moved it, when it did (SERVICE_WINDOW_CLOSED); null for staff moves.
    status_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    originating_run_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class MaintenanceTask(Base):
    """A guest-reported issue. Reported is not repaired; the room is never changed by it."""

    __tablename__ = "maintenance_tasks"
    __table_args__ = (
        CheckConstraint("category IN ('hvac', 'plumbing', 'electrical', 'other')", name="category"),
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'cancelled')", name="status"
        ),
        CheckConstraint("length(description) BETWEEN 1 AND 500", name="description"),
        Index("ix_maintenance_tasks_reservation", "reservation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    category: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20))
    originating_run_id: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class Approval(Base):
    """A durable human-review request. The payload never changes in place.

    ``human_decision`` records what the operator intended; ``status`` records what
    actually happened (executed, rejected, or an approve attempt that became stale
    or expired instead of executing).
    """

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("action_type IN ('late_checkout')", name="action_type"),
        CheckConstraint(
            "status IN ('pending', 'executed', 'rejected', 'expired', 'stale')", name="status"
        ),
        CheckConstraint(
            "human_decision IS NULL OR human_decision IN ('approve', 'reject')",
            name="human_decision",
        ),
        # At most one pending late-checkout approval per reservation.
        Index(
            "uq_approvals_one_pending_per_reservation",
            "reservation_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
        ),
        Index("ix_approvals_hotel_status", "hotel_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    action_type: Mapped[str] = mapped_column(String(20))
    immutable_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    observed_versions: Mapped[dict[str, Any]] = mapped_column(JSON)
    guest_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    status_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    originating_run_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    human_decision: Mapped[str | None] = mapped_column(String(10), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    execution_receipt_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


# --- Observation tables ------------------------------------------------------


class OperatorCommand(Base):
    """Idempotency record for operator decisions: (operator, client_request_id) + body hash."""

    __tablename__ = "operator_commands"
    __table_args__ = (
        UniqueConstraint(
            "operator_id", "client_request_id", name="uq_operator_commands_operator_request"
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    operator_id: Mapped[str] = mapped_column(String(64))
    client_request_id: Mapped[str] = mapped_column(String(36))
    body_hash: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[str] = mapped_column(String(40))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())


class OperationReceipt(Base):
    """Durable success receipt of a controlled write; unique per operation key."""

    __tablename__ = "operation_receipts"
    __table_args__ = (Index("ix_operation_receipts_run_id", "run_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(64), unique=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    reservation_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    artifact_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    committed_at: Mapped[datetime] = mapped_column(UTCDateTime())


class DemoSession(Base):
    __tablename__ = "demo_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    capability_hash: Mapped[str] = mapped_column(String(64), unique=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    guest_id: Mapped[str] = mapped_column(ForeignKey("guests.id"))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"))
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    conversation_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    accepted_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    context_restarted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("session_id", "client_request_id", name="uq_runs_session_request"),
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'interrupted')", name="status"
        ),
        Index("ix_runs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("demo_sessions.id"))
    client_request_id: Mapped[str] = mapped_column(String(36))
    input_hash: Mapped[str] = mapped_column(String(64))
    user_message: Mapped[str] = mapped_column(Text)
    final_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20))
    conversation_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    conversation_abandoned: Mapped[bool] = mapped_column(Boolean, default=False)
    # Who started the turn: the guest's message, or a hotel update the gate raised.
    initiated_by: Mapped[str] = mapped_column(
        String(10), default="guest", server_default=text("'guest'")
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40))
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    wall_time: Mapped[datetime] = mapped_column(UTCDateTime())
    demo_time: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


HOTEL_UPDATE_KINDS = (
    "review_closed",
    "task_closed",
    "room_out_of_service",
    "review_at_risk",
    "checkout_now_possible",
)


class HotelUpdate(Base):
    """Something the hotel did that one guest should hear about.

    Written by the deterministic gate in the same transaction as the change it reports,
    and delivered at most once as a hotel-initiated, read-only agent turn. ``facts`` holds
    enums, ids and times only, never a guest's or staff member's free text. It records
    what happened; it is not business state, and the agent re-reads the real records.
    """

    __tablename__ = "hotel_updates"
    __table_args__ = (
        UniqueConstraint("reservation_id", "kind", "ref", name="uq_hotel_updates_once"),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in HOTEL_UPDATE_KINDS) + ")", name="kind"
        ),
        CheckConstraint("status IN ('pending', 'sent', 'skipped')", name="status"),
        Index("ix_hotel_updates_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    reservation_id: Mapped[str] = mapped_column(ForeignKey("reservations.id"))
    kind: Mapped[str] = mapped_column(String(30))
    # What the update is about: an approval or task id, a room id, or a refused request.
    ref: Mapped[str] = mapped_column(String(40))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10))
    skip_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    demo_time: Mapped[datetime] = mapped_column(UTCDateTime())


# The order the gate recorded the updates in, which is the order the guest hears them. One
# transaction can record two within one clock tick (15.6 ms on Windows): a world event records
# the room, then the review it puts at risk. SQLite's rowid follows insertion, so it breaks
# that tie; the random id did not.
_HOTEL_UPDATE_ROWID: ColumnClause[int] = literal_column("hotel_updates.rowid", Integer)
HOTEL_UPDATE_RECORDED_ORDER = (HotelUpdate.created_at, _HOTEL_UPDATE_ROWID)
HOTEL_UPDATE_NEWEST_FIRST = (HotelUpdate.created_at.desc(), _HOTEL_UPDATE_ROWID.desc())


OBSERVATION_TABLES = frozenset(
    {
        "demo_sessions",
        "runs",
        "audit_events",
        "operation_receipts",
        "operator_commands",
        "hotel_updates",
    }
)


def business_tables() -> list[str]:
    """Every table that holds business state, derived from ORM metadata."""
    return sorted(name for name in Base.metadata.tables if name not in OBSERVATION_TABLES)
