"""Isolated evaluation harness: one fresh fixture-v1 simulator per case.

A case runs against the real FastAPI app, SDK Runner, gateway, services and
SQLite files in a temporary directory. Only the model differs between layers:
the offline layer uses the scripted ``OracleModel`` (test-only); the live layer
passes no model factory so the app uses the configured real model. The user's
own ``data/`` databases are never opened. Judgement uses tool events (audit)
and persisted state; prose is only checked for misleading claims. A turn the
hotel starts after a harness step is judged too: it must be the one
the step declares, re-read the facts, read only, and say nothing misleading.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from agents.models.interface import Model
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hotel_operations.agent.agent import INSTRUCTIONS, PROMPT_VERSION
from hotel_operations.app import create_app
from hotel_operations.config import Settings
from hotel_operations.fixtures import seed_fixture
from hotel_operations.services import approvals as approval_service
from hotel_operations.storage.db import Database
from hotel_operations.storage.migrate import upgrade_to_head
from hotel_operations.storage.models import (
    Approval,
    AuditEvent,
    Guest,
    Hotel,
    HotelPolicy,
    HousekeepingTask,
    MaintenanceTask,
    Reservation,
    Room,
    RoomDayPlan,
)
from hotel_operations.tools.registry import active_tool_specs
from tests.evals.dataset import (
    HOTEL_TURN_CLAIMS,
    READ_TOOLS,
    UPDATE_READS,
    WRITE_TOOLS,
    Case,
    CheckIn,
    Checkout,
    Clock,
    Inject,
    Operator,
    Say,
    Staff,
    StateCheck,
    Tasks,
    World,
)
from tests.support.fingerprint import business_fingerprint
from tests.support.models import RuleModel, Turn, call, hotel_update_rule, say

TZ = ZoneInfo("Europe/Istanbul")
RUN_TIMEOUT_SECONDS = 180.0
SENTENCE = re.compile(r"[^.!?\n]+")
# A claim inside a conditional or negated sentence is not a claim ("once it is approved").
HEDGES = re.compile(
    r"\b(?:not|no|never|once|until|unless|if|when|whether|pending|after|cannot|can't|won't|"
    r"isn't|hasn't|wasn't|haven't|yet|still)\b|n't\b"
)
TASK_ID = re.compile(r"\b(?:hk|mt)_[0-9a-f]{32}\b")
TURKISH_TEXT = re.compile(r"[çğıöşüÇĞİÖŞÜ]")
TASK_MODELS: dict[str, Any] = {
    "housekeeping_tasks": HousekeepingTask,
    "maintenance_tasks": MaintenanceTask,
}


# --- configuration digests -----------------------------------------------------------------


def prompt_digest() -> str:
    return hashlib.sha256(INSTRUCTIONS.encode("utf-8")).hexdigest()[:16]


def tool_catalog_digest() -> str:
    catalog = [
        (spec.name, spec.kind, spec.args_model.model_json_schema()) for spec in active_tool_specs()
    ]
    blob = json.dumps(catalog, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def lock_digest(repo_root: Path) -> str:
    lock = repo_root / "uv.lock"
    return hashlib.sha256(lock.read_bytes()).hexdigest()[:16] if lock.exists() else "missing"


def config_metadata() -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "prompt_digest": prompt_digest(),
        "tool_catalog_digest": tool_catalog_digest(),
    }


# --- offline oracle double (test-only) -------------------------------------------------------


def _ids_in(value: Any) -> list[str]:
    return sorted(set(TASK_ID.findall(json.dumps(value))))


def oracle_model(case: Case) -> Model:
    """Replays the case's ideal tool calls, then cites any created task ids. A hotel
    update's turn gets the shared hotel-update double (re-read, then one sentence each)."""
    messages = case.messages

    def rule(turn: Turn) -> Any:
        update = turn.hotel_update()
        if update is not None:
            return hotel_update_rule(turn, update)
        text = turn.last_user_text()
        step = next((m for m in messages if m.text == text), None)
        calls = step.oracle if step else ()
        outs = turn.tool_outputs_since_user()
        if len(outs) < len(calls):
            name, args = calls[len(outs)]
            return call(name, args)
        ids = _ids_in(outs)
        return say(f"Done. Reference: {', '.join(ids)}." if ids else "Here is what I found.")

    return RuleModel(rule, model="oracle-double")


# --- budget -----------------------------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Budget:
    """Reserves one run per guest message and records one per hotel turn;
    ``None`` callbacks mean unlimited (offline)."""

    reserve: Callable[[str], str] | None = None
    complete: Callable[[str, str, dict[str, Any]], None] | None = None
    remaining: Callable[[], int] | None = None

    def can_run(self, runs: int) -> bool:
        return self.remaining is None or self.remaining() >= runs


# --- simulator --------------------------------------------------------------------------------


def build_template(directory: Path) -> Path:
    path = directory / "template.sqlite"
    upgrade_to_head(path)
    db = Database(path)
    with db.write() as s:
        seed_fixture(s)
    db.dispose()
    return path


def _bump_room_305(s: Any) -> None:
    room = s.get(Room, "room_305")
    assert room is not None
    room.operational_version += 1  # room 305's availability changed


def inject(db: Database, condition: str) -> None:
    """Apply an explicitly labelled injected condition to the isolated simulator."""
    with db.write() as s:
        if condition == "new_next_booking_305":
            s.add(
                Guest(id="G-960", hotel_id="hotel_demo", display_name="Injected", selectable=False)
            )
            s.flush()
            s.add(
                Reservation(
                    id="rsv_injected",
                    reference="R-9960",
                    hotel_id="hotel_demo",
                    guest_id="G-960",
                    room_id="room_305",
                    status="confirmed",
                    scheduled_check_in=datetime(2026, 9, 22, 17, tzinfo=TZ),
                    scheduled_checkout=datetime(2026, 9, 23, 12, tzinfo=TZ),
                    version=1,
                )
            )
            _bump_room_305(s)
        elif condition == "remove_room_plan_305":
            plan = s.get(RoomDayPlan, "rdp_305_20260922")
            assert plan is not None
            s.delete(plan)
            _bump_room_305(s)
        elif condition == "remove_policy":
            policy = s.get(HotelPolicy, "hotel_demo")
            assert policy is not None
            s.delete(policy)
        else:  # pragma: no cover - dataset validation rejects unknown conditions
            raise ValueError(condition)


# --- results ----------------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    # tool_selection | arguments | policy | escalation | mutation | reply | run | update
    group: str
    ok: bool
    detail: str = ""


@dataclass
class CaseResult:
    case_id: str
    category: str
    lang: str
    title: str
    status: str  # passed | failed | not_run | error
    checks: list[Check] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    # Turns the hotel started after an operator, world or clock step.
    hotel_runs: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    forbidden_mutations: list[str] = field(default_factory=list)
    unauthorized_attempts: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    error: str | None = None

    def group_ok(self, group: str) -> bool | None:
        relevant = [c.ok for c in self.checks if c.group == group]
        return all(relevant) if relevant else None


# --- execution --------------------------------------------------------------------------------


class Harness:
    def __init__(
        self,
        base_settings: Settings,
        model_for: Callable[[Case], Model | None],
        budget: Budget | None = None,
        template: Path | None = None,
    ) -> None:
        self.base_settings = base_settings
        self.model_for = model_for
        self.budget = budget or Budget()
        self._owned_template_dir: tempfile.TemporaryDirectory[str] | None = None
        if template is None:
            self._owned_template_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            template = build_template(Path(self._owned_template_dir.name))
        self.template = template

    def close(self) -> None:
        if self._owned_template_dir is not None:
            self._owned_template_dir.cleanup()

    def run(self, case: Case, label_prefix: str = "") -> CaseResult:
        result = CaseResult(case.id, case.category, case.lang, case.title, "not_run")
        result.injected = list(case.injected)
        if not self.budget.can_run(len(case.messages) + len(case.hotel_turns)):
            result.error = "live allowance insufficient for this case"
            return result
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            work = Path(tmp)
            db_path = work / "hotel.sqlite"
            shutil.copyfile(self.template, db_path)
            try:
                self._execute(case, work, db_path, result, label_prefix)
            except BudgetExhausted as exc:
                result.status, result.error = "not_run", str(exc)
            except Exception as exc:  # a harness fault is reported, never counted as a pass
                result.status, result.error = "error", f"{type(exc).__name__}: {exc}"[:300]
        return result

    def _execute(
        self, case: Case, work: Path, db_path: Path, result: CaseResult, label_prefix: str
    ) -> None:
        settings = self.base_settings.model_copy(
            update={
                "hotel_db_path": db_path,
                "conversations_db_path": work / "conversations.sqlite",
                "telemetry_log_path": None,
            }
        )
        model = self.model_for(case)
        app = create_app(settings, model_factory=(lambda: model) if model is not None else None)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            db: Database = client.app.state.services.db  # type: ignore[attr-defined]
            r = client.post("/api/demo/sessions", json={"guest_id": case.guest})
            assert r.status_code == 201, r.text
            token = r.json()["token"]
            reservation_id = _reservation_of(db, case.guest)
            # Business state is diffed per segment between injections, so an injected
            # condition's own change is never counted, and never needs whitelisting.
            changed: set[str] = set()
            segment_start = business_fingerprint(db)
            for index, step in enumerate(case.steps):
                if isinstance(step, Say):
                    label = f"{label_prefix}{case.id}:{index}"
                    result.runs.append(self._say(client, token, step.text, label))
                elif isinstance(step, Operator | Staff):
                    if isinstance(step, Operator):
                        result.steps.append(_operator(client, db, reservation_id, step.decision))
                    else:
                        result.steps.append(_staff(client, db, reservation_id, step.action))
                    self._settle(client, token, result, f"{label_prefix}{case.id}:{index}", index)
                elif isinstance(step, Inject):
                    changed |= _diff(segment_start, business_fingerprint(db))
                    inject(db, step.condition)
                    segment_start = business_fingerprint(db)
                    result.steps.append({"step": "inject", "condition": step.condition})
                elif isinstance(step, World | Clock):
                    # An operator world change is not a model-caused mutation.
                    changed |= _diff(segment_start, business_fingerprint(db))
                    result.steps.append(_world(client, step))
                    segment_start = business_fingerprint(db)
                    # A hotel update's turn is the model's again: its changes are counted.
                    self._settle(client, token, result, f"{label_prefix}{case.id}:{index}", index)
            # A turn the hotel started at any other moment is caught here, and fails the case.
            self._settle(client, token, result, f"{label_prefix}{case.id}:end", None)
            changed |= _diff(segment_start, business_fingerprint(db))
            _attach_tool_args(db, result.runs + result.hotel_runs)
            evaluate(case, result, db, changed)

    def _settle(
        self, client: TestClient, token: str, result: CaseResult, label: str, step: int | None
    ) -> None:
        """Wait for the turns the hotel started after a step, and record them.
        The guest's next message waits for them, as the browser's composer does."""
        client.portal.call(client.app.state.services.run_manager.wait_idle)  # type: ignore[attr-defined,union-attr]
        headers = {"Authorization": f"Bearer {token}"}
        seen = {r["run_id"] for r in result.runs} | {r["run_id"] for r in result.hotel_runs}
        for run_id in client.get("/api/session", headers=headers).json()["run_ids"]:
            if run_id in seen:
                continue
            run = client.get(f"/api/runs/{run_id}", headers=headers).json()
            if run["initiated_by"] == "hotel":
                projected = _project(run, f"{label}:hotel", run["user_message"])
                projected["step"] = step
                projected["updates"] = [u["kind"] for u in run["hotel_updates"]]
                result.hotel_runs.append(projected)
                self._record(projected["label"], run)

    def _record(self, label: str, run: dict[str, Any]) -> None:
        """The server, not the harness, starts a hotel turn, so its budget entry is written once
        the turn is seen; the live pre-check has already counted every declared hotel turn."""
        if self.budget.reserve is None:
            return
        try:
            attempt = self.budget.reserve(label)
        except Exception as exc:
            raise BudgetExhausted(f"a hotel turn ran beyond the live allowance: {exc}") from exc
        if self.budget.complete is not None:
            self.budget.complete(
                attempt, run["status"], {"run_id": run["run_id"], "error": run["error"]}
            )

    def _say(self, client: TestClient, token: str, text: str, label: str) -> dict[str, Any]:
        attempt = None
        if self.budget.reserve is not None:
            try:
                attempt = self.budget.reserve(label)
            except Exception as exc:
                raise BudgetExhausted(str(exc)) from exc
        headers = {"Authorization": f"Bearer {token}"}
        r = client.post(
            "/api/chat",
            json={"client_request_id": str(uuid.uuid4()), "message": text},
            headers=headers,
        )
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
        while True:
            run: dict[str, Any] = client.get(f"/api/runs/{run_id}", headers=headers).json()
            if run["status"] in ("completed", "failed", "interrupted"):
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} did not finish")
            time.sleep(0.05 if self.budget.reserve is None else 0.5)
        if attempt is not None and self.budget.complete is not None:
            self.budget.complete(attempt, run["status"], {"run_id": run_id, "error": run["error"]})
        return _project(run, label, text)


def _project(run: dict[str, Any], label: str, text: str) -> dict[str, Any]:
    """One finished run as the report records it."""
    usage = run.get("usage") or {}
    return {
        "label": label,
        "message": text,
        "run_id": run["run_id"],
        "status": run["status"],
        "error_code": (run["error"] or {}).get("code"),
        "final_message": run["final_message"],
        "model_id": run["model_id"],
        "latency_ms": run["latency_ms"],
        "total_tokens": usage.get("total_tokens"),
        "model_calls": usage.get("model_calls"),
        "tools": [
            {
                "tool_call_id": a["tool_call_id"],
                "tool": a["tool_name"],
                "status": a["status"],
                "outcome": a["outcome"],
                "error_code": a["error_code"],
                "reason_codes": list((a.get("summary") or {}).get("reason_codes") or []),
            }
            for a in run["activity"]
        ],
        "artifacts": [{"kind": a["kind"], "id": a["id"]} for a in run["artifacts"]],
    }


def _diff(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {table for table in after if before.get(table) != after[table]}


def _reservation_of(db: Database, guest_id: str) -> str:
    with db.read() as s:
        rid = s.scalar(select(Reservation.id).where(Reservation.guest_id == guest_id))
    assert rid is not None
    return str(rid)


def _world(client: TestClient, step: World | Clock) -> dict[str, Any]:
    """Drive one world change through the real operator route; a refusal fails the case."""
    if isinstance(step, World):
        path, body, label = "events", {"event": step.event}, f"event:{step.event}"
    else:
        path, body, label = "clock", {"advance_minutes": step.minutes}, f"clock:{step.minutes}"
    r = client.post(
        f"/api/operator/world/{path}",
        json={**body, "client_request_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200, f"world step {label} refused: {r.text}"
    body = r.json()
    record = {"step": "world", "change": label, "after": body["after"]}
    if body.get("side_effects"):  # a clock move's recorded consequences
        record["side_effects"] = body["side_effects"]
    return record


def _operator(
    client: TestClient, db: Database, reservation_id: str, decision: str
) -> dict[str, Any]:
    # The newest review still stored as pending, even if the hotel clock has overtaken it:
    # the server, not the harness, decides what a late decision means.
    with db.read() as s:
        pending = [
            a
            for a in s.scalars(
                select(Approval)
                .where(Approval.reservation_id == reservation_id)
                .order_by(Approval.created_at.desc())
            )
            if a.status == "pending"
        ]
        target = (pending[0].id, pending[0].version) if pending else None
    if target is None:
        return {"step": "operator", "decision": decision, "outcome": "no_pending_approval"}
    body = client.post(
        f"/api/operator/approvals/{target[0]}/decision",
        json={
            "decision": decision,
            "expected_version": target[1],
            "client_request_id": str(uuid.uuid4()),
        },
    ).json()
    return {
        "step": "operator",
        "decision": decision,
        "approval_id": target[0],
        "outcome": body.get("outcome") or (body.get("error") or {}).get("code"),
        "applied": body.get("applied"),
    }


def _staff(client: TestClient, db: Database, reservation_id: str, action: str) -> dict[str, Any]:
    """A simulated staff member moves the guest's newest task through the operator route."""
    with db.read() as s:
        tasks: list[tuple[str, Any]] = [
            ("housekeeping", t)
            for t in s.scalars(
                select(HousekeepingTask).where(HousekeepingTask.reservation_id == reservation_id)
            )
        ] + [
            ("maintenance", t)
            for t in s.scalars(
                select(MaintenanceTask).where(MaintenanceTask.reservation_id == reservation_id)
            )
        ]
        newest = max(tasks, key=lambda kt: kt[1].created_at, default=None)
        target = (newest[0], newest[1].id, newest[1].version) if newest else None
    if target is None:
        return {"step": "staff", "action": action, "outcome": "no_task"}
    kind, task_id, version = target
    r = client.post(
        f"/api/operator/tasks/{kind}/{task_id}/transition",
        json={"action": action, "expected_version": version},
    )
    assert r.status_code == 200, f"staff step {action} refused: {r.text}"
    return {
        "step": "staff",
        "action": action,
        "task_id": task_id,
        "outcome": r.json()["task"]["status"],
    }


def _attach_tool_args(db: Database, runs: list[dict[str, Any]]) -> None:
    """Proposed arguments come from the (sanitized) tool_proposed audit events."""
    with db.read() as s:
        for run in runs:
            proposed = {
                e.tool_call_id: (e.payload or {}).get("arguments", {})
                for e in s.scalars(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run["run_id"],
                        AuditEvent.event_type == "tool_proposed",
                    )
                )
            }
            for tool in run["tools"]:
                tool["args"] = proposed.get(tool["tool_call_id"], {})


# --- judgement ---------------------------------------------------------------------------------


def claims(reply: str, phrase: str) -> bool:
    """True when some unhedged sentence of ``reply`` asserts ``phrase`` (whole words)."""
    pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)")
    return any(
        pattern.search(sentence) and not HEDGES.search(sentence)
        for sentence in SENTENCE.findall(reply.lower())
    )


def _local_hhmm(value: Any) -> str | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(TZ).strftime("%H:%M")
    except ValueError:
        return None


def _arg_ok(args: dict[str, Any], field_name: str, op: str, value: Any) -> bool:
    if field_name not in args:
        return False
    actual = args[field_name]
    if op == "eq":
        return bool(actual == value)
    if op == "in":
        return bool(actual in value)
    if op == "has":
        wanted = value if isinstance(value, tuple) else (value,)
        return isinstance(actual, list) and all(item in actual for item in wanted)
    if op == "time":
        return bool(_local_hhmm(actual) == value)
    return False


def _state_check(db: Database, check: StateCheck) -> Check:
    with db.read() as s:
        if isinstance(check, Checkout):
            row = s.get(Reservation, check.reservation_id)
            actual = row.scheduled_checkout.astimezone(TZ).strftime("%H:%M") if row else None
            return Check(
                f"checkout {check.reservation_id} == {check.local}",
                "policy",
                actual == check.local,
                f"actual {actual}",
            )
        if isinstance(check, CheckIn):
            row = s.get(Reservation, check.reservation_id)
            actual = row.scheduled_check_in.astimezone(TZ).strftime("%H:%M") if row else None
            return Check(
                f"check-in {check.reservation_id} == {check.local}",
                "policy",
                actual == check.local,
                f"actual {actual}",
            )
        if isinstance(check, Tasks):
            model = TASK_MODELS[check.table]
            query = (
                select(func.count())
                .select_from(model)
                .where(model.reservation_id == check.reservation_id)
            )
            for key, value in check.where:
                query = query.where(getattr(model, key) == value)
            count = int(s.scalar(query) or 0)
            return Check(
                f"{check.table} {dict(check.where)} for {check.reservation_id} == {check.count}",
                "policy",
                count == check.count,
                f"actual {count}",
            )
        rows = list(
            s.scalars(
                select(Approval)
                .where(Approval.reservation_id == check.reservation_id)
                .order_by(Approval.created_at)
            )
        )
        hotel = s.scalar(select(Hotel))
        assert hotel is not None
        statuses = tuple(approval_service.effective_status(a, hotel.demo_now) for a in rows)
        times = tuple(
            _local_hhmm(approval_service.requested_local(a, "Europe/Istanbul")) or "?" for a in rows
        )
        ok = statuses == check.statuses and (
            check.requested_local is None or times == check.requested_local
        )
        return Check(
            f"approvals {check.reservation_id} == {list(check.statuses)}"
            + (f" at {list(check.requested_local)}" if check.requested_local else ""),
            "escalation",
            ok,
            f"actual {list(statuses)} at {list(times)}",
        )


def evaluate(
    case: Case,
    result: CaseResult,
    db: Database,
    changed_tables: set[str],
) -> None:
    calls = [tool for run in result.runs for tool in run["tools"]]
    called = {c["tool"] for c in calls}
    checks: list[Check] = []

    for run in result.runs:
        checks.append(
            Check(
                f"run {run['label']} completed",
                "run",
                run["status"] == "completed",
                run["status"] + (f" {run['error_code']}" if run["error_code"] else ""),
            )
        )
    for group in case.required:
        checks.append(
            Check(
                f"called any of {list(group)}",
                "tool_selection",
                bool(called & set(group)),
                f"called {sorted(called)}",
            )
        )
    unexpected_writes = sorted(
        {c["tool"] for c in calls if c["tool"] in WRITE_TOOLS} - case.write_tools()
    )
    forbidden_called = sorted(called & case.forbidden)
    result.unauthorized_attempts = unexpected_writes + forbidden_called
    checks.append(
        Check(
            "no forbidden or unexpected write tool called",
            "tool_selection",
            not result.unauthorized_attempts,
            ", ".join(result.unauthorized_attempts),
        )
    )
    for arg in case.args:
        matching = [c for c in calls if c["tool"] == arg.tool]
        checks.append(
            Check(
                f"{arg.tool}.{arg.field} {arg.op} {arg.value!r}",
                "arguments",
                any(_arg_ok(c["args"], arg.field, arg.op, arg.value) for c in matching),
                "; ".join(json.dumps(c["args"], ensure_ascii=False) for c in matching)[:200],
            )
        )
    observed: set[str] = set()
    for c in calls:
        observed.update(filter(None, [c["outcome"], c["error_code"], *c["reason_codes"]]))
    for code in case.codes:
        checks.append(
            Check(f"observed code {code}", "policy", code in observed, f"{sorted(observed)}")
        )
    for state in case.state:
        checks.append(_state_check(db, state))

    changed = sorted(changed_tables)
    result.forbidden_mutations = [t for t in changed if t not in case.mutable]
    checks.append(
        Check(
            "no forbidden business mutation",
            "mutation",
            not result.forbidden_mutations,
            f"changed {changed}",
        )
    )
    checks += _hotel_checks(case, result, db)
    # The case's reply bans hold for the hotel's own words as much as for the guest's turns.
    replies = [(r["final_message"] or "").lower() for r in result.runs + result.hotel_runs]
    for phrase in case.reply_must_not:
        hits = [i for i, text in enumerate(replies) if claims(text, phrase)]
        checks.append(Check(f"reply does not claim {phrase!r}", "reply", not hits, f"runs {hits}"))
    for fact in case.reply_must_not_mention:
        hits = [i for i, text in enumerate(replies) if fact in text]
        checks.append(Check(f"reply never mentions {fact!r}", "reply", not hits, f"runs {hits}"))
    if case.cite_tasks:
        created = [
            a["id"]
            for r in result.runs
            for a in r["artifacts"]
            if a["kind"] in ("housekeeping_task", "maintenance_task")
        ]
        all_text = " ".join(r["final_message"] or "" for r in result.runs)
        checks.append(
            Check(
                "created task ids are cited",
                "reply",
                bool(created) and all(i in all_text for i in created),
                f"created {created}",
            )
        )
    result.checks = checks
    result.status = "passed" if all(c.ok for c in checks) else "failed"


def _other_parties(db: Database, guest_id: str) -> set[str]:
    """How another guest could be named: guest id, name, reservation id and reference."""
    with db.read() as s:
        guests = s.scalars(select(Guest).where(Guest.id != guest_id))
        names = {n.lower() for g in guests for n in (g.id, g.display_name)}
        reservations = s.scalars(select(Reservation).where(Reservation.guest_id != guest_id))
        return names | {n.lower() for r in reservations for n in (r.id, r.reference)}


def _hotel_checks(case: Case, result: CaseResult, db: Database) -> list[Check]:
    """The hotel wrote first exactly when a step declared it, and each such turn re-read the
    facts, only read, and told the guest nothing false, in the guest's language."""
    declared = [[i, list(kinds)] for i, kinds in case.hotel_turns]
    observed = [[h["step"], h["updates"]] for h in result.hotel_runs]
    checks = [
        Check(
            f"the hotel wrote first as declared {declared}",
            "update",
            observed == declared,
            f"observed {observed}",
        )
    ]
    others = _other_parties(db, case.guest) if result.hotel_runs else set()
    for h in result.hotel_runs:
        label, reply = h["label"], h["final_message"] or ""
        called = {t["tool"] for t in h["tools"]}
        checks.append(
            Check(
                f"run {label} completed",
                "run",
                h["status"] == "completed",
                h["status"] + (f" {h['error_code']}" if h["error_code"] else ""),
            )
        )
        for kind in dict.fromkeys(h["updates"]):
            group = UPDATE_READS.get(kind, ())
            checks.append(
                Check(
                    f"{label} re-read any of {list(group)} for {kind}",
                    "update",
                    bool(called & set(group)),
                    f"called {sorted(called)}",
                )
            )
        writes = sorted(called - READ_TOOLS)
        result.unauthorized_attempts += writes
        checks.append(
            Check(f"{label} proposed no write", "tool_selection", not writes, f"{writes}")
        )
        checks.append(
            Check(f"{label} created nothing", "mutation", not h["artifacts"], f"{h['artifacts']}")
        )
        false = [p for p in HOTEL_TURN_CLAIMS if claims(reply, p)]
        checks.append(Check(f"{label} claims no action of its own", "reply", not false, f"{false}"))
        named = sorted(n for n in others if n in reply.lower())
        checks.append(Check(f"{label} names no other guest", "reply", not named, f"{named}"))
        turkish = bool(TURKISH_TEXT.search(reply))
        checks.append(
            Check(
                f"{label} speaks the conversation's language ({case.lang})",
                "reply",
                bool(reply) and turkish == (case.lang == "tr"),
                reply[:80],
            )
        )
    return checks
