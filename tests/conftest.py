"""Shared offline fixtures: isolated migrated SQLite files, settings and app clients.

Outbound model traffic is denied for every non-live test: the OpenAI key is
unset in test settings, and the socket guard below refuses non-loopback
connections.
"""

from __future__ import annotations

import os
import shutil
import socket
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hotel_operations.app import create_app
from hotel_operations.config import Settings
from hotel_operations.fixtures import seed_fixture
from hotel_operations.storage.db import Database
from hotel_operations.storage.migrate import upgrade_to_head
from tests.support.models import RuleModel, reservation_rule

# --- deny outbound network in offline tests ------------------------------------------

_real_connect = socket.socket.connect


def _guarded_connect(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
        raise RuntimeError(f"offline test attempted outbound connection to {host!r}")
    return _real_connect(self, address)


@pytest.fixture(autouse=True)
def _deny_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    for var in ("OPENAI_API_KEY",):
        monkeypatch.delenv(var, raising=False)


# --- databases -------------------------------------------------------------------------


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "hotel.sqlite"
    upgrade_to_head(path)
    db = Database(path)
    with db.write() as s:
        seed_fixture(s)
    db.dispose()
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    target = tmp_path / "hotel.sqlite"
    shutil.copyfile(template_db, target)
    return target


def make_settings(tmp_path: Path, db_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "HOTEL_DB_PATH": db_path,
        "CONVERSATIONS_DB_PATH": tmp_path / "conversations.sqlite",
        "OPENAI_API_KEY": None,
        "HOTEL_TELEMETRY_LOG": tmp_path / "telemetry.jsonl",
    }
    settings = Settings(_env_file=None, **values)  # type: ignore[call-arg]
    return settings.model_copy(update=overrides)


@pytest.fixture
def settings(tmp_path: Path, db_path: Path) -> Settings:
    return make_settings(tmp_path, db_path)


@pytest.fixture
def db(db_path: Path) -> Iterator[Database]:
    database = Database(db_path)
    yield database
    database.dispose()


# --- app clients -------------------------------------------------------------------------


class Harness:
    """A running app (lifespan active) plus helpers to drive it like the browser."""

    def __init__(self, client: TestClient, model: Any) -> None:
        self.client = client
        self.model = model
        self.stopped = False

    def stop(self) -> None:
        """Stop the process gracefully (lifespan shutdown cancels in-flight runs)."""
        if not self.stopped:
            self.stopped = True
            self.client.__exit__(None, None, None)

    @property
    def services(self) -> Any:
        return self.client.app.state.services  # type: ignore[attr-defined]

    def session(self, guest_id: str = "G-001") -> dict[str, Any]:
        r = self.client.post("/api/demo/sessions", json={"guest_id": guest_id})
        assert r.status_code == 201, r.text
        body: dict[str, Any] = r.json()
        return body

    @staticmethod
    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def chat(self, token: str, message: str, request_id: str | None = None) -> Any:
        return self.client.post(
            "/api/chat",
            json={"client_request_id": request_id or str(uuid.uuid4()), "message": message},
            headers=self.auth(token),
        )

    def wait(self, token: str, run_id: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            r = self.client.get(f"/api/runs/{run_id}", headers=self.auth(token))
            assert r.status_code == 200, r.text
            body: dict[str, Any] = r.json()
            if body["status"] in ("completed", "failed", "interrupted"):
                return body
            if time.monotonic() > deadline:
                raise AssertionError(f"run {run_id} did not finish: {body['status']}")
            time.sleep(0.02)

    def settle(self) -> None:
        """Wait until no run is executing, including hotel updates' turns that an
        operator command started; the browser's composer waits the same way."""
        self.client.portal.call(self.services.run_manager.wait_idle)  # type: ignore[union-attr]

    def ask(self, token: str, message: str) -> dict[str, Any]:
        self.settle()
        r = self.chat(token, message)
        assert r.status_code == 202, r.text
        return self.wait(token, r.json()["run_id"])


@pytest.fixture
def make_harness(tmp_path: Path, db_path: Path) -> Iterator[Callable[..., Harness]]:
    harnesses: list[Harness] = []

    def factory(model: Any = None, **overrides: Any) -> Harness:
        model = model if model is not None else RuleModel(reservation_rule)
        settings = make_settings(tmp_path, db_path, **overrides)
        app = create_app(settings, model_factory=lambda: model)
        client = TestClient(app, base_url="http://127.0.0.1")
        client.__enter__()
        harnesses.append(Harness(client, model))
        return harnesses[-1]

    yield factory
    for h in harnesses:
        h.stop()


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("HOTEL_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set HOTEL_LIVE_TESTS=1 and pass -m live")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)
