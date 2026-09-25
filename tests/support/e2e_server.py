"""Offline E2E backend: the real app over fresh migrated SQLite files, with a
test-only rule-based model adapter. Browser tests backed by this server are
offline evidence, never live proof.

Usage (Playwright webServer): ``uv run python -m tests.support.e2e_server --port 8765``
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import openai
import uvicorn
from fastapi import APIRouter

from hotel_operations.app import create_app
from hotel_operations.clock import utcnow
from hotel_operations.config import Settings
from hotel_operations.fixtures import (
    DEMO_NOW,
    GUESTS,
    NEXT_ARRIVALS,
    POLICY_V1,
    ROOM_DAY_PLANS,
    seed_fixture,
)
from hotel_operations.services import world
from hotel_operations.storage.db import Database
from hotel_operations.storage.migrate import upgrade_to_head
from hotel_operations.storage.models import (
    Approval,
    AuditEvent,
    DemoSession,
    Hotel,
    HotelPolicy,
    Reservation,
    Room,
    RoomDayPlan,
)
from tests.support.models import RuleModel, Turn, call, hotel_rule

# Before the fixture's hotel clock, which only moves forward: a review with this expiry has lapsed.
LAPSED = DEMO_NOW - timedelta(days=1)


def e2e_rule(turn: Turn) -> Any:
    text = turn.last_user_text().lower()
    if "simulate provider outage" in text:
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        return openai.RateLimitError(
            "rate limited", response=httpx.Response(429, request=request), body=None
        )
    if "simulate narration failure" in text:
        # Fault injection: the write commits, then the provider fails while narrating.
        if not turn.tool_outputs_since_user():
            return call(
                "create_housekeeping_task", {"item": "towels", "quantity": 1, "notes": None}
            )
        return RuntimeError("injected narration failure")
    return hotel_rule(turn)


def restart_rule(hung: threading.Event) -> Any:
    """``simulate process restart``: commit a task, then stay in the narration call."""

    async def rule(turn: Turn) -> Any:
        if "simulate process restart" not in turn.last_user_text().lower():
            return e2e_rule(turn)
        if not turn.tool_outputs_since_user():
            return call(
                "create_housekeeping_task", {"item": "towels", "quantity": 1, "notes": None}
            )
        hung.set()
        await asyncio.sleep(3600)  # still in flight when the process restarts
        return None  # pragma: no cover

    return rule


def build(workdir: Path, control: dict[str, Any] | None = None, *, fresh: bool = True) -> Any:
    """Build the app over ``workdir``; ``fresh=False`` restarts over the existing files."""
    db_path = workdir / "hotel.sqlite"
    if fresh:
        upgrade_to_head(db_path)
        db = Database(db_path)
        with db.write() as s:
            seed_fixture(s)
        db.dispose()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        HOTEL_DB_PATH=db_path,
        CONVERSATIONS_DB_PATH=workdir / "conversations.sqlite",
        HOTEL_TELEMETRY_LOG=workdir / "telemetry.jsonl",  # never the user's data/ file
        OPENAI_API_KEY=None,
        HOTEL_UI_ORIGIN="http://127.0.0.1:5174",
    )
    hung = threading.Event()
    model = RuleModel(restart_rule(hung))
    app = create_app(settings, model_factory=lambda: model)

    # Test-only control route, mounted only in this offline E2E server.
    router = APIRouter(prefix="/__e2e__")

    @router.post("/close-sessions")
    def close_sessions() -> dict[str, int]:
        db2 = Database(db_path)
        with db2.write() as s:
            rows = list(s.query(DemoSession))
            for row in rows:
                row.closed_at = utcnow()
        db2.dispose()
        return {"closed": len(rows)}

    @router.post("/restore-checkouts")
    def restore_checkouts() -> dict[str, int]:
        """Put fixture guests' checkout and check-in times back (order-independent journeys)."""
        db2 = Database(db_path)
        with db2.write() as s:
            for guest in GUESTS:
                row = s.get(Reservation, guest.reservation_id)
                if row is not None and (
                    row.scheduled_checkout != guest.checkout
                    or row.scheduled_check_in != guest.check_in
                ):
                    row.scheduled_checkout = guest.checkout
                    row.scheduled_check_in = guest.check_in
                    row.version += 1
        db2.dispose()
        return {"restored": len(GUESTS)}

    @router.post("/restore-world")
    def restore_world() -> dict[str, bool]:
        """Undo hotel changes so world journeys are order-independent (versions still rise)."""
        db2 = Database(db_path)
        with db2.write() as s:
            hotel = s.get(Hotel, "hotel_demo")
            assert hotel is not None
            hotel.demo_now = DEMO_NOW
            for arrival in NEXT_ARRIVALS:
                row = s.get(Reservation, arrival.reservation_id)
                assert row is not None
                row.scheduled_check_in, row.status = arrival.check_in, "confirmed"
                row.version += 1
            for room in s.query(Room):
                room.out_of_service = False
                room.version += 1
                room.operational_version += 1
            for plan_id, _room, _ready, finish_by in ROOM_DAY_PLANS:
                plan = s.get(RoomDayPlan, plan_id)
                assert plan is not None
                plan.housekeeping_finish_by = finish_by
                plan.version += 1
            policy = s.get(HotelPolicy, "hotel_demo")
            assert policy is not None
            policy.auto_extension_until_local = str(POLICY_V1["auto_extension_until_local"])
            policy.version += 1
            s.query(AuditEvent).filter(AuditEvent.event_type.in_(world.WORLD_EVENT_TYPES)).delete()
            for row in s.query(Approval).filter(Approval.status == "pending"):
                row.expires_at = LAPSED  # an earlier journey's review cannot hold the slot
        db2.dispose()
        return {"restored": True}

    @router.post("/expire-approvals")
    def expire_approvals() -> dict[str, int]:
        """Injected condition: the hotel clock has overtaken every pending review."""
        db2 = Database(db_path)
        with db2.write() as s:
            rows = list(s.query(Approval).filter(Approval.status == "pending"))
            for row in rows:
                row.expires_at = LAPSED
        db2.dispose()
        return {"expired": len(rows)}

    @router.post("/change-operational-data")
    def change_operational_data() -> dict[str, int]:
        """Injected condition: room 305's operational data changed (e.g. its bookings)."""
        db2 = Database(db_path)
        with db2.write() as s:
            room = s.get(Room, "room_305")
            assert room is not None
            room.operational_version += 1
            version = room.operational_version
        db2.dispose()
        return {"operational_version": version}

    @router.post("/restart")
    async def restart() -> dict[str, bool]:
        """Stop this app once the restart-journey run is in flight; ``main`` then starts
        a new app over the same files, whose startup recovery sees the unfinished run."""
        for _ in range(200):
            if hung.is_set():
                break
            await asyncio.sleep(0.05)
        if control is None or "server" not in control:
            return {"restarting": False, "run_in_flight": hung.is_set()}
        control["restart"] = True
        control["server"].should_exit = True
        return {"restarting": True, "run_in_flight": hung.is_set()}

    app.include_router(router)
    return app


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="hotel-e2e-"))
    control: dict[str, Any] = {}
    fresh = True
    while True:  # one "process lifetime" per iteration; /__e2e__/restart ends one
        control["restart"] = False
        app = build(workdir, control, fresh=fresh)
        # A restart stands in for a process death, which waits for nobody. Without a bound,
        # uvicorn's graceful shutdown waits on the open SSE stream of the deliberately hung
        # run until a keep-alive write fails (up to 15 s): a flaky restart journey.
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            timeout_graceful_shutdown=2,
        )
        control["server"] = uvicorn.Server(config)
        control["server"].run()
        if not control["restart"]:
            return 0
        fresh = False


if __name__ == "__main__":
    raise SystemExit(main())
