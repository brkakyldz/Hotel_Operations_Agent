"""Local structured telemetry.

One JSON object per line on the ``hotel_operations.telemetry`` logger, optionally
mirrored to a local JSONL file. Only scalar fields (and lists of scalars) are
accepted, so prompts, messages, tool arguments, provider payloads, tokens and
hidden reasoning cannot be serialized by accident. Usage that is unknown stays
``null``; it is never recorded as zero. No remote tracing or paid platform.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hotel_operations.clock import iso, utcnow

log = logging.getLogger("hotel_operations.telemetry")
_SCALARS = (str, int, float, bool, type(None))
_MAX_STRING = 120
_lock = threading.Lock()


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, _SCALARS):
        return value
    if isinstance(value, list | tuple) and all(isinstance(v, _SCALARS) for v in value):
        return [_clean(v) for v in value][:20]
    return "<dropped>"  # never serialize an arbitrary object or mapping


def emit(event: str, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {"event": event, "at": iso(utcnow())}
    record.update({key: _clean(value) for key, value in fields.items()})
    log.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
    return record


class _JsonLinesHandler(logging.Handler):
    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.INFO)
        self.path = path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = record.getMessage()
            with _lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:  # pragma: no cover - telemetry must never break a request
            self.handleError(record)


def attach_file_sink(path: Path | None) -> Callable[[], None]:
    """Mirror telemetry to ``path`` (JSONL). Returns a detach function."""
    if path is None:
        return lambda: None
    handler = _JsonLinesHandler(path)
    log.addHandler(handler)
    if log.level == logging.NOTSET or log.level > logging.INFO:
        log.setLevel(logging.INFO)

    def detach() -> None:
        log.removeHandler(handler)

    return detach
