"""get_my_requests: a read tool over the guest's tasks and manager reviews."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hotel_operations.services.requests import (
    TOOL_PAGE_SIZE,
    TOOL_TEXT_LIMIT,
    list_my_requests,
)
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec


class RequestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str | None = Field(
        max_length=200,
        description="Opaque cursor from a previous result's next_cursor, or null for the newest.",
    )


def _handle(
    session: Session, binding: SessionBinding, args: RequestsArgs, _call: ToolCall
) -> ToolResult:
    read = list_my_requests(
        session, binding, args.cursor, page_size=TOOL_PAGE_SIZE, text_limit=TOOL_TEXT_LIMIT
    )
    requests = read["data"]["requests"]
    return ToolResult(
        data=read["data"],
        meta=read["meta"],
        outcome="ok",
        entity=("reservation", binding.reservation_id),
        audit_summary={"returned": len(requests)},
    )


GET_MY_REQUESTS = ToolSpec(
    name="get_my_requests",
    description=(
        "List the selected guest's own existing requests for their reservation (newest first, "
        f"up to {TOOL_PAGE_SIZE}; long guest notes are shortened), with current status. Read-only. "
        "Pending means recorded for simulated staff, not done."
    ),
    args_model=RequestsArgs,
    kind="read",
    handler=_handle,
)
