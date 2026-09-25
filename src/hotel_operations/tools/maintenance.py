"""create_maintenance_task: a controlled write tool."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hotel_operations.services.maintenance import (
    TOOL_NAME,
    create_maintenance_task,
    maintenance_payload,
)
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.services.writes import find_replay, replayed_result
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec, write_scope


class MaintenanceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["hvac", "plumbing", "electrical", "other"] = Field(
        description=(
            "Issue category: hvac (air conditioning, heating, ventilation), plumbing (water, "
            "leaks, drains, shower, toilet), electrical (lights, sockets, power) or other."
        )
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Short factual description of the issue in the guest's words (1-500 chars).",
    )


def _result(out: dict[str, object]) -> ToolResult:
    return ToolResult(
        data=out,
        meta={"receipt_id": out["receipt_id"], "replayed": out["replayed"]},
        outcome="pending",
        entity=("maintenance_task", str(out["task_id"])),
        audit_summary={
            "task_id": out["task_id"],
            "category": out["category"],
            "status": out["status"],
            "replayed": out["replayed"],
        },
    )


def _handle(
    session: Session, binding: SessionBinding, args: MaintenanceArgs, call: ToolCall
) -> ToolResult:
    out = create_maintenance_task(
        session,
        binding,
        write_scope(binding, call),
        category=args.category,
        description=args.description,
    )
    return _result(out)


def _lookup(
    session: Session, binding: SessionBinding, args: MaintenanceArgs, call: ToolCall
) -> ToolResult | None:
    payload = maintenance_payload(args.category, args.description)
    _, _, existing = find_replay(session, write_scope(binding, call), TOOL_NAME, payload)
    return None if existing is None else _result(replayed_result(existing))


CREATE_MAINTENANCE_TASK = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Report ONE maintenance issue in the selected checked-in guest's own room (for example "
        "the air conditioning not cooling, a leak, or a light not working). Call it only when "
        "the guest reports a problem, once per issue. The result is a pending report for "
        "simulated staff: it is not a repair, has no promised repair time and is not an "
        "emergency service."
    ),
    args_model=MaintenanceArgs,
    kind="write",
    handler=_handle,
    receipt_lookup=_lookup,
)
