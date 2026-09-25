"""Gateway failure paths: deadlines, transient storage, scope expiry, context limit."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from hotel_operations.clock import utcnow
from hotel_operations.storage.models import AuditEvent, DemoSession
from hotel_operations.tools import reservation as reservation_tool
from tests.conftest import Harness
from tests.support.models import RuleModel, Turn, call, say


def _echo_tool_result(turn: Turn) -> Any:
    outs = turn.tool_outputs_since_user()
    return call("get_my_reservation") if not outs else say(json.dumps(outs[-1]))


def test_tool_deadline_exceeded_is_reported_not_success(make_harness: Any) -> None:
    h = make_harness(RuleModel(_echo_tool_result), tool_deadline_seconds=1e-9)
    token = h.session()["token"]
    run = h.ask(token, "checkout?")
    envelope = json.loads(run["final_message"])
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "DEADLINE_EXCEEDED"
    assert run["activity"][0]["status"] == "failed"


def test_read_retries_once_after_transient_storage_failure(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = reservation_tool.read_my_reservation
    calls = {"n": 0}

    def flaky(session: Any, binding: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return real(session, binding)

    monkeypatch.setattr(reservation_tool, "read_my_reservation", flaky)
    h = make_harness(RuleModel(_echo_tool_result))
    token = h.session()["token"]
    run = h.ask(token, "checkout?")
    assert json.loads(run["final_message"])["ok"] is True
    assert calls["n"] == 2


def test_persistent_storage_failure_is_safe_data_unavailable(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(session: Any, binding: Any) -> Any:
        raise OperationalError("SELECT secret_sql FROM x", {}, Exception("disk I/O error"))

    monkeypatch.setattr(reservation_tool, "read_my_reservation", broken)
    h = make_harness(RuleModel(_echo_tool_result))
    token = h.session()["token"]
    run = h.ask(token, "checkout?")
    envelope = json.loads(run["final_message"])
    assert envelope["error"]["code"] == "DATA_UNAVAILABLE"
    assert envelope["error"]["retryable"] is True
    assert "secret_sql" not in run["final_message"] and "disk" not in run["final_message"]


def test_session_closing_mid_run_blocks_tool(make_harness: Any) -> None:
    holder: dict[str, Any] = {}

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            with holder["db"].write() as s:
                for row in s.scalars(select(DemoSession)):
                    row.closed_at = utcnow()
            return call("get_my_reservation")
        return say(json.dumps(outs[-1]))

    h = make_harness(RuleModel(rule))
    holder["db"] = h.services.db
    token = h.session()["token"]
    r = h.chat(token, "checkout?")
    run_id = r.json()["run_id"]
    h.client.portal.call(h.services.run_manager.wait_idle)
    with h.services.db.read() as s:
        rejected = s.scalar(
            select(AuditEvent).where(
                AuditEvent.run_id == run_id, AuditEvent.event_type == "tool_rejected"
            )
        )
        assert rejected is not None and rejected.reason_codes == ["INVALID_SESSION"]


def test_context_limit_rejected_before_dispatch(make_harness: Any) -> None:
    model = RuleModel(lambda t: say("should not be called"))
    h = make_harness(model, max_estimated_input_chars=100)
    token = h.session()["token"]
    run = h.ask(token, "x" * 200)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "CONTEXT_LIMIT"
    assert model.turns == []


def test_storage_unavailable_at_admission_is_503_and_nothing_accepted(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hotel_operations.api.routes as routes

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("INSERT INTO runs", {}, Exception("database is locked"))

    token = harness.session()["token"]
    monkeypatch.setattr(routes, "admit_run", broken)
    r = harness.chat(token, "hi")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert "INSERT" not in r.text


def test_parallel_proposals_are_serialized_by_gateway(make_harness: Any) -> None:
    active = {"now": 0, "max": 0}
    lock = threading.Lock()
    real = reservation_tool.read_my_reservation

    def tracking(session: Any, binding: Any) -> Any:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            import time

            time.sleep(0.05)
            return real(session, binding)
        finally:
            with lock:
                active["now"] -= 1

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs:
            return [call("get_my_reservation"), call("get_my_reservation")]
        return say("ok")

    mp = pytest.MonkeyPatch()
    mp.setattr(reservation_tool, "read_my_reservation", tracking)
    try:
        h = make_harness(RuleModel(rule))
        token = h.session()["token"]
        run = h.ask(token, "x")
    finally:
        mp.undo()
    assert [a["status"] for a in run["activity"]] == ["completed", "completed"]
    assert active["max"] == 1
