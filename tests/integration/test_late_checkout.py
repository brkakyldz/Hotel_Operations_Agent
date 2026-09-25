"""Deterministic late checkout through the real Runner, gateway and SQLite."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from hotel_operations.services import checkout as checkout_service
from hotel_operations.services import sessions
from hotel_operations.services.writes import WriteScope
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import (
    AuditEvent,
    Base,
    Guest,
    HotelPolicy,
    OperationReceipt,
    Reservation,
    Room,
    RoomDayPlan,
)
from tests.conftest import Harness
from tests.support.fingerprint import business_fingerprint
from tests.support.models import RuleModel, Turn, call, hotel_rule, say

TZ = ZoneInfo("Europe/Istanbul")
PRIVATE_MARKERS = ("R-2001", "R-2002", "rsv_2001", "rsv_2002", "G-901", "G-902", "Internal arrival")


def iso(hour: int, minute: int = 0) -> str:
    return f"2026-09-22T{hour:02d}:{minute:02d}:00+03:00"


def _count(db: Database, model: type[Base]) -> int:
    with db.read() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def _checkout(db: Database, reservation_id: str = "rsv_1042") -> tuple[str, int]:
    with db.read() as s:
        row = s.get(Reservation, reservation_id)
        assert row is not None
        return row.scheduled_checkout.astimezone(TZ).strftime("%H:%M"), row.version


def _operational_version(db: Database, room_id: str) -> int:
    with db.read() as s:
        room = s.get(Room, room_id)
        assert room is not None
        return room.operational_version


def _tool_envelopes(tool: str, args: dict[str, Any]) -> Any:
    """A rule that proposes one call and replies with the raw tool envelope (as JSON)."""

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        return call(tool, args) if not outs else say(json.dumps(outs[-1]))

    return rule


def _envelope(h: Harness, token: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(h.ask(token, "x")["final_message"])
    return result


@pytest.fixture
def hotel(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(hotel_rule))
    return h


# --- automatic extension ------------------------------------------------------------------


def test_emma_two_pm_applies_once_atomically(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    before = business_fingerprint(hotel.services.db)
    op_before = _operational_version(hotel.services.db, "room_305")
    run = hotel.ask(token, "Please extend my checkout to 14:00")
    assert run["final_message"].startswith("Checkout request applied (auto_allowed)")
    assert _checkout(hotel.services.db) == ("14:00", 2)
    assert _operational_version(hotel.services.db, "room_305") == op_before + 1
    after = business_fingerprint(hotel.services.db)
    assert {t for t in after if after[t] != before[t]} == {"reservations", "rooms"}
    [artifact] = run["artifacts"]
    assert artifact["kind"] == "checkout_change"
    assert artifact["result"]["previous_checkout_local"] == iso(12)
    assert artifact["result"]["current_checkout_local"] == iso(14)
    with hotel.services.db.read() as s:
        changed = s.scalar(select(AuditEvent).where(AuditEvent.event_type == "checkout_changed"))
        assert changed is not None
        assert changed.payload["before"] == iso(12) and changed.payload["after"] == iso(14)
        assert changed.reason_codes == ["WITHIN_AUTOMATIC_WINDOW"]
    # The context panel refresh reads the same authoritative state.
    body = hotel.client.get("/api/me/reservation", headers=hotel.auth(token)).json()
    assert body["reservation"]["scheduled_checkout"] == iso(14)
    assert body["meta"]["versions"]["reservation"] == 2


def test_duplicate_proposals_http_replay_and_new_message_never_double_apply(
    make_harness: Any,
) -> None:
    args = {"requested_checkout_local": iso(14), "reason": None}

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            return [call("request_late_checkout", args), call("request_late_checkout", args)]
        return say(json.dumps([o["data"] for o in outs]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    rid = str(uuid.uuid4())
    first = h.chat(token, "late checkout 14:00", rid)
    run = h.wait(token, first.json()["run_id"])
    a, b = json.loads(run["final_message"])
    assert (a["outcome"], a["replayed"], b["replayed"]) == ("applied", False, True)
    assert a["receipt_id"] == b["receipt_id"]
    assert h.chat(token, "late checkout 14:00", rid).json()["replayed"] is True
    assert _checkout(h.services.db) == ("14:00", 2)
    # A new message with the same time is a new intent, but there is nothing left to change.
    again, _ = json.loads(h.ask(token, "late checkout 14:00 again")["final_message"])
    assert (again["outcome"], again["reason_codes"]) == ("no_change", ["ALREADY_GRANTED"])
    assert _checkout(h.services.db) == ("14:00", 2)
    assert _count(h.services.db, OperationReceipt) == 1


def test_never_shortens_an_extended_checkout(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    hotel.ask(token, "Please extend my checkout to 14:00")
    run = hotel.ask(token, "Change my checkout to 13:00")
    assert run["final_message"].startswith("Checkout request no_change")
    assert _checkout(hotel.services.db) == ("14:00", 2)


# --- review, denial and preconditions -----------------------------------------------------


def test_review_eligible_request_creates_pending_approval_not_a_change(
    make_harness: Any,
) -> None:
    """A review-eligible request creates a real pending approval; the checkout itself is
    unchanged."""
    h = make_harness(
        RuleModel(
            _tool_envelopes(
                "request_late_checkout", {"requested_checkout_local": iso(15), "reason": "flight"}
            )
        )
    )
    token = h.session("G-001")["token"]
    before = business_fingerprint(h.services.db)
    envelope = _envelope(h, token)
    data = envelope["data"]
    assert envelope["ok"] is True
    assert (data["decision"], data["outcome"]) == ("approval_required", "pending_approval")
    assert data["approval_id"].startswith("apr_") and data["receipt_id"]
    assert data["current_checkout_local"] == iso(12)
    after = business_fingerprint(h.services.db)
    assert {t for t in after if after[t] != before[t]} == {"approvals"}
    assert _checkout(h.services.db) == ("12:00", 1)


def test_daniel_next_arrival_conflict_denies_without_exposing_the_next_guest(
    make_harness: Any,
) -> None:
    h = make_harness(
        RuleModel(
            _tool_envelopes(
                "request_late_checkout", {"requested_checkout_local": iso(15), "reason": None}
            )
        )
    )
    token = h.session("G-002")["token"]
    before = business_fingerprint(h.services.db)
    envelope = _envelope(h, token)
    data = envelope["data"]
    assert (data["decision"], data["outcome"]) == ("denied", "denied")
    assert data["reason_codes"] == ["NEXT_ARRIVAL_CONFLICT"]
    assert data["latest_feasible_checkout_local"] == iso(14)
    assert business_fingerprint(h.services.db) == before
    text = json.dumps(envelope)
    assert not any(marker in text for marker in PRIVATE_MARKERS)


def test_after_latest_extension_is_denied(make_harness: Any) -> None:
    h = make_harness(
        RuleModel(
            _tool_envelopes(
                "request_late_checkout", {"requested_checkout_local": iso(16, 1), "reason": None}
            )
        )
    )
    token = h.session("G-001")["token"]
    data = _envelope(h, token)["data"]
    assert (data["outcome"], data["reason_codes"]) == ("denied", ["AFTER_LATEST_EXTENSION"])
    assert _checkout(h.services.db) == ("12:00", 1)


def test_arriving_guest_cannot_extend(hotel: Harness) -> None:
    token = hotel.session("G-003")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "Please extend my checkout to 14:00")
    assert run["final_message"] == "That did not work: NOT_CHECKED_IN."
    assert run["activity"][0]["status"] == "rejected"
    assert business_fingerprint(hotel.services.db) == before


@pytest.mark.parametrize(
    "value", ["2026-09-22T11:00:00Z", "2026-09-22T14:00:00", "2pm", "2026-09-23T11:00:00+03:00"]
)
def test_invalid_times_are_rejected_without_mutation(make_harness: Any, value: str) -> None:
    for tool, args in (
        ("evaluate_late_checkout", {"requested_checkout_local": value}),
        ("request_late_checkout", {"requested_checkout_local": value, "reason": None}),
    ):
        h = make_harness(RuleModel(_tool_envelopes(tool, args)))
        token = h.session("G-001")["token"]
        before = business_fingerprint(h.services.db)
        envelope = _envelope(h, token)
        assert envelope["ok"] is False and envelope["error"]["code"] == "INVALID_TIME"
        assert business_fingerprint(h.services.db) == before


# --- evaluation is advisory; the write re-decides from current state --------------------


def test_evaluation_is_read_only_and_advisory(make_harness: Any) -> None:
    h = make_harness(
        RuleModel(_tool_envelopes("evaluate_late_checkout", {"requested_checkout_local": iso(15)}))
    )
    token = h.session("G-001")["token"]
    before = business_fingerprint(h.services.db)
    envelope = _envelope(h, token)
    assert envelope["data"]["advisory"] is True
    assert envelope["data"]["decision"] == "approval_required"
    assert set(envelope["meta"]["versions"]) >= {
        "reservation",
        "room",
        "policy",
        "room_day_plan",
        "operational",
    }
    assert business_fingerprint(h.services.db) == before


@pytest.mark.parametrize("change", ["new_next_arrival", "room_out_of_service"])
def test_state_change_between_evaluation_and_request_is_rechecked(
    make_harness: Any, db_path: Path, change: str
) -> None:
    def mutate() -> None:
        other = Database(db_path)
        with other.write() as s:
            if change == "new_next_arrival":
                s.add(Guest(id="G-950", hotel_id="hotel_demo", display_name="X", selectable=False))
                s.flush()
                s.add(
                    Reservation(
                        id="rsv_x",
                        reference="R-9950",
                        hotel_id="hotel_demo",
                        guest_id="G-950",
                        room_id="room_305",
                        status="confirmed",
                        scheduled_check_in=datetime(2026, 9, 22, 14, 30, tzinfo=TZ),
                        scheduled_checkout=datetime(2026, 9, 23, 12, tzinfo=TZ),
                        version=1,
                    )
                )
                room_row = s.get(Room, "room_305")
                assert room_row is not None
                room_row.operational_version += 1
            else:
                room = s.get(Room, "room_305")
                assert room is not None
                room.out_of_service = True
                room.version += 1
        other.dispose()

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            return call("evaluate_late_checkout", {"requested_checkout_local": iso(14)})
        if len(outs) == 1:
            assert outs[0]["data"]["decision"] == "auto_allowed"
            mutate()  # the world changes after the advisory observation
            return call(
                "request_late_checkout", {"requested_checkout_local": iso(14), "reason": None}
            )
        return say(json.dumps(outs[-1]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    data = json.loads(h.ask(token, "x")["final_message"])["data"]
    expected = "NEXT_ARRIVAL_CONFLICT" if change == "new_next_arrival" else "ROOM_OUT_OF_SERVICE"
    assert data["outcome"] == "denied" and expected in data["reason_codes"]
    assert _checkout(h.services.db) == ("12:00", 1)


@pytest.mark.parametrize("gap", ["no_room_day_plan", "no_turnaround_policy", "overlapping_booking"])
def test_missing_or_inconsistent_operational_data_is_unavailable(
    make_harness: Any, db_path: Path, gap: str
) -> None:
    setup = Database(db_path)
    with setup.write() as s:
        if gap == "no_room_day_plan":
            plan = s.get(RoomDayPlan, "rdp_305_20260922")
            assert plan is not None
            s.delete(plan)
        elif gap == "no_turnaround_policy":
            policy_row = s.get(HotelPolicy, "hotel_demo")
            assert policy_row is not None
            policy_row.turnaround_minutes = None
        else:
            nxt = s.get(Reservation, "rsv_2001")
            assert nxt is not None
            nxt.scheduled_check_in = datetime(2026, 9, 22, 11, tzinfo=TZ)  # overlaps Emma
    setup.dispose()
    for tool, args in (
        ("evaluate_late_checkout", {"requested_checkout_local": iso(13)}),
        ("request_late_checkout", {"requested_checkout_local": iso(13), "reason": None}),
    ):
        h = make_harness(RuleModel(_tool_envelopes(tool, args)))
        token = h.session("G-001")["token"]
        before = business_fingerprint(h.services.db)
        envelope = _envelope(h, token)
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "OPERATIONAL_DATA_UNAVAILABLE"
        assert business_fingerprint(h.services.db) == before


# --- atomicity and concurrency ------------------------------------------------------------


def test_receipt_failure_rolls_back_the_checkout_change(
    hotel: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(checkout_service, "record_receipt", boom)
    token = hotel.session("G-001")["token"]
    op_before = _operational_version(hotel.services.db, "room_305")
    run = hotel.ask(token, "Please extend my checkout to 14:00")
    assert run["final_message"] == "That did not work: TOOL_FAILED."
    assert run["artifacts"] == []
    assert _checkout(hotel.services.db) == ("12:00", 1)
    assert _operational_version(hotel.services.db, "room_305") == op_before
    with hotel.services.db.read() as s:
        assert (
            s.scalar(select(AuditEvent).where(AuditEvent.event_type == "checkout_changed")) is None
        )


def test_concurrent_requests_extend_exactly_once(db_path: Path) -> None:
    setup = Database(db_path)
    with setup.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        binding = sessions.binding_of(demo)
    setup.dispose()
    results: list[Any] = []
    barrier = threading.Barrier(4)

    def worker(n: int) -> None:
        # Two share one operation key (replay); two are separate intents (no_change after).
        scope = WriteScope("hotel_demo", "rsv_1042", binding.session_id, "run_x", f"req-{n % 3}")
        db = Database(db_path, busy_timeout_ms=5000)
        barrier.wait()
        try:
            with db.write() as s:
                results.append(
                    checkout_service.request_late_checkout(
                        s, binding, scope, requested_checkout_local=iso(14), reason=None
                    )
                )
        except OperationalError as exc:  # pragma: no cover - diagnostic only
            results.append(exc)
        finally:
            db.dispose()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(isinstance(r, dict) for r in results), results
    applied = [r for r in results if r["outcome"] == "applied" and not r["replayed"]]
    assert len(applied) == 1
    assert all(r["outcome"] in ("applied", "no_change") for r in results)
    check = Database(db_path)
    assert _checkout(check) == ("14:00", 2)
    check.dispose()


# --- API and fixture upgrade --------------------------------------------------------------


def test_reservation_refresh_route_is_scoped(hotel: Harness) -> None:
    daniel = hotel.session("G-002")["token"]
    body = hotel.client.get("/api/me/reservation", headers=hotel.auth(daniel)).json()
    assert body["reservation"]["room_number"] == "412"
    assert body["reservation"]["reservation_reference"] == "R-1088"
    assert hotel.client.get("/api/me/reservation").status_code == 401
    overrides = hotel.client.get(
        "/api/me/reservation?reservation_id=rsv_1042", headers=hotel.auth(daniel)
    ).json()
    assert overrides["reservation"]["reservation_reference"] == "R-1088"


def test_next_arrivals_are_not_selectable(hotel: Harness) -> None:
    guests = hotel.client.get("/api/demo/guests").json()["guests"]
    assert [g["guest_id"] for g in guests] == ["G-001", "G-002", "G-003"]
    assert hotel.client.post("/api/demo/sessions", json={"guest_id": "G-901"}).status_code >= 400


def test_a_first_revision_database_upgrade_gets_the_operational_fixture(tmp_path: Path) -> None:
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
    with db.write() as s:
        _, demo = sessions.create_session(s, "G-002")
        binding = sessions.binding_of(demo)
    with db.read() as s:
        emma_15 = checkout_service.evaluate_late_checkout(
            s,
            replace(binding, guest_id="G-001", reservation_id="rsv_1042", room_id="room_305"),
            iso(15),
        )["data"]
        daniel_15 = checkout_service.evaluate_late_checkout(s, binding, iso(15))["data"]
        daniel_14 = checkout_service.evaluate_late_checkout(s, binding, iso(14))["data"]
    db.dispose()
    assert emma_15["decision"] == "approval_required"
    assert daniel_15["reason_codes"] == ["NEXT_ARRIVAL_CONFLICT"]
    assert daniel_14["decision"] == "auto_allowed"
