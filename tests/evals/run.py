"""Run the behavior/state evaluation (75 cases; see tests/evals/dataset.py).

Offline (test double, no key, no network; validates expectations and harness)::

    uv run python -m tests.evals.run --offline

Live (the real configured model; one pass over the selected cases)::

    uv run python -m tests.evals.run --live --confirm

A live pass makes one model run per guest message and one per turn the hotel starts after
a harness step. It is capped at the number of runs the selected cases declare, and the
count is printed before anything is spent. Results go to ``.eval-runs/`` (gitignored),
written after every case.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hotel_operations.config import Settings
from tests.evals.dataset import (
    CASES,
    DATASET_VERSION,
    FIXTURE_VERSION,
    dataset_digest,
    total_messages,
    total_runs,
)
from tests.evals.harness import (
    Budget,
    BudgetExhausted,
    CaseResult,
    Harness,
    config_metadata,
    lock_digest,
    oracle_model,
)
from tests.evals.report import case_record, markdown, summarize

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = REPO_ROOT / ".eval-runs"


def live_budget(runs: int) -> Budget:
    """At most ``runs`` model runs: one pass over the selected cases, nothing more."""
    used: list[str] = []

    def reserve(label: str) -> str:
        if len(used) >= runs:
            raise BudgetExhausted(f"the pass declared {runs} runs; {label} would exceed it")
        used.append(label)
        return label

    return Budget(reserve=reserve, complete=None, remaining=lambda: runs - len(used))


def git_revision() -> str:
    try:
        head = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain", "--", "src", "tests"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("+dirty" if dirty else "")


def _write(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.with_suffix(".md").write_text(markdown(report) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="oracle test double (no key)")
    mode.add_argument("--live", action="store_true", help="real configured model")
    parser.add_argument("--confirm", action="store_true", help="required for --live")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--out", type=Path, help="output JSON path (Markdown written beside it)")
    args = parser.parse_args(argv)

    selected = CASES
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",")}
        selected = tuple(c for c in CASES if c.id in wanted)
        if {c.id for c in selected} != wanted:
            parser.error(f"unknown case ids: {sorted(wanted - {c.id for c in selected})}")
    messages, runs = total_messages(selected), total_runs(selected)

    if args.live:
        if not args.confirm:
            parser.error("--live spends real model calls; add --confirm")
        settings = Settings()
        if settings.openai_api_key is None:
            print("OPENAI_API_KEY is not configured: live evaluation blocked.", file=sys.stderr)
            return 2
        harness = Harness(settings, lambda case: None, live_budget(runs))
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = args.out or OUTPUT / f"eval-live-{stamp}.json"
        label_prefix = f"{out.stem}:"
        model_label, effort = settings.openai_model, settings.openai_reasoning_effort
        print(
            f"Live pass: {len(selected)} cases, {runs} model runs "
            f"({messages} guest messages and {runs - messages} hotel turns) on {model_label}."
        )
    else:
        settings = Settings(_env_file=None, OPENAI_API_KEY=None)
        harness = Harness(settings, oracle_model)
        out = args.out or OUTPUT / "eval-offline.json"
        label_prefix = ""
        model_label, effort = "oracle-double (test-only)", "n/a"

    metadata: dict[str, Any] = {
        "mode": "live" if args.live else "offline-oracle",
        "dataset_version": DATASET_VERSION,
        "dataset_digest": dataset_digest(),
        "fixture_version": FIXTURE_VERSION,
        "revision": git_revision(),
        "uv_lock_digest": lock_digest(REPO_ROOT),
        "model": model_label,
        "reasoning_effort": effort,
        **config_metadata(),
        "cases_selected": [c.id for c in selected],
        "declared_guest_messages": messages,
        "declared_model_runs": runs,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
    }
    results: list[CaseResult] = []
    try:
        for case in selected:
            results.append(harness.run(case, label_prefix))
            report = {
                "metadata": metadata,
                "summary": summarize(results),
                "cases": [case_record(r) for r in results],
            }
            _write(out, report)
    finally:
        harness.close()
    metadata["finished_at"] = datetime.now(UTC).isoformat()
    summary = summarize(results)
    report = {"metadata": metadata, "summary": summary, "cases": [case_record(r) for r in results]}
    _write(out, report)
    gate, d = summary["gate"], summary["denominators"]
    print(
        f"{metadata['mode']}: {d['passed']}/{d['total']} passed, {d['failed']} failed, "
        f"{d['error']} error, {d['not_run']} not run; forbidden mutations "
        f"{gate['forbidden_mutations']}; gate {'PASS' if gate['passed'] else 'FAIL'} -> {out}"
    )
    return 0 if gate["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
