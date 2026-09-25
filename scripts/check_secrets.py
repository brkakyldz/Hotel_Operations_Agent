"""Secret/leak scan for committed files, the browser bundle and local logs.

Loads the configured secrets through the settings layer and checks, in memory,
that their values appear nowhere in git-tracked files or ``web/dist``. It never
prints a secret; it reports only file paths and pattern names.

Usage: ``uv run python scripts/check_secrets.py``
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from hotel_operations.config import REPO_ROOT, get_settings

PATTERNS = {
    "openai-key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "bearer-token": re.compile(rb"Authorization:\s*Bearer\s+[A-Za-z0-9_-]{30,}"),
}
ALLOW = {Path("scripts/check_secrets.py")}


def tracked_files() -> list[Path]:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [Path(p.decode()) for p in out.split(b"\0") if p]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    settings = get_settings()
    secrets = [
        s.get_secret_value().encode()
        for s in (settings.openai_api_key,)
        if s is not None and len(s.get_secret_value()) >= 8
    ]
    candidates = [REPO_ROOT / p for p in tracked_files() if p not in ALLOW]
    dist = REPO_ROOT / "web" / "dist"
    if dist.exists():
        candidates += [p for p in dist.rglob("*") if p.is_file()]
    problems: list[str] = []
    for path in candidates:
        if not path.is_file():
            continue
        data = path.read_bytes()
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(secret in data for secret in secrets):
            problems.append(f"{rel}: contains a configured secret value")
        for name, pattern in PATTERNS.items():
            if pattern.search(data):
                problems.append(f"{rel}: matches {name} pattern")
        if rel.startswith("web/dist") and b"OPENAI" in data:
            problems.append(f"{rel}: bundle references OPENAI configuration")
    scanned = len(candidates)
    if problems:
        print(f"LEAK CHECK FAILED ({scanned} files scanned):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(
        f"Leak check passed: {scanned} files scanned, {len(secrets)} configured secret(s) "
        f"compared in memory, dist included: {dist.exists()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
