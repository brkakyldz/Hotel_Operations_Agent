"""Programmatic Alembic upgrade (used by tests, seed and reset)."""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from hotel_operations.config import REPO_ROOT


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"db={db_path}"])
    return cfg


def upgrade_to_head(db_path: Path) -> None:
    command.upgrade(alembic_config(db_path), "head")
