"""The model-facing tool catalog. Only tools listed here are ever registered.

Five read tools (reservation, policy, requests and the two advisory evaluations) and four
controlled writes (housekeeping, maintenance, late checkout, early check-in).
"""

from __future__ import annotations

from hotel_operations.tools.checkin import EVALUATE_EARLY_CHECK_IN, REQUEST_EARLY_CHECK_IN
from hotel_operations.tools.checkout import EVALUATE_LATE_CHECKOUT, REQUEST_LATE_CHECKOUT
from hotel_operations.tools.gateway import ToolSpec
from hotel_operations.tools.housekeeping import CREATE_HOUSEKEEPING_TASK
from hotel_operations.tools.maintenance import CREATE_MAINTENANCE_TASK
from hotel_operations.tools.policy import GET_HOTEL_POLICY
from hotel_operations.tools.requests import GET_MY_REQUESTS
from hotel_operations.tools.reservation import GET_MY_RESERVATION


def read_tool_specs() -> list[ToolSpec]:
    """A hotel update's turn: the guest is told and facts re-read, nothing acted on."""
    return [spec for spec in active_tool_specs() if spec.kind == "read"]


def active_tool_specs() -> list[ToolSpec]:
    return [
        GET_MY_RESERVATION,
        GET_HOTEL_POLICY,
        CREATE_HOUSEKEEPING_TASK,
        CREATE_MAINTENANCE_TASK,
        GET_MY_REQUESTS,
        EVALUATE_LATE_CHECKOUT,
        REQUEST_LATE_CHECKOUT,
        EVALUATE_EARLY_CHECK_IN,
        REQUEST_EARLY_CHECK_IN,
    ]
