"""Seed fixture v1 into an empty hotel database.

Usage: ``uv run python -m hotel_operations.seed``. Idempotent: an existing demo
is never reset (a destructive reset is a separate explicit command).
"""

from __future__ import annotations

import sys

from hotel_operations.config import get_settings
from hotel_operations.fixtures import FIXTURE_VERSION, seed_fixture
from hotel_operations.storage.db import Database


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    settings = get_settings()
    db = Database(settings.hotel_db_path, settings.sqlite_busy_timeout_ms)
    try:
        with db.write() as session:
            inserted = seed_fixture(session)
    finally:
        db.dispose()
    if inserted:
        print(f"Seeded {FIXTURE_VERSION} into {settings.hotel_db_path}")
    else:
        print(f"Database already contains data; left unchanged ({settings.hotel_db_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
