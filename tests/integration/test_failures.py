"""Reproducible failure scenarios preserve invariants and report accurate outcomes.

Each test injects one fault at a named boundary and asserts both the persisted state and
what the guest-facing projection says.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from agents import SQLiteSession
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from hotel_operations.clock import utcnow
from hotel_operations.services import housekeeping as hk_service
from hotel_operations.services import sessions
from hotel_operations.services.writes import WriteScope
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import (
    AuditEvent,
    Base,
    DemoSession,
    HousekeepingTask,
    MaintenanceTask,
    RunRecord,
)
from tests.support.models import RuleModel, Turn, call, hotel_rule, say

FAKE_KEY = "sk" + "-test-FAKE-KEY-0123456789abcdef"  # split so the leak scanner ignores it


def _count(db: Database, model: type[Base]) -> int:
    with db.read() as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.02)


def _telemetry(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- process interruption ------------------------------------------------------------------


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_process_stop_mid_run_is_interrupted_never_replayed(
    make_harness: Any, db_path: Path, boundary: str
) -> None:
    async def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if boundary == "after_commit" and not outs:
            return call(
                "create_housekeeping_task", {"item": "towels", "quantity": 2, "notes": None}
            )
        await asyncio.sleep(3600)  # the model call is still in flight when the process stops
        return say("unreachable")  # pragma: no cover

    h1 = make_harness(RuleModel(rule))
    token = h1.session("G-001")["token"]
    run_id = h1.chat(token, "two towels").json()["run_id"]
    db = h1.services.db
    if boundary == "after_commit":
        _wait_until(lambda: _count(db, HousekeepingTask) == 1)
    else:
        _wait_until(lambda: len(h1.model.turns) >= 1)
    h1.stop()

    h2 = make_harness(RuleModel(hotel_rule))  # a fresh process over the same files
    run = h2.client.get(f"/api/runs/{run_id}", headers=h2.auth(token)).json()
    assert (run["status"], run["error"]["code"]) == ("interrupted", "RUN_INTERRUPTED")
    # A graceful stop rolls back exactly this run's conversation items; the
    # conversation itself is not restarted and holds nothing from the interrupted run.
    assert run["turn_forgotten"] is True and run["conversation_restarted"] is False
    with h2.services.db.read() as s:
        row = s.get(RunRecord, run_id)
        assert row is not None
        key = row.conversation_key
    stored = SQLiteSession(key, h2.services.settings.conversations_db_path)
    try:
        assert h2.client.portal.call(stored.get_items) == []
    finally:
        stored.close()
    tasks = _count(h2.services.db, HousekeepingTask)
    if boundary == "after_commit":
        assert tasks == 1
        assert [a["kind"] for a in run["artifacts"]] == ["housekeeping_task"]
        assert run["outcome_detail"] == "action_committed_response_failed"
    else:
        assert tasks == 0 and run["artifacts"] == [] and run["outcome_detail"] is None
    time.sleep(0.2)
    assert _count(h2.services.db, HousekeepingTask) == tasks  # nothing was re-executed


def test_hard_crash_after_commit_is_recovered_at_startup(make_harness: Any, db_path: Path) -> None:
    """Process death without shutdown: the run row still says running at the next start."""
    db = Database(db_path)
    with db.write() as s:
        _, demo = sessions.create_session(s, "G-001")
        binding = sessions.binding_of(demo)
        s.add(
            RunRecord(
                id="run_crashed",
                session_id=demo.id,
                client_request_id=str(uuid.uuid4()),
                input_hash="0" * 64,
                user_message="two towels",
                status="running",
                conversation_key=demo.conversation_key,
                created_at=utcnow(),
                started_at=utcnow(),
            )
        )
    scope = WriteScope("hotel_demo", "rsv_1042", binding.session_id, "run_crashed", "rid")
    with db.write() as s:
        hk_service.create_housekeeping_task(
            s, binding, scope, item="towels", quantity=2, notes=None
        )
    db.dispose()

    h = make_harness(RuleModel(hotel_rule))
    with h.services.db.read() as s:
        row = s.get(RunRecord, "run_crashed")
        assert row is not None and row.status == "interrupted"
        demo_row = s.get(DemoSession, binding.session_id)
        assert demo_row is not None and demo_row.context_restarted_at is not None
    assert _count(h.services.db, HousekeepingTask) == 1


# --- lost responses and commit uncertainty ---------------------------------------------------


def test_lost_chat_response_after_a_write_replays_the_same_run(make_harness: Any) -> None:
    h = make_harness(RuleModel(hotel_rule))
    token = h.session("G-001")["token"]
    rid = str(uuid.uuid4())
    first = h.chat(token, "two towels", rid).json()
    done = h.wait(token, first["run_id"])
    # The browser never saw the 202 and retries the identical request.
    again = h.chat(token, "two towels", rid).json()
    assert (again["run_id"], again["replayed"]) == (first["run_id"], True)
    assert _count(h.services.db, HousekeepingTask) == 1
    replayed = h.wait(token, again["run_id"])
    assert replayed["artifacts"] == done["artifacts"]


def test_commit_uncertainty_is_resolved_by_receipt_not_by_rewriting(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_harness(RuleModel(hotel_rule))
    gateway = h.services.run_manager.gateway
    real = gateway._run_service
    raised = {"n": 0}

    def commit_then_fail(spec: Any, ctx: Any, args: Any, call_: Any) -> Any:
        result = real(spec, ctx, args, call_)  # the transaction has committed here
        if spec.kind == "write" and raised["n"] == 0:
            raised["n"] += 1
            raise OperationalError("COMMIT", {}, Exception("disk I/O error"))
        return result

    monkeypatch.setattr(gateway, "_run_service", commit_then_fail)
    token = h.session("G-001")["token"]
    run = h.ask(token, "two towels")
    assert raised["n"] == 1
    assert run["activity"][0]["status"] == "completed"
    assert run["final_message"].startswith("Recorded request hk_")
    assert _count(h.services.db, HousekeepingTask) == 1  # found by receipt, not written twice


def test_database_contention_on_a_write_fails_safely_then_recovers(
    make_harness: Any, db_path: Path
) -> None:
    holding, release = threading.Event(), threading.Event()

    def hold_writer_lock() -> None:
        other = Database(db_path)
        with other.write() as s:
            s.execute(text("SELECT count(*) FROM runs")).scalar()
            holding.set()
            release.wait(10)
        other.dispose()

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if not outs and "contention" in turn.last_user_text():
            threading.Thread(target=hold_writer_lock, daemon=True).start()
            assert holding.wait(5)
            return call(
                "create_housekeeping_task", {"item": "towels", "quantity": 1, "notes": None}
            )
        if outs and not outs[-1].get("ok"):
            release.set()  # let the run record its own outcome
            return say(f"That did not work: {outs[-1]['error']['code']}.")
        return hotel_rule(turn)

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    try:
        run = h.ask(token, "towel during contention")
    finally:
        release.set()  # never leave the external writer holding the lock
    assert run["final_message"] == "That did not work: DATA_UNAVAILABLE."
    assert run["activity"][0]["status"] == "failed"
    assert _count(h.services.db, HousekeepingTask) == 0
    assert h.ask(token, "one towel please")["activity"][0]["status"] == "completed"
    assert _count(h.services.db, HousekeepingTask) == 1
    with h.services.db.read() as s:
        failed = s.scalars(
            select(AuditEvent).where(
                AuditEvent.run_id == run["run_id"], AuditEvent.event_type == "tool_failed"
            )
        ).one()
        assert failed.reason_codes == ["DATA_UNAVAILABLE"]  # evidence recorded late, not lost


def test_lock_contention_on_the_terminal_write_does_not_strand_or_fail_the_run(
    make_harness: Any, db_path: Path
) -> None:
    holding = threading.Event()

    def hold_briefly() -> None:
        other = Database(db_path)
        with other.write() as s:
            s.execute(text("SELECT count(*) FROM runs")).scalar()
            holding.set()
            time.sleep(1.5)  # longer than the busy timeout, shorter than the retry budget
        other.dispose()

    def rule(turn: Turn) -> Any:
        threading.Thread(target=hold_briefly, daemon=True).start()
        assert holding.wait(5)
        return say("Here is your answer.")

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    run = h.ask(token, "hello")
    assert run["status"] == "completed"
    assert run["final_message"] == "Here is your answer."


def _provider_error(kind: str) -> Exception:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    if kind == "unreachable":
        return openai.APIConnectionError(request=request)
    return openai.InternalServerError(
        "upstream error", response=httpx.Response(500, request=request), body=None
    )


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("timeout", "PROVIDER_TIMEOUT"),
        ("unreachable", "PROVIDER_UNAVAILABLE"),
        ("server_error", "PROVIDER_ERROR"),
    ],
)
def test_provider_failure_after_a_commit_is_retryable_and_keeps_the_receipt(
    make_harness: Any, kind: str, code: str
) -> None:
    def rule(turn: Turn) -> Any:
        if not turn.tool_outputs_since_user():
            return call(
                "create_housekeeping_task", {"item": "towels", "quantity": 1, "notes": None}
            )
        return _provider_error(kind)  # the narration turn never arrives

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    run = h.ask(token, "towels please")
    assert run["status"] == "failed"
    assert run["error"]["code"] == code
    assert run["error"]["retryable"] is True
    assert run["final_message"] is None  # no invented narration
    assert run["activity"][0]["status"] == "completed"
    assert run["outcome_detail"] == "action_committed_response_failed"
    assert [a["kind"] for a in run["artifacts"]] == ["housekeeping_task"]
    assert _count(h.services.db, HousekeepingTask) == 1
    requests = h.client.get("/api/me/requests", headers=h.auth(token)).json()
    assert len(requests["requests"]) == 1


# --- logs, telemetry and sanitized projections ---------------------------------------------


def test_logs_telemetry_and_audit_hold_no_secrets_text_or_reasoning(
    make_harness: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    description = "PRIVATE-GUEST-TEXT the shower is leaking"

    def rule(turn: Turn) -> Any:
        outs = turn.tool_outputs_since_user()
        if "fail" in turn.last_user_text():
            return RuntimeError("provider exploded")
        if not outs:
            return call(
                "create_maintenance_task", {"category": "plumbing", "description": description}
            )
        return say("Reported.")

    h = make_harness(
        RuleModel(rule),
        openai_api_key=SecretStr(FAKE_KEY),
    )
    token = h.session("G-001")["token"]
    h.ask(token, f"please report: {description}")
    h.ask(token, "now fail")
    h.stop()
    telemetry_text = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    with Database(Path(h.services.settings.hotel_db_path)).read() as s:
        audit_text = json.dumps([e.payload for e in s.scalars(select(AuditEvent))])
    for blob in (caplog.text, telemetry_text, audit_text):
        assert FAKE_KEY not in blob
        assert token not in blob
        assert "PRIVATE-GUEST-TEXT" not in blob
        assert "encrypted_content" not in blob
    assert "please report" not in telemetry_text  # user messages are not telemetry
    with Database(Path(h.services.settings.hotel_db_path)).read() as s:
        task = s.scalar(select(MaintenanceTask))
        assert task is not None and task.description == description  # kept where it belongs


def test_telemetry_sums_usage_and_matches_the_run_record(make_harness: Any, tmp_path: Path) -> None:
    h = make_harness(RuleModel(hotel_rule))
    token = h.session("G-001")["token"]
    run = h.ask(token, "two towels")
    events = _telemetry(tmp_path / "telemetry.jsonl")
    [finished] = [
        e for e in events if e["event"] == "run_finished" and e["run_id"] == run["run_id"]
    ]
    tools = [e for e in events if e["event"] == "tool_finished" and e["run_id"] == run["run_id"]]
    assert finished["status"] == "completed" and finished["usage_complete"] is True
    assert finished["model_calls"] == finished["sdk_reported_requests"] == 2
    assert finished["total_tokens"] == run["usage"]["total_tokens"] == 2 * 120
    assert finished["latency_ms"] == run["latency_ms"]
    assert [(t["tool"], t["status"]) for t in tools] == [("create_housekeeping_task", "completed")]
    assert any(e["event"] == "run_admitted" and e["run_id"] == run["run_id"] for e in events)


def test_failed_run_telemetry_leaves_unknown_usage_null(make_harness: Any, tmp_path: Path) -> None:
    def rule(turn: Turn) -> Any:
        return RuntimeError("provider exploded before any usage")

    h = make_harness(RuleModel(rule))
    token = h.session("G-001")["token"]
    run = h.ask(token, "hello")
    [finished] = [
        e
        for e in _telemetry(tmp_path / "telemetry.jsonl")
        if e["event"] == "run_finished" and e["run_id"] == run["run_id"]
    ]
    assert (finished["status"], finished["error_code"]) == ("failed", "RUN_FAILED")
    assert finished["sdk_reported_requests"] is None and finished["usage_complete"] is False
    # No model call reported usage: every total is unknown, never zero.
    assert finished["model_calls"] == 0
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert finished[key] is None, key
    stored = h.client.get(f"/api/runs/{run['run_id']}", headers=h.auth(token)).json()["usage"]
    assert stored["total_tokens"] is None and stored["complete"] is False


def test_operator_event_projection_is_sanitized(make_harness: Any) -> None:
    h = make_harness(RuleModel(hotel_rule))
    token = h.session("G-001")["token"]
    h.ask(token, "The shower is leaking PRIVATE-DESC")
    h.ask(token, "Please extend my checkout to 14:00")
    body = h.client.get("/api/operator/events?limit=50").json()
    types = [e["event_type"] for e in body["events"]]
    assert {"session_created", "task_created", "checkout_changed", "tool_completed"} <= set(types)
    dumped = json.dumps(body)
    assert "PRIVATE-DESC" not in dumped and token not in dumped
    changed = next(e for e in body["events"] if e["event_type"] == "checkout_changed")
    assert changed["payload"]["after"] == "2026-09-22T14:00:00+03:00"
    older = h.client.get(f"/api/operator/events?limit=2&before_id={body['events'][0]['id']}")
    assert [e["id"] for e in older.json()["events"]] == [e["id"] for e in body["events"][1:3]]


def test_empty_telemetry_path_disables_the_file_sink(tmp_path: Path, db_path: Path) -> None:
    from tests.conftest import make_settings

    settings = make_settings(tmp_path, db_path)
    assert settings.telemetry_log_path == tmp_path / "telemetry.jsonl"
    disabled = type(settings)(_env_file=None, HOTEL_TELEMETRY_LOG="")  # type: ignore[call-arg]
    assert disabled.telemetry_log_path is None


def test_storage_failure_while_starting_a_run_is_a_retryable_storage_error(
    make_harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hotel_operations.agent.run_service import RunManager

    original = RunManager._mark_running
    calls = {"n": 0}

    def flaky(self: RunManager, run_id: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
        return original(self, run_id)

    monkeypatch.setattr(RunManager, "_mark_running", flaky)
    h = make_harness()
    token = h.session("G-001")["token"]
    failed = h.ask(token, "hello")
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "STORAGE_UNAVAILABLE"
    assert failed["error"]["retryable"] is True
    assert h.ask(token, "hello again")["status"] == "completed"


@pytest.mark.parametrize("outcome", ["commits", "refused"])
def test_cancelled_audit_write_settles_without_duplicate_or_loss(outcome: str) -> None:
    from types import SimpleNamespace

    from hotel_operations.tools.gateway import AgentRunContext, ToolGateway

    written: list[list[dict[str, Any]]] = []

    def slow_audit(entries: list[dict[str, Any]]) -> None:
        time.sleep(0.2)  # still running when the run is cancelled
        if outcome == "refused":
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
        written.append(list(entries))

    gateway = ToolGateway.__new__(ToolGateway)
    gateway._audit = slow_audit  # type: ignore[method-assign]
    binding = SimpleNamespace(hotel_id="hotel_demo", session_id="ses_x")
    ctx = AgentRunContext(binding=binding, run_id="run_x", client_request_id="c", deadline=0.0)  # type: ignore[arg-type]
    ctx.deferred_audit.append({"event_type": "tool_proposed", "run_id": "run_x"})

    async def cancel_mid_write() -> None:
        task = asyncio.create_task(gateway._audit_async(ctx, event_type="tool_failed"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_mid_write())
    if outcome == "commits":
        assert [len(w) for w in written] == [2]
        assert ctx.deferred_audit == []  # committed once: nothing left to write again
    else:
        assert written == []
        assert [e["event_type"] for e in ctx.deferred_audit] == ["tool_proposed", "tool_failed"]


def test_a_cancelled_tool_call_waits_for_its_storage_thread() -> None:
    """A stop or Start over never finds a cancelled run still inside a tool's transaction.

    Start over cancels in-flight runs and then needs the database to itself; an abandoned
    thread still holding its connection made it refuse with DATABASE_IN_USE.
    """
    from hotel_operations.tools.gateway import ToolGateway

    started, release = threading.Event(), threading.Event()
    finished: list[str] = []

    def service(*_args: Any) -> dict[str, Any]:
        started.set()
        release.wait(5)
        finished.append("closed")
        return {}

    gateway = ToolGateway.__new__(ToolGateway)
    gateway._run_service = service  # type: ignore[method-assign]

    async def cancel_mid_call() -> None:
        task = asyncio.create_task(
            gateway._execute(None, None, None, None, lambda *_a: {})  # type: ignore[arg-type]
        )
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()  # still waiting for the thread's transaction
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["closed"]

    asyncio.run(cancel_mid_call())
