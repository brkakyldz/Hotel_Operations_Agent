"""Operator-controlled world events and demo clock.

Every test drives the real Runner, gateway, services and SQLite. World changes go
through the operator HTTP routes, and their consequences are observed through the
guest's own tool results and the operator review queue.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import select, text

from hotel_operations.services import housekeeping as hk_service
from hotel_operations.services import sessions
from hotel_operations.services.writes import WriteScope
from hotel_operations.storage.models import (
    Approval,
    AuditEvent,
    Hotel,
    HousekeepingTask,
    Reservation,
    Room,
)
from hotel_operations.tools.registry import active_tool_specs
from tests.conftest import Harness
from tests.support.models import RuleModel, Turn, call, say

PRIVATE_MARKERS = ("R-2001", "R-2002", "rsv_2001", "rsv_2002", "G-901", "G-902", "Internal arrival")


def iso(hour: int, minute: int = 0) -> str:
    return f"2026-09-22T{hour:02d}:{minute:02d}:00+03:00"


def _envelope_rule(turn: Turn) -> Any:
    """``checkout HH:MM`` requests; anything else reads requests. Replies with the raw envelope."""
    outputs = turn.tool_outputs_since_user()
    if outputs:
        return say(json.dumps(outputs[-1]))
    match = re.search(r"checkout (\d\d):(\d\d)", turn.last_user_text())
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return call(
            "request_late_checkout",
            {"requested_checkout_local": iso(hour, minute), "reason": None},
        )
    return call("get_my_requests", {"cursor": None})


@pytest.fixture
def world(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(_envelope_rule))
    return h


def _ask(h: Harness, token: str, text: str) -> dict[str, Any]:
    run = h.ask(token, text)
    assert run["status"] == "completed", run
    envelope: dict[str, Any] = json.loads(run["final_message"])
    return envelope


def _request(h: Harness, token: str, hour: int, minute: int = 0) -> dict[str, Any]:
    envelope = _ask(h, token, f"checkout {hour:02d}:{minute:02d}")
    assert envelope["ok"], envelope
    data: dict[str, Any] = envelope["data"]
    return data


def _event(h: Harness, key: str, rid: str | None = None) -> Any:
    return h.client.post(
        "/api/operator/world/events",
        json={"event": key, "client_request_id": rid or str(uuid.uuid4())},
    )


def _clock(h: Harness, minutes: int, rid: str | None = None) -> Any:
    return h.client.post(
        "/api/operator/world/clock",
        json={"advance_minutes": minutes, "client_request_id": rid or str(uuid.uuid4())},
    )


def _pending(h: Harness) -> list[dict[str, Any]]:
    r = h.client.get("/api/operator/approvals")
    assert r.status_code == 200, r.text
    items: list[dict[str, Any]] = r.json()["approvals"]
    return items


def _decide(h: Harness, item: dict[str, Any], decision: str = "approve") -> dict[str, Any]:
    r = h.client.post(
        f"/api/operator/approvals/{item['approval_id']}/decision",
        json={
            "decision": decision,
            "expected_version": item["version"],
            "client_request_id": str(uuid.uuid4()),
        },
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


# --- authority and catalog --------------------------------------------------------------------


def test_world_commands_take_a_closed_body_and_no_actor(world: Harness) -> None:
    """No credential; the body still names nothing but the change itself."""
    for extra in ({"actor": "G-001"}, {"demo_now": "2026-09-22T18:00:00+03:00"}):
        r = world.client.post(
            "/api/operator/world/clock",
            json={"advance_minutes": 30, "client_request_id": str(uuid.uuid4()), **extra},
        )
        assert r.status_code == 422, extra
    with world.services.db.read() as s:
        hotel = s.get(Hotel, "hotel_demo")
        assert hotel is not None
        assert hotel.demo_now.hour == 7  # 10:00 Istanbul in UTC: nothing moved


def test_world_state_lists_the_closed_catalog(world: Harness) -> None:
    r = world.client.get("/api/operator/world")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["demo_time"] == iso(10)
    assert body["clock_limit"] == iso(18)
    assert body["clock_steps"] == [30, 60, 120]
    assert "fictional" in body["source"]
    keys = [e["key"] for e in body["events"]]
    assert keys == [
        "arrival_305_early",
        "arrival_412_cancelled",
        "room_305_out_of_service",
        "cleaning_305_shortened",
    ]
    assert all(e["available"] and not e["applied"] for e in body["events"])
    assert _event(world, "delete_everything").status_code == 404
    # The board offers each change next to the fact it changes; the shortened cleaning
    # window stays in the catalog but off the board.
    targets = {e["key"]: (e["room"], e["fact"]) for e in body["events"]}
    assert targets == {
        "arrival_305_early": ("305", "next_arrival"),
        "arrival_412_cancelled": ("412", "next_arrival"),
        "room_305_out_of_service": ("305", "in_service"),
        "cleaning_305_shortened": ("305", None),  # its room, but no fact: off the board
    }


def _board(h: Harness) -> dict[str, dict[str, Any]]:
    body = h.client.get("/api/operator/world").json()
    return {r["room_number"]: r for r in body["rooms"]}


def _latest(room: dict[str, Any]) -> tuple[str | None, bool, str | None]:
    return room["latest_checkout"], room["latest_needs_review"], room["latest_blocked_by"]


def test_the_board_shows_each_room_as_the_rules_see_it(world: Harness) -> None:
    body = world.client.get("/api/operator/world").json()
    assert body["policy"] == {
        "version": 1,
        "standard_checkout": "12:00",
        "automatic_until": "14:00",
        "manager_until": "16:00",
    }
    board = _board(world)
    assert list(board) == ["218", "305", "412"]
    emma, daniel, sofia = board["305"], board["412"], board["218"]
    assert (emma["guest_name"], emma["checkout"], emma["next_arrival"]) == (
        "Emma Wilson",
        iso(12),
        iso(18),
    )
    assert _latest(emma) == (iso(16), True, None)
    assert (daniel["next_arrival"], _latest(daniel)) == (iso(15), (iso(14), False, None))
    assert (sofia["status"], sofia["check_in"], _latest(sofia)) == (
        "confirmed",
        iso(15),
        (None, False, None),
    )
    text = json.dumps(body)
    for marker in PRIVATE_MARKERS:
        assert marker not in text  # only when a room must be ready, never who arrives


def test_every_board_change_shows_at_once(world: Harness) -> None:
    assert _event(world, "arrival_305_early").status_code == 200
    emma = _board(world)["305"]
    assert (emma["next_arrival"], _latest(emma)) == (iso(15, 30), (iso(14, 30), True, None))

    assert _event(world, "arrival_412_cancelled").status_code == 200
    daniel = _board(world)["412"]
    assert (daniel["next_arrival"], _latest(daniel)) == (None, (iso(16), True, None))

    assert _event(world, "room_305_out_of_service").status_code == 200
    emma = _board(world)["305"]
    assert (emma["in_service"], _latest(emma)) == (False, (None, False, "ROOM_OUT_OF_SERVICE"))


def test_the_board_follows_the_hotel_clock(world: Harness) -> None:
    for _ in range(3):
        assert _clock(world, 120).status_code == 200  # 10:00 → 16:00
    assert _latest(_board(world)["305"]) == (None, False, "TIME_PASSED")


def test_the_model_has_no_world_or_clock_tool() -> None:
    names = {spec.name for spec in active_tool_specs()}
    assert names == {
        "get_my_reservation",
        "get_hotel_policy",
        "create_housekeeping_task",
        "get_my_requests",
        "create_maintenance_task",
        "evaluate_late_checkout",
        "request_late_checkout",
        "evaluate_early_check_in",
        "request_early_check_in",
    }
    assert not any(word in name for name in names for word in ("world", "clock", "event"))


# --- each event changes a real decision -------------------------------------------------------


def test_early_arrival_makes_the_pending_review_stale_and_denies_a_new_request(
    world: Harness,
) -> None:
    emma = world.session("G-001")["token"]
    assert _request(world, emma, 15)["outcome"] == "pending_approval"
    [before] = _pending(world)
    assert before["fresh"] is True and before["changed_components"] == []
    assert before["blocking_reason"] is None and before["world_changes"] == []

    r = _event(world, "arrival_305_early")
    assert r.status_code == 200, r.text
    assert (r.json()["before"], r.json()["after"]) == ("next arrival 18:00", "next arrival 15:30")

    [item] = _pending(world)
    assert item["fresh"] is False
    assert item["changed_components"] == ["operational"]
    assert item["blocking_reason"] == "CHANGED:operational"
    [change] = item["world_changes"]
    assert change["kind"] == "event" and change["key"] == "arrival_305_early"

    decided = _decide(world, item)
    assert (decided["outcome"], decided["applied"]) == ("stale", False)
    assert decided["approval"]["status_reason"] == "CHANGED:operational"
    assert decided["approval"]["world_changes"][0]["key"] == "arrival_305_early"
    with world.services.db.read() as s:
        emma_rsv = s.get(Reservation, "rsv_1042")
        assert emma_rsv is not None and emma_rsv.version == 1  # checkout unchanged at 12:00

    again = _request(world, emma, 15)
    assert (again["outcome"], again["reason_codes"]) == ("denied", ["NEXT_ARRIVAL_CONFLICT"])
    assert again["latest_feasible_checkout_local"] == iso(14, 30)
    text = json.dumps(again)
    assert not any(marker in text for marker in PRIVATE_MARKERS)


def test_cancelled_arrival_turns_a_denial_into_a_review(world: Harness) -> None:
    daniel = world.session("G-002")["token"]
    first = _request(world, daniel, 14, 30)
    assert (first["outcome"], first["reason_codes"]) == ("denied", ["NEXT_ARRIVAL_CONFLICT"])
    assert _event(world, "arrival_412_cancelled").status_code == 200
    second = _request(world, daniel, 14, 30)
    assert second["outcome"] == "pending_approval"
    assert second["reason_codes"] == ["REQUIRES_MANAGER_REVIEW"]


def test_out_of_service_room_denies_late_checkout(world: Harness) -> None:
    emma = world.session("G-001")["token"]
    with world.services.db.read() as s:
        room = s.get(Room, "room_305")
        assert room is not None
        operational = room.operational_version
    r = _event(world, "room_305_out_of_service")
    assert r.status_code == 200 and r.json()["components"] == ["operational", "room"]
    with world.services.db.read() as s:
        room = s.get(Room, "room_305")
        other = s.get(Room, "room_412")
        assert room is not None and room.operational_version == operational + 1
        assert other is not None and other.operational_version == 1  # another room: untouched
    data = _request(world, emma, 13)
    assert data["outcome"] == "denied" and "ROOM_OUT_OF_SERVICE" in data["reason_codes"]


def test_shortened_cleaning_window_denies_and_moves_latest_feasible(world: Harness) -> None:
    emma = world.session("G-001")["token"]
    assert _event(world, "cleaning_305_shortened").status_code == 200
    data = _request(world, emma, 15)
    assert (data["outcome"], data["reason_codes"]) == ("denied", ["CLEANING_WINDOW_EXCEEDED"])
    assert data["latest_feasible_checkout_local"] == iso(14, 30)


def test_the_checkout_policy_cannot_be_changed_from_the_world(world: Harness) -> None:
    """The policy is fixed: no world event moves the checkout window."""
    assert _event(world, "policy_v2_auto_1300").status_code == 404
    body = world.client.get("/api/operator/world").json()
    assert body["policy"]["automatic_until"] == "14:00"
    assert all("policy" not in e["components"] for e in body["events"])


def test_another_rooms_event_neither_stales_nor_is_listed_on_a_review(world: Harness) -> None:
    """Room 412's change is not Emma's: her review stays fresh and does not list it."""
    emma = world.session("G-001")["token"]
    assert _request(world, emma, 15)["outcome"] == "pending_approval"
    assert _event(world, "arrival_412_cancelled").status_code == 200
    [item] = _pending(world)
    assert item["fresh"] is True and item["blocking_reason"] is None
    assert item["world_changes"] == []
    assert _event(world, "arrival_305_early").status_code == 200  # her own room's change
    [item] = _pending(world)
    assert item["fresh"] is False
    assert [c["key"] for c in item["world_changes"]] == ["arrival_305_early"]


# --- the clock ----------------------------------------------------------------------------------


def test_clock_reaching_the_requested_time_expires_the_review(world: Harness) -> None:
    """The hotel clock, not real time, ends a review, and the move that reaches
    the requested time records the expiry itself; a late approve finds nothing to decide."""
    emma = world.session("G-001")["token"]
    assert _request(world, emma, 15)["outcome"] == "pending_approval"
    for minutes in (120, 120):  # 14:00: still open
        moved = _clock(world, minutes).json()
        assert moved["side_effects"] == {"expired_reviews": [], "cancelled_tasks": []}
    [item] = _pending(world)
    assert item["expires_at"] == iso(15) and item["blocking_reason"] is None
    moved = _clock(world, 60).json()  # 15:00
    assert moved["side_effects"]["expired_reviews"] == [item["approval_id"]]
    assert _pending(world) == []
    with world.services.db.read() as s:
        row = s.get(Approval, item["approval_id"])
        assert row is not None
        assert (row.status, row.status_reason, row.version) == (
            "expired",
            "EXPIRED_BEFORE_DECISION",
            item["version"] + 1,
        )
        [event] = s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "approval_expired")
        ).all()
        assert (event.actor_type, event.entity_id) == ("system", item["approval_id"])
    for version, code in (
        (item["version"], "VERSION_CONFLICT"),  # the queue the manager had open is outdated
        (item["version"] + 1, "APPROVAL_NOT_PENDING"),  # and nothing is left to decide
    ):
        r = world.client.post(
            f"/api/operator/approvals/{item['approval_id']}/decision",
            json={
                "decision": "approve",
                "expected_version": version,
                "client_request_id": str(uuid.uuid4()),
            },
        )
        assert r.status_code == 409 and r.json()["error"]["code"] == code
    with world.services.db.read() as s:
        reservation = s.get(Reservation, row.reservation_id)
        assert reservation is not None
        assert reservation.version == 1  # nothing was applied


def test_a_clock_replay_reports_the_same_expiry_without_repeating_it(world: Harness) -> None:
    emma = world.session("G-001")["token"]
    approval_id = _request(world, emma, 14, 30)["approval_id"]
    for minutes in (120, 120):  # 14:00
        assert _clock(world, minutes).status_code == 200
    rid = str(uuid.uuid4())
    first = _clock(world, 30, rid).json()  # 14:30
    second = _clock(world, 30, rid).json()
    assert first["side_effects"]["expired_reviews"] == [approval_id]
    assert second["replayed"] and second["side_effects"] == first["side_effects"]
    with world.services.db.read() as s:
        assert len(s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "approval_expired")
        ).all()) == 1  # fmt: skip


def test_a_passed_pending_review_no_longer_blocks_a_new_request(world: Harness) -> None:
    emma = world.session("G-001")["token"]
    assert _request(world, emma, 14, 30)["outcome"] == "pending_approval"
    for minutes in (120, 120, 60):  # 15:00
        assert _clock(world, minutes).status_code == 200
    data = _request(world, emma, 15, 30)
    assert data["outcome"] == "pending_approval"
    with world.services.db.read() as s:
        rows = {a.status: a.status_reason for a in s.scalars(select(Approval))}
    assert rows == {"expired": "EXPIRED_BEFORE_DECISION", "pending": None}


def test_a_request_for_a_passed_time_is_rejected(world: Harness) -> None:
    emma = world.session("G-001")["token"]
    for minutes in (120, 120, 60):
        assert _clock(world, minutes).status_code == 200
    envelope = _ask(world, emma, "checkout 14:30")
    assert not envelope["ok"] and envelope["error"]["code"] == "INVALID_TIME"


def test_clock_only_moves_forward_in_fixed_steps_within_the_demo_day(world: Harness) -> None:
    assert _clock(world, 45).status_code == 422
    for _ in range(4):
        assert _clock(world, 120).status_code == 200
    state = world.client.get("/api/operator/world").json()
    assert state["demo_time"] == iso(18) and state["clock_steps"] == []
    r = _clock(world, 30)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DEMO_CLOCK_LIMIT"
    guest = world.client.get(
        "/api/session", headers=world.auth(world.session("G-003")["token"])
    ).json()
    assert guest["demo_time"] == iso(18)


def _housekeeping(h: Harness, guest: str, item: str, quantity: int = 1) -> str:
    """A housekeeping request filed through the real write service, as the tool files it."""
    with h.services.db.write() as s:
        _, demo = sessions.create_session(s, guest)
        binding = sessions.binding_of(demo)
        rid = uuid.uuid4().hex
        scope = WriteScope(
            "hotel_demo", binding.reservation_id, binding.session_id, f"run_{rid}", rid
        )
        out = hk_service.create_housekeeping_task(
            s, binding, scope, item=item, quantity=quantity, notes=None
        )
    assert out["outcome"] == "pending", out
    task_id: str = out["task_id"]
    return task_id


def _task_rows(h: Harness) -> dict[str, tuple[str, str | None]]:
    with h.services.db.read() as s:
        return {t.id: (t.status, t.status_reason) for t in s.scalars(select(HousekeepingTask))}


def test_the_end_of_cleaning_hours_cancels_only_unstarted_room_cleaning(world: Harness) -> None:
    """Crossing 16:00 closes the service window. A cleaning nobody started is
    cancelled by the system with its reason; started work and towels stay with staff."""
    for minutes in (120, 120):  # 14:00, inside cleaning hours
        assert _clock(world, minutes).status_code == 200
    waiting = _housekeeping(world, "G-001", "room_cleaning")
    started = _housekeeping(world, "G-002", "room_cleaning")
    towels = _housekeeping(world, "G-001", "towels", 2)
    r = world.client.post(
        f"/api/operator/tasks/housekeeping/{started}/transition",
        json={"action": "start", "expected_version": 1},
    )
    assert r.status_code == 200, r.text
    assert _clock(world, 60).json()["side_effects"]["cancelled_tasks"] == []  # 15:00
    moved = _clock(world, 60).json()  # 16:00: the window closes
    assert moved["side_effects"]["cancelled_tasks"] == [waiting]
    assert _task_rows(world) == {
        waiting: ("cancelled", "SERVICE_WINDOW_CLOSED"),
        started: ("in_progress", None),
        towels: ("pending", None),
    }
    with world.services.db.read() as s:
        [event] = s.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "task_status_changed", AuditEvent.entity_id == waiting
            )
        ).all()
        assert (event.actor_type, event.outcome) == ("system", "cancelled")
        assert event.reason_codes == ["SERVICE_WINDOW_CLOSED"]
    emma = world.session("G-001")["token"]
    mine = world.client.get("/api/me/requests", headers=world.auth(emma)).json()["requests"]
    cleaning = next(r for r in mine if r["id"] == waiting)
    assert (cleaning["status"], cleaning["status_reason"]) == ("cancelled", "SERVICE_WINDOW_CLOSED")
    assert _clock(world, 60).json()["side_effects"]["cancelled_tasks"] == []  # 17:00: once


# --- idempotency, one-shot events, preconditions ------------------------------------------------


def test_clock_replay_with_the_same_request_id_advances_once(world: Harness) -> None:
    rid = str(uuid.uuid4())
    first = _clock(world, 60, rid)
    second = _clock(world, 60, rid)
    assert first.status_code == second.status_code == 200
    assert (first.json()["replayed"], second.json()["replayed"]) == (False, True)
    assert second.json()["after"] == first.json()["after"] == "11:00"
    state = world.client.get("/api/operator/world").json()
    assert state["demo_time"] == iso(11)
    conflict = _clock(world, 30, rid)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_an_event_happens_once(world: Harness) -> None:
    rid = str(uuid.uuid4())
    assert _event(world, "room_305_out_of_service", rid).status_code == 200
    replay = _event(world, "room_305_out_of_service", rid)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    again = _event(world, "room_305_out_of_service")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "WORLD_EVENT_ALREADY_APPLIED"
    state = world.client.get("/api/operator/world").json()
    item = next(e for e in state["events"] if e["key"] == "room_305_out_of_service")
    assert item["applied"] and not item["available"]
    with world.services.db.read() as s:
        applied = s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "world_event_applied")
        ).all()
    assert len(applied) == 1


def test_early_arrival_refuses_to_overlap_an_extended_checkout(world: Harness) -> None:
    emma = world.session("G-001")["token"]
    assert _request(world, emma, 15)["outcome"] == "pending_approval"
    [item] = _pending(world)
    assert _decide(world, item)["outcome"] == "executed"  # checkout 15:00; ready 16:00 > 15:30
    r = _event(world, "arrival_305_early")
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORLD_EVENT_NOT_APPLICABLE"
    state = world.client.get("/api/operator/world").json()
    early = next(e for e in state["events"] if e["key"] == "arrival_305_early")
    assert not early["available"] and "too late" in early["unavailable_reason"]


def test_world_changes_appear_in_the_sanitized_audit_view(world: Harness) -> None:
    rid = str(uuid.uuid4())
    assert _clock(world, 30, rid).status_code == 200
    assert _event(world, "arrival_412_cancelled").status_code == 200
    events = world.client.get("/api/operator/events").json()["events"]
    [cancel, clock] = [e for e in events if e["entity_type"] == "world"]
    assert (cancel["event_type"], cancel["entity_id"]) == (
        "world_event_applied",
        "arrival_412_cancelled",
    )
    assert cancel["actor_type"] == "operator"
    assert (clock["payload"]["before"], clock["payload"]["after"]) == ("10:00", "10:30")
    text = json.dumps(events)
    assert rid not in text and "body_hash" not in text


# --- start over ---------------------------------------------------------------------


def _reset(h: Harness, body: Any = None) -> Any:
    return h.client.post(
        "/api/operator/world/reset", json={"confirm": True} if body is None else body
    )


def test_start_over_needs_an_explicit_confirmation(world: Harness) -> None:
    assert _event(world, "arrival_305_early").status_code == 200
    assert _reset(world, {}).status_code == 422
    assert _reset(world, {"confirm": False}).status_code == 422
    # Nothing was discarded by a refused reset.
    assert "arrival_305_early" in _applied(world.client.get("/api/operator/world"))


def _applied(response: Any) -> set[str]:
    assert response.status_code == 200, response.text
    return {e["key"] for e in response.json()["events"] if e["applied"]}


def _emma_checkout(h: Harness) -> Any:
    with h.services.db.read() as s:
        return s.scalars(select(Reservation).where(Reservation.guest_id == "G-001")).one()


def test_start_over_restores_the_fixture_while_the_app_keeps_running(world: Harness) -> None:
    original = _emma_checkout(world).scheduled_checkout
    guest = world.session("G-001")["token"]
    assert _request(world, guest, 15)["outcome"] == "pending_approval"
    assert _event(world, "arrival_305_early").status_code == 200
    assert _clock(world, 120).status_code == 200

    r = _reset(world)
    assert r.status_code == 200, r.text
    assert {e["key"] for e in r.json()["events"] if e["applied"]} == set()

    with world.services.db.read() as s:
        hotel = s.get(Hotel, "hotel_demo")
        assert hotel is not None
        assert hotel.demo_now.hour == 7  # back to 10:00 Istanbul
        assert s.scalars(select(Approval)).all() == []
    assert _emma_checkout(world).scheduled_checkout == original
    assert _pending(world) == []
    # The old world's guest sessions die with it.
    assert world.client.get("/api/session", headers=world.auth(guest)).status_code == 401

    # The same running app serves the fresh world: the scenario can be played again.
    fresh = world.session("G-001")["token"]
    assert _request(world, fresh, 15)["outcome"] == "pending_approval"


def test_start_over_waits_for_a_tool_call_the_sdk_left_running(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK cancels a stopped run's tool task without awaiting it. Start over still waits
    for that call's transaction to end; it used to refuse with DATABASE_IN_USE."""
    from hotel_operations.tools.gateway import ToolGateway

    inside, release = threading.Event(), threading.Event()
    original = ToolGateway._run_service

    def held_open(self: ToolGateway, *args: Any) -> Any:
        with self.db.read() as s:
            s.execute(text("SELECT 1"))
            inside.set()
            release.wait(10)
        return original(self, *args)

    monkeypatch.setattr(ToolGateway, "_run_service", held_open)
    h: Harness = make_harness()
    guest = h.session("G-001")["token"]
    assert h.chat(guest, "What is my reservation?").status_code == 202
    assert inside.wait(10)  # the tool call holds a connection
    threading.Timer(0.5, release.set).start()

    r = _reset(h)
    assert r.status_code == 200, r.text
    assert h.client.get("/api/session", headers=h.auth(guest)).status_code == 401


def test_requests_during_start_over_get_a_retryable_503(world: Harness) -> None:
    """A poll that lands mid-reset must not reopen the database the reset is replacing."""
    guest = world.session("G-001")["token"]
    world.services.resetting = True
    try:
        r = world.client.get("/api/session", headers=world.auth(guest))
    finally:
        world.services.resetting = False
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "RESET_IN_PROGRESS"
    assert r.json()["error"]["retryable"] is True
    assert world.client.get("/api/session", headers=world.auth(guest)).status_code == 200


def test_a_start_over_that_fails_early_still_leaves_a_working_app(
    world: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If recovering the stopped runs fails, the app must not keep the closed run manager."""
    from hotel_operations.agent.run_service import RunManager

    def boom(self: RunManager) -> None:
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(RunManager, "recover_interrupted", boom)
    with pytest.raises(RuntimeError):
        world.client.post("/api/operator/world/reset", json={"confirm": True})
    monkeypatch.undo()
    assert world.services.resetting is False
    guest = world.session("G-001")["token"]
    assert _request(world, guest, 15)["outcome"] == "pending_approval"


def test_the_board_shows_each_room_whole(make_harness: Any) -> None:
    """Cleaning state, open requests and a waiting review sit beside the stay."""
    from tests.support.models import hotel_rule

    h: Harness = make_harness(RuleModel(hotel_rule))
    emma = h.session("G-001")["token"]
    daniel = h.session("G-002")["token"]
    board = _board(h)
    assert (board["305"]["housekeeping_state"], board["218"]["housekeeping_state"]) == (
        "dirty",
        "inspected",
    )
    assert board["305"]["open_tasks"] == [] and board["305"]["pending_review"] is None

    h.ask(emma, "Could I get two towels? SECRET-NOTE please")
    h.ask(emma, "The air conditioning isn't cooling. SECRET-DESCRIPTION")
    h.ask(daniel, "Please clean my room.")
    h.ask(emma, "Can I check out at 15:00?")
    board = _board(h)
    assert [(t["kind"], t["what"], t["status"]) for t in board["305"]["open_tasks"]] == [
        ("housekeeping", "2 towels", "pending"),
        ("maintenance", "hvac", "pending"),
    ]
    assert [t["what"] for t in board["412"]["open_tasks"]] == ["room_cleaning"]
    review = board["305"]["pending_review"]
    assert (review["requested_checkout"], review["blocking_reason"]) == (iso(15), None)
    # A change the approve would find shows on the board at once, as in the review queue.
    assert _event(h, "arrival_305_early").status_code == 200
    assert _board(h)["305"]["pending_review"]["blocking_reason"] == "CHANGED:operational"
    text = json.dumps(h.client.get("/api/operator/world").json())
    assert "SECRET" not in text  # a guest's own words never reach the board
    for marker in PRIVATE_MARKERS:
        assert marker not in text
