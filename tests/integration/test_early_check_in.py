"""Early check-in through the real Runner, gateway and SQLite."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from hotel_operations.services import checkin as checkin_service
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import (
    AuditEvent,
    Guest,
    OperationReceipt,
    Reservation,
    Room,
)
from tests.conftest import Harness
from tests.support.fingerprint import business_fingerprint
from tests.support.models import RuleModel, Turn, call, hotel_rule, say

TZ = ZoneInfo("Europe/Istanbul")


def iso(hour: int, minute: int = 0) -> str:
    return f"2026-09-22T{hour:02d}:{minute:02d}:00+03:00"


def _check_in(db: Database, reservation_id: str = "rsv_1101") -> tuple[str, int, str]:
    with db.read() as s:
        row = s.get(Reservation, reservation_id)
        assert row is not None
        return row.scheduled_check_in.astimezone(TZ).strftime("%H:%M"), row.version, row.status


def _operational_version(db: Database, room_id: str) -> int:
    with db.read() as s:
        room = s.get(Room, room_id)
        assert room is not None
        return room.operational_version


def _receipts(db: Database) -> int:
    with db.read() as s:
        return int(s.scalar(select(func.count()).select_from(OperationReceipt)) or 0)


@pytest.fixture
def hotel(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(hotel_rule))
    return h


def test_sofia_checks_in_early_once_atomically(hotel: Harness) -> None:
    token = hotel.session("G-003")["token"]
    before = business_fingerprint(hotel.services.db)
    op_before = _operational_version(hotel.services.db, "room_218")
    run = hotel.ask(token, "Can I check in at 13:00?")
    assert run["final_message"].startswith("Early check-in request applied (auto_allowed)")
    # Moved earlier; still an arrival: checking in at the desk is a separate step.
    assert _check_in(hotel.services.db) == ("13:00", 2, "confirmed")
    assert _operational_version(hotel.services.db, "room_218") == op_before + 1
    after = business_fingerprint(hotel.services.db)
    assert {t for t in after if after[t] != before[t]} == {"reservations", "rooms"}
    [artifact] = run["artifacts"]
    assert artifact["kind"] == "check_in_change"
    assert artifact["result"]["previous_check_in_local"] == iso(15)
    assert artifact["result"]["current_check_in_local"] == iso(13)
    with hotel.services.db.read() as s:
        changed = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "check_in_changed"))
        assert changed is not None
        assert (changed.payload["before"], changed.payload["after"]) == (iso(15), iso(13))
        assert changed.reason_codes == ["EARLY_CHECK_IN_AVAILABLE"]
    body = hotel.client.get("/api/me/reservation", headers=hotel.auth(token)).json()
    assert body["reservation"]["scheduled_check_in"] == iso(13)


def test_before_the_earliest_time_is_denied_with_the_earliest_possible(hotel: Harness) -> None:
    token = hotel.session("G-003")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "Can I check in at 11:00?")
    assert run["final_message"] == (
        f"Early check-in request denied (denied); check-in is {iso(15)}. Earliest possible: 12:00."
    )
    assert run["artifacts"] == []
    assert business_fingerprint(hotel.services.db) == before
    with hotel.services.db.read() as s:
        event = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "check_in_evaluated"))
        assert event is not None and event.reason_codes == ["BEFORE_EARLIEST_CHECK_IN"]


def test_a_later_or_equal_time_is_no_change(hotel: Harness) -> None:
    token = hotel.session("G-003")["token"]
    run = hotel.ask(token, "I'd like to check in at 16:00")
    assert run["final_message"].startswith("Early check-in request no_change (no_change)")
    assert _check_in(hotel.services.db) == ("15:00", 1, "confirmed")
    assert _receipts(hotel.services.db) == 0


def test_checked_in_guest_cannot_use_early_check_in(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    run = hotel.ask(token, "Can I check in at 13:00?")
    assert run["final_message"] == "That did not work: NOT_ARRIVING."
    assert _check_in(hotel.services.db, "rsv_1042")[1] == 1


def test_advisory_questions_change_nothing(hotel: Harness) -> None:
    token = hotel.session("G-003")["token"]
    before = business_fingerprint(hotel.services.db)
    named = hotel.ask(token, "Would a 13:00 check-in be possible?")
    assert named["final_message"].startswith("Advisory, nothing changed: auto_allowed")
    asap = hotel.ask(token, "How early can I check in?")
    # "As early as possible" is evaluated at the current hotel time (10:00): too early.
    assert asap["final_message"] == (
        "Advisory, nothing changed: denied (BEFORE_EARLIEST_CHECK_IN). Earliest possible: 12:00."
    )
    tools = [a["tool_name"] for a in asap["activity"]]
    assert tools == ["get_my_reservation", "evaluate_early_check_in"]
    assert business_fingerprint(hotel.services.db) == before
    policy = hotel.ask(token, "What time is check-in?")
    assert policy["final_message"] == (
        "Check-in is from 15:00; early check-in from 12:00 when the room is ready."
    )


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"housekeeping_state": "dirty"}, "ROOM_NOT_READY"),
        ({"out_of_service": True}, "ROOM_OUT_OF_SERVICE"),
    ],
)
def test_room_state_is_re_read_at_write_time(
    make_harness: Any, change: dict[str, object], code: str
) -> None:
    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            return call("request_early_check_in", {"requested_check_in_local": iso(13)})
        return say(json.dumps(outs[-1]["data"]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-003")["token"]
    with h.services.db.write() as s:
        room = s.get(Room, "room_218")
        assert room is not None
        for key, value in change.items():
            setattr(room, key, value)
    data = json.loads(h.ask(token, "x")["final_message"])
    assert (data["outcome"], data["reason_codes"]) == ("denied", [code])
    assert data["earliest_possible_check_in_local"] is None
    assert _check_in(h.services.db) == ("15:00", 1, "confirmed")


def test_a_stay_still_in_the_room_blocks_without_being_exposed(make_harness: Any) -> None:
    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            return call("request_early_check_in", {"requested_check_in_local": iso(12, 30)})
        return say(json.dumps(outs[-1]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-003")["token"]
    with h.services.db.write() as s:
        s.add(
            Guest(id="G-990", hotel_id="hotel_demo", display_name="Hidden Stayer", selectable=False)
        )
        s.add(
            Reservation(
                id="rsv_9900",
                reference="R-9900",
                hotel_id="hotel_demo",
                guest_id="G-990",
                room_id="room_218",
                status="checked_in",
                scheduled_check_in=datetime(2026, 9, 21, 12, tzinfo=UTC),
                scheduled_checkout=datetime(2026, 9, 22, 10, tzinfo=UTC),  # 13:00 local
                version=1,
            )
        )
    raw = h.ask(token, "x")["final_message"]
    envelope = json.loads(raw)
    assert envelope["data"]["reason_codes"] == ["ROOM_STILL_OCCUPIED"]
    assert envelope["data"]["earliest_possible_check_in_local"] == iso(13)
    for marker in ("R-9900", "rsv_9900", "G-990", "Hidden Stayer"):
        assert marker not in raw


def test_duplicate_proposals_and_http_replay_apply_once(make_harness: Any) -> None:
    args = {"requested_check_in_local": iso(13)}

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            return [call("request_early_check_in", args), call("request_early_check_in", args)]
        return say(json.dumps([o["data"] for o in outs]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-003")["token"]
    rid = str(uuid.uuid4())
    run = h.wait(token, h.chat(token, "check in 13:00", rid).json()["run_id"])
    a, b = json.loads(run["final_message"])
    assert (a["outcome"], a["replayed"], b["replayed"]) == ("applied", False, True)
    assert a["receipt_id"] == b["receipt_id"]
    assert h.chat(token, "check in 13:00", rid).json()["replayed"] is True
    again, _ = json.loads(h.ask(token, "check in 13:00 again")["final_message"])
    assert (again["outcome"], again["reason_codes"]) == ("no_change", ["ALREADY_ALLOWED"])
    assert _check_in(h.services.db) == ("13:00", 2, "confirmed")
    assert _receipts(h.services.db) == 1


def test_receipt_failure_rolls_back_the_change(
    hotel: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(checkin_service, "record_receipt", boom)
    token = hotel.session("G-003")["token"]
    op_before = _operational_version(hotel.services.db, "room_218")
    run = hotel.ask(token, "Can I check in at 13:00?")
    assert run["final_message"] == "That did not work: TOOL_FAILED."
    assert _check_in(hotel.services.db) == ("15:00", 1, "confirmed")
    assert _operational_version(hotel.services.db, "room_218") == op_before
    with hotel.services.db.read() as s:
        assert (
            s.scalar(select(AuditEvent).where(AuditEvent.event_type == "check_in_changed")) is None
        )


def test_upgraded_database_gets_the_check_in_policy(tmp_path: Path) -> None:
    from alembic import command

    from hotel_operations.storage.migrate import alembic_config, upgrade_to_head
    from hotel_operations.storage.models import HotelPolicy
    from tests.support.eras import seed_core_schema_era

    # A first-revision database (seeded before any policy existed) upgraded through every
    # migration.
    path = tmp_path / "first.sqlite"
    command.upgrade(alembic_config(path), "0001")
    db = Database(path)
    with db.write() as s:
        seed_core_schema_era(s)
    db.dispose()
    upgrade_to_head(path)
    db = Database(path)
    with db.read() as s:
        row = s.get(HotelPolicy, "hotel_demo")
        assert row is not None
        assert (row.standard_check_in_local, row.early_check_in_from_local, row.version) == (
            "15:00",
            "12:00",
            1,
        )
    db.dispose()
