"""Housekeeping policy v1 in the write service: decided inside the transaction, audited,
and a refusal creates nothing."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from hotel_operations.services import housekeeping as hk_service
from hotel_operations.services import sessions
from hotel_operations.services.policy import read_policies
from hotel_operations.services.writes import WriteScope
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import (
    AuditEvent,
    Hotel,
    HotelPolicy,
    HousekeepingTask,
    OperationReceipt,
)
from tests.conftest import Harness
from tests.support.fingerprint import business_fingerprint
from tests.support.models import RuleModel, hotel_rule

TZ = ZoneInfo("Europe/Istanbul")
_calls = iter(range(1, 10_000))


def _binding(db: Database, guest: str = "G-001") -> sessions.SessionBinding:
    with db.write() as s:
        _, demo = sessions.create_session(s, guest)
        return sessions.binding_of(demo)


def _clock(db: Database, hhmm: str) -> None:
    hour, minute = map(int, hhmm.split(":"))
    with db.write() as s:
        hotel = s.get(Hotel, "hotel_demo")
        assert hotel is not None
        hotel.demo_now = datetime(2026, 9, 22, hour, minute, tzinfo=TZ)


def _ask(db: Database, binding: sessions.SessionBinding, item: str, qty: int = 1) -> dict[str, Any]:
    n = next(_calls)
    scope = WriteScope(
        "hotel_demo", binding.reservation_id, binding.session_id, f"run_{n}", f"r{n}"
    )
    with db.write() as s:
        return hk_service.create_housekeeping_task(
            s, binding, scope, item=item, quantity=qty, notes=None
        )


def _count(db: Database, model: type[Any]) -> int:
    with db.read() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.parametrize(
    ("at", "outcome"),
    [("08:59", "denied"), ("09:00", "pending"), ("15:59", "pending"), ("16:00", "denied")],
)
def test_room_cleaning_follows_the_hotel_clock(db: Database, at: str, outcome: str) -> None:
    binding = _binding(db)
    _clock(db, at)
    out = _ask(db, binding, "room_cleaning")
    assert out["outcome"] == outcome
    if outcome == "denied":
        assert out["reason_codes"] == ["OUTSIDE_CLEANING_HOURS"]
        assert out["next_available_local"].endswith("09:00:00+03:00")
        assert out["nothing_created"] is True and out["receipt_id"] is None


def test_a_refusal_is_audited_and_changes_nothing(db: Database) -> None:
    binding = _binding(db)
    _clock(db, "16:30")
    before = business_fingerprint(db)
    receipts = _count(db, OperationReceipt)
    _ask(db, binding, "room_cleaning")
    assert business_fingerprint(db) == before
    assert _count(db, OperationReceipt) == receipts
    with db.read() as s:
        event = s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "housekeeping_evaluated")
        ).one()
    assert (event.outcome, event.reason_codes) == ("denied", ["OUTSIDE_CLEANING_HOURS"])


def test_one_open_room_cleaning_per_stay(db: Database) -> None:
    binding = _binding(db)
    first = _ask(db, binding, "room_cleaning")
    again = _ask(db, binding, "room_cleaning")
    assert again["outcome"] == "already_requested"
    assert (again["existing_task_id"], again["existing_status"]) == (first["task_id"], "pending")
    assert _count(db, HousekeepingTask) == 1
    # Once staff complete it, a new cleaning can be asked for.
    with db.write() as s:
        task = s.get(HousekeepingTask, first["task_id"])
        assert task is not None
        task.status = "completed"
    assert _ask(db, binding, "room_cleaning")["outcome"] == "pending"
    assert _count(db, HousekeepingTask) == 2


def test_stay_limits_count_every_request_except_cancelled_ones(db: Database) -> None:
    binding = _binding(db)
    six = _ask(db, binding, "towels", 6)
    assert (six["outcome"], six["remaining_this_stay"]) == ("pending", 2)
    assert _ask(db, binding, "towels", 2)["remaining_this_stay"] == 0
    refused = _ask(db, binding, "towels", 1)
    assert (refused["outcome"], refused["reason_codes"]) == ("denied", ["STAY_LIMIT_REACHED"])
    assert refused["remaining_this_stay"] == 0
    with db.write() as s:
        task = s.get(HousekeepingTask, six["task_id"])
        assert task is not None
        task.status = "cancelled"
    assert _ask(db, binding, "towels", 5)["outcome"] == "pending"
    # Pillows have their own limit, and the stay's towels do not count against it.
    assert _ask(db, binding, "pillows", 4)["remaining_this_stay"] == 0


def test_the_same_request_key_replays_the_created_task(db: Database) -> None:
    binding = _binding(db)
    scope = WriteScope("hotel_demo", "rsv_1042", binding.session_id, "run_x", "req-x")
    with db.write() as s:
        first = hk_service.create_housekeeping_task(
            s, binding, scope, item="room_cleaning", quantity=1, notes=None
        )
    with db.write() as s:
        replay = hk_service.create_housekeeping_task(
            s, binding, scope, item="room_cleaning", quantity=1, notes=None
        )
    assert (replay["task_id"], replay["replayed"]) == (first["task_id"], True)
    assert _count(db, HousekeepingTask) == 1


def test_missing_rule_values_fail_closed(db: Database) -> None:
    from hotel_operations import errors

    binding = _binding(db)
    with db.write() as s:
        policy = s.get(HotelPolicy, "hotel_demo")
        assert policy is not None
        policy.towels_per_stay = None
    with pytest.raises(errors.DomainError) as caught:
        _ask(db, binding, "towels", 1)
    assert caught.value.code == "POLICY_UNAVAILABLE"
    assert _count(db, HousekeepingTask) == 0


def test_the_policy_topic_states_the_same_rules(db: Database) -> None:
    binding = _binding(db)
    with db.read() as s:
        (data,) = read_policies(s, binding, ["housekeeping"])["data"]["topics"]
    assert (
        data["room_cleaning_from_local"],
        data["room_cleaning_until_local"],
        data["room_cleaning_open_requests_per_stay"],
        data["towels_per_stay"],
        data["pillows_per_stay"],
    ) == ("09:00", "16:00", 1, 8, 4)


def test_the_agent_reports_an_open_cleaning_instead_of_filing_another(
    make_harness: Any,
) -> None:
    h: Harness = make_harness(RuleModel(hotel_rule))
    token = h.session("G-001")["token"]
    first = h.ask(token, "Please clean my room.")
    assert first["activity"][0]["summary"]["status"] == "pending"
    second = h.ask(token, "Please clean my room.")
    step = second["activity"][0]
    assert (step["tool_name"], step["status"]) == ("create_housekeeping_task", "completed")
    assert step["summary"]["outcome"] == "already_requested"
    assert second["final_message"].startswith("Housekeeping request already_requested")
    assert second["artifacts"] == []


def test_upgraded_database_gets_the_housekeeping_policy(tmp_path: Path) -> None:
    from alembic import command

    from hotel_operations.storage.migrate import alembic_config, upgrade_to_head
    from tests.support.eras import seed_core_schema_era

    path = tmp_path / "old.sqlite"
    command.upgrade(alembic_config(path), "0001")
    db = Database(path)
    with db.write() as s:
        seed_core_schema_era(s)
    db.dispose()
    upgrade_to_head(path)
    db = Database(path)
    try:
        with db.read() as s:
            policy = s.get(HotelPolicy, "hotel_demo")
            assert policy is not None
            assert (
                policy.room_cleaning_from_local,
                policy.room_cleaning_until_local,
                policy.towels_per_stay,
                policy.pillows_per_stay,
                policy.version,
            ) == ("09:00", "16:00", 8, 4, 1)
    finally:
        db.dispose()
