"""Durable run lifecycle: admission, bounded in-process execution and recovery.

``queued -> running -> completed | failed | interrupted``. The
accepted run is persisted before an in-process task is scheduled. This is not a
durable job queue: process death interrupts a run, and startup recovery marks
it interrupted without replaying it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import openai
from agents import (
    MaxTurnsExceeded,
    ModelBehaviorError,
    RunHooks,
    Runner,
    RunResultStreaming,
    SQLiteSession,
)
from agents.exceptions import ModelTimeoutError
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.run_context import RunContextWrapper
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from hotel_operations import errors, telemetry
from hotel_operations.agent import hotel_turns
from hotel_operations.agent.admission import ACTIVE_STATUSES, admission_blocker, input_hash
from hotel_operations.agent.agent import INSTRUCTIONS, PROMPT_VERSION, build_agent
from hotel_operations.agent.run_events import RunChannel, RunEventBroker
from hotel_operations.agent.runtime import build_run_config
from hotel_operations.clock import utcnow
from hotel_operations.config import Settings
from hotel_operations.ids import new_id
from hotel_operations.services import audit
from hotel_operations.services.sessions import SessionBinding, binding_of, revalidate_binding
from hotel_operations.storage.db import Database
from hotel_operations.storage.models import DemoSession, Hotel, OperationReceipt, RunRecord
from hotel_operations.tools.gateway import (
    AgentRunContext,
    ToolGateway,
    run_to_completion,
    write_deferred_audit,
)
from hotel_operations.tools.registry import read_tool_specs

log = logging.getLogger(__name__)

__all__ = ["ACTIVE_STATUSES", "RunManager", "admit_run", "input_hash"]

FINISH_WRITE_ATTEMPTS = 8
ModelFactory = Callable[[], Model]


def _artifact_refs(session: Any, run_id: str) -> list[dict[str, Any]]:
    return [
        {"kind": r.artifact_type, "id": r.artifact_id, "receipt_id": r.id}
        for r in session.scalars(select(OperationReceipt).where(OperationReceipt.run_id == run_id))
    ]


# --- admission -----------------------------------------------------------------


def admit_run(
    db: Database,
    settings: Settings,
    binding: SessionBinding,
    client_request_id: str,
    message: str,
) -> tuple[RunRecord, bool]:
    """Persist and admit a run inside one ``BEGIN IMMEDIATE`` transaction.

    Returns ``(run, created)``. The same key and body returns the existing run
    (including a terminal one) without a new model execution.
    """
    digest = input_hash(message)
    with db.write() as s:
        existing = s.scalar(
            select(RunRecord).where(
                RunRecord.session_id == binding.session_id,
                RunRecord.client_request_id == client_request_id,
            )
        )
        if existing is not None:
            if existing.input_hash != digest:
                raise errors.DomainError(
                    "IDEMPOTENCY_CONFLICT",
                    "This request ID was already used with a different message.",
                    http_status=409,
                )
            return existing, False

        demo_session = s.get(DemoSession, binding.session_id)
        if demo_session is None:
            raise errors.invalid_session()
        revalidate_binding(s, binding)
        blocker = admission_blocker(s, settings, demo_session)
        if blocker is not None:
            raise blocker

        run = RunRecord(
            id=new_id("run"),
            session_id=binding.session_id,
            client_request_id=client_request_id,
            input_hash=digest,
            user_message=message,
            status="queued",
            conversation_key=demo_session.conversation_key,
            created_at=utcnow(),
            artifact_refs=[],
        )
        s.add(run)
        demo_session.accepted_turn_count += 1
        s.flush()
        audit.append(
            s,
            hotel_id=binding.hotel_id,
            event_type="request_accepted",
            actor_type="guest",
            actor_id=binding.guest_id,
            session_id=binding.session_id,
            run_id=run.id,
            outcome="queued",
        )
        return run, True


# --- usage accounting --------------------------------------------------------------


class UsageHooks(RunHooks[AgentRunContext]):
    """Records every model call's usage and latency so run totals are true sums."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._started: float | None = None

    async def on_llm_start(
        self, context: Any, agent: Any, system_prompt: Any, input_items: Any
    ) -> None:
        self._started = time.perf_counter()

    async def on_llm_end(
        self, context: RunContextWrapper[AgentRunContext], agent: Any, response: ModelResponse
    ) -> None:
        latency = (
            None if self._started is None else int((time.perf_counter() - self._started) * 1000)
        )
        usage = response.usage
        self.calls.append(
            {
                "request_id": response.request_id,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "reasoning_tokens": usage.output_tokens_details.reasoning_tokens,
                "total_tokens": usage.total_tokens,
                "latency_ms": latency,
            }
        )
        self._started = None

    def summary(self, sdk_reported_requests: int | None) -> dict[str, Any]:
        def total(key: str) -> int | None:
            # A sum over the calls that reported the value; with none, it is unknown (null),
            # never zero. A partial sum after a mid-flight failure is flagged incomplete below.
            known = [int(c[key]) for c in self.calls if c[key] is not None]
            return sum(known) if known else None

        return {
            "model_calls": self.calls,
            "recorded_calls": len(self.calls),
            "sdk_reported_requests": sdk_reported_requests,
            "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"),
            "reasoning_tokens": total("reasoning_tokens"),
            "total_tokens": total("total_tokens"),
            "model_latency_ms": total("latency_ms"),
            # Usage for a call that failed mid-flight is unknown, never recorded as zero.
            "complete": sdk_reported_requests is not None
            and sdk_reported_requests == len(self.calls),
        }


def _emit_run(run: RunRecord, usage: dict[str, Any] | None) -> None:
    """Telemetry for a finished run: sums over every model call, unknowns left null."""
    usage = usage or {}
    telemetry.emit(
        "run_finished",
        run_id=run.id,
        status=run.status,
        error_code=run.error_code,
        model_id=run.model_id,
        prompt_version=PROMPT_VERSION,
        latency_ms=run.latency_ms,
        model_calls=usage.get("recorded_calls"),
        sdk_reported_requests=usage.get("sdk_reported_requests"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        usage_complete=usage.get("complete"),
        artifacts=len(run.artifact_refs or []),
    )


# --- error mapping -------------------------------------------------------------------


@dataclass(frozen=True)
class RunFailure:
    code: str
    message: str
    retryable: bool


class ContextLimitExceeded(Exception):
    pass


def classify_failure(exc: BaseException) -> RunFailure:
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return RunFailure(
            "RUN_DEADLINE_EXCEEDED", "The request took too long and was stopped.", True
        )
    if isinstance(exc, ContextLimitExceeded):
        return RunFailure(
            "CONTEXT_LIMIT",
            "This message does not fit in the model's context. Start a new conversation; "
            "hotel records are kept.",
            False,
        )
    if isinstance(exc, MaxTurnsExceeded):
        return RunFailure(
            "RUN_MAX_TURNS", "The agent needed too many steps and was stopped.", False
        )
    if isinstance(exc, OperationalError):
        return RunFailure(
            "STORAGE_UNAVAILABLE",
            "Hotel storage was temporarily unavailable, so the request stopped. "
            "Check your requests before trying again.",
            True,
        )
    if isinstance(exc, ModelTimeoutError | openai.APITimeoutError):
        return RunFailure("PROVIDER_TIMEOUT", "The language model did not respond in time.", True)
    if isinstance(exc, openai.RateLimitError):
        return RunFailure(
            "PROVIDER_RATE_LIMITED", "The language model is rate limited right now.", True
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return RunFailure(
            "PROVIDER_AUTH_FAILED", "The language model rejected the configured credentials.", False
        )
    if isinstance(exc, openai.APIConnectionError):
        return RunFailure("PROVIDER_UNAVAILABLE", "The language model could not be reached.", True)
    if isinstance(exc, openai.APIStatusError):
        return RunFailure("PROVIDER_ERROR", "The language model returned an error.", True)
    if isinstance(exc, ModelBehaviorError):
        text = str(exc)
        if "response.incomplete" in text:
            if "max_output_tokens" in text:
                return RunFailure(
                    "MODEL_OUTPUT_LIMIT", "The model hit its output limit before finishing.", False
                )
            return RunFailure("MODEL_INCOMPLETE", "The model response was incomplete.", False)
        if "not found in agent" in text:
            return RunFailure(
                "UNKNOWN_TOOL", "The model proposed a tool that does not exist.", False
            )
        return RunFailure("MODEL_BEHAVIOR_ERROR", "The model produced an invalid response.", False)
    return RunFailure("RUN_FAILED", "The request failed unexpectedly.", False)


# --- conversation history ----------------------------------------------------------------


def _item_chars(item: Any) -> int:
    return len(json.dumps(item, ensure_ascii=False, default=str))


def _starts_turn(item: Any) -> bool:
    """A guest's message, or a hotel update's developer message."""
    return (
        isinstance(item, dict)
        and item.get("role") in ("user", "developer")
        and item.get("type", "message") == "message"
    )


def trim_history(
    history: list[Any], new_items: list[Any], budget_chars: int, fixed_chars: int
) -> tuple[list[Any], int]:
    """Keep the newest history that fits the budget; return ``(kept, dropped_count)``.

    History is cut only where a turn starts (a guest's message or a hotel update), so a
    tool call is never separated from its output and reasoning items stay with the turn
    that produced them. The stored SQLiteSession and the on-screen transcript are
    untouched; only the model input shrinks.
    """
    sizes = [_item_chars(i) for i in history]
    fixed = fixed_chars + sum(_item_chars(i) for i in new_items)
    starts = [0, *(i for i, item in enumerate(history) if i > 0 and _starts_turn(item))]
    starts.append(len(history))
    for start in starts:
        if fixed + sum(sizes[start:]) <= budget_chars:
            return history[start:], start
    return [], len(history)


@dataclass(frozen=True)
class PreparedRun:
    """What ``_mark_running`` hands the execution: the trusted scope and the model input."""

    binding: SessionBinding
    input: str | list[Any]
    conversation_key: str
    client_request_id: str
    initiated_by: str


@dataclass
class HistoryWindow:
    """Per-run ``session_input_callback``: trims model input, records what it dropped."""

    budget_chars: int
    fixed_chars: int
    dropped: int = 0

    def __call__(self, history: list[Any], new_items: list[Any]) -> list[Any]:
        kept, self.dropped = trim_history(history, new_items, self.budget_chars, self.fixed_chars)
        return kept + new_items


# --- execution -------------------------------------------------------------------------


async def _stop_stream(result: RunResultStreaming) -> None:
    """Make sure the SDK's background run loop has fully stopped.

    On a timeout, a cancellation or an error the loop may still be running (or writing its
    session items); a conversation rollback must only start once it has stopped.
    """
    task = result.run_loop_task
    if task is None or task.done():
        return
    result.cancel()
    with contextlib.suppress(BaseException):
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))


class RunManager:
    """Schedules accepted runs as lifespan-managed in-process tasks."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        model_factory: ModelFactory,
        gateway: ToolGateway | None = None,
        *,
        provider_available: bool = True,
    ) -> None:
        self.db = db
        self.settings = settings
        self.model_factory = model_factory
        self.gateway = gateway or ToolGateway(db, settings)
        # Without a model no hotel update can be told; the gate's rows are skipped instead.
        self.provider_available = provider_available
        self._tasks: set[asyncio.Task[None]] = set()
        # Set by shutdown: nothing new starts, so a reset or stop leaves no orphan task.
        self._closing = False
        # Live progress hints for runs executing here; never authoritative.
        self.events = RunEventBroker()

    # lifecycle ---------------------------------------------------------------------

    def schedule(self, run_id: str) -> None:
        if self._closing:
            return  # the row stays queued; recovery marks it interrupted, never replayed
        task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        self._closing = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self.gateway.idle()

    def recover_interrupted(self) -> list[str]:
        """Startup recovery: queued/running rows become interrupted; never replayed."""
        with self.db.write() as s:
            rows = list(s.scalars(select(RunRecord).where(RunRecord.status.in_(ACTIVE_STATUSES))))
            for run in rows:
                self._finish_failed(
                    s,
                    run,
                    RunFailure(
                        "RUN_INTERRUPTED",
                        "The server restarted while this "
                        "request was in progress. It was not retried.",
                        False,
                    ),
                    status="interrupted",
                )
            return [r.id for r in rows]

    def skip_undelivered_updates(self) -> int:
        """Startup: hotel updates the previous process never delivered are not replayed."""
        with self.db.write() as s:
            return hotel_turns.skip_undelivered(s)

    # hotel updates ------------------------------------------------------------------------

    def _deliver(self, s: Any) -> list[str]:
        """Admit hotel runs inside the caller's write. A savepoint keeps a delivery failure
        from undoing the write it rides on; the updates then wait for the next chance."""
        if self._closing:
            return []
        try:
            with s.begin_nested():
                return hotel_turns.deliver_pending(
                    s, self.settings, provider_available=self.provider_available
                )
        except Exception:
            log.exception("hotel update delivery failed; the updates stay pending")
            return []

    async def deliver_hotel_updates(self) -> list[str]:
        """After an operator command: start the hotel turns its gate recorded, if possible."""

        def admit() -> list[str]:
            with self.db.write() as s:
                return self._deliver(s)

        try:
            admitted = await asyncio.to_thread(admit)
        except OperationalError:
            log.warning("hotel update delivery postponed: storage busy")
            return []
        self._start_hotel_turns(admitted)
        return admitted

    def _start_hotel_turns(self, admitted: list[str] | None) -> None:
        """Schedule hotel turns admitted by a write that has now committed."""
        for run_id in admitted or ():
            telemetry.emit("run_admitted", run_id=run_id, replayed=False, initiated_by="hotel")
            self.schedule(run_id)

    # execution -------------------------------------------------------------------------

    async def _execute(self, run_id: str) -> None:
        channel = self.events.open(run_id)
        try:
            await self._run(run_id, channel)
        finally:
            # Only after the terminal state is persisted: a subscriber told "done" then
            # reads the final run from storage.
            self.events.close(run_id)

    async def _run(self, run_id: str, channel: RunChannel) -> None:
        started = time.perf_counter()
        deadline = started + self.settings.run_deadline_seconds
        hooks = UsageHooks()
        deferred: list[dict[str, Any]] = []
        # The conversation segment this run writes to, and how many items it held before the
        # run: on failure exactly the items after `baseline` are rolled back.
        conversation_key: str | None = None
        baseline: int | None = None
        try:
            prepared = await run_to_completion(self._mark_running, run_id)
            if prepared is None:
                return
            conversation_key = prepared.conversation_key
            hotel_turn = prepared.initiated_by == "hotel"
            ctx = AgentRunContext(
                binding=prepared.binding,
                run_id=run_id,
                client_request_id=prepared.client_request_id,
                deadline=deadline,
                deferred_audit=deferred,
                initiated_by=prepared.initiated_by,
            )
            sdk_session = SQLiteSession(conversation_key, self.settings.conversations_db_path)
            try:
                baseline = len(await sdk_session.get_items())
                self._check_input_budget(prepared.input)
                window = HistoryWindow(self.settings.max_estimated_input_chars, len(INSTRUCTIONS))
                model = self.model_factory()
                # A hotel update may be told and re-read, never acted on.
                agent = build_agent(model, self.gateway, read_tool_specs() if hotel_turn else None)
                result = Runner.run_streamed(
                    agent,
                    prepared.input,
                    context=ctx,
                    max_turns=self.settings.max_turns,
                    hooks=hooks,
                    run_config=replace(
                        build_run_config(self.settings), session_input_callback=window
                    ),
                    session=sdk_session,
                )
                try:
                    async with asyncio.timeout(max(0.0, deadline - time.perf_counter())):
                        async for event in result.stream_events():
                            channel.observe(event)
                    if result.run_loop_exception is not None:
                        raise result.run_loop_exception
                finally:
                    await _stop_stream(result)
            finally:
                sdk_session.close()
            if window.dropped:
                telemetry.emit("history_trimmed", run_id=run_id, dropped_items=window.dropped)
            usage = hooks.summary(result.context_wrapper.usage.requests)
            admitted = await self._persist_finish(
                self._finish_completed,
                run_id,
                str(result.final_output),
                usage,
                int((time.perf_counter() - started) * 1000),
                self._model_id(model),
                deferred,
            )
            self._start_hotel_turns(admitted)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail(
                    run_id,
                    RunFailure(
                        "RUN_INTERRUPTED",
                        "The server stopped while this request was in "
                        "progress. It was not retried.",
                        False,
                    ),
                    "interrupted",
                    hooks.summary(None),
                    int((time.perf_counter() - started) * 1000),
                    deferred,
                    conversation_key,
                    baseline,
                )
            )
            raise
        except Exception as exc:  # classified; never converted into a success
            failure = classify_failure(exc)
            log.warning("run %s failed: %s (%s)", run_id, failure.code, type(exc).__name__)
            await self._fail(
                run_id,
                failure,
                "failed",
                hooks.summary(None),
                int((time.perf_counter() - started) * 1000),
                deferred,
                conversation_key,
                baseline,
            )

    async def _fail(
        self,
        run_id: str,
        failure: RunFailure,
        status: str,
        usage: dict[str, Any],
        latency_ms: int,
        deferred: list[dict[str, Any]],
        conversation_key: str | None,
        baseline: int | None,
    ) -> None:
        """Forget only this run's conversation items, then persist the terminal state.

        When the rollback cannot be confirmed the whole segment is abandoned instead (the
        conversation key rotates), so a partial SDK segment never reaches the next run.
        """
        rolled_back = await self._rollback_conversation(conversation_key, baseline)
        admitted = await self._persist_finish(
            self._finish_failed_by_id,
            run_id,
            failure,
            status,
            usage,
            latency_ms,
            deferred,
            not rolled_back,
        )
        if status != "interrupted":  # a stopping server starts nothing new
            self._start_hotel_turns(admitted)

    async def _rollback_conversation(self, key: str | None, baseline: int | None) -> bool:
        if key is None:
            return True  # the run never reached its conversation: nothing to forget
        if baseline is None:
            return False  # the segment could not even be read: abandon it
        try:
            session = SQLiteSession(key, self.settings.conversations_db_path)
            try:
                extra = len(await session.get_items()) - baseline
                for _ in range(max(0, extra)):
                    await session.pop_item()
                return len(await session.get_items()) == baseline
            finally:
                session.close()
        except Exception:  # any doubt falls back to abandoning the whole segment
            log.warning("conversation rollback failed for %s; rotating the key", key)
            return False

    async def _persist_finish(self, finish: Callable[..., Any], *args: Any) -> Any:
        """Write a run's terminal state, retrying transient lock contention.

        A finish write that loses a lock race must not leave the run "running" (and the
        UI waiting) or turn a completed narration into a failure. If storage stays
        unavailable the row keeps its active status and startup recovery marks it
        interrupted; it is never replayed.
        """
        delay = 0.1
        for attempt in range(FINISH_WRITE_ATTEMPTS):
            try:
                return await run_to_completion(finish, *args)
            except OperationalError:
                if attempt == FINISH_WRITE_ATTEMPTS - 1:
                    log.error("run %s terminal state not persisted: storage unavailable", args[0])
                    return None
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)
        return None

    @staticmethod
    def _model_id(model: Model) -> str | None:
        name = getattr(model, "model", None)
        return str(name) if name is not None else type(model).__name__

    def _check_input_budget(self, run_input: str | list[Any]) -> None:
        """Only an input that cannot fit even with no history fails; history is trimmed."""
        items = run_input if isinstance(run_input, list) else [
            {"role": "user", "content": run_input}
        ]  # fmt: skip
        estimate = len(INSTRUCTIONS) + sum(_item_chars(i) for i in items)
        if estimate > self.settings.max_estimated_input_chars:
            raise ContextLimitExceeded(f"estimated input {estimate} chars")

    def _mark_running(self, run_id: str) -> PreparedRun | None:
        with self.db.write() as s:
            run = s.get(RunRecord, run_id)
            if run is None or run.status != "queued":
                return None
            demo_session = s.get(DemoSession, run.session_id)
            assert demo_session is not None
            run.status = "running"
            run.started_at = utcnow()
            run_input: str | list[Any] = run.user_message
            if run.initiated_by == "hotel":
                # The model reads the recorded update; the transcript shows only its title.
                hotel = s.get(Hotel, demo_session.hotel_id)
                assert hotel is not None
                text = hotel_turns.developer_text(hotel_turns.run_updates(s, run.id), hotel)
                run_input = [{"role": "developer", "content": text}]
            return PreparedRun(
                binding=binding_of(demo_session),
                input=run_input,
                conversation_key=run.conversation_key,
                client_request_id=run.client_request_id,
                initiated_by=run.initiated_by,
            )

    def _finish_completed(
        self,
        run_id: str,
        final: str,
        usage: dict[str, Any],
        latency_ms: int,
        model_id: str | None,
        deferred: list[dict[str, Any]],
    ) -> list[str]:
        with self.db.write() as s:
            run = s.get(RunRecord, run_id)
            if run is None or run.status != "running":
                return []
            write_deferred_audit(s, deferred)
            demo_session = s.get(DemoSession, run.session_id)
            assert demo_session is not None
            run.status = "completed"
            run.final_message = final
            run.finished_at = utcnow()
            run.usage = usage
            run.latency_ms = latency_ms
            run.model_id = model_id
            run.artifact_refs = _artifact_refs(s, run.id)
            _emit_run(run, usage)
            audit.append(
                s,
                hotel_id=demo_session.hotel_id,
                event_type="run_completed",
                actor_type="system",
                session_id=run.session_id,
                run_id=run.id,
                outcome="completed",
                payload={
                    "prompt_version": PROMPT_VERSION,
                    "latency_ms": latency_ms,
                    "total_tokens": usage.get("total_tokens"),
                },
            )
            s.flush()
            # The session is free again: a hotel update waiting for it starts now.
            return self._deliver(s)

    def _finish_failed_by_id(
        self,
        run_id: str,
        failure: RunFailure,
        status: str,
        usage: dict[str, Any],
        latency_ms: int,
        deferred: list[dict[str, Any]],
        rotate: bool = True,
    ) -> list[str]:
        with self.db.write() as s:
            run = s.get(RunRecord, run_id)
            if run is None or run.status not in ACTIVE_STATUSES:
                return []
            write_deferred_audit(s, deferred)
            run.usage = usage
            run.latency_ms = latency_ms
            self._finish_failed(s, run, failure, status=status, rotate=rotate)
            if status == "interrupted":
                return []
            s.flush()
            return self._deliver(s)

    def _finish_failed(
        self, s: Any, run: RunRecord, failure: RunFailure, *, status: str, rotate: bool = True
    ) -> None:
        demo_session = s.get(DemoSession, run.session_id)
        assert demo_session is not None
        run.status = status
        run.error_code = failure.code
        run.error_message = failure.message
        run.finished_at = utcnow()
        # A failed narration does not erase committed actions: keep their references.
        run.artifact_refs = _artifact_refs(s, run.id)
        # Normally the run's own items were already rolled back, so only this turn is
        # forgotten. When that could not be confirmed (or after a process death, where the
        # pre-run item count is unknown) the possibly partial SDK segment is abandoned by
        # rotating the conversation key. Business records and the transcript stay.
        if rotate and demo_session.conversation_key == run.conversation_key:
            demo_session.conversation_key = new_id("conv")
            demo_session.context_restarted_at = utcnow()
        run.conversation_abandoned = rotate
        _emit_run(run, run.usage)
        audit.append(
            s,
            hotel_id=demo_session.hotel_id,
            event_type="run_interrupted" if status == "interrupted" else "run_failed",
            actor_type="system",
            session_id=run.session_id,
            run_id=run.id,
            outcome=status,
            reason_codes=[failure.code],
        )
