"""evaluate_late_checkout (read) and request_late_checkout (controlled write)."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.domain.checkout import parse_requested
from hotel_operations.services.checkout import (
    TOOL_NAME,
    evaluate_late_checkout,
    request_late_checkout,
    request_payload,
)
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.services.writes import find_replay, normalize_text, replayed_result
from hotel_operations.storage.models import Hotel
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec, write_scope

TIME_DESCRIPTION = (
    "The requested checkout as a complete ISO 8601 local date-time WITH the hotel's UTC offset, "
    "on the guest's departure date, e.g. 2026-09-22T14:00:00+03:00. Use 24-hour time."
)


class EvaluateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_checkout_local: str = Field(max_length=40, description=TIME_DESCRIPTION)


class RequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_checkout_local: str = Field(max_length=40, description=TIME_DESCRIPTION)
    reason: str | None = Field(
        max_length=300, description="Optional short reason from the guest (max 300), or null."
    )


def _summary(data: dict[str, object]) -> dict[str, object]:
    keys = ("requested_checkout_local", "decision", "outcome", "reason_codes", "replayed")
    return {k: data[k] for k in keys if k in data}


def _evaluate(
    session: Session, binding: SessionBinding, args: EvaluateArgs, _call: ToolCall
) -> ToolResult:
    read = evaluate_late_checkout(session, binding, args.requested_checkout_local)
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
    session: Session, binding: SessionBinding, args: RequestArgs, call: ToolCall
) -> ToolResult:
    out = request_late_checkout(
        session,
        binding,
        write_scope(binding, call),
        requested_checkout_local=args.requested_checkout_local,
        reason=args.reason,
    )
    return _result(out, binding)


def _lookup(
    session: Session, binding: SessionBinding, args: RequestArgs, call: ToolCall
) -> ToolResult | None:
    hotel = session.get(Hotel, binding.hotel_id)
    if hotel is None:
        raise errors.data_unavailable()
    requested = parse_requested(args.requested_checkout_local, ZoneInfo(hotel.timezone))
    payload = request_payload(requested, normalize_text(args.reason))
    _, _, existing = find_replay(session, write_scope(binding, call), TOOL_NAME, payload)
    if existing is None:
        return None
    return _result(replayed_result(existing), binding)


EVALUATE_LATE_CHECKOUT = ToolSpec(
    name="evaluate_late_checkout",
    description=(
        "Check, without changing anything, whether the selected checked-in guest could check out "
        "later on their departure day. Returns an advisory decision (auto_allowed, "
        "approval_required, denied or no_change) with reasons. Use it when the guest asks "
        "whether a later checkout is possible. It is not a booking and not permission to act."
    ),
    args_model=EvaluateArgs,
    kind="read",
    handler=_evaluate,
)

REQUEST_LATE_CHECKOUT = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Ask the hotel system to extend the selected checked-in guest's checkout to a specific "
        "time on their departure day. The system re-checks the rules itself and returns the "
        "outcome: applied (checkout changed), denied, no_change (already at least that late) or "
        "pending_approval (a manager must review it; nothing changes unless approved). Call it "
        "only when the guest asks to change their checkout."
    ),
    args_model=RequestArgs,
    kind="write",
    handler=_request,
    receipt_lookup=_lookup,
)
