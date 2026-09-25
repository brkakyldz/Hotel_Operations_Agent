"""Authoritative artifact references for a run, read from committed receipts.

The UI shows these instead of trusting model prose. They are read from the
database at projection time, so they remain visible even if the run's final
narration or its own status write failed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations.clock import iso
from hotel_operations.services.approvals import effective_status
from hotel_operations.storage.models import (
    Approval,
    Hotel,
    HousekeepingTask,
    MaintenanceTask,
    OperationReceipt,
)

ARTIFACT_MODELS: dict[str, Any] = {
    "housekeeping_task": HousekeepingTask,
    "maintenance_task": MaintenanceTask,
    "approval": Approval,
}


def current_status(session: Session, artifact_type: str | None, artifact_id: str | None) -> Any:
    model = ARTIFACT_MODELS.get(artifact_type or "")
    if model is None or artifact_id is None:
        return None
    row = session.get(model, artifact_id)
    if isinstance(row, Approval):
        hotel = session.get(Hotel, row.hotel_id)
        return row.status if hotel is None else effective_status(row, hotel.demo_now)
    return None if row is None else getattr(row, "status", None)


def run_artifacts(session: Session, run_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for receipt in session.scalars(
        select(OperationReceipt)
        .where(OperationReceipt.run_id == run_id)
        .order_by(OperationReceipt.committed_at, OperationReceipt.id)
    ):
        out.append(
            {
                "kind": receipt.artifact_type,
                "id": receipt.artifact_id,
                "receipt_id": receipt.id,
                "tool_name": receipt.tool_name,
                "committed_at": iso(receipt.committed_at),
                # Creation-time result (immutable) and the artifact's current status.
                "result": receipt.result,
                "current_status": current_status(
                    session, receipt.artifact_type, receipt.artifact_id
                ),
            }
        )
    return out
