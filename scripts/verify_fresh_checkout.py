"""Fresh-checkout verification.

Clones the current commit into a temporary directory, installs locked
dependencies, migrates and seeds an isolated empty database, starts the API and
the UI with the documented commands, probes them, and optionally runs the full
offline check suite there. Never touches the working checkout's database.

Usage: ``uv run python scripts/verify_fresh_checkout.py [--full]``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHELL = os.name == "nt"


def run(cmd: str, cwd: Path, env: dict[str, str], timeout: int = 900) -> float:
    started = time.monotonic()
    print(f"$ {cmd}", flush=True)
    proc = subprocess.run(  # noqa: S602 - fixed command strings
        cmd,
        cwd=cwd,
        env=env,
        shell=True,
        timeout=timeout,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    took = time.monotonic() - started
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        raise SystemExit(f"FAILED ({proc.returncode}) after {took:.1f}s: {cmd}")
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    print(f"  ok in {took:.1f}s  {tail[:160]}", flush=True)
    return took


def wait_http(url: str, timeout: float = 60) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - loopback only
                return r.read().decode("utf-8")
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    full = "--full" in sys.argv
    head = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,  # noqa: S607
    ).stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="hotel-fresh-"))
    work = tmp / "checkout"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "VIRTUAL_ENV"))}
    env["HOTEL_DB_PATH"] = str(tmp / "data" / "hotel.sqlite")
    env["CONVERSATIONS_DB_PATH"] = str(tmp / "data" / "conversations.sqlite")
    env["OPENAI_API_KEY"] = ""  # a fresh checkout has no secrets file
    results: dict[str, object] = {"commit": head, "full": full}
    procs: list[subprocess.Popen[bytes]] = []
    try:
        run(f'git clone --quiet --no-local "{REPO}" "{work}"', tmp, env)
        run(f"git checkout --quiet {head}", work, env)
        assert not (work / ".env").exists()
        run("uv sync --locked", work, env)
        run("uv run alembic upgrade head", work, env)
        run("uv run python -m hotel_operations.seed", work, env)
        run("uv run python -m hotel_operations.seed", work, env)  # idempotent
        run("npm --prefix web ci", work, env)
        run("npm --prefix web run build", work, env)
        api = subprocess.Popen(  # noqa: S602
            "uv run uvicorn hotel_operations.app:app --host 127.0.0.1 --port 8811 --workers 1",
            cwd=work,
            env=env,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(api)
        health = json.loads(wait_http("http://127.0.0.1:8811/api/health"))
        guests = json.loads(wait_http("http://127.0.0.1:8811/api/demo/guests"))["guests"]
        print(f"  api health={health['status']} guests={[g['guest_id'] for g in guests]}")
        ui_env = {**env, "HOTEL_API_ORIGIN": "http://127.0.0.1:8811"}
        ui = subprocess.Popen(  # noqa: S602
            "npx vite --host 127.0.0.1 --port 5190 --strictPort",
            cwd=work / "web",
            env=ui_env,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(ui)
        html = wait_http("http://127.0.0.1:5190/")
        proxied = json.loads(wait_http("http://127.0.0.1:5190/api/demo/guests"))
        print(f"  ui served={'<div id="root">' in html} proxied_guests={len(proxied['guests'])}")
        results.update(
            api_health=health["status"],
            guests=len(guests),
            ui_ok='<div id="root">' in html,
            proxied=len(proxied["guests"]),
        )
        if full:
            for cmd in (
                "uv run ruff check .",
                "uv run ruff format --check .",
                "uv run mypy src",
                'uv run pytest -q -m "not live"',
                "npm --prefix web run typecheck",
                "npm --prefix web run test -- --run",
                "npx --prefix web playwright install chromium",
                "npm --prefix web run test:e2e",
                "uv run python scripts/check_secrets.py",
            ):
                run(cmd, work, env, timeout=1800)
            results["full_suite"] = "passed"
        print("FRESH CHECKOUT VERIFIED", json.dumps(results))
        return 0
    finally:
        for p in procs:
            if SHELL:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)],  # noqa: S603,S607
                    capture_output=True,
                )
            else:
                p.kill()
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
