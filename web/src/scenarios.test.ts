import { describe, expect, it } from "vitest";
import type { Artifact, OperatorDecision, RunView, ToolActivity } from "./api";
import {
  SCENARIOS,
  checkoutResult,
  clockPlan,
  decisionNarrative,
  expectedText,
  hotelNarrative,
  matches,
  sayNarrative,
  sayResult,
  unchangedNarrative,
} from "./scenarios";

function tool(over: Partial<ToolActivity>): ToolActivity {
  return {
    tool_call_id: null,
    tool_name: "request_late_checkout",
    status: "completed",
    proposed_at: null,
    finished_at: null,
    outcome: null,
    error_code: null,
    entity_type: "reservation",
    entity_id: "rsv_1042",
    summary: {},
    duration_ms: 3,
    ...over,
  };
}

function run(activity: ToolActivity[], over: Partial<RunView> = {}): RunView {
  return {
    run_id: "run_1",
    client_request_id: "c1",
    status: "completed",
    user_message: "Can I check out at 15:00?",
    final_message: "ok",
    error: null,
    outcome_detail: null,
    activity,
    artifacts: [],
    conversation_restarted: false,
    turn_forgotten: false,
    created_at: null,
    started_at: null,
    finished_at: null,
    model_id: null,
    latency_ms: null,
    usage: null,
    ...over,
  };
}

function receipt(kind: string): Artifact {
  return {
    kind,
    id: "apr_1",
    receipt_id: "rcp_1",
    tool_name: "request_late_checkout",
    committed_at: null,
    result: {},
    current_status: null,
  };
}

describe("checkoutResult", () => {
  it("reads the filed request's outcome from recorded tool activity, not the reply", () => {
    const result = checkoutResult(
      run([
        tool({
          outcome: "pending_approval",
          summary: {
            requested_checkout_local: "2026-09-22T15:00:00+03:00",
            outcome: "pending_approval",
            reason_codes: [],
          },
        }),
      ]),
    );
    expect(result).toEqual({ outcome: "pending", time: "15:00", reasons: [] });
  });

  it("lets a filed request outrank an earlier check, and ignores failed calls", () => {
    const result = sayResult(
      run([
        tool({ tool_name: "evaluate_late_checkout", summary: { decision: "approval_required" } }),
        tool({ status: "rejected", error_code: "PENDING_APPROVAL_CONFLICT" }),
        tool({
          summary: { outcome: "denied", reason_codes: ["NEXT_ARRIVAL_CONFLICT"] },
        }),
      ]),
    );
    expect(result.outcome).toBe("denied");
    expect(matches({ outcome: "denied", reason: "NEXT_ARRIVAL_CONFLICT" }, result)).toBe(true);
    expect(matches({ outcome: "denied", reason: "ROOM_OUT_OF_SERVICE" }, result)).toBe(false);
  });

  it("does not count a mere check as a filed review", () => {
    const result = sayResult(
      run([tool({ tool_name: "evaluate_late_checkout", summary: { decision: "approval_required" } })]),
    );
    expect(result.outcome).toBe("evaluated_review");
    expect(matches({ outcome: "pending" }, result)).toBe(false);
    expect(sayNarrative("Emma", result)).toContain("has not filed it yet");
  });

  it("reports an answer without any checkout tool", () => {
    expect(checkoutResult(run([])).outcome).toBe("none");
    expect(checkoutResult(null).outcome).toBe("none");
  });
});

describe("a line that must change nothing", () => {
  const unchanged = { outcome: "unchanged", why: "No tool can reach another guest." } as const;
  const read = tool({ tool_name: "get_my_reservation", entity_id: "rsv_1042" });

  it("is met by a finished turn that left no receipt, whatever it read", () => {
    expect(matches(unchanged, sayResult(run([read])))).toBe(true);
    expect(matches(unchanged, sayResult(run([])))).toBe(true);
    expect(unchangedNarrative("Emma", unchanged)).toBe(
      "Emma's message changed nothing in the hotel: the agent's turn left no receipt. " +
        "No tool can reach another guest.",
    );
    expect(expectedText(unchanged)).toBe("no change to the hotel");
  });

  it("is not met by a turn with a receipt, and says what it changed", () => {
    const result = sayResult(run([read], { artifacts: [receipt("maintenance_task")] }));
    expect(matches(unchanged, result)).toBe(false);
    expect(sayNarrative("Emma", result)).toBe(
      "Emma's message changed the hotel: a maintenance report was created.",
    );
  });

  it("is not met by a turn that failed or never ran: no receipt is not proof then", () => {
    expect(matches(unchanged, sayResult(run([], { status: "failed" })))).toBe(false);
    expect(matches(unchanged, sayResult(null))).toBe(false);
  });
});

describe("hotelNarrative", () => {
  it("names what the hotel's turn carried and that the agent could only read", () => {
    const hotel = run(
      [
        tool({ tool_name: "get_my_requests" }),
        tool({ tool_name: "get_my_reservation" }),
      ],
      {
        initiated_by: "hotel",
        hotel_updates: [
          { kind: "review_closed", title: "Late checkout until 15:30 declined", demo_time: "" },
        ],
      },
    );
    expect(hotelNarrative("Emma", hotel)).toBe(
      "Hotel update: Late checkout until 15:30 declined. The agent wrote to Emma first after " +
        "re-reading the facts (2 read tool steps). It had read tools only, so it could offer a " +
        "next step but not take it.",
    );
  });
});

describe("narratives", () => {
  it("say what a refusal means in the hotel's words", () => {
    expect(
      sayNarrative("Emma", { outcome: "denied", time: "15:00", reasons: ["NEXT_ARRIVAL_CONFLICT"] }),
    ).toBe(
      "Emma asked for 15:00. The checkout rules refused it: the room must be ready for its next arrival.",
    );
  });

  it("explain a stale approve with the simulated change that caused it", () => {
    const decision = {
      decision: "approve",
      outcome: "stale",
      applied: false,
      receipt_id: null,
      replayed: false,
      approval: { status_reason: "CHANGED:operational", requested_checkout_local: null },
    } as unknown as OperatorDecision;
    const text = decisionNarrative(decision, [
      {
        kind: "event",
        key: "arrival_305_early",
        title: "Room 305's next guest arrives early (15:30 instead of 18:00)",
        components: ["operational"],
        before: null,
        after: null,
        wall_time: null,
        demo_time: null,
      },
    ]);
    expect(text).toContain("You approved, but nothing was changed");
    expect(text).toContain("Since the guest asked: Room 305's next guest arrives early");
  });
});

describe("clockPlan", () => {
  it("reaches the target in the steps the server allows", () => {
    expect(clockPlan("10:00", "14:30")).toEqual([120, 120, 30]);
    expect(clockPlan("14:30", "14:30")).toEqual([]);
    expect(clockPlan("10:00", "10:45", [30])).toEqual([30, 30]);
  });
});

describe("SCENARIOS", () => {
  it("use only the closed world catalog and fictional demo guests", () => {
    const events = SCENARIOS.flatMap((s) => s.steps.flatMap((st) => (st.kind === "event" ? [st.event] : [])));
    expect(new Set(events)).toEqual(
      new Set(["arrival_305_early", "arrival_412_cancelled"]),
    );
    for (const s of SCENARIOS) {
      expect(["G-001", "G-002"]).toContain(s.guestId);
      expect(s.naive.length).toBeGreaterThan(40);
      expect(s.here.length).toBeGreaterThan(40);
    }
  });

  it("put every hotel step right after something only the hotel side does", () => {
    // A guest's own turn never makes the hotel speak: the gate runs only inside a
    // decision, a task move, a world event or a clock move.
    for (const s of SCENARIOS) {
      s.steps.forEach((step, i) => {
        if (step.kind !== "hotel") return;
        expect(["event", "clock", "decide"]).toContain(s.steps[i - 1]?.kind);
      });
    }
  });

  it("show the hotel speaking first and a manager rejecting", () => {
    const offer = SCENARIOS.find((s) => s.id === "offer")!;
    expect(offer.steps.map((s) => s.kind)).toEqual([
      "say", "event", "hotel", "say", "decide", "hotel",
    ]); // prettier-ignore
    const authority = SCENARIOS.find((s) => s.id === "authority")!;
    const decide = authority.steps.find((s) => s.kind === "decide");
    expect(decide).toMatchObject({ decision: "reject", expect: "rejected" });
  });
});
