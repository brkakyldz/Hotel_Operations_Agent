"""In-process progress channel for one running run.

The channel is a hint, never a record. It holds the provisional reply text streamed so
far and a counter that moves whenever tool activity changes. Subscribers (the SSE route)
read snapshots of it; the persisted run (``GET /api/runs/{id}``) stays the only authority
for the final message, tool activity and receipts. It lives on the RunManager, so it is
per process; a subscriber that finds no channel reads the persisted run instead.

A snapshot, not an event queue: a slow or absent subscriber can never make the run wait
or buffer without bound.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Tool items the SDK announces while streaming; each one means the persisted activity
# (written by the gateway's audit trail) is worth re-reading.
TOOL_ITEM_EVENTS = frozenset({"tool_called", "tool_output"})


@dataclass
class RunChannel:
    run_id: str
    text: str = ""
    activity: int = 0
    done: bool = False
    version: int = 0
    _new_response: bool = False
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def snapshot(self) -> dict[str, Any]:
        return {"text": self.text, "activity": self.activity, "done": self.done}

    def _bump(self) -> None:
        self.version += 1
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def observe(self, event: Any) -> None:
        """Fold one SDK stream event into the snapshot."""
        kind = getattr(event, "type", None)
        if kind == "raw_response_event":
            data = getattr(event, "data", None)
            data_type = getattr(data, "type", None)
            if data_type == "response.created":
                # Show only the newest response's text: a later model call replaces it.
                self._new_response = True
            elif data_type == "response.output_text.delta":
                if self._new_response:
                    self.text = ""
                    self._new_response = False
                self.text += str(getattr(data, "delta", ""))
                self._bump()
        elif kind == "run_item_stream_event" and getattr(event, "name", None) in TOOL_ITEM_EVENTS:
            self.activity += 1
            self._bump()

    def finish(self) -> None:
        """Called after the run's terminal state is persisted."""
        self.done = True
        self._bump()

    async def wait_past(self, seen_version: int) -> None:
        """Return once the channel has moved past ``seen_version`` (bound it with a timeout)."""
        if self.version != seen_version:
            return
        await self._changed.wait()


class RunEventBroker:
    """The channels of runs executing in this process, keyed by globally unique run id."""

    def __init__(self) -> None:
        self._channels: dict[str, RunChannel] = {}

    def open(self, run_id: str) -> RunChannel:
        channel = RunChannel(run_id)
        self._channels[run_id] = channel
        return channel

    def get(self, run_id: str) -> RunChannel | None:
        return self._channels.get(run_id)

    def close(self, run_id: str) -> None:
        channel = self._channels.pop(run_id, None)
        if channel is not None and not channel.done:
            channel.finish()
