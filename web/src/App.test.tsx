import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { policyTopicsRead } from "./App";
import type { ToolActivity } from "./api";
import type { Turn } from "./useDemoSession";

// A small in-memory fake of the backend HTTP contract (test double only).
type Handler = (url: string, init: RequestInit) => { status: number; body: unknown };

const GUESTS = [
  { guest_id: "G-001", display_name: "Emma Wilson", room_number: "305", reservation_reference: "R-1042", display_status: "Checked in" },
  { guest_id: "G-002", display_name: "Daniel Kim", room_number: "412", reservation_reference: "R-1088", display_status: "Checked in" },
  { guest_id: "G-003", display_name: "Sofia Rossi", room_number: "218", reservation_reference: "R-1101", display_status: "Arriving today" },
];

function sessionInfo(guestId: string, runIds: string[] = [], turns = 0) {
  const g = GUESTS.find((x) => x.guest_id === guestId)!;
  return {
    session_id: `ses_${guestId}`,
    expires_at: "2099-01-01T00:00:00+00:00",
    accepted_turn_count: turns,
    turn_limit: 10,
    context_restarted_at: null,
    context: {
      guest_id: g.guest_id,
      guest_name: g.display_name,
      room_number: g.room_number,
      reservation_reference: g.reservation_reference,
      display_status: g.display_status,
      hotel_name: "Demo Hotel (fictional)",
      timezone: "Europe/Istanbul",
      demo_time: "2026-09-22T10:00:00+03:00",
    },
    demo_time: "2026-09-22T10:00:00+03:00",
    timezone: "Europe/Istanbul",
    simulation_label: "Simulation",
    run_ids: runIds,
  };
}

function run(id: string, status: string, message: string, extra: Record<string, unknown> = {}) {
  return {
    run_id: id,
    client_request_id: `crid-${id}`,
    status,
    user_message: message,
    final_message: null,
    error: null,
    outcome_detail: null,
    activity: [],
    artifacts: [],
    conversation_restarted: false,
    turn_forgotten: false,
    created_at: null,
    started_at: null,
    finished_at: null,
    model_id: null,
    latency_ms: null,
    usage: null,
    ...extra,
  };
}

let handler: Handler;
const calls: { url: string; method: string; body: unknown }[] = [];

beforeEach(() => {
  calls.length = 0;
  window.localStorage.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({
        url,
        method: init.method ?? "GET",
        body: init.body ? JSON.parse(String(init.body)) : null,
      });
      const { status, body } = handler(url, init);
      if (body && typeof body === "object" && "__sse" in body) {
        const sse = (body as { __sse: string }).__sse;
        return new Response(sse, { status, headers: { "Content-Type": "text/event-stream" } });
      }
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function reservationBody(checkout: string) {
  return {
    reservation: {
      reservation_reference: "R-1042",
      room_number: "305",
      status: "checked_in",
      display_status: "Checked in",
      scheduled_check_in: "2026-09-20T15:00:00+03:00",
      scheduled_checkout: checkout,
      reservation_version: 1,
    },
  };
}

function arrivalBody(checkIn: string) {
  return {
    reservation: {
      reservation_reference: "R-1101",
      room_number: "218",
      status: "confirmed",
      display_status: "Arriving today",
      scheduled_check_in: checkIn,
      scheduled_checkout: "2026-09-24T12:00:00+03:00",
      reservation_version: 1,
    },
  };
}

function backend(
  opts: {
    runStatuses?: string[];
    chatStatus?: number;
    chatCode?: string;
    requests?: unknown[];
    artifacts?: unknown[];
    /** Successive scheduled_checkout values returned by /api/me/reservation. */
    checkouts?: string[];
    /** Successive scheduled_check_in values for Sofia (arriving) from /api/me/reservation. */
    checkIns?: string[];
    /** Older requests served for `?cursor=older` when the first page says there are more. */
    olderRequests?: unknown[];
    /** The manager's queue (GET /api/operator/approvals). */
    approvals?: unknown[];
  } = {},
) {
  // Each token is one session bound to one guest, with its own runs (like the backend).
  const sessions: Record<string, { guest: string; runIds: string[] }> = {};
  let created = 0;
  const runs: Record<string, number> = {};
  let reservationReads = 0;
  const statuses = opts.runStatuses ?? ["running", "completed"];
  return (url: string, init: RequestInit) => {
    const method = init.method ?? "GET";
    const auth = (init.headers as Record<string, string> | undefined)?.Authorization ?? "";
    const own = sessions[auth.replace("Bearer ", "")];
    if (url === "/api/demo/guests") return { status: 200, body: { guests: GUESTS } };
    if (url === "/api/demo/sessions" && method === "POST") {
      const guest = JSON.parse(String(init.body)).guest_id as string;
      created += 1;
      const token = `tok-${guest}-${created}`;
      sessions[token] = { guest, runIds: [] };
      return { status: 201, body: { ...sessionInfo(guest), token } };
    }
    if (url === "/api/session") {
      if (!own) {
        return {
          status: 401,
          body: { error: { code: "INVALID_SESSION", message: "Session invalid.", retryable: false } },
        };
      }
      return { status: 200, body: sessionInfo(own.guest, own.runIds, own.runIds.length) };
    }
    if (url === "/api/me/requests?cursor=older") {
      return { status: 200, body: { requests: opts.olderRequests ?? [], next_cursor: null } };
    }
    if (url === "/api/me/requests") {
      return {
        status: 200,
        body: { requests: opts.requests ?? [], next_cursor: opts.olderRequests ? "older" : null },
      };
    }
    if (url === "/api/me/reservation" && own?.guest === "G-003") {
      const seq = opts.checkIns ?? ["2026-09-22T15:00:00+03:00"];
      return { status: 200, body: arrivalBody(seq[Math.min(reservationReads++, seq.length - 1)]) };
    }
    if (url === "/api/me/reservation") {
      const seq = opts.checkouts ?? ["2026-09-22T12:00:00+03:00"];
      const checkout = seq[Math.min(reservationReads++, seq.length - 1)];
      return { status: 200, body: reservationBody(checkout) };
    }
    if (url === "/api/chat") {
      if (opts.chatStatus) {
        return {
          status: opts.chatStatus,
          body: {
            error: {
              code: opts.chatCode ?? "RUN_IN_PROGRESS",
              message: "Another request is running.",
              retryable: false,
            },
          },
        };
      }
      const runId = `run_${own?.guest ?? "x"}_${(own?.runIds.length ?? 0) + 1}`;
      own?.runIds.push(runId);
      return { status: 202, body: { run_id: runId, status: "queued", poll: `/api/runs/${runId}`, replayed: false } };
    }
    if (url.startsWith("/api/runs/")) {
      const id = url.split("/").pop()!;
      runs[id] = (runs[id] ?? -1) + 1;
      const status = statuses[Math.min(runs[id], statuses.length - 1)];
      const done = status === "completed";
      return {
        status: 200,
        body: run(id, status, "When is my checkout?", done
          ? {
              final_message: "Your checkout is 2026-09-22 12:00.",
              artifacts: opts.artifacts ?? [],
              activity: [
                {
                  tool_call_id: "c1", tool_name: "get_my_reservation", status: "completed",
                  proposed_at: "2026-09-22T18:00:00+00:00", finished_at: "2026-09-22T18:00:01+00:00",
                  outcome: "ok", error_code: null, entity_type: "reservation", entity_id: "rsv_1042",
                  summary: { reservation_reference: "R-1042" }, duration_ms: 4,
                },
              ],
            }
          : {}),
      };
    }
    if (url === "/api/operator/world") return { status: 200, body: WORLD };
    if (url.startsWith("/api/operator/approvals")) {
      return { status: 200, body: { approvals: opts.approvals ?? [], next_cursor: null } };
    }
    if (url.startsWith("/api/operator/tasks")) return { status: 200, body: { tasks: [], truncated: false } };
    return { status: 404, body: { error: { code: "NOT_FOUND", message: "nf", retryable: false } } };
  };
}

const WORLD = {
  source: "Simulated world event (fictional)",
  demo_time: "2026-09-22T10:00:00+03:00",
  timezone: "Europe/Istanbul",
  clock_limit: "2026-09-22T18:00:00+03:00",
  clock_steps: [30, 60, 120],
  events: [],
};

function review(status: string, version = 1) {
  return {
    approval_id: "apr_1", status, version, guest_name: "Emma Wilson", room_number: "305",
    reservation_reference: "R-1042", requested_checkout_local: "2026-09-22T15:00:00+03:00",
    current_checkout_local: "2026-09-22T12:00:00+03:00", guest_reason: null,
    created_at: "2026-09-22T07:00:00+00:00", expires_at: "2026-09-22T15:00:00+03:00", fresh: true,
    human_decision: status === "pending" ? null : "approve", decided_by: null, decided_at: null,
    status_reason: null,
  };
}

describe("chat-first demo", () => {
  it("selects a guest, sends a message, shows states, reply and actual tool activity", async () => {
    handler = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    const context = await screen.findByTestId("guest-context");
    expect(within(context).getByText("Emma Wilson")).toBeInTheDocument();
    expect(screen.getByTestId("demo-time")).toHaveTextContent("2026-09-22 10:00");
    expect(screen.getByTestId("simulation-label")).toHaveTextContent(/fictional/i);

    const box = screen.getByLabelText("Message");
    await waitFor(() => expect(box).toHaveFocus());
    await user.type(box, "When is my checkout?{Enter}");
    expect(await screen.findByText("When is my checkout?")).toBeInTheDocument();
    expect(await screen.findByTestId("agent-reply", {}, { timeout: 4000 })).toHaveTextContent(
      "Your checkout is 2026-09-22 12:00.",
    );
    // The tool steps are attached to the reply they produced, not a separate list.
    const items = within(screen.getByTestId("turn")).getAllByTestId("activity-item");
    expect(items).toHaveLength(1);
    expect(screen.getAllByTestId("activity-item")).toHaveLength(1);
    expect(screen.getByTestId("steps")).toHaveTextContent("1 tool step");
    expect(items[0]).toHaveTextContent("get_my_reservation");
    expect(items[0]).toHaveTextContent("completed");
    const chat = calls.find((c) => c.url === "/api/chat")!;
    expect(chat.body).toMatchObject({ message: "When is my checkout?" });
    expect(Object.keys(chat.body as object).sort()).toEqual(["client_request_id", "message"]);
  });

  it("switching guests shows each guest's own conversation and restores it on return", async () => {
    handler = backend({ runStatuses: ["completed"] });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "hi{Enter}");
    await screen.findByTestId("agent-reply");
    await user.click(screen.getByTestId("guest-G-002"));
    await waitFor(() => expect(screen.queryByTestId("turn")).not.toBeInTheDocument());
    expect(screen.queryByTestId("activity-item")).not.toBeInTheDocument();
    const context = await screen.findByTestId("guest-context");
    expect(within(context).getByText("Daniel Kim")).toBeInTheDocument();

    // Back to Emma: her session is restored from the server, not replaced.
    await user.click(screen.getByTestId("guest-G-001"));
    expect(await screen.findByTestId("agent-reply")).toHaveTextContent("Your checkout is");
    const back = await screen.findByTestId("guest-context");
    expect(within(back).getByText("Emma Wilson")).toBeInTheDocument();
    const sessions = calls.filter((c) => c.url === "/api/demo/sessions");
    expect(sessions.map((c) => (c.body as { guest_id: string }).guest_id)).toEqual(["G-001", "G-002"]);
    expect(calls.filter((c) => c.url === "/api/chat")).toHaveLength(1);
  });

  it("clicking the guest already on screen does nothing", async () => {
    handler = backend({ runStatuses: ["completed"] });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "hi{Enter}");
    await screen.findByTestId("agent-reply");
    const before = calls.length;
    await user.click(screen.getByTestId("guest-G-001"));
    await act(async () => {});
    expect(screen.getByTestId("agent-reply")).toBeInTheDocument();
    expect(calls.slice(before).some((c) => c.url === "/api/demo/sessions")).toBe(false);
  });

  it("New conversation starts a fresh session for the same guest on request", async () => {
    handler = backend({ runStatuses: ["completed"] });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "hi{Enter}");
    await screen.findByTestId("agent-reply");
    await user.click(screen.getByTestId("new-conversation"));
    await waitFor(() => expect(screen.queryByTestId("turn")).not.toBeInTheDocument());
    const sessions = calls.filter((c) => c.url === "/api/demo/sessions");
    expect(sessions.map((c) => (c.body as { guest_id: string }).guest_id)).toEqual(["G-001", "G-001"]);
    const stored = JSON.parse(window.localStorage.getItem("hotel-demo-sessions")!);
    expect(stored.tokens["G-001"]).toBe("tok-G-001-2");
  });

  it("replaces a guest's expired stored session with a new one on selection", async () => {
    window.localStorage.setItem(
      "hotel-demo-sessions",
      JSON.stringify({ active: null, tokens: { "G-002": "tok-gone" } }),
    );
    handler = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-002"));
    const context = await screen.findByTestId("guest-context");
    expect(within(context).getByText("Daniel Kim")).toBeInTheDocument();
    expect(calls.filter((c) => c.url === "/api/demo/sessions")).toHaveLength(1);
    expect(screen.queryByTestId("session-notice")).toBeNull();
  });

  it("offers Retry instead of giving up silently when the server stays unreachable", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    window.localStorage.setItem(
      "hotel-demo-sessions",
      JSON.stringify({ active: "G-002", tokens: { "G-002": "tok-G-002" } }),
    );
    let down = true;
    handler = (url) => {
      if (url === "/api/demo/guests") return { status: 200, body: { guests: GUESTS } };
      if (down) {
        return { status: 503, body: { error: { code: "UNAVAILABLE", message: "down", retryable: true } } };
      }
      if (url === "/api/session") return { status: 200, body: sessionInfo("G-002", [], 0) };
      if (url === "/api/me/requests") return { status: 200, body: { requests: [], next_cursor: null } };
      if (url === "/api/me/reservation") {
        return { status: 200, body: reservationBody("2026-09-22T12:00:00+03:00") };
      }
      return { status: 404, body: {} };
    };
    render(<App />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_000);
    });
    const problem = await screen.findByTestId("restore-problem");
    expect(problem).toHaveTextContent(/kept in this tab/);
    expect(window.localStorage.getItem("hotel-demo-sessions")).toContain("tok-G-002");
    down = false;
    await act(async () => {
      within(problem).getByRole("button", { name: /Retry/ }).click();
    });
    const context = await screen.findByTestId("guest-context");
    expect(within(context).getByText("Daniel Kim")).toBeInTheDocument();
    expect(screen.queryByTestId("restore-problem")).toBeNull();
    expect(calls.some((c) => c.url === "/api/demo/sessions")).toBe(false);
  });

  it("loads older requests page by page", async () => {
    const item = (id: string, what: string) => ({
      kind: "housekeeping", id, status: "pending", status_meaning: "", created_at: null,
      room_number: "305", receipt_id: null, details: { item: what, quantity: 1, notes: null },
    });
    handler = backend({ requests: [item("hk_new", "towels")], olderRequests: [item("hk_old", "pillows")] });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    expect(await screen.findAllByTestId("request-item")).toHaveLength(1);
    await user.click(screen.getByTestId("show-older"));
    await waitFor(() => expect(screen.getAllByTestId("request-item")).toHaveLength(2));
    expect(screen.getAllByTestId("request-item")[1]).toHaveTextContent("pillows");
    expect(screen.queryByTestId("show-older")).toBeNull();
    // A later re-read of the first page does not offer the pages already shown again.
    await user.click(screen.getByTestId("tab-requests"));
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/me/requests").length).toBeGreaterThan(1));
    expect(screen.queryByTestId("show-older")).toBeNull();
  });

  it("approving in the Manager tab changes the guest's checkout on the same screen", async () => {
    const request = (status: string) => ({
      kind: "late_checkout_review", id: "apr_1", status, status_meaning: "", created_at: null,
      room_number: "305", receipt_id: "rcp_9",
      details: { requested_checkout_local: "2026-09-22T15:00:00+03:00", expires_at: null, human_decision: null },
    });
    let decided = false;
    const base = backend();
    handler = (url, init) => {
      if (url === "/api/me/requests") {
        return { status: 200, body: { requests: [request(decided ? "executed" : "pending")], next_cursor: null } };
      }
      if (url === "/api/me/reservation") {
        return { status: 200, body: reservationBody(decided ? "2026-09-22T15:00:00+03:00" : "2026-09-22T12:00:00+03:00") };
      }
      if (url === "/api/operator/approvals/apr_1/decision") {
        decided = true;
        return {
          status: 200,
          body: { approval: review("executed", 2), decision: "approve", outcome: "executed", applied: true, receipt_id: "rcp_9", replayed: false },
        };
      }
      if (url.startsWith("/api/operator/approvals")) {
        return { status: 200, body: { approvals: [review(decided ? "executed" : "pending", decided ? 2 : 1)], next_cursor: null } };
      }
      return base(url, init);
    };
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await screen.findByText(/Pending manager review/);
    expect(await screen.findByTestId("manager-count")).toHaveTextContent("1");
    await user.click(screen.getByTestId("tab-manager"));
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByTestId("operator-result")).toHaveTextContent("Emma's checkout is now 15:00");
    // The guest card beside the desk shows the effect without leaving the screen.
    await waitFor(() => expect(screen.getByTestId("checkout-time")).toHaveTextContent("2026-09-22 15:00"));
    expect(screen.queryByTestId("manager-count")).toBeNull();
    await user.click(screen.getByTestId("tab-requests"));
    expect(await screen.findByText(/Approved by a manager/)).toBeInTheDocument();
  });

  it("disables guest switching while a request is running", async () => {
    handler = backend({ runStatuses: ["running"] });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "slow{Enter}");
    await waitFor(() => expect(screen.getByTestId("guest-G-002")).toBeDisabled());
    expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
  });

  it("shows Sofia as arriving, with her check-in time and arrival examples", async () => {
    handler = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-003"));
    const context = await screen.findByTestId("guest-context");
    expect(within(context).getByText("Arriving today")).toBeInTheDocument();
    expect(await screen.findByTestId("check-in-time")).toHaveTextContent("2026-09-22 15:00 (hotel time)");
    // No in-room service examples that would only be refused for an arriving guest.
    const examples = screen.getByRole("group", { name: "Example requests" });
    expect(examples).toHaveTextContent("How early could I check in today?");
    expect(examples).not.toHaveTextContent("towels");
    await user.click(screen.getByTestId("guest-G-001"));
    await waitFor(() => expect(screen.getByRole("group", { name: "Example requests" })).toHaveTextContent("towels"));
    expect(screen.queryByTestId("check-in-time")).toBeNull();
  });

  it("shows an applied early check-in as a receipt and refreshes the check-in time", async () => {
    handler = backend({
      runStatuses: ["completed"],
      checkIns: ["2026-09-22T15:00:00+03:00", "2026-09-22T13:00:00+03:00"],
      artifacts: [
        {
          kind: "check_in_change",
          id: "rsv_1101",
          receipt_id: "rcp_9",
          tool_name: "request_early_check_in",
          committed_at: null,
          result: {
            outcome: "applied",
            previous_check_in_local: "2026-09-22T15:00:00+03:00",
            current_check_in_local: "2026-09-22T13:00:00+03:00",
          },
          current_status: null,
        },
      ],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-003"));
    await user.type(screen.getByLabelText("Message"), "Can I check in at 13:00?{Enter}");
    const receipt = await screen.findByTestId("receipt");
    expect(receipt).toHaveTextContent("Check-in moved earlier");
    expect(receipt).toHaveTextContent("2026-09-22 15:00 → 2026-09-22 13:00");
    expect(screen.getByTestId("receipt-status")).toHaveTextContent("arrival at the desk still to come");
    await waitFor(() =>
      expect(screen.getByTestId("check-in-time")).toHaveTextContent("2026-09-22 13:00 (hotel time)"),
    );
  });

  it("renders an admission conflict as not accepted", async () => {
    handler = backend({ chatStatus: 409, chatCode: "SESSION_HISTORY_LIMIT" });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "again{Enter}");
    expect(await screen.findByTestId("turn-error")).toHaveTextContent("SESSION_HISTORY_LIMIT");
    expect(screen.getByTestId("turn-status")).toHaveTextContent("Not accepted");
  });

  it("keeps the guest's words when the hotel spoke first", async () => {
    handler = backend({ chatStatus: 409 }); // RUN_IN_PROGRESS: a hotel update's turn runs
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "again{Enter}");
    await waitFor(() => expect(screen.getByLabelText("Message")).toHaveValue("again"));
    expect(screen.queryByTestId("turn")).toBeNull();
    expect(screen.queryByTestId("turn-error")).toBeNull();
  });

  it("recovers a conversation the server no longer knows by asking to pick the guest again", async () => {
    const base = backend();
    handler = (url, init) =>
      url.startsWith("/api/runs/")
        ? { status: 401, body: { error: { code: "INVALID_SESSION", message: "invalid", retryable: false } } }
        : base(url, init);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "hi{Enter}");
    expect(await screen.findByTestId("session-notice")).toHaveTextContent(/no longer open/i);
    expect(screen.getByLabelText("Message")).toBeDisabled();
  });

  it("restores the transcript after a reload without re-submitting", async () => {
    window.localStorage.setItem(
      "hotel-demo-sessions",
      JSON.stringify({ active: "G-002", tokens: { "G-002": "tok-G-002" } }),
    );
    handler = (url) => {
      if (url === "/api/demo/guests") return { status: 200, body: { guests: GUESTS } };
      if (url === "/api/session") return { status: 200, body: sessionInfo("G-002", ["run_9"], 1) };
      if (url === "/api/me/requests") return { status: 200, body: { requests: [], next_cursor: null } };
      if (url === "/api/me/reservation") return { status: 200, body: reservationBody("2026-09-22T12:00:00+03:00") };
      if (url === "/api/runs/run_9")
        return { status: 200, body: run("run_9", "completed", "Old question", { final_message: "Old answer" }) };
      return { status: 404, body: {} };
    };
    render(<App />);
    expect(await screen.findByText("Old answer")).toBeInTheDocument();
    expect(screen.getByText("Old question")).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/chat")).toBe(false);
  });

  it("keeps the stored session through a transient 5xx while restoring", async () => {
    window.localStorage.setItem(
      "hotel-demo-sessions",
      JSON.stringify({ active: "G-002", tokens: { "G-002": "tok-G-002" } }),
    );
    let sessionCalls = 0;
    handler = (url) => {
      if (url === "/api/demo/guests") return { status: 200, body: { guests: GUESTS } };
      if (url === "/api/session") {
        sessionCalls += 1; // the backend is restarting on the first attempt
        return sessionCalls === 1
          ? { status: 502, body: { error: { code: "BAD_GATEWAY", message: "bad gateway", retryable: true } } }
          : { status: 200, body: sessionInfo("G-002", ["run_9"], 1) };
      }
      if (url === "/api/me/requests") return { status: 200, body: { requests: [], next_cursor: null } };
      if (url === "/api/me/reservation") return { status: 200, body: reservationBody("2026-09-22T12:00:00+03:00") };
      if (url === "/api/runs/run_9")
        return { status: 200, body: run("run_9", "interrupted", "Old question") };
      return { status: 404, body: {} };
    };
    render(<App />);
    expect(await screen.findByText("Old question", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByTestId("turn-status")).toHaveTextContent("Interrupted");
    expect(sessionCalls).toBe(2);
    expect(screen.queryByTestId("session-notice")).toBeNull();
    expect(window.localStorage.getItem("hotel-demo-sessions")).toContain("tok-G-002");
    expect(calls.some((c) => c.url === "/api/chat")).toBe(false);
  });

  it("Shift+Enter inserts a newline instead of sending", async () => {
    handler = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "line1{Shift>}{Enter}{/Shift}line2");
    expect(screen.getByLabelText("Message")).toHaveValue("line1\nline2");
    expect(calls.some((c) => c.url === "/api/chat")).toBe(false);
    await act(async () => {});
  });

  it("shows a pending receipt and request without ever calling it delivered", async () => {
    const task = {
      kind: "housekeeping",
      id: "hk_1",
      status: "pending",
      status_meaning: "Recorded for simulated staff; not yet done.",
      created_at: "2026-09-22T10:05:00+03:00",
      room_number: "305",
      receipt_id: "rcp_1",
      details: { item: "towels", quantity: 2, notes: null },
    };
    handler = backend({
      runStatuses: ["completed"],
      requests: [task],
      artifacts: [
        {
          kind: "housekeeping_task",
          id: "hk_1",
          receipt_id: "rcp_1",
          tool_name: "create_housekeeping_task",
          committed_at: null,
          result: { item: "towels", quantity: 2, status: "pending" },
          current_status: "pending",
        },
      ],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "two towels{Enter}");
    const receipt = await screen.findByTestId("receipt");
    expect(receipt).toHaveTextContent("hk_1");
    expect(receipt).toHaveTextContent("towels × 2");
    expect(screen.getByTestId("receipt-status")).toHaveTextContent(/Pending/);
    const item = await screen.findByTestId("request-item");
    expect(item).toHaveTextContent("Pending"); // said once, in words (no raw status pill)
    expect(item).not.toHaveTextContent("pendingPending");
    for (const el of [receipt, item]) {
      expect(el.textContent ?? "").not.toMatch(/delivered|completed/i);
      expect(el).toHaveTextContent(/not yet done/);
    }
  });

  it("shows a maintenance report as reported, never repaired, and its text as plain text", async () => {
    const issue = {
      kind: "maintenance",
      id: "mt_1",
      status: "pending",
      status_meaning: "Reported to simulated staff; not yet repaired.",
      created_at: "2026-09-22T10:05:00+03:00",
      room_number: "305",
      receipt_id: "rcp_2",
      details: { category: "hvac", guest_description: "<b>AC</b> is not cooling" },
    };
    handler = backend({
      runStatuses: ["completed"],
      requests: [issue],
      artifacts: [
        {
          kind: "maintenance_task",
          id: "mt_1",
          receipt_id: "rcp_2",
          tool_name: "create_maintenance_task",
          committed_at: null,
          result: { category: "hvac", status: "pending" },
          current_status: "pending",
        },
      ],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "the AC is broken{Enter}");
    const receipt = await screen.findByTestId("receipt");
    expect(receipt).toHaveTextContent("Maintenance report");
    expect(receipt).toHaveTextContent("Air conditioning / heating");
    const item = await screen.findByTestId("request-item");
    expect(item).toHaveTextContent("<b>AC</b> is not cooling");
    expect(item.querySelector("b")).toBeNull();
    for (const el of [receipt, item]) {
      expect(el.textContent ?? "").not.toMatch(/(?<!not yet )repaired|fixed|completed/i);
      expect(el).toHaveTextContent(/not yet repaired/);
    }
  });

  it("refreshes the checkout time after an applied checkout change", async () => {
    handler = backend({
      runStatuses: ["completed"],
      checkouts: ["2026-09-22T12:00:00+03:00", "2026-09-22T14:00:00+03:00"],
      artifacts: [
        {
          kind: "checkout_change",
          id: "rsv_1042",
          receipt_id: "rcp_3",
          tool_name: "request_late_checkout",
          committed_at: null,
          result: {
            outcome: "applied",
            previous_checkout_local: "2026-09-22T12:00:00+03:00",
            current_checkout_local: "2026-09-22T14:00:00+03:00",
          },
          current_status: null,
        },
      ],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    expect(await screen.findByTestId("checkout-time")).toHaveTextContent("2026-09-22 12:00");
    await user.type(screen.getByLabelText("Message"), "checkout at 14:00 please{Enter}");
    const receipt = await screen.findByTestId("receipt");
    expect(receipt).toHaveTextContent("Checkout changed");
    expect(receipt).toHaveTextContent("2026-09-22 12:00 → 2026-09-22 14:00");
    expect(screen.getByTestId("receipt-status")).toHaveTextContent("Applied");
    await waitFor(() =>
      expect(screen.getByTestId("checkout-time")).toHaveTextContent("2026-09-22 14:00"),
    );
  });

  it("shows one time on screen, the hotel's: no record ages or real-time countdowns", async () => {
    handler = backend({
      requests: [
        {
          kind: "late_checkout_review", id: "apr_1", status: "pending", status_meaning: "",
          created_at: new Date(Date.now() - 3 * 60_000).toISOString(), room_number: "305", receipt_id: "rcp_1",
          details: { requested_checkout_local: "2026-09-22T15:00:00+03:00", expires_at: "2026-09-22T15:00:00+03:00", human_decision: null },
        },
      ],
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    const card = await screen.findByTestId("request-item");
    expect(card).toHaveTextContent("until 2026-09-22 15:00 (hotel time)");
    expect(card.textContent ?? "").not.toMatch(/ago|expires in|real time/);
    expect(screen.getByLabelText("Hotel time (simulated)")).toHaveTextContent("2026-09-22 10:00");
  });

  it("streams a provisional reply, then shows the stored reply in its place", async () => {
    const base = backend({ runStatuses: ["running", "running", "completed"] });
    let streamAuth = "";
    handler = (url, init) => {
      if (url.endsWith("/events")) {
        streamAuth = (init.headers as Record<string, string>).Authorization;
        const snap = { text: "Your check", activity: 0, done: false };
        const sse = `event: snapshot\ndata: ${JSON.stringify(snap)}\n\nevent: done\ndata: {}\n\n`;
        return { status: 200, body: { __sse: sse } };
      }
      return base(url, init);
    };
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "When is my checkout?{Enter}");
    expect(await screen.findByTestId("agent-streaming")).toHaveTextContent("Your check");
    expect(await screen.findByTestId("agent-reply", {}, { timeout: 4000 })).toHaveTextContent(
      "Your checkout is 2026-09-22 12:00.",
    );
    expect(screen.queryByTestId("agent-streaming")).toBeNull();
    expect(streamAuth).toMatch(/^Bearer tok-G-001-/);
    const streamCall = calls.find((c) => c.url.endsWith("/events"))!;
    expect(streamCall.url).not.toContain("tok-"); // the capability never goes in the URL
    expect(calls.filter((c) => c.url === "/api/chat")).toHaveLength(1);
  });

  it("shows a turn the hotel started, marked as the hotel's, after the guest's", async () => {
    const base = backend({ runStatuses: ["completed"] });
    const title = "Late checkout review for 14:30 expired";
    const hotelRun = run("run_hotel_1", "completed", title, {
      initiated_by: "hotel",
      hotel_updates: [{ kind: "review_closed", title, demo_time: "2026-09-22T14:30:00+03:00" }],
      final_message: "Your late checkout review for 14:30 has expired; checkout stays at 12:00.",
      activity: [
        {
          tool_call_id: "h1", tool_name: "get_my_requests", status: "completed",
          proposed_at: "2026-09-22T18:00:00+00:00", finished_at: "2026-09-22T18:00:01+00:00",
          outcome: "ok", error_code: null, entity_type: null, entity_id: null, summary: {},
          duration_ms: 3,
        },
      ],
    }); // prettier-ignore
    let guestTurnEnded = false;
    let hotelListed = false;
    const review = {
      kind: "approval", id: "apr_1", receipt_id: "rcp_1", tool_name: "request_late_checkout",
      committed_at: null, current_status: "pending",
      result: { outcome: "pending_approval", requested_checkout_local: "2026-09-22T14:30:00+03:00" },
    }; // prettier-ignore
    handler = (url, init) => {
      if (url === "/api/runs/run_hotel_1") return { status: 200, body: hotelRun };
      const r = base(url, init);
      if (url.startsWith("/api/runs/")) {
        guestTurnEnded = true;
        // The review waits until the hotel's turn exists: the clock expired it just before.
        const status = hotelListed ? "expired" : "pending";
        (r.body as { artifacts: unknown[] }).artifacts = [{ ...review, current_status: status }];
      }
      // The guest's turn ended; its terminal write admitted the hotel's turn.
      if (url === "/api/session" && guestTurnEnded && r.status === 200) {
        hotelListed = true;
        const body = r.body as { run_ids: string[] };
        return { ...r, body: { ...body, run_ids: [...body.run_ids, "run_hotel_1"] } };
      }
      return r;
    };
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "When is my checkout?{Enter}");
    const mark = await screen.findByTestId("hotel-update");
    expect(mark).toHaveTextContent("Hotel update · 14:30");
    expect(mark).toHaveTextContent(title);
    const turns = screen.getAllByTestId("turn");
    expect(turns).toHaveLength(2);
    expect(within(turns[0]).queryByTestId("hotel-update")).toBeNull(); // the guest's own words
    expect(within(turns[1]).getByTestId("agent-reply")).toHaveTextContent("has expired");
    expect(within(turns[1]).queryByText("You said:")).toBeNull();
    // The guest's receipt was re-read with the hotel's turn: it no longer says "pending".
    await waitFor(() =>
      expect(within(turns[0]).getByTestId("receipt")).toHaveTextContent("Expired before a decision"),
    );
  });

  it("renders model text as text, never HTML", async () => {
    const base = backend({ runStatuses: ["completed"] });
    handler = (url, init) => {
      const r = base(url, init);
      if (url.startsWith("/api/runs/")) {
        (r.body as unknown as { final_message: string }).final_message = "<img src=x onerror=alert(1)>";
      }
      return r;
    };
    const user = userEvent.setup();
    const { container } = render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "x{Enter}");
    expect(await screen.findByTestId("agent-reply")).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(container.querySelector("img")).toBeNull();
  });
});

describe("policyTopicsRead", () => {
  const step = (summary: Record<string, unknown>, status: ToolActivity["status"] = "completed"): ToolActivity => ({
    tool_call_id: "call-1", tool_name: "get_hotel_policy", status, proposed_at: null, finished_at: null,
    outcome: "ok", error_code: null, entity_type: "hotel_policy", entity_id: "hotel_demo", summary, duration_ms: 4,
  });
  const turn = (activity: ToolActivity[]) =>
    ({ clientRequestId: "c", message: "m", runId: "r", error: null, phase: "done",
       run: run("r", "completed", "m", { activity }) }) as unknown as Turn;

  it("marks every topic one policy read returned", () => {
    const read = step({ topics: ["breakfast", "parking"] });
    const refused = step({}, "rejected");
    expect(policyTopicsRead([turn([read, refused])])).toEqual(["breakfast", "parking"]);
  });

  it("uses only the latest reply that read anything", () => {
    const earlier = turn([step({ topics: ["dining"] })]);
    const latest = turn([step({ topics: ["luggage"] })]);
    expect(policyTopicsRead([earlier, latest, turn([])])).toEqual(["luggage"]);
  });
});

describe("things the screen says once, and says up front", () => {
  const withHealth = (configured: boolean): Handler => {
    const base = backend();
    return (url, init) =>
      url === "/api/health"
        ? { status: 200, body: { status: "ok", simulation_label: "x", provider_configured: configured } }
        : base(url, init);
  };

  it("says before the first message that no model is configured", async () => {
    handler = withHealth(false);
    render(<App />);
    const notice = await screen.findByTestId("provider-missing");
    expect(notice).toHaveTextContent("No OpenAI key is configured");
    expect(notice).toHaveTextContent("env.example");
  });

  it("says nothing about the model when a key is configured", async () => {
    handler = withHealth(true);
    render(<App />);
    await screen.findByTestId("guest-G-001");
    await waitFor(() => expect(calls.some((c) => c.url === "/api/health")).toBe(true));
    expect(screen.queryByTestId("provider-missing")).toBeNull();
  });

  it("shows the hotel's timezone before any guest is picked", async () => {
    handler = backend();
    render(<App />);
    await waitFor(() => expect(screen.getByLabelText("Hotel time (simulated)")).toHaveTextContent("Europe/Istanbul"));
  });

  it("keeps a receipt's recorded status beside its status now", async () => {
    handler = backend({
      runStatuses: ["completed"],
      artifacts: [
        {
          kind: "housekeeping_task", id: "hk_1", receipt_id: "rcp_1",
          tool_name: "create_housekeeping_task", committed_at: null,
          result: { item: "room_cleaning", quantity: 1, status: "pending" },
          current_status: "completed",
        },
      ], // prettier-ignore
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    await user.type(screen.getByLabelText("Message"), "clean my room{Enter}");
    const receipt = await screen.findByTestId("receipt");
    expect(receipt).toHaveTextContent("Room cleaning"); // a cleaning has no count
    expect(receipt).not.toHaveTextContent("× 1");
    expect(within(receipt).getByTestId("receipt-status")).toHaveTextContent("Now: Completed");
    expect(within(receipt).getByTestId("receipt-recorded")).toHaveTextContent("Recorded as pending");
  });

  it("does not send on the Enter that confirms an input-method composition", async () => {
    handler = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-001"));
    const box = screen.getByLabelText("Message");
    await user.type(box, "こんにちは");
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(calls.some((c) => c.url === "/api/chat")).toBe(false);
    expect(box).toHaveValue("こんにちは");
  });
});
