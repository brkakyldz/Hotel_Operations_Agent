"""Simulated staff move tasks along through the operator surface only."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from hotel_operations.storage.models import AuditEvent, HousekeepingTask, Reservation
from hotel_operations.tools.registry import active_tool_specs
from tests.conftest import Harness
from tests.support.models import RuleModel, hotel_rule


@pytest.fixture
def hotel(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(hotel_rule))
    return h


def _task(h: Harness, token: str, message: str = "two towels please") -> dict[str, Any]:
    run = h.ask(token, message)
    [artifact] = run["artifacts"]
    return {"run": run, "id": artifact["id"], "kind": artifact["kind"].removesuffix("_task")}


def _move(h: Harness, kind: str, task_id: str, action: str, version: int) -> Any:
    return h.client.post(
        f"/api/operator/tasks/{kind}/{task_id}/transition",
        json={"action": action, "expected_version": version},
    )


def test_operator_moves_a_task_to_completed_and_the_guest_sees_it(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    created = _task(hotel, token)
    board = hotel.client.get("/api/operator/tasks").json()
    [item] = board["tasks"]
    assert (item["task_id"], item["status"], item["version"]) == (created["id"], "pending", 1)
    assert item["guest_name"] == "Emma Wilson" and item["room_number"] == "305"
    assert item["actions"] == ["start", "cancel"]

    started = _move(hotel, "housekeeping", created["id"], "start", 1)
    assert started.status_code == 200, started.text
    assert (started.json()["task"]["status"], started.json()["task"]["version"]) == (
        "in_progress",
        2,
    )
    done = _move(hotel, "housekeeping", created["id"], "complete", 2).json()["task"]
    assert (done["status"], done["actions"]) == ("completed", [])

    # The guest's own views reflect it: request list and the run's receipt status.
    mine = hotel.client.get("/api/me/requests", headers=hotel.auth(token)).json()["requests"]
    assert [(r["id"], r["status"]) for r in mine] == [(created["id"], "completed")]
    assert mine[0]["status_meaning"] == "Marked completed in the simulator."
    run = hotel.client.get(
        f"/api/runs/{created['run']['run_id']}", headers=hotel.auth(token)
    ).json()
    assert run["artifacts"][0]["current_status"] == "completed"
    # Completed tasks leave the default (open) board.
    assert hotel.client.get("/api/operator/tasks").json()["tasks"] == []
    assert len(hotel.client.get("/api/operator/tasks?status=all").json()["tasks"]) == 1

    with hotel.services.db.read() as s:
        events = list(
            s.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == "task_status_changed")
                .order_by(AuditEvent.id)
            )
        )
        assert [(e.actor_type, e.outcome, e.payload["before"]) for e in events] == [
            ("operator", "in_progress", "pending"),
            ("operator", "completed", "in_progress"),
        ]
    hotel.settle()  # the guest is told in a hotel update's turn, after the move
    feed = hotel.client.get("/api/operator/events?limit=50").json()["events"]
    changed = next(e for e in feed if e["event_type"] == "task_status_changed")
    assert changed["payload"] == {"before": "in_progress", "after": "completed", "task_version": 3}


def test_maintenance_can_be_cancelled_while_in_progress(hotel: Harness) -> None:
    token = hotel.session("G-002")["token"]
    created = _task(hotel, token, "The air conditioning is not cooling")
    assert created["kind"] == "maintenance"
    assert _move(hotel, "maintenance", created["id"], "start", 1).status_code == 200
    body = _move(hotel, "maintenance", created["id"], "cancel", 2).json()["task"]
    assert body["status"] == "cancelled"
    assert body["details"]["category"] == "hvac"


def test_stale_version_and_illegal_moves_change_nothing(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    created = _task(hotel, token)
    illegal = _move(hotel, "housekeeping", created["id"], "complete", 1)  # not started yet
    assert (illegal.status_code, illegal.json()["error"]["code"]) == (409, "INVALID_TRANSITION")
    assert _move(hotel, "housekeeping", created["id"], "start", 1).status_code == 200
    stale = _move(hotel, "housekeeping", created["id"], "cancel", 1)  # replay of an old view
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "VERSION_CONFLICT")
    missing = _move(hotel, "housekeeping", "hk_does_not_exist", "start", 1)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "TASK_NOT_FOUND")
    wrong_kind = _move(hotel, "maintenance", created["id"], "start", 2)
    assert wrong_kind.status_code == 404
    with hotel.services.db.read() as s:
        row = s.get(HousekeepingTask, created["id"])
        assert row is not None and (row.status, row.version) == ("in_progress", 2)


def test_task_operations_are_operator_commands_not_model_tools(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    created = _task(hotel, token)
    extra = hotel.client.post(
        f"/api/operator/tasks/housekeeping/{created['id']}/transition",
        json={"action": "start", "expected_version": 1, "status": "completed"},
    )
    assert extra.status_code == 422
    names = {spec.name for spec in active_tool_specs()}
    assert not any("task" in n and n.startswith(("update", "complete", "start")) for n in names)
    assert names.isdisjoint({"complete_task", "update_task_status", "transition_task"})
    # Moving a task never touches the reservation.
    with hotel.services.db.read() as s:
        before = s.get(Reservation, "rsv_1042")
        assert before is not None
        version = before.version
    _move(hotel, "housekeeping", created["id"], "start", 1)
    with hotel.services.db.read() as s:
        after = s.get(Reservation, "rsv_1042")
        assert after is not None and after.version == version
