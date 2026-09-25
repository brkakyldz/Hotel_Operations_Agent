"""Evaluation report: denominators, metrics, release gate and a Markdown summary.

Every case counts in the denominator. A case that did not run is ``not_run``
and a harness fault is ``error``; neither is ever a pass. The live completion
rate is ``passed / total``. Mocked (oracle) runs are labelled as such and never
reported as live results.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict
from typing import Any

from tests.evals.dataset import CASES, CATEGORY_COUNTS
from tests.evals.harness import CaseResult

COMPLETION_GATE = 0.90
GROUPS = (
    "tool_selection", "arguments", "policy", "escalation", "mutation", "reply", "run", "update"
)  # fmt: skip


def _rate(values: list[bool]) -> dict[str, Any]:
    return {
        "passed": sum(values),
        "of": len(values),
        "rate": round(sum(values) / len(values), 4) if values else None,
    }


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    total = len(CASES)
    by_status = {
        s: [r for r in results if r.status == s] for s in ("passed", "failed", "error", "not_run")
    }
    missing = total - len(results)  # cases never handed to the harness count as not run
    executed = by_status["passed"] + by_status["failed"]
    groups: dict[str, Any] = {}
    for group in GROUPS:
        values = [v for r in executed if (v := r.group_ok(group)) is not None]
        groups[group] = _rate(values)
    injected = [r.status == "passed" for r in executed if r.injected]
    runs = [run for r in results for run in r.runs + r.hotel_runs]
    latencies = [run["latency_ms"] for run in runs if run["latency_ms"] is not None]
    tokens = [run["total_tokens"] for run in runs if run["total_tokens"] is not None]
    forbidden = sum(len(r.forbidden_mutations) for r in results)
    unauthorized = sum(len(r.unauthorized_attempts) for r in results)
    passed = len(by_status["passed"])
    completion = round(passed / total, 4)
    per_category = {
        category: _rate([r.status == "passed" for r in results if r.category == category])
        | {"declared": count}
        for category, count in CATEGORY_COUNTS.items()
    }
    per_language = {
        lang: _rate([r.status == "passed" for r in results if r.lang == lang])
        for lang in ("en", "tr")
    }
    gate = {
        "completion_rate": completion,
        "completion_threshold": COMPLETION_GATE,
        "completion_ok": completion >= COMPLETION_GATE,
        "forbidden_mutations": forbidden,
        "forbidden_mutations_ok": forbidden == 0,
        "all_cases_ran": not by_status["not_run"] and not by_status["error"] and missing == 0,
    }
    gate["passed"] = bool(
        gate["completion_ok"] and gate["forbidden_mutations_ok"] and gate["all_cases_ran"]
    )
    return {
        "denominators": {
            "total": total,
            "attempted": len(results) - len(by_status["not_run"]),
            "completed": len(executed),
            "passed": passed,
            "failed": len(by_status["failed"]),
            "error": len(by_status["error"]),
            "not_run": len(by_status["not_run"]) + missing,
        },
        "task_completion": completion,
        "metrics": {
            "tool_selection": groups["tool_selection"],
            "argument_accuracy": groups["arguments"],
            "policy_compliance": groups["policy"],
            "escalation_accuracy": groups["escalation"],
            "no_forbidden_mutation": groups["mutation"],
            "truthful_replies": groups["reply"],
            "runs_completed": groups["run"],
            "hotel_updates": groups["update"],
            "error_recovery_injected": _rate(injected),
            "forbidden_mutations": forbidden,
            "unauthorized_attempts": unauthorized,
        },
        "per_category": per_category,
        "per_language": per_language,
        "usage": {
            "runs": len(runs),
            "hotel_runs": sum(len(r.hotel_runs) for r in results),
            "latency_ms_mean": round(statistics.mean(latencies)) if latencies else None,
            "latency_ms_p50": round(statistics.median(latencies)) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
            "total_tokens": sum(tokens) if tokens else None,
            "tokens_known_for_runs": len(tokens),
        },
        "gate": gate,
        "failures": [
            {
                "case_id": r.case_id,
                "status": r.status,
                "error": r.error,
                "failed_checks": [f"{c.name} [{c.detail}]" for c in r.checks if not c.ok],
            }
            for r in results
            if r.status != "passed"
        ],
    }


def case_record(result: CaseResult) -> dict[str, Any]:
    return asdict(result)


def markdown(report: dict[str, Any]) -> str:
    meta, summary = report["metadata"], report["summary"]
    d, m, gate = summary["denominators"], summary["metrics"], summary["gate"]

    def pct(value: dict[str, Any]) -> str:
        if not value["of"]:
            return "n/a"
        return f"{value['passed']}/{value['of']} ({value['rate']:.0%})"

    lines = [
        f"# Evaluation {meta['mode']} — {meta['dataset_version']} ({meta['started_at'][:10]})",
        "",
        f"- Mode: **{meta['mode']}**"
        + (" (test double, not live evidence)" if meta["mode"] != "live" else ""),
        f"- Model: `{meta['model']}`, reasoning effort `{meta['reasoning_effort']}`",
        f"- Revision `{meta['revision']}`, dataset digest `{meta['dataset_digest']}`, "
        f"prompt `{meta['prompt_version']}` (`{meta['prompt_digest']}`), "
        f"tools `{meta['tool_catalog_digest']}`, fixture `{meta['fixture_version']}`",
        "",
        "| Denominator | Count |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in d.items()],
        "",
        "| Metric | Result |",
        "| --- | --- |",
        f"| Task completion (passed / total) | {d['passed']}/{d['total']} "
        f"({summary['task_completion']:.0%}) |",
        f"| Tool selection | {pct(m['tool_selection'])} |",
        f"| Argument accuracy | {pct(m['argument_accuracy'])} |",
        f"| Policy compliance (codes + state) | {pct(m['policy_compliance'])} |",
        f"| Escalation accuracy (approval state) | {pct(m['escalation_accuracy'])} |",
        f"| Truthful replies (no misleading claim, ids cited) | {pct(m['truthful_replies'])} |",
        f"| Hotel updates (turn as declared, facts re-read) | {pct(m['hotel_updates'])} |",
        f"| Error recovery (injected conditions) | {pct(m['error_recovery_injected'])} |",
        f"| Forbidden business mutations | {m['forbidden_mutations']} |",
        f"| Unauthorized / unexpected write attempts | {m['unauthorized_attempts']} |",
        f"| Latency mean / p50 / max (ms) | {summary['usage']['latency_ms_mean']} / "
        f"{summary['usage']['latency_ms_p50']} / {summary['usage']['latency_ms_max']} |",
        f"| Model runs (of which the hotel started) | {summary['usage']['runs']} "
        f"({summary['usage']['hotel_runs']}) |",
        f"| Total tokens ({summary['usage']['tokens_known_for_runs']} runs with usage) | "
        f"{summary['usage']['total_tokens']} |",
        "",
        f"**Release gate:** {'PASS' if gate['passed'] else 'FAIL'} — completion "
        f"{gate['completion_rate']:.0%} (threshold {gate['completion_threshold']:.0%}), "
        f"forbidden mutations {gate['forbidden_mutations']}, "
        f"all cases ran: {gate['all_cases_ran']}.",
        "",
        "| Category | Passed |",
        "| --- | --- |",
        *[f"| {k} | {pct(v)} |" for k, v in summary["per_category"].items()],
        "",
    ]
    if summary["failures"]:
        lines += ["## Failures (every one reviewed)", ""]
        for f in summary["failures"]:
            detail = "; ".join(f["failed_checks"]) or f["error"] or ""
            lines.append(f"- **{f['case_id']}** ({f['status']}): {detail}")
        lines.append("")
    return "\n".join(lines)
