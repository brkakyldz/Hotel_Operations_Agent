"""Stable machine error codes shared by HTTP routes and tool envelopes."""

from __future__ import annotations


class DomainError(Exception):
    """A safe, projectable failure. Never carries SQL, stack traces or secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after


def invalid_session() -> DomainError:
    return DomainError(
        "INVALID_SESSION", "Session is not valid. Select a guest again.", http_status=401
    )


def session_scope_stale() -> DomainError:
    return DomainError(
        "SESSION_SCOPE_STALE",
        "The selected guest's reservation or room assignment changed. Select the guest again.",
        http_status=409,
    )


def data_unavailable() -> DomainError:
    return DomainError(
        "DATA_UNAVAILABLE",
        "Hotel data is temporarily unavailable.",
        http_status=503,
        retryable=True,
    )


def deadline_exceeded() -> DomainError:
    return DomainError(
        "DEADLINE_EXCEEDED", "The operation ran out of time.", http_status=504, retryable=False
    )


def not_found(what: str = "Resource") -> DomainError:
    return DomainError("NOT_FOUND", f"{what} not found.", http_status=404)
