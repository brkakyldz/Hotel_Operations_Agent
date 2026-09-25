"""create_housekeeping_task: a controlled write tool."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hotel_operations.services.housekeeping import TOOL_NAME, create_housekeeping_task
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.services.writes import find_replay, normalize_text, replayed_result
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec, write_scope


class HousekeepingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: Literal["towels", "pillows", "room_cleaning"] = Field(
        description="What the guest wants: towels, pillows or room_cleaning."
    )
    quantity: int = Field(
        ge=1,
        le=6,
        description="Total number requested in this message (1-6). Use 1 for room_cleaning.",
    )
    notes: str | None = Field(
        max_length=300,
        description="Optional short note from the guest (max 300 characters), or null.",
    )


def _result(out: dict[str, object]) -> ToolResult:
    meta = {"receipt_id": out["receipt_id"], "replayed": out["replayed"]}
    if "task_id" in out:  # created (or replayed): a pending task with its receipt
        return ToolResult(
            data=out,
            meta=meta,
            outcome="pending",
            entity=("housekeeping_task", str(out["task_id"])),
            audit_summary={
                "task_id": out["task_id"],
                "item": out["item"],
                "quantity": out["quantity"],
                "status": out["status"],
                "remaining_this_stay": out.get("remaining_this_stay"),
                "replayed": out["replayed"],
            },
        )
    # Refused, or a cleaning already open: nothing was created (housekeeping policy v1).
    existing = out.get("existing_task_id")
    summary = {
        "outcome": out["outcome"],
        "item": out["item"],
        "quantity": out["quantity"],
        "reason_codes": out["reason_codes"],
        "next_available_local": out.get("next_available_local"),
        "remaining_this_stay": out.get("remaining_this_stay"),
        "existing_task_id": existing,
    }
    return ToolResult(
        data=out,
        meta=meta,
        outcome=str(out["outcome"]),
        entity=("housekeeping_task", str(existing)) if existing else None,
        audit_summary={k: v for k, v in summary.items() if v is not None},
    )


def _handle(
    session: Session, binding: SessionBinding, args: HousekeepingArgs, call: ToolCall
) -> ToolResult:
    out = create_housekeeping_task(
        session,
        binding,
        write_scope(binding, call),
        item=args.item,
        quantity=args.quantity,
        notes=args.notes,
    )
    return _result(out)


def _lookup(
    session: Session, binding: SessionBinding, args: HousekeepingArgs, call: ToolCall
) -> ToolResult | None:
    payload = {"item": args.item, "quantity": args.quantity, "notes": normalize_text(args.notes)}
    _, _, existing = find_replay(session, write_scope(binding, call), TOOL_NAME, payload)
    return None if existing is None else _result(replayed_result(existing))


CREATE_HOUSEKEEPING_TASK = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Create ONE pending housekeeping request for the selected checked-in guest's own room "
        "(towels, pillows or room cleaning). Use the total quantity asked for in the current "
        "message. Only call this when the guest actually asks for the service, never for an "
        "information question. The hotel's rules decide it: outcome 'pending' is a request "
        "recorded for simulated staff (not a delivery; do not promise a time); 'denied' means "
        "nothing was created (see reason_codes, next_available_local, remaining_this_stay); "
        "'already_requested' means a room cleaning is already open (existing_task_id)."
    ),
    args_model=HousekeepingArgs,
    kind="write",
    handler=_handle,
    receipt_lookup=_lookup,
)
