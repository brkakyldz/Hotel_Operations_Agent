"""SQLite engine and transaction helpers for hotel.sqlite.

Every connection sets ``foreign_keys=ON``, the 500 ms busy timeout and WAL
journal mode. The driver's own transaction handling is disabled so that we emit
``BEGIN`` ourselves: plain reads use a deferred ``BEGIN``; check-then-write
transactions use ``BEGIN IMMEDIATE`` so the writer lock is taken before the
decision reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

IMMEDIATE_OPTION = "hotel_immediate"


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def create_hotel_engine(path: Path, busy_timeout_ms: int = 500) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        sqlite_url(path),
        connect_args={"timeout": busy_timeout_ms / 1000, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
        # Disable pysqlite's implicit deferred BEGIN; we emit BEGIN ourselves.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        if conn.get_execution_options().get(IMMEDIATE_OPTION):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.exec_driver_sql("BEGIN")

    return engine


class Database:
    """Owns the engine and hands out short read or immediate-write sessions."""

    def __init__(self, path: Path, busy_timeout_ms: int = 500) -> None:
        self.path = path
        self.engine = create_hotel_engine(path, busy_timeout_ms)
        self._read_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._write_factory = sessionmaker(
            bind=self.engine.execution_options(**{IMMEDIATE_OPTION: True}),
            expire_on_commit=False,
        )

    @contextmanager
    def read(self) -> Iterator[Session]:
        """A short deferred transaction for reads; never commits business changes."""
        session = self._read_factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    @contextmanager
    def write(self) -> Iterator[Session]:
        """A ``BEGIN IMMEDIATE`` transaction: writer lock first, then decision reads.

        Commits on normal exit, rolls back on any exception.
        """
        session = self._write_factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
