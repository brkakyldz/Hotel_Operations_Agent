"""evaluate_early_check_in (read) and request_early_check_in (controlled write)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hotel_operations.services.checkin import (
    TOOL_NAME,
    evaluate_early_check_in,
    parse_requested,
    request_early_check_in,
    request_payload,
)
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.services.writes import find_replay, replayed_result
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec, write_scope

TIME_DESCRIPTION = (
    "The requested check-in as a complete ISO 8601 local date-time WITH the hotel's UTC offset, "
    "on the guest's arrival date, e.g. 2026-09-22T13:00:00+03:00. Use 24-hour time."
)


class CheckInArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_check_in_local: str = Field(max_length=40, description=TIME_DESCRIPTION)


def _summary(data: dict[str, object]) -> dict[str, object]:
    keys = ("requested_check_in_local", "decision", "outcome", "reason_codes", "replayed")
    return {k: data[k] for k in keys if k in data}


def _evaluate(
    session: Session, binding: SessionBinding, args: CheckInArgs, _call: ToolCall
) -> ToolResult:
    read = evaluate_early_check_in(session, binding, args.requested_check_in_local)
    data = read["data"]
    return ToolResult(
        data=data,
        meta=read["meta"],
        outcome=str(data["decision"]),
        entity=("reservation", binding.reservation_id),
        audit_summary=_summary(data),
    )


def _result(out: dict[str, object], binding: SessionBinding) -> ToolResult:
    return ToolResult(
        data=out,
        meta={"receipt_id": out["receipt_id"], "replayed": out["replayed"]},
        outcome=str(out["outcome"]),
        entity=("reservation", binding.reservation_id),
        audit_summary=_summary(out),
    )


def _request(
    session: Session, binding: SessionBinding, args: CheckInArgs, call: ToolCall
) -> ToolResult:
    out = request_early_check_in(
        session,
        binding,
        write_scope(binding, call),
        requested_check_in_local=args.requested_check_in_local,
    )
    return _result(out, binding)


def _lookup(
    session: Session, binding: SessionBinding, args: CheckInArgs, call: ToolCall
) -> ToolResult | None:
    requested = parse_requested(session, binding, args.requested_check_in_local)
    _, _, existing = find_replay(
        session, write_scope(binding, call), TOOL_NAME, request_payload(requested)
    )
    if existing is None:
        return None
    return _result(replayed_result(existing), binding)


EVALUATE_EARLY_CHECK_IN = ToolSpec(
    name="evaluate_early_check_in",
    description=(
        "Check, without changing anything, whether the selected arriving guest (a confirmed "
        "reservation, not yet checked in) could check in earlier on their arrival day. Returns an "
        "advisory decision (auto_allowed, denied or no_change) with reasons and the earliest "
        "possible time. Use it when the guest asks whether or how early they could check in. It "
        "is not a booking and not permission to act."
    ),
    args_model=CheckInArgs,
    kind="read",
    handler=_evaluate,
)

REQUEST_EARLY_CHECK_IN = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Ask the hotel system to move the selected arriving guest's check-in earlier to a "
        "specific time on their arrival day. The system re-checks the rules itself and returns "
        "the outcome: applied (check-in time moved), denied or no_change (already allowed from "
        "that time). There is no manager review for early check-in. Call it only when the guest "
        "asks to check in earlier."
    ),
    args_model=CheckInArgs,
    kind="write",
    handler=_request,
    receipt_lookup=_lookup,
)
