"""get_my_reservation: a read tool."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from hotel_operations.services.reservations import read_my_reservation
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec


class NoArguments(BaseModel):
    """Strict empty input: any key (for example a reservation ID) is rejected."""

    model_config = ConfigDict(extra="forbid")


def _handle(
    session: Session, binding: SessionBinding, _args: NoArguments, _call: ToolCall
) -> ToolResult:
    read = read_my_reservation(session, binding)
    data = read["data"]
    return ToolResult(
        data=data,
        meta=read["meta"],
        outcome="ok",
        entity=read["entity"],
        audit_summary={
            "reservation_reference": data["reservation_reference"],
            "status": data["status"],
            "reservation_version": data["reservation_version"],
        },
    )


GET_MY_RESERVATION = ToolSpec(
    name="get_my_reservation",
    description=(
        "Retrieve the selected guest's own current reservation from the hotel system: "
        "reference, room, status, scheduled and actual check-in/checkout times and version. "
        "Takes no arguments; it can only ever read the selected guest's reservation."
    ),
    args_model=NoArguments,
    kind="read",
    handler=_handle,
)
