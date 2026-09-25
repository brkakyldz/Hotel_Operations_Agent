"""Report configuration presence without revealing any secret value.

Usage: ``uv run python -m hotel_operations.doctor``
"""

from __future__ import annotations

import sys

from hotel_operations.config import get_settings


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    settings = get_settings()
    key = settings.openai_api_key
    print(f"OPENAI_API_KEY: {'present' if key and key.get_secret_value() else 'absent'}")
    print(f"OPENAI_MODEL: {settings.openai_model}")
    print(f"OPENAI_REASONING_EFFORT: {settings.openai_reasoning_effort}")
    print(f"HOTEL_DB_PATH: {settings.hotel_db_path}")
    print(f"tracing_enabled: {settings.tracing_enabled}")
    print(f"tracing_sensitive_data: {settings.tracing_sensitive_data}")
    print(f"responses_store: {settings.responses_store}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
