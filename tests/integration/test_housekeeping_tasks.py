"""Hotel policy answers without writes; scoped, idempotent, atomic housekeeping tasks."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from hotel_operations import errors
from hotel_operations.services import audit as audit_module
from hotel_operations.services import housekeeping as hk_service
from hotel_operations.services import sessions
from hotel_operations.services.policy import read_policies
from hotel_operations.services.writes import WriteScope
from hotel_operations.storage.db import Database
from hotel_operations.storage.migrate import upgrade_to_head
from hotel_operations.storage.models import (
    AuditEvent,
    Base,
    HotelPolicy,
    HousekeepingTask,
    OperationReceipt,
)
from tests.conftest import Harness
from tests.support.fingerprint import business_fingerprint
from tests.support.models import RuleModel, Turn, call, hotel_rule, say


def _count(db: Database, model: type[Base]) -> int:
    with db.read() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def hotel(make_harness: Any) -> Harness:
    h: Harness = make_harness(RuleModel(hotel_rule))
    return h


# --- policy ------------------------------------------------------------------------------


def test_policy_values_are_the_stored_fixture(db: Database) -> None:
    with db.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        binding = sessions.binding_of(demo)
    with db.read() as s:
        read = read_policies(s, binding, ["breakfast", "housekeeping", "maintenance", "checkout"])
    breakfast, housekeeping, maintenance, checkout = read["data"]["topics"]
    assert (breakfast["breakfast_start_local"], breakfast["breakfast_end_local"]) == (
        "07:00",
        "10:30",
    )
    assert read["data"]["source"] == "Simulated hotel policy"
    assert housekeeping["supported_items"] == ["towels", "pillows", "room_cleaning"]
    assert housekeeping["delivery_time_promised"] is False
    assert maintenance["emergency_service"] is False
    # Checkout thresholds are stored fixture values.
    assert (
        checkout["standard_checkout_local"],
        checkout["automatic_extension_until_local"],
        checkout["reviewed_extension_until_local"],
        checkout["turnaround_minutes"],
    ) == ("12:00", "14:00", "16:00", 60)


def test_breakfast_question_makes_no_business_write(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "When is breakfast?")
    assert run["final_message"] == "Breakfast is served 07:00-10:30."
    assert [a["tool_name"] for a in run["activity"]] == ["get_hotel_policy"]
    assert run["artifacts"] == []
    assert business_fingerprint(hotel.services.db) == before


def test_a_first_revision_database_upgrade_gets_policy_row(tmp_path: Path) -> None:
    from alembic import command

    from hotel_operations.storage.migrate import alembic_config
    from tests.support.eras import seed_core_schema_era

    path = tmp_path / "first.sqlite"
    command.upgrade(alembic_config(path), "0001")
    db = Database(path)
    with db.write() as s:
        seed_core_schema_era(s)  # only the first revision's tables, as that build had
    db.dispose()
    upgrade_to_head(path)
    db = Database(path)
    with db.read() as s:
        policy = s.get(HotelPolicy, "hotel_demo")
        assert policy is not None and policy.breakfast_end_local == "10:30"
    db.dispose()


# --- housekeeping via the real Runner --------------------------------------------------------


def test_two_towels_create_exactly_one_pending_task(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "Could I get two towels please?")
    [artifact] = run["artifacts"]
    assert artifact["kind"] == "housekeeping_task"
    assert artifact["current_status"] == "pending"
    assert artifact["id"] in run["final_message"]
    with hotel.services.db.read() as s:
        [task] = list(s.scalars(select(HousekeepingTask)))
        assert (task.item, task.quantity, task.status) == ("towels", 2, "pending")
        assert (task.reservation_id, task.room_id) == ("rsv_1042", "room_305")
        assert task.originating_run_id == run["run_id"]
        receipt = s.scalar(select(OperationReceipt))
        assert receipt is not None and receipt.artifact_id == task.id == artifact["id"]
        events = [e.event_type for e in s.scalars(select(AuditEvent).order_by(AuditEvent.id))]
    assert "task_created" in events
    after = business_fingerprint(hotel.services.db)
    changed = {t for t in after if after[t] != before[t]}
    assert changed == {"housekeeping_tasks"}  # no room state change, no other table


def test_duplicate_identical_proposals_in_one_request_replay(make_harness: Any) -> None:
    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        args = {"item": "towels", "quantity": 2, "notes": "  extra   soft "}
        if not outs:
            return [call("create_housekeeping_task", args), call("create_housekeeping_task", args)]
        return say(json.dumps([o["data"] for o in outs]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    run = h.ask(token, "towels")
    first, second = json.loads(run["final_message"])
    assert first["task_id"] == second["task_id"]
    assert (first["replayed"], second["replayed"]) == (False, True)
    assert first["notes"] == "extra soft"  # canonicalized
    assert _count(h.services.db, HousekeepingTask) == 1
    assert _count(h.services.db, OperationReceipt) == 1


def test_http_replay_does_not_duplicate_but_new_intent_may(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    rid = str(uuid.uuid4())
    r1 = hotel.chat(token, "two towels", rid)
    hotel.wait(token, r1.json()["run_id"])
    r2 = hotel.chat(token, "two towels", rid)
    assert r2.json()["replayed"] is True
    assert _count(hotel.services.db, HousekeepingTask) == 1
    hotel.ask(token, "two towels")  # a new guest message is a new intent
    assert _count(hotel.services.db, HousekeepingTask) == 2


def test_arriving_guest_cannot_create_in_room_task(hotel: Harness) -> None:
    token = hotel.session("G-003")["token"]
    before = business_fingerprint(hotel.services.db)
    run = hotel.ask(token, "two towels please")
    assert run["final_message"] == "That did not work: NOT_CHECKED_IN."
    assert run["activity"][0]["status"] == "rejected"
    assert business_fingerprint(hotel.services.db) == before
    assert _count(hotel.services.db, OperationReceipt) == 0


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"item": "towels", "quantity": 7, "notes": None}, "INVALID_ARGUMENT"),
        ({"item": "towels", "quantity": 0, "notes": None}, "INVALID_ARGUMENT"),
        ({"item": "room_cleaning", "quantity": 2, "notes": None}, "INVALID_ARGUMENT"),
        ({"item": "champagne", "quantity": 1, "notes": None}, "INVALID_ARGUMENT"),
        ({"item": "towels", "quantity": 1, "notes": "x" * 301}, "INVALID_ARGUMENT"),
        (
            {"item": "towels", "quantity": 1, "notes": None, "room_id": "room_412"},
            "INVALID_ARGUMENT",
        ),
        ({"item": "towels", "quantity": 1}, "INVALID_ARGUMENT"),
    ],
)
def test_invalid_housekeeping_arguments_rejected(
    make_harness: Any, args: dict[str, Any], code: str
) -> None:
    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        return call("create_housekeeping_task", args) if not outs else say(json.dumps(outs[-1]))

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    envelope = json.loads(h.ask(token, "x")["final_message"])
    assert envelope["ok"] is False and envelope["error"]["code"] == code
    assert _count(h.services.db, HousekeepingTask) == 0


# --- atomicity and concurrency ----------------------------------------------------------------


@pytest.mark.parametrize("failpoint", ["audit", "receipt"])
def test_rollback_when_audit_or_receipt_fails(
    hotel: Harness, monkeypatch: pytest.MonkeyPatch, failpoint: str
) -> None:
    if failpoint == "audit":
        real = audit_module.append

        def boom_audit(session: Any, **kw: Any) -> Any:
            if kw.get("event_type") == "task_created":
                raise RuntimeError("audit write failed")
            return real(session, **kw)

        monkeypatch.setattr(audit_module, "append", boom_audit)
    else:

        def boom_receipt(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("receipt write failed")

        monkeypatch.setattr(hk_service, "record_receipt", boom_receipt)
    token = hotel.session("G-001")["token"]
    run = hotel.ask(token, "two towels")
    assert run["final_message"] == "That did not work: TOOL_FAILED."
    assert run["activity"][0]["status"] == "failed"
    assert run["artifacts"] == []
    assert _count(hotel.services.db, HousekeepingTask) == 0
    assert _count(hotel.services.db, OperationReceipt) == 0
    with hotel.services.db.read() as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.event_type == "task_created")) is None


def test_concurrent_identical_writes_create_one_task(db_path: Path) -> None:
    setup = Database(db_path)
    with setup.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        binding = sessions.binding_of(demo)
    setup.dispose()
    scope = WriteScope("hotel_demo", "rsv_1042", binding.session_id, "run_x", "req-1")
    results: list[Any] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        db = Database(db_path, busy_timeout_ms=5000)
        barrier.wait()
        try:
            with db.write() as s:
                results.append(
                    hk_service.create_housekeeping_task(
                        s, binding, scope, item="towels", quantity=2, notes=None
                    )
                )
        except OperationalError as exc:  # pragma: no cover - diagnostic only
            results.append(exc)
        finally:
            db.dispose()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(isinstance(r, dict) for r in results), results
    assert len({r["task_id"] for r in results}) == 1
    assert sorted(r["replayed"] for r in results) == [False, True, True, True]
    check = Database(db_path)
    assert _count(check, HousekeepingTask) == 1
    check.dispose()


def test_write_retry_only_after_confirmed_no_commit(
    hotel: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = hk_service.create_housekeeping_task
    calls = {"n": 0}

    def flaky(*a: Any, **kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return real(*a, **kw)

    import hotel_operations.tools.housekeeping as hk_tool

    monkeypatch.setattr(hk_tool, "create_housekeeping_task", flaky)
    token = hotel.session("G-001")["token"]
    run = hotel.ask(token, "two towels")
    assert calls["n"] == 2
    assert run["activity"][0]["status"] == "completed"
    assert _count(hotel.services.db, HousekeepingTask) == 1


def test_narration_failure_after_commit_keeps_receipt(make_harness: Any) -> None:
    def rule(turn: Turn) -> Any:
        if not turn.tool_outputs_since_user():
            return call(
                "create_housekeeping_task", {"item": "pillows", "quantity": 1, "notes": None}
            )
        return RuntimeError("provider died while narrating")

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    run = h.ask(token, "a pillow")
    assert run["status"] == "failed"
    assert run["outcome_detail"] == "action_committed_response_failed"
    [artifact] = run["artifacts"]
    assert artifact["kind"] == "housekeeping_task" and artifact["current_status"] == "pending"
    requests = h.client.get("/api/me/requests", headers=h.auth(token)).json()["requests"]
    assert [r["id"] for r in requests] == [artifact["id"]]


# --- requests projection ----------------------------------------------------------------------


def test_requests_are_reservation_scoped_and_survive_restart(make_harness: Any) -> None:
    h = make_harness(RuleModel(hotel_rule))
    emma = h.session("G-001")["token"]
    h.ask(emma, "two towels")
    h.ask(emma, "a pillow")
    daniel = h.session("G-002")["token"]
    assert h.client.get("/api/me/requests", headers=h.auth(daniel)).json()["requests"] == []
    overrides = h.client.get(
        "/api/me/requests?guest_id=G-001&reservation_id=rsv_1042", headers=h.auth(daniel)
    ).json()
    assert overrides["requests"] == []  # query parameters cannot widen scope
    h2 = make_harness(RuleModel(hotel_rule))  # new process over the same files
    emma_again = h2.session("G-001")["token"]  # new session, same reservation
    body = h2.client.get("/api/me/requests", headers=h2.auth(emma_again)).json()
    assert [r["details"]["item"] for r in body["requests"]] == ["pillows", "towels"]
    assert all(r["status"] == "pending" for r in body["requests"])
    assert all("not yet" in r["status_meaning"] for r in body["requests"])
    run = h2.ask(emma_again, "what requests do I have?")
    assert run["final_message"] == "You have 2 request(s), 2 pending."


def test_requests_pagination_and_cursor_validation(db: Database) -> None:
    from hotel_operations.services.requests import list_my_requests

    with db.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        binding = sessions.binding_of(demo)
        # 25 requests on one stay: lift the per-stay towel limit (housekeeping policy v1).
        policy = s.get(HotelPolicy, "hotel_demo")
        assert policy is not None
        policy.towels_per_stay = 100
    for i in range(25):
        scope = WriteScope("hotel_demo", "rsv_1042", binding.session_id, f"run_{i}", f"req-{i}")
        with db.write() as s:
            hk_service.create_housekeeping_task(
                s, binding, scope, item="towels", quantity=1, notes=f"n{i}"
            )
    with db.read() as s:
        first = list_my_requests(s, binding, None)["data"]
        second = list_my_requests(s, binding, first["next_cursor"])["data"]
        with pytest.raises(errors.DomainError) as exc:
            list_my_requests(s, binding, "not-a-cursor!!")
    assert len(first["requests"]) == 20 and first["next_cursor"]
    assert len(second["requests"]) == 5 and second["next_cursor"] is None
    ids = [r["id"] for r in first["requests"] + second["requests"]]
    assert len(set(ids)) == 25
    assert exc.value.code == "INVALID_CURSOR"


def test_the_models_request_list_stays_inside_the_projection_bound(db: Database) -> None:
    """A stay full of long reports still reads: the tool pages by 10 and clips guest text."""
    from hotel_operations.config import get_settings
    from hotel_operations.services import maintenance as mt_service
    from hotel_operations.services.requests import TOOL_PAGE_SIZE, TOOL_TEXT_LIMIT
    from hotel_operations.tools.requests import GET_MY_REQUESTS, RequestsArgs

    with db.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        binding = sessions.binding_of(demo)
    for i in range(25):
        scope = WriteScope("hotel_demo", "rsv_1042", binding.session_id, f"run_{i}", f"mt-{i}")
        with db.write() as s:
            mt_service.create_maintenance_task(
                s, binding, scope, category="other", description=f"{i:02d} " + "é" * 497
            )
    with db.read() as s:
        result = GET_MY_REQUESTS.handler(s, binding, RequestsArgs(cursor=None), None)  # type: ignore[arg-type]
    page = result.data["requests"]
    assert len(page) == TOOL_PAGE_SIZE and result.data["next_cursor"]
    assert all(len(r["details"]["guest_description"]) <= TOOL_TEXT_LIMIT for r in page)
    assert page[0]["details"]["guest_description"].endswith("…")
    text = json.dumps({"data": result.data, "meta": result.meta}, ensure_ascii=False)
    assert len(text) < get_settings().max_tool_projection_chars * 3 // 4


def test_api_invalid_cursor_is_422(hotel: Harness) -> None:
    token = hotel.session("G-001")["token"]
    r = hotel.client.get("/api/me/requests?cursor=%%%", headers=hotel.auth(token))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_CURSOR"
