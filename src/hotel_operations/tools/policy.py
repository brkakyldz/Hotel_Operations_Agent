"""get_hotel_policy: a read tool; one call reads several topics."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from hotel_operations.services.policy import ALL_TOPICS, PolicyTopic, read_policies
from hotel_operations.services.sessions import SessionBinding
from hotel_operations.tools.gateway import ToolCall, ToolResult, ToolSpec


class PolicyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: list[PolicyTopic] = Field(
        min_length=1,
        max_length=len(ALL_TOPICS),
        description="Every topic the question needs, read together in this one call.",
    )


def _handle(
    session: Session, binding: SessionBinding, args: PolicyArgs, _call: ToolCall
) -> ToolResult:
    read = read_policies(session, binding, args.topics)
    data = read["data"]
    summary = {"topics": [t["topic"] for t in data["topics"]]}
    if "unavailable_topics" in data:
        summary["unavailable_topics"] = data["unavailable_topics"]
    return ToolResult(
        data=data,
        meta=read["meta"],
        outcome="ok",
        entity=("hotel_policy", binding.hotel_id),
        audit_summary=summary,
    )


GET_HOTEL_POLICY = ToolSpec(
    name="get_hotel_policy",
    description=(
        "Read the fictional hotel's current stored policy for one or more topics in a single "
        "call: breakfast hours; general check-in or checkout rules; what housekeeping or "
        "maintenance requests are supported; facilities (pool, gym, spa hours); dining "
        "(restaurant, bar, room service hours); internet (Wi-Fi); house_rules (quiet hours, "
        "smoking, pets); parking; luggage storage; room_amenities (what is in the room). Pass "
        "every topic the question needs, or all twelve for an overview of the hotel's "
        "policies. Read-only. Returns only stored values; a topic without stored data is "
        "listed in unavailable_topics, so say you do not have it rather than guessing. It does "
        "not decide an individual guest's early check-in or late checkout, and it books nothing."
    ),
    args_model=PolicyArgs,
    kind="read",
    handler=_handle,
)
