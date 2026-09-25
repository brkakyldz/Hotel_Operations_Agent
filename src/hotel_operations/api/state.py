"""Process-wide services held on ``app.state`` and injected into routes."""

from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import dataclass, field

from fastapi import Request

from hotel_operations import errors, telemetry
from hotel_operations.agent.run_service import ModelFactory, RunManager
from hotel_operations.config import Settings
from hotel_operations.reset import ResetRefused, reset_demo
from hotel_operations.storage.db import Database

log = logging.getLogger(__name__)


@dataclass
class AppServices:
    """Everything a route may use: the single process-wide instance."""

    settings: Settings
    db: Database
    run_manager: RunManager
    provider_available: bool
    model_factory: ModelFactory
    _reset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # True while Start over swaps the databases: requests then get a retryable 503 instead of
    # reopening a connection to a file the reset is about to move aside.
    resetting: bool = False

    def provider_configured(self) -> bool:
        return self.provider_available

    async def reset_world(self) -> None:
        """Start over: restore fixture v1 in both databases while the app keeps running.

        Runs in flight are cancelled and end as interrupted before the old world is
        discarded; every guest session token dies with it. The rebuild itself is the
        CLI reset (``hotel_operations.reset``), which moves the old files aside first
        and restores them if rebuilding fails, so a failed reset leaves the previous
        world in place. Either way this reopens whichever world is now on disk.
        """
        async with self._reset_lock:
            self.resetting = True
            try:
                await self.run_manager.shutdown()
                await asyncio.to_thread(self.run_manager.recover_interrupted)
                self.db.dispose()
                gc.collect()  # finalize closed SQLite handles, which Windows keeps locked
                await asyncio.to_thread(
                    reset_demo,
                    self.settings.hotel_db_path,
                    self.settings.conversations_db_path,
                    confirm=True,
                )
            except ResetRefused as exc:
                log.warning("in-app reset refused: %s", exc.code)
                raise errors.DomainError(
                    "RESET_REFUSED",
                    f"The hotel could not be reset: {exc.message}",
                    http_status=409,
                    retryable=True,
                ) from exc
            finally:
                self.db = Database(
                    self.settings.hotel_db_path, self.settings.sqlite_busy_timeout_ms
                )
                self.run_manager = RunManager(
                    self.db,
                    self.settings,
                    self.model_factory,
                    provider_available=self.provider_available,
                )
                self.resetting = False
            telemetry.emit("demo_reset")


def get_services(request: Request) -> AppServices:
    services: AppServices = request.app.state.services
    if services.resetting:
        raise errors.DomainError(
            "RESET_IN_PROGRESS",
            "The hotel is starting over. Try again in a moment.",
            http_status=503,
            retryable=True,
            retry_after=1,
        )
    return services
