"""Explicit, destructive demo reset: restore fixture v1 in both databases.

    uv run python -m hotel_operations.reset --confirm

Refuses without ``--confirm``, when the target is not a demo database, while any
run is queued/running, and while a running application instance holds the
database open. The databases are moved aside first and restored if rebuilding
fails, so a refused or failed reset leaves the previous demo intact. It only
touches the two paths it is given (defaults: the configured settings paths).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from hotel_operations.config import get_settings
from hotel_operations.fixtures import FIXTURE_VERSION, seed_fixture
from hotel_operations.storage.db import Database
from hotel_operations.storage.migrate import upgrade_to_head

SIDE_SUFFIXES = ("", "-wal", "-shm", "-journal")
BACKUP_SUFFIX = ".reset-backup"


class ResetRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResetResult:
    hotel_db: Path
    conversations_db: Path
    fixture_version: str


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0)


def _inspect_hotel_db(path: Path) -> None:
    """A demo database must look like one, and must have no queued/running run."""
    try:
        conn = _connect_ro(path)
    except sqlite3.Error as exc:
        raise ResetRefused("DATABASE_UNREADABLE", f"Cannot open {path}: {exc}") from exc
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        if not {"alembic_version", "hotels"} <= tables:
            raise ResetRefused(
                "NOT_A_DEMO_DATABASE", f"{path} is not a Hotel Operations demo database."
            )
        active = 0
        if "runs" in tables:
            active = conn.execute(
                "SELECT count(*) FROM runs WHERE status IN ('queued', 'running')"
            ).fetchone()[0]
        if active:
            raise ResetRefused(
                "ACTIVE_RUNS",
                f"{active} run(s) are queued or running. Wait for them to finish (or start and "
                "stop the app once so interrupted runs are recovered), then retry.",
            )
    except sqlite3.OperationalError as exc:
        raise ResetRefused("DATABASE_IN_USE", f"{path} is busy: {exc}") from exc
    finally:
        conn.close()


def _assert_not_held_open(path: Path) -> None:
    """An exclusive lock fails while any other connection (e.g. the running app) is open."""
    try:
        conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    except sqlite3.Error as exc:
        raise ResetRefused("DATABASE_IN_USE", f"Cannot open {path}: {exc}") from exc
    try:
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise ResetRefused(
            "DATABASE_IN_USE",
            f"{path} is held open by another process (is the app running?). Stop it and retry.",
        ) from exc
    finally:
        conn.close()


def _move_aside(paths: list[Path]) -> list[tuple[Path, Path]]:
    moved: list[tuple[Path, Path]] = []
    try:
        for base in paths:
            for suffix in SIDE_SUFFIXES:
                src = Path(f"{base}{suffix}")
                if src.exists():
                    dst = Path(f"{src}{BACKUP_SUFFIX}")
                    if dst.exists():
                        dst.unlink()
                    src.replace(dst)
                    moved.append((src, dst))
    except OSError as exc:
        _restore(moved)
        raise ResetRefused(
            "DATABASE_IN_USE", f"Could not move {exc.filename} aside; is the app running?"
        ) from exc
    return moved


def _restore(moved: list[tuple[Path, Path]]) -> None:
    for src, dst in reversed(moved):
        if src.exists():
            src.unlink()
        dst.replace(src)


def reset_demo(hotel_db: Path, conversations_db: Path, *, confirm: bool) -> ResetResult:
    if not confirm:
        raise ResetRefused(
            "CONFIRMATION_REQUIRED",
            "This deletes all demo sessions, runs, tasks and checkout changes. "
            "Re-run with --confirm to proceed.",
        )
    if hotel_db.resolve() == conversations_db.resolve():
        raise ResetRefused("INVALID_PATHS", "The two database paths must differ.")
    if hotel_db.exists():
        _inspect_hotel_db(hotel_db)
        _assert_not_held_open(hotel_db)
    if conversations_db.exists():
        _assert_not_held_open(conversations_db)
    moved = _move_aside([hotel_db, conversations_db])
    try:
        hotel_db.parent.mkdir(parents=True, exist_ok=True)
        upgrade_to_head(hotel_db)
        db = Database(hotel_db)
        try:
            with db.write() as session:
                seed_fixture(session)
        finally:
            db.dispose()
    except BaseException:
        for suffix in SIDE_SUFFIXES:
            Path(f"{hotel_db}{suffix}").unlink(missing_ok=True)
        _restore(moved)
        raise
    for _, dst in moved:
        dst.unlink(missing_ok=True)
    return ResetResult(hotel_db, conversations_db, FIXTURE_VERSION)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="python -m hotel_operations.reset",
        description="Restore the fictional demo to fixture v1 (destructive).",
    )
    parser.add_argument("--confirm", action="store_true", help="required: really reset")
    parser.add_argument("--hotel-db", type=Path, default=None)
    parser.add_argument("--conversations-db", type=Path, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    hotel_db = args.hotel_db or settings.hotel_db_path
    conversations_db = args.conversations_db or settings.conversations_db_path
    try:
        result = reset_demo(hotel_db, conversations_db, confirm=args.confirm)
    except ResetRefused as exc:
        print(f"Reset refused ({exc.code}): {exc.message}")
        return 2
    print(
        f"Demo reset to {result.fixture_version}: {result.hotel_db} rebuilt, "
        f"{result.conversations_db} cleared. All sessions were invalidated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
