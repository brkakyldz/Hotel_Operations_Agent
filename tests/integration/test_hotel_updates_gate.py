"""The deterministic gate that decides a guest should hear about a hotel change.

The gate writes into the ``hotel_updates`` outbox inside the transaction of the change it
reports: an operator decision, a staff task move, a clock move or a world event. These
tests drive the real routes and services and read the outbox rows; delivery (the agent
turn) is tested in ``test_hotel_update_runs``; here it may or may not have happened.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from hotel_operations.services import operator as operator_service
from hotel_operations.storage.models import Approval, Hotel, HotelUpdate
from tests.conftest import Harness
from tests.support.models import RuleModel, Turn, call, say

TZ = ZoneInfo("Europe/Istanbul")
MARKER = "PRIVATE-NOTE-7731"


def iso(hour: int, minute: int = 0) -> str:
    return f"2026-09-22T{hour:02d}:{minute:02d}:00+03:00"


def _rule(turn: Turn) -> Any:
    """``checkout HH:MM``, ``towels``, ``cleaning`` and ``broken`` requests; replies with the
    raw envelope so the test reads the service result."""
    outputs = turn.tool_outputs_since_user()
    if outputs:
        return say(json.dumps(outputs[-1]))
    text = turn.last_user_text()
    if match := re.search(r"checkout (\d\d):(\d\d)", text):
        return call(
            "request_late_checkout",
            {
                "requested_checkout_local": iso(int(match.group(1)), int(match.group(2))),
                "reason": None,
            },
        )
    if "towels" in text:
        return call("create_housekeeping_task", {"item": "towels", "quantity": 2, "notes": MARKER})
    if "cleaning" in text:
        return call(
            "create_housekeeping_task", {"item": "room_cleaning", "quantity": 1, "notes": None}
        )
    if "broken" in text:
        return call(
            "create_maintenance_task",
            {"category": "hvac", "description": f"The AC is broken {MARKER}"},
        )
    return call("get_my_requests", {"cursor": None})


@pytest.fixture
def hotel(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(_rule))
    return h


def _ask(h: Harness, guest: str, text: str) -> dict[str, Any]:
    run = h.ask(h.session(guest)["token"], text)
    assert run["status"] == "completed", run
    envelope: dict[str, Any] = json.loads(run["final_message"])
    assert envelope["ok"], envelope
    data: dict[str, Any] = envelope["data"]
    return data


def _updates(h: Harness) -> list[dict[str, Any]]:
    with h.services.db.read() as s:
        rows = s.scalars(select(HotelUpdate).order_by(HotelUpdate.created_at, HotelUpdate.id))
        return [
            {
                "reservation": u.reservation_id,
                "kind": u.kind,
                "ref": u.ref,
                "facts": u.facts,
                "demo_time": u.demo_time.astimezone(TZ).strftime("%H:%M"),
            }
            for u in rows
        ]


def _clock(h: Harness, minutes: int) -> dict[str, Any]:
    r = h.client.post(
        "/api/operator/world/clock",
        json={"advance_minutes": minutes, "client_request_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def _event(h: Harness, key: str) -> None:
    r = h.client.post(
        "/api/operator/world/events", json={"event": key, "client_request_id": str(uuid.uuid4())}
    )
    assert r.status_code == 200, r.text


def _decide(h: Harness, approval_id: str, decision: str, rid: str | None = None) -> Any:
    [item] = [
        a
        for a in h.client.get("/api/operator/approvals?status=all").json()["approvals"]
        if a["approval_id"] == approval_id
    ]
    return h.client.post(
        f"/api/operator/approvals/{approval_id}/decision",
        json={
            "decision": decision,
            "expected_version": item["version"],
            "client_request_id": rid or str(uuid.uuid4()),
        },
    )


def _move(h: Harness, kind: str, task_id: str, action: str, version: int) -> None:
    r = h.client.post(
        f"/api/operator/tasks/{kind}/{task_id}/transition",
        json={"action": action, "expected_version": version},
    )
    assert r.status_code == 200, r.text


# --- review outcomes --------------------------------------------------------------------------


@pytest.mark.parametrize(("decision", "status"), [("approve", "executed"), ("reject", "rejected")])
def test_a_manager_decision_is_reported_to_the_guest(
    hotel: Harness, decision: str, status: str
) -> None:
    approval_id = _ask(hotel, "G-001", "checkout 15:00")["approval_id"]
    assert _updates(hotel) == []  # asking is the guest's own turn: nothing to report
    assert _decide(hotel, approval_id, decision).status_code == 200
    assert _updates(hotel) == [
        {
            "reservation": "rsv_1042",
            "kind": "review_closed",
            "ref": approval_id,
            "facts": {
                "approval_id": approval_id,
                "status": status,
                "status_reason": None,
                "requested_checkout_local": iso(15),
            },
            "demo_time": "10:00",
        }
    ]


def test_a_replayed_decision_reports_once(hotel: Harness) -> None:
    approval_id = _ask(hotel, "G-001", "checkout 15:00")["approval_id"]
    rid = str(uuid.uuid4())
    first = _decide(hotel, approval_id, "reject", rid)
    assert first.status_code == 200
    replay = hotel.client.post(
        f"/api/operator/approvals/{approval_id}/decision",
        json={"decision": "reject", "expected_version": 1, "client_request_id": rid},
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert [u["kind"] for u in _updates(hotel)] == ["review_closed"]


def test_a_rolled_back_decision_leaves_no_update(
    hotel: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The update commits with the change it reports, or not at all."""
    approval_id = _ask(hotel, "G-001", "checkout 15:00")["approval_id"]

    def fail(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("storage failed after the decision")

    monkeypatch.setattr(operator_service, "_jsonable", fail)
    with pytest.raises(RuntimeError):
        _decide(hotel, approval_id, "approve")
    assert _updates(hotel) == []
    with hotel.services.db.read() as s:
        row = s.get(Approval, approval_id)
        assert row is not None and row.status == "pending"


def test_the_clock_reaching_the_requested_time_is_reported(hotel: Harness) -> None:
    approval_id = _ask(hotel, "G-001", "checkout 14:30")["approval_id"]
    for minutes in (120, 120):
        _clock(hotel, minutes)
    assert _updates(hotel) == []  # 14:00: still waiting
    _clock(hotel, 30)
    [update] = _updates(hotel)
    assert (update["kind"], update["ref"], update["demo_time"]) == (
        "review_closed",
        approval_id,
        "14:30",
    )
    assert update["facts"]["status"] == "expired"
    assert update["facts"]["status_reason"] == "EXPIRED_BEFORE_DECISION"


def test_expiry_found_during_the_guests_own_turn_is_not_reported(hotel: Harness) -> None:
    """The guest's own agent turn already tells them; the gate stays out of it."""
    _ask(hotel, "G-001", "checkout 14:30")
    with hotel.services.db.write() as s:  # the clock moved without the clock command
        row = s.get(Hotel, "hotel_demo")
        assert row is not None
        row.demo_now = datetime(2026, 9, 22, 14, 30, tzinfo=TZ)
    data = _ask(hotel, "G-001", "checkout 15:00")
    assert data["outcome"] == "pending_approval"
    with hotel.services.db.read() as s:
        assert sorted(a.status for a in s.scalars(select(Approval))) == ["expired", "pending"]
    assert _updates(hotel) == []


# --- staff task outcomes ----------------------------------------------------------------------


def test_staff_finishing_or_cancelling_a_request_is_reported(hotel: Harness) -> None:
    towels = _ask(hotel, "G-001", "two towels please")["task_id"]
    broken = _ask(hotel, "G-002", "the AC is broken")["task_id"]
    _move(hotel, "housekeeping", towels, "start", 1)
    assert _updates(hotel) == []  # started is not finished
    _move(hotel, "housekeeping", towels, "complete", 2)
    _move(hotel, "maintenance", broken, "cancel", 1)
    updates = _updates(hotel)
    assert [(u["reservation"], u["kind"], u["ref"]) for u in updates] == [
        ("rsv_1042", "task_closed", towels),
        ("rsv_1088", "task_closed", broken),
    ]
    assert updates[0]["facts"] == {
        "task_id": towels,
        "task_kind": "housekeeping",
        "status": "completed",
        "item": "towels",
        "quantity": 2,
        "reason": None,
    }
    assert updates[1]["facts"] == {
        "task_id": broken,
        "task_kind": "maintenance",
        "status": "cancelled",
        "category": "hvac",
        "reason": None,
    }
    assert MARKER not in json.dumps(updates)  # a guest's own words never enter an update


def test_the_end_of_cleaning_hours_is_reported(hotel: Harness) -> None:
    for minutes in (120, 120):  # 14:00
        _clock(hotel, minutes)
    cleaning = _ask(hotel, "G-001", "cleaning please")["task_id"]
    _clock(hotel, 60)  # 15:00
    assert _updates(hotel) == []
    _clock(hotel, 60)  # 16:00
    [update] = _updates(hotel)
    assert (update["kind"], update["ref"]) == ("task_closed", cleaning)
    assert update["demo_time"] == "16:00"
    assert (update["facts"]["status"], update["facts"]["reason"]) == (
        "cancelled",
        "SERVICE_WINDOW_CLOSED",
    )


# --- hotel changes that affect the guest ------------------------------------------------------


def test_an_early_arrival_puts_the_waiting_review_at_risk(hotel: Harness) -> None:
    approval_id = _ask(hotel, "G-001", "checkout 15:00")["approval_id"]
    _event(hotel, "arrival_305_early")
    [update] = _updates(hotel)
    assert (update["reservation"], update["kind"], update["ref"]) == (
        "rsv_1042",
        "review_at_risk",
        approval_id,
    )
    assert update["facts"]["requested_checkout_local"] == iso(15)
    assert update["facts"]["cause"] == "arrival_305_early"
    assert update["facts"]["reason_codes"]  # why 15:00 no longer fits, as codes


def test_a_review_that_still_fits_is_not_at_risk(hotel: Harness) -> None:
    """14:30 still fits before the early arrival: no message (the approve would find the
    review stale, and that is reported when it happens)."""
    _ask(hotel, "G-001", "checkout 14:30")
    _event(hotel, "arrival_305_early")
    assert _updates(hotel) == []


def test_taking_the_room_out_of_service_is_reported(hotel: Harness) -> None:
    approval_id = _ask(hotel, "G-001", "checkout 15:00")["approval_id"]
    _event(hotel, "room_305_out_of_service")
    updates = {u["kind"]: u for u in _updates(hotel)}
    assert set(updates) == {"room_out_of_service", "review_at_risk"}
    assert updates["room_out_of_service"]["facts"] == {"room_number": "305"}
    assert updates["review_at_risk"]["ref"] == approval_id
    assert updates["review_at_risk"]["facts"]["reason_codes"] == ["ROOM_OUT_OF_SERVICE"]
    assert {u["reservation"] for u in updates.values()} == {"rsv_1042"}


def test_a_cancelled_arrival_makes_a_refused_time_possible(hotel: Harness) -> None:
    refused = _ask(hotel, "G-002", "checkout 15:00")
    assert refused["outcome"] == "denied"
    _event(hotel, "arrival_412_cancelled")
    [update] = _updates(hotel)
    assert (update["reservation"], update["kind"]) == ("rsv_1088", "checkout_now_possible")
    assert update["facts"] == {
        "requested_checkout_local": iso(15),
        "decision_now": "approval_required",
        "cause": "arrival_412_cancelled",
    }


def test_a_cancelled_arrival_tells_no_one_who_was_not_refused(hotel: Harness) -> None:
    _ask(hotel, "G-001", "checkout 15:00")  # Emma waits in another room
    _ask(hotel, "G-002", "checkout 13:00")  # Daniel got what he asked for
    _event(hotel, "arrival_412_cancelled")
    assert _updates(hotel) == []


def test_a_refusal_the_guest_moved_on_from_is_not_revived(hotel: Harness) -> None:
    _ask(hotel, "G-002", "checkout 15:00")  # refused
    assert _ask(hotel, "G-002", "checkout 13:30")["outcome"] == "applied"
    _event(hotel, "arrival_412_cancelled")
    assert _updates(hotel) == []
