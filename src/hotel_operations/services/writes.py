"""Controlled-write helpers: canonical operation keys and durable receipts.

The operation key is derived from trusted request context plus a canonical
payload fingerprint: ``(session_id, client_request_id, tool_name, payload)``.
Never from a model-supplied key or a provider tool-call ID. Replaying the same
normalized write within one guest request returns the committed result; a new
guest message is a new intent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.clock import utcnow
from hotel_operations.ids import new_id
from hotel_operations.storage.models import OperationReceipt


@dataclass(frozen=True)
class WriteScope:
    """Trusted identifiers for one controlled write (never model-supplied)."""

    hotel_id: str
    reservation_id: str
    session_id: str
    run_id: str
    client_request_id: str


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def operation_key(scope: WriteScope, tool_name: str, digest: str) -> str:
    raw = "\x1f".join((scope.session_id, scope.client_request_id, tool_name, digest))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def find_replay(
    session: Session, scope: WriteScope, tool_name: str, payload: dict[str, Any]
) -> tuple[str, str, OperationReceipt | None]:
    """Return ``(operation_key, payload_hash, existing_receipt)``."""
    digest = payload_hash(payload)
    key = operation_key(scope, tool_name, digest)
    existing = session.scalar(select(OperationReceipt).where(OperationReceipt.operation_key == key))
    if existing is not None and existing.payload_hash != digest:
        raise errors.DomainError(
            "IDEMPOTENCY_CONFLICT",
            "This operation key was already used with a different payload.",
            http_status=409,
        )
    return key, digest, existing


def replayed_result(receipt: OperationReceipt) -> dict[str, Any]:
    return {**receipt.result, "receipt_id": receipt.id, "replayed": True}


def record_receipt(
    session: Session,
    *,
    key: str,
    digest: str,
    scope: WriteScope,
    tool_name: str,
    result: dict[str, Any],
    artifact_type: str | None,
    artifact_id: str | None,
) -> OperationReceipt:
    receipt = OperationReceipt(
        id=new_id("rcp"),
        operation_key=key,
        hotel_id=scope.hotel_id,
        reservation_id=scope.reservation_id,
        session_id=scope.session_id,
        run_id=scope.run_id,
        client_request_id=scope.client_request_id,
        tool_name=tool_name,
        payload_hash=digest,
        result=result,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        committed_at=utcnow(),
    )
    session.add(receipt)
    session.flush()
    return receipt
