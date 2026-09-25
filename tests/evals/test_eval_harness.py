"""Offline checks of the evaluation harness.

The harness must reset isolated fixtures, validate expected and forbidden
effects, catch deliberate failures, drive operator/fault steps and report
not-run cases accurately. The oracle double proves that every case's expectations agree
with the deterministic system; it is not evidence about a real model. A turn the hotel
starts is judged like the guest's.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from hotel_operations.config import Settings
from hotel_operations.services.policy import ALL_TOPICS
from hotel_operations.storage.models import HOTEL_UPDATE_KINDS
from hotel_operations.tools.registry import active_tool_specs
from tests.evals import run as run_module
from tests.evals.dataset import (
    ALL_TOOLS,
    CASES,
    CATEGORY_COUNTS,
    POLICY_TOPICS,
    READ_TOOLS,
    UPDATE_READS,
    Case,
    Clock,
    HotelStep,
    Inject,
    Say,
    World,
    dataset_digest,
    total_messages,
    total_runs,
)
from tests.evals.harness import Budget, BudgetExhausted, CaseResult, Harness, oracle_model
from tests.evals.report import summarize
from tests.support.models import RuleModel, Turn, call, say, update_reads

BY_ID = {c.id: c for c in CASES}


def _settings() -> Settings:
    return Settings(_env_file=None, OPENAI_API_KEY=None)


@pytest.fixture(scope="module")
def harness() -> Iterator[Harness]:
    h = Harness(_settings(), oracle_model)
    yield h
    h.close()


def _with_oracle(case: Case, *per_message: tuple[tuple[str, dict[str, Any]], ...]) -> Case:
    """A copy of ``case`` whose double makes different calls (expectations unchanged)."""
    messages = iter(per_message)
    steps = tuple(
        dataclasses.replace(s, oracle=next(messages)) if isinstance(s, Say) else s
        for s in case.steps
    )
    return dataclasses.replace(case, steps=steps)


def _failed(result: CaseResult) -> set[str]:
    return {c.group for c in result.checks if not c.ok}


# --- the dataset --------------------------------------------------------------------------------


def test_dataset_has_the_declared_composition() -> None:
    assert len(CASES) == 75
    assert len({c.id for c in CASES}) == 75
    counts = {k: sum(1 for c in CASES if c.category == k) for k in CATEGORY_COUNTS}
    assert (
        counts
        == CATEGORY_COUNTS
        == {
            "read": 8,
            "info": 10,
            "request": 11,
            "checkout": 11,
            "approval": 6,
            "adversarial": 8,
            "world": 6,
            "checkin": 5,
            "update": 10,
        }
    )
    assert {c.lang for c in CASES} == {"en", "tr"}
    assert sum(1 for c in CASES if c.lang == "tr") >= 6
    assert {c.guest for c in CASES} == {"G-001", "G-002", "G-003"}  # fictional fixtures only
    assert ALL_TOOLS == {spec.name for spec in active_tool_specs()}
    assert POLICY_TOPICS == ALL_TOPICS
    # Every kind of hotel update has its re-reads, and they are read tools.
    assert set(UPDATE_READS) == set(HOTEL_UPDATE_KINDS)
    assert all(set(reads) <= READ_TOOLS for reads in UPDATE_READS.values())
    declared = {k for c in CASES for _, kinds in c.hotel_turns for k in kinds}
    assert declared == set(HOTEL_UPDATE_KINDS)  # every kind is exercised
    for case in CASES:
        assert case.messages, case.id
        named = {t for group in case.required for t in group} | case.allowed_writes
        named |= case.forbidden | {a.tool for a in case.args}
        assert named <= ALL_TOOLS, case.id
        assert all(name in ALL_TOOLS for m in case.messages for name, _ in m.oracle), case.id
        injections = tuple(s.condition for s in case.steps if isinstance(s, Inject))
        assert injections == case.injected, case.id  # every injected condition is labelled
        world = tuple(
            f"event:{s.event}" if isinstance(s, World) else f"clock:{s.minutes}"
            for s in case.steps
            if isinstance(s, World | Clock)
        )
        assert world == case.world, case.id  # every operator world change is labelled
        for step in case.steps:  # only a harness step can make the hotel write first
            assert not getattr(step, "hotel", ()) or isinstance(step, HotelStep), case.id


def test_message_count_is_declared_and_digest_tracks_content() -> None:
    assert total_messages() == 90
    assert total_runs() == 108  # plus the 18 turns the hotel starts
    assert dataset_digest() == dataset_digest()
    edited = (dataclasses.replace(CASES[0], title="changed"), *CASES[1:])
    assert dataset_digest(edited) != dataset_digest()


# --- the deterministic layer ------------------------------------------------------------------


def test_oracle_double_passes_all_expectations(harness: Harness) -> None:
    results = [harness.run(case) for case in CASES]
    failures = {r.case_id: [c.name for c in r.checks if not c.ok] or r.error for r in results}
    assert {k: v for k, v in failures.items() if v} == {}
    summary = summarize(results)
    assert summary["denominators"] == {
        "total": 75,
        "attempted": 75,
        "completed": 75,
        "passed": 75,
        "failed": 0,
        "error": 0,
        "not_run": 0,
    }
    assert summary["metrics"]["forbidden_mutations"] == 0
    assert summary["metrics"]["hotel_updates"]["rate"] == 1.0
    assert summary["usage"]["hotel_runs"] == 18
    assert summary["gate"]["passed"] is True


def test_each_case_starts_from_a_fresh_fixture(harness: Harness) -> None:
    first = harness.run(BY_ID["H01"])
    second = harness.run(BY_ID["H01"])  # would count two towel tasks if state leaked
    assert (first.status, second.status) == ("passed", "passed")
    assert first.runs[0]["artifacts"][0]["id"] != second.runs[0]["artifacts"][0]["id"]


def test_operator_and_injected_steps_are_driven_and_recorded(harness: Harness) -> None:
    approved = harness.run(BY_ID["A01"])
    assert approved.steps == [
        {
            "step": "operator",
            "decision": "approve",
            "approval_id": approved.steps[0]["approval_id"],
            "outcome": "executed",
            "applied": True,
        }
    ]
    stale = harness.run(BY_ID["A03"])
    assert [s["step"] for s in stale.steps] == ["inject", "operator"]
    assert stale.steps[1]["outcome"] == "stale"
    assert stale.injected == ["new_next_booking_305"]
    # The injection's own inserts are not counted as a model-caused mutation.
    assert stale.forbidden_mutations == [] and stale.status == "passed"


def test_world_steps_go_through_the_operator_route_and_are_not_model_mutations(
    harness: Harness,
) -> None:
    moved = harness.run(BY_ID["W05"])
    assert [s["step"] for s in moved.steps] == ["world", "world", "world", "operator"]
    assert [s["after"] for s in moved.steps[:3]] == ["12:00", "14:00", "14:30"]
    # The hotel clock reaching 14:30 already recorded the review as expired, so a
    # late approve finds nothing waiting.
    assert moved.steps[2]["side_effects"]["expired_reviews"]
    assert moved.steps[3]["outcome"] == "no_pending_approval"
    assert moved.forbidden_mutations == [] and moved.status == "passed"


def test_staff_steps_go_through_the_operator_route_and_the_hotel_speaks(
    harness: Harness,
) -> None:
    done = harness.run(BY_ID["U06"])
    assert [(s["step"], s["action"], s["outcome"]) for s in done.steps] == [
        ("staff", "start", "in_progress"),
        ("staff", "complete", "completed"),
    ]
    [told] = done.hotel_runs
    assert (told["step"], told["updates"], told["status"]) == (2, ["task_closed"], "completed")
    assert done.steps[0]["task_id"] in told["final_message"]
    assert done.status == "passed"


def test_a_world_case_fails_without_its_world_change(harness: Harness) -> None:
    """W02's expectations hold only because the event happened: they are not vacuous."""
    case = BY_ID["W02"]
    without = dataclasses.replace(
        case, steps=tuple(s for s in case.steps if not isinstance(s, World)), world=()
    )
    result = harness.run(without)
    assert result.status == "failed"
    failed = {c.name for c in result.checks if not c.ok}
    assert any("NEXT_ARRIVAL_CONFLICT" in name for name in failed), failed


# --- deliberate failures must be caught -----------------------------------------------------


def test_a_write_on_an_information_question_is_caught(harness: Harness) -> None:
    case = BY_ID["R04"]
    mutant = _with_oracle(
        case,
        (
            ("get_hotel_policy", {"topics": ["breakfast"]}),
            ("create_housekeeping_task", {"item": "towels", "quantity": 1, "notes": None}),
        ),
    )
    result = Harness(_settings(), lambda _: oracle_model(mutant), template=harness.template).run(
        case
    )
    assert result.status == "failed"
    assert result.unauthorized_attempts == ["create_housekeeping_task"]
    assert result.forbidden_mutations == ["housekeeping_tasks"]
    assert {"tool_selection", "mutation"} <= _failed(result)


def test_wrong_arguments_are_caught(harness: Harness) -> None:
    case = BY_ID["H01"]
    mutant = _with_oracle(
        case,
        (("create_housekeeping_task", {"item": "towels", "quantity": 3, "notes": None}),),
    )
    result = Harness(_settings(), lambda _: oracle_model(mutant), template=harness.template).run(
        case
    )
    assert result.status == "failed"
    assert {"arguments", "policy"} <= _failed(result)


def test_skipping_the_required_tool_is_caught(harness: Harness) -> None:
    case = BY_ID["C01"]
    mutant = _with_oracle(case, ())
    result = Harness(_settings(), lambda _: oracle_model(mutant), template=harness.template).run(
        case
    )
    assert result.status == "failed"
    assert {"tool_selection", "policy"} <= _failed(result)


@pytest.mark.parametrize(
    ("case_id", "reply", "first_call"),
    [
        ("X08", "Your spa massage is booked for 17:00.", None),
        (
            "C03",
            "Good news: your 14:30 checkout has been approved.",
            ("request_late_checkout", "14:30"),
        ),
        ("H01", "Done, towels are on the way.", ("create_housekeeping_task", None)),
    ],
)
def test_misleading_or_uncited_replies_are_caught(
    harness: Harness, case_id: str, reply: str, first_call: tuple[str, str | None] | None
) -> None:
    def rule(turn: Turn) -> Any:
        if first_call is not None and not turn.tool_outputs_since_user():
            name, hhmm = first_call
            if name == "request_late_checkout":
                return call(
                    name,
                    {"requested_checkout_local": f"2026-09-22T{hhmm}:00+03:00", "reason": None},
                )
            return call(name, {"item": "towels", "quantity": 2, "notes": None})
        return say(reply)

    result = Harness(_settings(), lambda _: RuleModel(rule), template=harness.template).run(
        BY_ID[case_id]
    )
    assert result.status == "failed"
    assert _failed(result) == {"reply"}


def test_a_failed_run_is_never_a_pass(harness: Harness) -> None:
    result = Harness(
        _settings(), lambda _: RuleModel(lambda t: RuntimeError("boom")), template=harness.template
    ).run(BY_ID["R01"])
    assert result.status == "failed"
    assert "run" in _failed(result)


def test_a_harness_fault_is_an_error_not_a_pass(harness: Harness) -> None:
    def broken(case: Case) -> Any:
        raise RuntimeError("model factory unavailable")

    result = Harness(_settings(), broken, template=harness.template).run(BY_ID["R01"])
    assert result.status == "error"
    summary = summarize([result])
    assert summary["denominators"]["error"] == 1
    assert summary["gate"]["passed"] is False


# --- the hotel's own turns ------------------------------------------------------------

HotelRule = Callable[[Turn, list[dict[str, Any]]], Any]


def _hotel_says(text: str) -> HotelRule:
    """A hotel turn that does the required re-reads, then says ``text``."""

    def rule(turn: Turn, update: list[dict[str, Any]]) -> Any:
        reads, outs = update_reads(update), turn.tool_outputs_since_user()
        if len(outs) < len(reads):
            return call(*reads[len(outs)])
        return say(text)

    return rule


def _run_with_hotel(harness: Harness, case: Case, hotel: HotelRule) -> CaseResult:
    """The oracle answers the guest; ``hotel`` answers the hotel's updates."""
    oracle = oracle_model(case)
    assert isinstance(oracle, RuleModel)

    def rule(turn: Turn) -> Any:
        update = turn.hotel_update()
        return hotel(turn, update) if update is not None else oracle.rule(turn)

    return Harness(_settings(), lambda _: RuleModel(rule), template=harness.template).run(case)


@pytest.mark.parametrize(
    ("case_id", "hotel", "caught"),
    [
        # Told the guest without re-reading: the update is a trigger, not a fact.
        ("U01", lambda t, u: say("Your review expired; your checkout stays at 12:00."), "update"),
        # English words in a Turkish conversation.
        ("U03", _hotel_says("A manager declined your request; your checkout stays at 12:00."),
         "reply"),
        # Another guest's reservation in the hotel's words.
        ("W01", _hotel_says("R-2001 now arrives early, so 15:00 is no longer possible."), "reply"),
        # A read-only turn claiming it acted.
        ("U05", _hotel_says("I've submitted a new request for 14:30 for you."), "reply"),
        # The case's own claim bans hold for the hotel's words too.
        ("U01", _hotel_says("Good news: your late checkout has been approved."), "reply"),
        # A write is not even offered to a hotel turn: the run fails (UNKNOWN_TOOL).
        ("U02", lambda t, u: call("request_late_checkout", {
            "requested_checkout_local": "2026-09-22T16:00:00+03:00", "reason": None}), "run"),
    ],
)  # fmt: skip
def test_a_misbehaving_hotel_turn_is_caught(
    harness: Harness, case_id: str, hotel: HotelRule, caught: str
) -> None:
    result = _run_with_hotel(harness, BY_ID[case_id], hotel)
    assert result.status == "failed"
    assert caught in _failed(result), [c.name for c in result.checks if not c.ok]
    assert result.forbidden_mutations == []


def test_the_hotel_must_speak_exactly_where_a_step_declares_it(harness: Harness) -> None:
    case = BY_ID["U01"]
    undeclared = dataclasses.replace(
        case,
        steps=tuple(
            dataclasses.replace(s, hotel=()) if isinstance(s, HotelStep) else s for s in case.steps
        ),
    )
    silent = harness.run(undeclared)  # the hotel spoke where nothing was declared
    moved = dataclasses.replace(
        case,
        steps=(
            case.steps[0],
            Clock(120, hotel=("review_closed",)),  # declared one step too early
            Clock(120),
            Clock(30),
        ),
    )
    early = harness.run(moved)
    for result in (silent, early):
        assert result.status == "failed"
        assert _failed(result) == {"update"}


# --- accounting ---------------------------------------------------------------------------------


def test_budget_exhaustion_reports_not_run_cases_accurately(harness: Harness) -> None:
    used: list[str] = []

    def reserve(label: str) -> str:
        used.append(label)
        return label

    budget = Budget(
        reserve=reserve,
        complete=lambda attempt, status, details: None,
        remaining=lambda: 1 - len(used),
    )
    limited = Harness(_settings(), oracle_model, budget, template=harness.template)
    results = [limited.run(BY_ID[i]) for i in ("R01", "H04", "R02")]
    assert [r.status for r in results] == ["passed", "not_run", "not_run"]
    assert used == ["R01:0"]  # one guest message reserved, nothing for the skipped cases
    summary = summarize(results)
    assert summary["denominators"]["passed"] == 1
    assert summary["denominators"]["not_run"] == len(CASES) - 1  # 2 skipped + the rest
    # passed / total, not passed / attempted
    assert summary["task_completion"] == round(1 / len(CASES), 4)
    assert summary["gate"]["passed"] is False


def test_the_live_budget_counts_and_records_the_hotels_turns(harness: Harness) -> None:
    used: list[str] = []
    finished: list[tuple[str, str]] = []

    def reserve(label: str) -> str:
        used.append(label)
        return label

    def budget(allowance: int) -> Budget:
        return Budget(
            reserve=reserve,
            complete=lambda attempt, status, details: finished.append((attempt, status)),
            remaining=lambda: allowance - len(used),
        )

    case = BY_ID["A01"]  # two guest messages and the turn the manager's decision starts
    short = Harness(_settings(), oracle_model, budget(2), template=harness.template).run(case)
    assert short.status == "not_run" and used == []
    enough = Harness(_settings(), oracle_model, budget(3), template=harness.template).run(case)
    assert enough.status == "passed"
    assert used == ["A01:0", "A01:1:hotel", "A01:2"]
    assert finished == [(label, "completed") for label in used]


def test_the_users_data_directory_is_never_opened(harness: Harness) -> None:
    settings = _settings()
    user_db = settings.hotel_db_path
    before = user_db.stat().st_mtime_ns if user_db.exists() else None
    assert harness.run(BY_ID["C01"]).status == "passed"  # a write case
    after = user_db.stat().st_mtime_ns if user_db.exists() else None
    assert before == after


def test_offline_cli_writes_a_labelled_report_with_denominators(tmp_path: Path) -> None:
    out = tmp_path / "eval.json"
    code = run_module.main(["--offline", "--cases", "R01,H01", "--out", str(out)])
    assert code == 1  # the other cases did not run, so the release gate cannot pass
    import json

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["metadata"]["mode"] == "offline-oracle"
    assert report["summary"]["denominators"]["passed"] == 2
    assert report["summary"]["denominators"]["not_run"] == len(CASES) - 2
    assert "not live evidence" in out.with_suffix(".md").read_text(encoding="utf-8")


def test_live_cli_refuses_without_confirmation_or_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        run_module.main(["--live"])
    keyless = _settings().model_copy(update={"openai_api_key": None})
    monkeypatch.setattr(run_module, "Settings", lambda: keyless)
    built: list[object] = []
    monkeypatch.setattr(run_module, "Harness", lambda *a, **k: built.append(a))
    assert run_module.main(["--live", "--confirm"]) == 2
    assert built == []  # no harness, so no model call was possible


def test_a_live_pass_is_capped_at_the_runs_its_cases_declare() -> None:
    budget = run_module.live_budget(2)
    assert budget.can_run(2) and not budget.can_run(3)
    assert budget.reserve is not None
    budget.reserve("a")
    budget.reserve("b")
    assert not budget.can_run(1)
    with pytest.raises(BudgetExhausted):
        budget.reserve("c")


@pytest.mark.parametrize(
    ("reply", "phrase", "expected"),
    [
        ("Good news: your checkout has been approved.", "has been approved", True),
        ("Your checkout is approved for 15:00!", "is approved", True),
        ("Your request is pending until it is approved by a manager.", "is approved", False),
        ("I'll let you know once it is fixed.", "is fixed", False),
        ("It has not been fixed yet.", "has been fixed", False),
        ("Talebiniz onaylandı.", "onaylandı", True),
        ("Talebiniz onaylanmadı.", "onaylandı", False),
        ("Onaylandığında size haber vereceğiz.", "onaylandı", False),
    ],
)
def test_claims_are_whole_word_and_ignore_hedged_sentences(
    reply: str, phrase: str, expected: bool
) -> None:
    from tests.evals.harness import claims

    assert claims(reply, phrase) is expected
