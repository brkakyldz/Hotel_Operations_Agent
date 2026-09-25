"""Run admission checks shared by a guest's message and a hotel update.

Both start a run in a guest's conversation, so both obey the same limits: one active run
per session and two across the app.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hotel_operations import errors
from hotel_operations.config import Settings
from hotel_operations.storage.models import DemoSession, RunRecord

ACTIVE_STATUSES = ("queued", "running")


def input_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _active(session: Session, *where: object) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(RunRecord)
        .where(RunRecord.status.in_(ACTIVE_STATUSES), *where)  # type: ignore[arg-type]
    )
    return int(count or 0)


def admission_blocker(
    session: Session, settings: Settings, demo_session: DemoSession
) -> errors.DomainError | None:
    """Why no new run may start in this session now, or None when one may."""
    if _active(session, RunRecord.session_id == demo_session.id) >= (
        settings.max_active_runs_per_session
    ):
        return errors.DomainError(
            "RUN_IN_PROGRESS",
            "Another request in this session is still running. Wait for it to finish.",
            http_status=409,
        )
    if _active(session) >= settings.max_active_runs_global:
        return errors.DomainError(
            "CAPACITY_EXCEEDED",
            "The agent is already writing two replies. Try again in a few seconds.",
            http_status=429,
            retryable=True,
            retry_after=5,
        )
    return None
