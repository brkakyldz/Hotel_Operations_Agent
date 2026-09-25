"""Streamed run progress over Server-Sent Events.

The stream carries hints only (provisional text, an activity counter, done); the
persisted run remains the authority, which these tests also check.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from hotel_operations.agent.run_events import RunChannel
from tests.conftest import Harness
from tests.support.models import RuleModel, Turn, call, say


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        out.append((event, data))
    return out


def _gated_model(release: threading.Event, reply: str) -> RuleModel:
    """Reads the reservation, then waits for the test before answering."""

    async def rule(turn: Turn) -> Any:
        if not turn.tool_outputs_since_user():
            return call("get_my_reservation")
        while not release.is_set():  # noqa: ASYNC110 - released from the test thread
            await asyncio.sleep(0.01)
        return say(reply)

    return RuleModel(rule)


def test_live_run_streams_snapshots_then_done(make_harness: Any) -> None:
    release = threading.Event()
    h: Harness = make_harness(_gated_model(release, "Your checkout is at noon."))
    token = h.session("G-001")["token"]
    run_id = h.chat(token, "When is my checkout?").json()["run_id"]
    body: dict[str, str] = {}

    def read() -> None:
        r = h.client.get(f"/api/runs/{run_id}/events", headers=h.auth(token))
        body["status"] = str(r.status_code)
        body["type"] = r.headers["content-type"]
        body["text"] = r.text

    reader = threading.Thread(target=read)
    reader.start()
    # The model is parked mid-run: the tool step has happened, the reply has not.
    for _ in range(200):
        if h.services.run_manager.events.get(run_id) is not None:
            channel = h.services.run_manager.events.get(run_id)
            if channel is not None and channel.activity >= 2:
                break
        threading.Event().wait(0.02)
    release.set()
    reader.join(timeout=10)
    assert body["status"] == "200"
    assert body["type"].startswith("text/event-stream")
    events = _events(body["text"])
    kinds = [e for e, _ in events]
    assert kinds[-1] == "done" and events[-1][1] == {"live": True}
    snapshots = [d for e, d in events if e == "snapshot"]
    assert any(s["activity"] >= 2 for s in snapshots)  # tool called + tool output
    assert snapshots[-1]["text"] == "Your checkout is at noon."
    # The persisted run is the authority and agrees.
    run = h.wait(token, run_id)
    assert run["final_message"] == "Your checkout is at noon."
    assert [a["tool_name"] for a in run["activity"]] == ["get_my_reservation"]
    assert h.services.run_manager.events.get(run_id) is None  # channel closed


def test_finished_run_gets_a_single_done_event(harness: Harness) -> None:
    token = harness.session("G-002")["token"]
    run = harness.ask(token, "my room?")
    r = harness.client.get(f"/api/runs/{run['run_id']}/events", headers=harness.auth(token))
    assert r.status_code == 200
    assert _events(r.text) == [("done", {"live": False})]


def test_events_are_scoped_to_the_owning_session(harness: Harness) -> None:
    emma = harness.session("G-001")["token"]
    daniel = harness.session("G-002")["token"]
    run = harness.ask(emma, "my room?")
    url = f"/api/runs/{run['run_id']}/events"
    other = harness.client.get(url, headers=harness.auth(daniel))
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "NOT_FOUND"
    assert harness.client.get(url).status_code == 401
    # A capability in the query string is never accepted.
    assert harness.client.get(f"{url}?token={emma}").status_code == 401


def test_channel_keeps_only_the_newest_responses_text() -> None:
    class Raw:
        type = "raw_response_event"

        def __init__(self, data_type: str, delta: str = "") -> None:
            self.data = type("D", (), {"type": data_type, "delta": delta})()

    class Item:
        type = "run_item_stream_event"

        def __init__(self, name: str) -> None:
            self.name = name

    channel = RunChannel("run_x")
    for event in (
        Raw("response.created"),
        Raw("response.output_text.delta", "Let me "),
        Raw("response.output_text.delta", "check."),
        Item("tool_called"),
        Item("tool_output"),
        Raw("response.created"),
        Raw("response.output_text.delta", "Noon."),
    ):
        channel.observe(event)
    assert channel.snapshot() == {"text": "Noon.", "activity": 2, "done": False}
    before = channel.version
    channel.finish()
    assert channel.version == before + 1 and channel.snapshot()["done"] is True
