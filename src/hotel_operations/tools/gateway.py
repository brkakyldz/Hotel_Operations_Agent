"""Execution gateway shared by every model-facing tool.

For every proposal: record a sanitized proposed event; validate the schema;
resolve and revalidate session scope; enforce tool availability and budget;
execute the read or controlled-write service; normalize errors; record the
result; return a bounded typed envelope to the SDK loop.

The model never supplies scope, actor, idempotency keys or versions. Those come
from the trusted ``AgentRunContext`` built by the run service.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from agents import FunctionTool
from agents.strict_schema import ensure_strict_json_schema
from agents.tool_context import ToolContext
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from hotel_operations import errors, telemetry
from hotel_operations.clock import iso, utcnow
from hotel_operations.config import Settings
from hotel_operations.services import audit
from hotel_operations.services.sessions import SessionBinding, check_session_active
from hotel_operations.services.writes import WriteScope
from hotel_operations.storage.db import Database

log = logging.getLogger(__name__)

# Execution failures (as opposed to rejections for validation, scope or policy).
EXECUTION_FAILURE_CODES = frozenset(
    {"DEADLINE_EXCEEDED", "DATA_UNAVAILABLE", "STORAGE_UNCERTAIN", "TOOL_FAILED"}
)


@dataclass
class AgentRunContext:
    """Trusted server-side context for one run. Never exposed as tool arguments."""

    binding: SessionBinding
    run_id: str
    client_request_id: str
    deadline: float  # time.perf_counter() value for the whole run
    proposals: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    # Observation audit that storage refused mid-run; written, in order, by the next audit
    # write that succeeds or with the run's terminal state (lost only if the terminal write
    # itself exhausts its retry budget; business records are unaffected).
    deferred_audit: list[dict[str, Any]] = field(default_factory=list)
    # Who started the run: a hotel update's turn may read, never write.
    initiated_by: str = "guest"


def write_deferred_audit(session: Any, entries: list[dict[str, Any]]) -> None:
    """Write deferred observation audit inside the caller's (terminal) transaction."""
    for entry in entries:
        audit.append(session, **entry)


async def run_to_completion[T](fn: Callable[..., T], /, *args: Any) -> T:
    """Run blocking storage work in a thread and await it to completion, even when cancelled.

    A thread cannot be stopped. Abandoning one mid-transaction lets a stopping run end
    while its connection is still open, and a Start over then finds the database in use.
    """
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        await asyncio.wait([work])
        if not work.cancelled():
            work.exception()  # retrieved: the caller is stopping whatever the thread did
        raise


@dataclass(frozen=True)
class ToolCall:
    """Information a service may use about the current call (never model-controlled)."""

    ctx: AgentRunContext
    tool_name: str
    tool_call_id: str
    deadline: float


@dataclass(frozen=True)
class ToolResult:
    data: dict[str, Any]
    meta: dict[str, Any]
    outcome: str = "ok"
    entity: tuple[str, str] | None = None
    audit_summary: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Session, SessionBinding, Any, ToolCall], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    kind: Literal["read", "write"]
    handler: Handler
    # Writes only: a read-only receipt lookup for the same operation key, used to
    # resolve an uncertain storage outcome before any retry (never a blind rewrite).
    receipt_lookup: ReceiptLookup | None = None

    def json_schema(self) -> dict[str, Any]:
        """Strict provider schema. Numeric/length/item-count bounds are described in text
        and enforced at runtime by the Pydantic model and the service, not the provider."""
        schema = _strip_constraints(self.args_model.model_json_schema())
        schema.setdefault("properties", {})
        schema["additionalProperties"] = False
        schema["required"] = list(schema["properties"].keys())
        return ensure_strict_json_schema(schema)


ReceiptLookup = Callable[[Session, SessionBinding, Any, ToolCall], ToolResult | None]

_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"title", "default", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
     "minLength", "maxLength", "minItems", "maxItems", "pattern", "format"}
)  # fmt: skip


def _strip_constraints(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "properties" and isinstance(v, dict):
                # Keys here are property names, never schema keywords.
                out[k] = {name: _strip_constraints(sub) for name, sub in v.items()}
            elif k not in _UNSUPPORTED_SCHEMA_KEYS:
                out[k] = _strip_constraints(v)
        return out
    if isinstance(node, list):
        return [_strip_constraints(v) for v in node]
    return node


def _envelope(
    *,
    ok: bool,
    outcome: str,
    data: dict[str, Any] | None,
    error: errors.DomainError | None,
    meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": ok,
        "outcome": outcome,
        "data": data,
        "error": None
        if error is None
        else {"code": error.code, "message": error.message, "retryable": error.retryable},
        "meta": meta,
    }


def storage_uncertain() -> errors.DomainError:
    return errors.DomainError(
        "STORAGE_UNCERTAIN",
        "Storage failed and it is not yet known whether the change was saved. "
        "Check the guest's requests before trying again.",
        http_status=503,
    )


def tool_failed() -> errors.DomainError:
    return errors.DomainError(
        "TOOL_FAILED", "The tool failed unexpectedly; no change was saved.", http_status=500
    )


def write_scope(binding: SessionBinding, call: ToolCall) -> WriteScope:
    """Trusted write scope built from the session binding and run context."""
    return WriteScope(
        hotel_id=binding.hotel_id,
        reservation_id=binding.reservation_id,
        session_id=binding.session_id,
        run_id=call.ctx.run_id,
        client_request_id=call.ctx.client_request_id,
    )


# Guest free text is untrusted and stays in its business row only (e.g. a maintenance
# description); audit keeps its length, never the words.
FREE_TEXT_ARGUMENTS = frozenset({"notes", "description", "reason"})
MAX_AUDITED_STRING = 64
# A short list of scalars (get_hotel_policy's topics) is kept item by item.
MAX_AUDITED_ITEMS = 12


def _sanitize_value(key: str, value: Any, *, nested: bool = False) -> Any:
    if isinstance(value, str):
        if key in FREE_TEXT_ARGUMENTS:
            return {"redacted_text_length": len(value)}
        if len(value) > MAX_AUDITED_STRING:
            return {"truncated_text_length": len(value)}
        return value
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, list) and not nested and len(value) <= MAX_AUDITED_ITEMS:
        return [_sanitize_value(key, item, nested=True) for item in value]
    return {"non_scalar": type(value).__name__}


def _sanitize_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {"unparseable": True, "length": len(raw or "")}
    if not isinstance(parsed, dict):
        return {"non_object": True}
    if len(parsed) > 12:
        return {"too_many_keys": len(parsed)}
    return {str(k)[:40]: _sanitize_value(str(k), v) for k, v in parsed.items()}


class ToolGateway:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        # Tool calls still running, including those of a cancelled run (see `idle`).
        self._calls: set[asyncio.Task[Any]] = set()

    async def idle(self) -> None:
        """Wait until no tool call is running.

        The SDK cancels a stopped run's tool tasks without awaiting them, so a stop that
        must release the database (Start over) waits here for their storage work to end.
        """
        while self._calls:
            await asyncio.wait(list(self._calls))

    # -- registration -----------------------------------------------------------

    def function_tools(self, specs: list[ToolSpec]) -> list[FunctionTool]:
        tools: list[FunctionTool] = []
        for spec in specs:

            async def on_invoke(ctx: ToolContext[Any], raw: str, _spec: ToolSpec = spec) -> str:
                return await self.invoke(_spec, ctx, raw)

            tools.append(
                FunctionTool(
                    name=spec.name,
                    description=spec.description,
                    params_json_schema=spec.json_schema(),
                    on_invoke_tool=on_invoke,
                    strict_json_schema=True,
                )
            )
        return tools

    # -- audit helpers ------------------------------------------------------------

    @staticmethod
    def _audit_entry(ctx: AgentRunContext, **kwargs: Any) -> dict[str, Any]:
        return {
            "hotel_id": ctx.binding.hotel_id,
            "session_id": ctx.binding.session_id,
            "run_id": ctx.run_id,
            "actor_type": "agent",
            "wall_time": utcnow(),
            **kwargs,
        }

    def _audit(self, entries: list[dict[str, Any]]) -> None:
        with self.db.write() as s:
            for entry in entries:
                audit.append(s, **entry)

    async def _audit_async(self, ctx: AgentRunContext, **kwargs: Any) -> bool:
        """Append observation evidence; False when storage refused it (deferred).

        Observation audit is not the business record (that commits atomically with the
        mutation and its receipt), so a lock here never escapes as a crashed run: the
        entry is kept on the run context, in order, and written by the next audit write
        that succeeds or with the run's terminal state.
        """
        pending = [*ctx.deferred_audit, self._audit_entry(ctx, **kwargs)]
        write = asyncio.ensure_future(asyncio.to_thread(self._audit, pending))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # The thread cannot be stopped: wait for it (bounded by the busy timeout) so
            # the context records what actually committed, never a duplicate or a loss.
            await asyncio.wait([write])
            with contextlib.suppress(Exception):
                self._settle(ctx, pending, write)
            raise
        except OperationalError:
            pass
        if not self._settle(ctx, pending, write):
            log.warning("audit %s deferred: storage unavailable", kwargs.get("event_type"))
            return False
        return True

    @staticmethod
    def _settle(ctx: AgentRunContext, pending: list[dict[str, Any]], write: Any) -> bool:
        """Clear what committed, keep in order what storage refused; re-raise the rest."""
        error = write.exception()
        if error is None:
            ctx.deferred_audit.clear()
            return True
        if isinstance(error, OperationalError):
            ctx.deferred_audit[:] = pending
            return False
        raise error

    # -- execution ----------------------------------------------------------------

    async def invoke(self, spec: ToolSpec, tool_ctx: ToolContext[Any], raw_args: str) -> str:
        ctx = tool_ctx.context
        if not isinstance(ctx, AgentRunContext):  # pragma: no cover - wiring error
            raise RuntimeError("tool invoked without trusted run context")
        call_id = tool_ctx.tool_call_id
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - the SDK runs each tool call in a task
            raise RuntimeError("tool invoked outside a task")
        self._calls.add(task)
        try:
            # Serialize gateway execution even if the model emits parallel proposals.
            async with ctx.lock:
                envelope = await self._invoke_locked(spec, ctx, call_id, raw_args)
        finally:
            self._calls.discard(task)
        text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
        if len(text) > self.settings.max_tool_projection_chars:
            err = errors.DomainError("PROJECTION_TOO_LARGE", "Tool result exceeded its bound.")
            text = json.dumps(
                _envelope(
                    ok=False, outcome="error", data=None, error=err, meta=self._meta(ctx, call_id)
                )
            )
        return text

    def _meta(self, ctx: AgentRunContext, call_id: str, **extra: Any) -> dict[str, Any]:
        meta: dict[str, Any] = {"run_id": ctx.run_id, "tool_call_id": call_id}
        meta.update(extra)
        return meta

    async def _invoke_locked(
        self, spec: ToolSpec, ctx: AgentRunContext, call_id: str, raw_args: str
    ) -> dict[str, Any]:
        ctx.proposals += 1
        started = time.perf_counter()
        event: dict[str, Any] = {
            "tool_call_id": call_id,
            "tool_name": spec.name,
            "proposed_at": iso(utcnow()),
            "status": "proposed",
        }
        ctx.tool_events.append(event)
        proposal_recorded = await self._audit_async(
            ctx,
            event_type="tool_proposed",
            tool_name=spec.name,
            tool_call_id=call_id,
            payload={"arguments": _sanitize_args(raw_args), "proposal_number": ctx.proposals},
        )

        def reject(err: errors.DomainError, event_type: str = "tool_rejected") -> dict[str, Any]:
            event.update(
                status="rejected" if event_type == "tool_rejected" else "failed",
                error_code=err.code,
            )
            return {"_reject": (err, event_type)}

        outcome: dict[str, Any]
        if not proposal_recorded:
            # Never execute a proposal whose evidence could not be written.
            outcome = reject(errors.data_unavailable(), "tool_failed")
        elif ctx.proposals > self.settings.max_tool_proposals:
            outcome = reject(
                errors.DomainError(
                    "TOOL_BUDGET_EXCEEDED", "Tool proposal budget for this request is exhausted."
                )
            )
        elif ctx.initiated_by == "hotel" and spec.kind == "write":
            # Defense in depth: such a turn is built with read tools only.
            outcome = reject(
                errors.DomainError(
                    "WRITE_NOT_ALLOWED_IN_HOTEL_UPDATE",
                    "A hotel update can be told, not acted on. Offer the guest the next step "
                    "and let them ask for it.",
                )
            )
        else:
            try:
                args = spec.args_model.model_validate_json(raw_args or "{}")
            except ValidationError:
                outcome = reject(
                    errors.DomainError(
                        "INVALID_ARGUMENT", "Tool arguments did not match the schema."
                    )
                )
            else:
                remaining = min(
                    self.settings.tool_deadline_seconds, ctx.deadline - time.perf_counter()
                )
                if remaining <= 0:
                    outcome = reject(errors.deadline_exceeded(), "tool_failed")
                else:
                    call = ToolCall(
                        ctx=ctx,
                        tool_name=spec.name,
                        tool_call_id=call_id,
                        deadline=time.perf_counter() + remaining,
                    )
                    outcome = await self._execute(spec, ctx, args, call, reject)

        duration_ms = int((time.perf_counter() - started) * 1000)
        if "_reject" in outcome:
            err, event_type = outcome["_reject"]
            await self._audit_async(
                ctx,
                event_type=event_type,
                tool_name=spec.name,
                tool_call_id=call_id,
                outcome="error",
                reason_codes=[err.code],
                payload={"duration_ms": duration_ms},
            )
            telemetry.emit(
                "tool_finished",
                run_id=ctx.run_id,
                tool=spec.name,
                kind=spec.kind,
                status=event["status"],
                error_code=err.code,
                duration_ms=duration_ms,
            )
            return _envelope(
                ok=False,
                outcome="error",
                data=None,
                error=err,
                meta=self._meta(ctx, call_id, observed_at=iso(utcnow())),
            )

        result: ToolResult = outcome["result"]
        entity_type, entity_id = result.entity or (None, None)
        event.update(
            status="completed",
            outcome=result.outcome,
            entity_id=entity_id,
            summary=result.audit_summary,
        )
        await self._audit_async(
            ctx,
            event_type="tool_completed",
            tool_name=spec.name,
            tool_call_id=call_id,
            outcome=result.outcome,
            entity_type=entity_type,
            entity_id=entity_id,
            payload={"duration_ms": duration_ms, "summary": result.audit_summary},
        )
        telemetry.emit(
            "tool_finished",
            run_id=ctx.run_id,
            tool=spec.name,
            kind=spec.kind,
            status="completed",
            outcome=result.outcome,
            replayed=bool(result.audit_summary.get("replayed", False)),
            duration_ms=duration_ms,
        )
        return _envelope(
            ok=True,
            outcome=result.outcome,
            data=result.data,
            error=None,
            meta=self._meta(ctx, call_id, **result.meta),
        )

    async def _execute(
        self,
        spec: ToolSpec,
        ctx: AgentRunContext,
        args: BaseModel,
        call: ToolCall,
        reject: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        # Reads may retry once after a transient storage failure. Writes may retry once
        # only after a receipt lookup conclusively shows the first attempt did not commit.
        for attempt in range(2):
            try:
                # Awaited to completion (never abandoned mid-transaction); the service
                # itself checks the deadline before beginning and before committing.
                result = await run_to_completion(self._run_service, spec, ctx, args, call)
                return {"result": result}
            except errors.DomainError as err:
                infrastructure = err.retryable or err.code in EXECUTION_FAILURE_CODES
                return reject(err, "tool_failed" if infrastructure else "tool_rejected")
            except OperationalError:
                log.warning("storage error in tool %s (attempt %s)", spec.name, attempt + 1)
                if attempt == 1:
                    return reject(errors.data_unavailable(), "tool_failed")
                if spec.kind == "write":
                    try:
                        committed = await run_to_completion(
                            self._lookup_receipt, spec, ctx, args, call
                        )
                    except OperationalError:
                        return reject(storage_uncertain(), "tool_failed")
                    if committed is not None:
                        return {"result": committed}
            except Exception:
                # Unexpected failure inside the service transaction: it rolled back. For a
                # write, confirm via the receipt before reporting; never retry blindly.
                log.exception("unexpected error in tool %s", spec.name)
                if spec.kind == "write":
                    try:
                        committed = await run_to_completion(
                            self._lookup_receipt, spec, ctx, args, call
                        )
                    except Exception:
                        return reject(storage_uncertain(), "tool_failed")
                    if committed is not None:
                        return {"result": committed}
                return reject(tool_failed(), "tool_failed")
        return reject(errors.data_unavailable(), "tool_failed")  # pragma: no cover

    def _lookup_receipt(
        self, spec: ToolSpec, ctx: AgentRunContext, args: BaseModel, call: ToolCall
    ) -> ToolResult | None:
        if spec.receipt_lookup is None:
            raise OperationalError("receipt lookup unavailable", {}, Exception("no lookup"))
        with self.db.read() as s:
            return spec.receipt_lookup(s, ctx.binding, args, call)

    def _run_service(
        self, spec: ToolSpec, ctx: AgentRunContext, args: BaseModel, call: ToolCall
    ) -> ToolResult:
        if time.perf_counter() >= call.deadline:
            raise errors.deadline_exceeded()
        if spec.kind == "read":
            with self.db.read() as s:
                check_session_active(s, ctx.binding)
                result = spec.handler(s, ctx.binding, args, call)
            if time.perf_counter() >= call.deadline:
                raise errors.deadline_exceeded()
            return result
        with self.db.write() as s:
            check_session_active(s, ctx.binding)
            result = spec.handler(s, ctx.binding, args, call)
            if time.perf_counter() >= call.deadline:
                raise errors.deadline_exceeded()  # rolls back the write transaction
            return result
