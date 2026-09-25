import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

// The guided scenarios run on the main screen against a small fake of the HTTP
// contract (test double only).

const GUEST_TOKEN = "guest-capability";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const GUESTS = [
  { guest_id: "G-001", display_name: "Emma Wilson", room_number: "305", reservation_reference: "R-1042", display_status: "In house" },
  { guest_id: "G-002", display_name: "Daniel Kim", room_number: "412", reservation_reference: "R-1088", display_status: "In house" },
];

function context(guestId: string) {
  const g = GUESTS.find((x) => x.guest_id === guestId)!;
  return {
    guest_id: g.guest_id,
    guest_name: g.display_name,
    room_number: g.room_number,
    reservation_reference: g.reservation_reference,
    display_status: g.display_status,
    hotel_name: "Demo Hotel",
    timezone: "Europe/Istanbul",
    demo_time: "2026-09-22T10:00:00+03:00",
  };
}

let calls: { method: string; url: string; auth: string | null; body: unknown }[] = [];
let guest = "G-002";
let clockAt = "10:00";
let cancelled = false; // room 412's next arrival
let forcedOutcome: string | null = null; // makes the agent's result differ from the step
let requested = "14:30";
let reviewFiled = false;
let runs: { outcome: string; reasons: string[] }[] = [];
let hotelSpeaks = false; // the cancelled arrival makes the hotel tell Daniel
let decided: "approve" | "reject" | null = null; // the manager's decision on the review
let notTold: string | null = null; // the hotel's update was skipped for this reason

/** A line no tool can act on: the agent only reads the guest's own reservation. */
function readsOnly(message: string): boolean {
  return /show me|sql/i.test(message);
}

function hotelRun(runId: string, kind: string, title: string) {
  return {
    run_id: runId, client_request_id: `hup_${runId}`, status: "completed",
    initiated_by: "hotel", user_message: title,
    hotel_updates: [{ kind, title, demo_time: world().demo_time }],
    final_message: `The hotel says: ${title}.`,
    error: null, outcome_detail: null, artifacts: [],
    activity: [
      {
        tool_call_id: "h1", tool_name: "get_my_requests", status: "completed",
        proposed_at: null, finished_at: null, outcome: "ok", error_code: null,
        entity_type: "reservation", entity_id: "rsv_x", duration_ms: 3, summary: {},
      },
    ],
    conversation_restarted: false, turn_forgotten: false, created_at: null,
    started_at: null, finished_at: null, model_id: null, latency_ms: null, usage: null,
  }; // prettier-ignore
}

/** What the checkout rules decide for a request now: Daniel's room must be ready at 15:00. */
function decideCheckout(): { outcome: string; reasons: string[] } {
  if (forcedOutcome) return { outcome: forcedOutcome, reasons: [] };
  if (guest === "G-002" && !cancelled) return { outcome: "denied", reasons: ["NEXT_ARRIVAL_CONFLICT"] };
  return { outcome: "pending_approval", reasons: ["REQUIRES_MANAGER_REVIEW"] };
}

function world() {
  return {
    source: "Simulated world event (fictional)",
    demo_time: `2026-09-22T${clockAt}:00+03:00`,
    clock_limit: "2026-09-22T18:00:00+03:00",
    clock_steps: [30, 60, 120],
    events: [
      {
        key: "arrival_412_cancelled",
        title: "Room 412's next arrival is cancelled",
        effect: "",
        components: ["operational"],
        room: "412",
        fact: "next_arrival",
        action: "Cancel this arrival",
        applied: cancelled,
        applied_at: null,
        available: !cancelled,
        unavailable_reason: null,
      },
    ],
    rooms: GUESTS.map((g) => {
      const daniel = g.guest_id === "G-002";
      const arrival = daniel && !cancelled ? "15:00" : daniel ? null : "18:00";
      const latest = daniel && !cancelled ? "14:00" : "16:00";
      return {
        room_number: g.room_number,
        guest_id: g.guest_id,
        guest_name: g.display_name,
        status: "checked_in",
        check_in: "2026-09-21T15:00:00+03:00",
        checkout: "2026-09-22T12:00:00+03:00",
        in_service: true,
        housekeeping_state: "dirty" as const,
        open_tasks: [],
        pending_review: null,
        latest_update:
          daniel && cancelled && notTold
            ? {
                kind: "checkout_now_possible" as const,
                title: "Checkout at 14:30 may now be possible",
                demo_time: null,
                status: "skipped" as const,
                skip_reason: notTold,
              }
            : null,
        next_arrival: arrival ? `2026-09-22T${arrival}:00+03:00` : null,
        latest_checkout: `2026-09-22T${latest}:00+03:00`,
        latest_needs_review: latest > "14:00",
        latest_blocked_by: null,
      };
    }),
    policy: {
      version: 1,
      standard_checkout: "12:00",
      automatic_until: "14:00",
      manager_until: "16:00",
    },
  };
}

function review() {
  const lapsed = clockAt >= requested; // the hotel clock has reached the requested time
  const status =
    decided === "approve" ? "executed" : decided === "reject" ? "rejected" : lapsed ? "expired" : "pending";
  return {
    approval_id: "apr_1",
    status,
    version: 1,
    guest_name: context(guest).guest_name,
    room_number: context(guest).room_number,
    reservation_reference: context(guest).reservation_reference,
    requested_checkout_local: `2026-09-22T${requested}:00+03:00`,
    current_checkout_local: "2026-09-22T12:00:00+03:00",
    guest_reason: null,
    created_at: "2026-09-22T07:00:00+00:00",
    expires_at: `2026-09-22T${requested}:00+03:00`,
    fresh: true,
    human_decision: decided,
    decided_by: decided ? "manager" : null,
    decided_at: null,
    status_reason: lapsed && !decided ? "EXPIRED_BEFORE_DECISION" : null,
  };
}

function minutes(t: string): number {
  return Number(t.slice(0, 2)) * 60 + Number(t.slice(3, 5));
}

beforeEach(() => {
  hotelSpeaks = false;
  decided = null;
  notTold = null;
  calls = [];
  guest = "G-002";
  clockAt = "10:00";
  cancelled = false;
  forcedOutcome = null;
  requested = "14:30";
  reviewFiled = false;
  runs = [];
  window.localStorage.clear();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const auth = new Headers(init.headers).get("Authorization");
      const body = init.body ? JSON.parse(String(init.body)) : null;
      calls.push({ method, url, auth, body });
      if (url === "/api/demo/guests") return json({ guests: GUESTS });
      if (url === "/api/demo/sessions") {
        guest = body.guest_id;
        return json({
          session_id: "s1",
          token: GUEST_TOKEN,
          expires_at: "2099-01-01T00:00:00Z",
          context: context(guest),
          demo_time: context(guest).demo_time,
          timezone: "Europe/Istanbul",
          simulation_label: "Simulation",
        });
      }
      if (url === "/api/session") {
        return json({
          session_id: "s1",
          expires_at: "2099-01-01T00:00:00Z",
          accepted_turn_count: 0,
          turn_limit: 40,
          context_restarted_at: null,
          context: context(guest),
          demo_time: context(guest).demo_time,
          timezone: "Europe/Istanbul",
          simulation_label: "Simulation",
          run_ids: [
            ...(hotelSpeaks && cancelled ? ["run_hotel"] : []),
            ...(hotelSpeaks && decided ? ["run_hotel_2"] : []),
          ],
        });
      }
      if (url === "/api/runs/run_hotel_2") {
        const verb = decided === "approve" ? "approved" : "declined";
        return json(hotelRun("run_hotel_2", "review_closed", `Late checkout until ${requested} ${verb}`));
      }
      if (url === "/api/runs/run_hotel") {
        const title = "Checkout at 14:30 may now be possible";
        return json({
          run_id: "run_hotel", client_request_id: "hup_1", status: "completed",
          initiated_by: "hotel", user_message: title,
          hotel_updates: [{ kind: "checkout_now_possible", title, demo_time: world().demo_time }],
          final_message: "Good news: 14:30 may now be possible. Shall I request it for you?",
          error: null, outcome_detail: null, artifacts: [],
          activity: [
            {
              tool_call_id: "h1", tool_name: "evaluate_late_checkout", status: "completed",
              proposed_at: null, finished_at: null, outcome: "approval_required", error_code: null,
              entity_type: "reservation", entity_id: "rsv_x", duration_ms: 3,
              summary: {
                requested_checkout_local: "2026-09-22T14:30:00+03:00",
                outcome: "approval_required", reason_codes: ["REQUIRES_MANAGER_REVIEW"],
              },
            },
          ],
          conversation_restarted: false, turn_forgotten: false, created_at: null,
          started_at: null, finished_at: null, model_id: null, latency_ms: null, usage: null,
        }); // prettier-ignore
      }
      if (url === "/api/me/requests") return json({ requests: [], next_cursor: null });
      if (url === "/api/me/reservation") {
        return json({
          reservation: {
            reservation_reference: context(guest).reservation_reference,
            room_number: context(guest).room_number,
            status: "checked_in",
            display_status: "In house",
            scheduled_check_in: null,
            scheduled_checkout: "2026-09-22T12:00:00+03:00",
            reservation_version: 1,
          },
        });
      }
      if (url === "/api/operator/world") return json(world());
      if (url === "/api/operator/world/reset") {
        cancelled = false;
        clockAt = "10:00";
        reviewFiled = false;
        return json(world());
      }
      if (url === "/api/operator/world/events") {
        cancelled = true;
        return json({
          kind: "event",
          key: "arrival_412_cancelled",
          title: "Room 412's next arrival is cancelled",
          components: ["operational"],
          before: "next arrival 15:00",
          after: "no next arrival",
          demo_time: world().demo_time,
          replayed: false,
        });
      }
      if (url === "/api/operator/world/clock") {
        const from = clockAt;
        const to = minutes(clockAt) + body.advance_minutes;
        clockAt = `${String(Math.floor(to / 60)).padStart(2, "0")}:${String(to % 60).padStart(2, "0")}`;
        return json({
          kind: "clock", key: "clock", title: "Hotel clock moved", components: [],
          before: from, after: clockAt, demo_time: world().demo_time, replayed: false,
        });
      }
      if (url === "/api/operator/approvals/apr_1/decision") {
        decided = body.decision;
        const approval = review();
        return json({
          approval, decision: body.decision, outcome: approval.status,
          applied: approval.status === "executed", receipt_id: null, replayed: false,
        }); // prettier-ignore
      }
      if (url.startsWith("/api/operator/approvals")) {
        return json({ approvals: reviewFiled ? [review()] : [], next_cursor: null });
      }
      if (url.startsWith("/api/operator/tasks")) return json({ tasks: [], truncated: false });
      if (url === "/api/chat") {
        const result = readsOnly(body.message) ? { outcome: "read", reasons: [] } : decideCheckout();
        runs.push(result);
        reviewFiled = reviewFiled || result.outcome === "pending_approval";
        return json({ run_id: `run_${runs.length}`, status: "queued", poll: "", replayed: false });
      }
      if (/^\/api\/runs\/run_\d+\/events$/.test(url)) return json({ error: null }, 404);
      const runMatch = /^\/api\/runs\/run_(\d+)$/.exec(url);
      if (runMatch) {
        const n = Number(runMatch[1]);
        const sent = calls.filter((c) => c.url === "/api/chat")[n - 1].body as {
          client_request_id: string;
          message: string;
        };
        const { outcome: runOutcome, reasons } = runs[n - 1];
        const read = runOutcome === "read";
        return json({
          run_id: `run_${n}`,
          client_request_id: sent.client_request_id,
          status: "completed",
          user_message: sent.message,
          final_message: read
            ? "I can only show your own reservation."
            : `Your ${requested} request: ${runOutcome}.`,
          error: null,
          outcome_detail: null,
          activity: [
            {
              tool_call_id: "t1",
              tool_name: read ? "get_my_reservation" : "request_late_checkout",
              status: "completed",
              proposed_at: null,
              finished_at: null,
              outcome: read ? "ok" : runOutcome,
              error_code: null,
              entity_type: "reservation",
              entity_id: "rsv_x",
              summary: read
                ? {}
                : {
                    requested_checkout_local: `2026-09-22T${requested}:00+03:00`,
                    outcome: runOutcome,
                    reason_codes: reasons,
                  },
              duration_ms: 4,
            },
          ],
          artifacts: [],
          conversation_restarted: false,
          turn_forgotten: false,
          created_at: null,
          started_at: null,
          finished_at: null,
          model_id: null,
          latency_ms: null,
          usage: null,
        });
      }
      return json({ error: { code: "NOT_FOUND", message: url, retryable: false } }, 404);
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function start(id: string) {
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByTestId("mode-guided"));
  const card = await screen.findByTestId(`scenario-${id}`);
  await user.click(within(card).getByRole("button", { name: /^Start/ }));
  await screen.findByTestId("scenario-guide");
  return user;
}

describe("guided scenarios on the main screen", () => {
  it("runs a scenario from a clean hotel, in its own read-only layout", async () => {
    hotelSpeaks = true;
    const user = await start("offer");
    // A scenario starts from a clean hotel, then a fresh conversation for its guest.
    const reset = calls.findIndex((c) => c.url === "/api/operator/world/reset");
    const session = calls.findIndex((c) => c.url === "/api/demo/sessions");
    expect(reset).toBeGreaterThanOrEqual(0);
    expect(calls[reset]).toMatchObject({ method: "POST", auth: null, body: { confirm: true } });
    expect(session).toBeGreaterThan(reset);
    expect(screen.getAllByTestId("scenario-step")[0]).toHaveAttribute("data-state", "current");
    // The tour is locked: no hotel board, no tabs, no hand-driven change or decision.
    expect(screen.getByTestId("mode-guided")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("hotel-board")).toBeNull();
    expect(screen.queryByTestId("tab-manager")).toBeNull();
    expect(screen.queryByTestId("world-event-arrival_412_cancelled")).toBeNull();
    const card = await screen.findByTestId("room-card");
    expect(card).toHaveTextContent("Room 412 now · Daniel Kim");
    expect(card).toHaveTextContent("Automatic until 14:00, manager until 16:00");
    expect(within(card).queryByRole("button")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Send as Daniel" }));
    // The guest line goes through the guest's own scoped session.
    const chat = calls.find((c) => c.url === "/api/chat")!;
    expect(chat.auth).toBe(`Bearer ${GUEST_TOKEN}`);
    expect(chat.body).toMatchObject({ message: "Can I check out at 14:30?" });
    expect(await screen.findByTestId("scenario-timeline")).toHaveTextContent(
      "Daniel asked for 14:30. The checkout rules refused it: the room must be ready for its next arrival.",
    );

    await user.click(await screen.findByRole("button", { name: "Make it happen" }));
    const post = calls.find((c) => c.url === "/api/operator/world/events")!;
    expect(post.auth).toBeNull();
    expect(post.body).toMatchObject({ event: "arrival_412_cancelled" });
    expect(await screen.findByTestId("scenario-timeline")).toHaveTextContent(
      "Simulated hotel change: Room 412's next arrival is cancelled",
    );
    // The room card marks what the change moved.
    expect(await screen.findByTestId("changed-412:next_arrival")).toHaveTextContent("15:00 None");
    expect(screen.getByTestId("changed-412:latest")).toHaveTextContent("14:00 16:00");

    await waitFor(() =>
      expect(screen.getAllByTestId("scenario-step")[2]).toHaveAttribute("data-state", "done"),
    );
    await user.click(await screen.findByRole("button", { name: "Send as Daniel" }));
    await waitFor(() =>
      expect(screen.getByTestId("scenario-timeline")).toHaveTextContent(
        "Daniel asked for 14:30. The agent filed a manager review.",
      ),
    );
    // The review the agent filed is waiting beside the chat, decided only from a story step.
    const item = await screen.findByTestId("operator-item");
    expect(item).toHaveTextContent("Daniel Kim · room 412");
    expect(within(item).queryByRole("button", { name: "Approve" })).toBeNull();

    // Leaving the tour keeps the hotel as the story left it.
    const resets = calls.filter((c) => c.url === "/api/operator/world/reset").length;
    await user.click(screen.getByRole("button", { name: "Exit tour" }));
    expect(await screen.findByTestId("hotel-board")).toBeInTheDocument();
    expect(screen.getByTestId("mode-explore")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("board-room-412")).toHaveTextContent("16:00with a manager");
    expect(calls.filter((c) => c.url === "/api/operator/world/reset")).toHaveLength(resets);
  });

  it("counts the hotel's own turn for the hotel step only, never for the guest's line", async () => {
    hotelSpeaks = true;
    const user = await start("offer");
    await user.click(screen.getByRole("button", { name: "Send as Daniel" }));
    await user.click(await screen.findByRole("button", { name: "Make it happen" }));
    // The change made the hotel tell Daniel; the turn appears in his conversation...
    expect(await screen.findByTestId("hotel-update")).toHaveTextContent(
      "Checkout at 14:30 may now be possible",
    );
    // ...and ticks the hotel's step, but it is not Daniel answering: his line still waits.
    const steps = () => screen.getAllByTestId("scenario-step");
    await waitFor(() => expect(steps()[2]).toHaveAttribute("data-state", "done"));
    expect(steps()[3]).toHaveAttribute("data-state", "current");
    expect(screen.queryByTestId("scenario-note")).toBeNull();
    expect(calls.filter((c) => c.url === "/api/chat")).toHaveLength(1);
  });

  it("lets the hotel offer and the guest ask: each hotel step ticks from its own update", async () => {
    hotelSpeaks = true;
    const user = await start("offer");
    const steps = () => screen.getAllByTestId("scenario-step");
    const timeline = () => screen.getByTestId("scenario-timeline");

    await user.click(screen.getByRole("button", { name: "Send as Daniel" }));
    await user.click(await screen.findByRole("button", { name: "Make it happen" }));
    // No button: the change made the hotel start Daniel's turn, and its update ticks the step.
    await waitFor(() => expect(steps()[2]).toHaveAttribute("data-state", "done"));
    expect(timeline()).toHaveTextContent(
      "Hotel update: Checkout at 14:30 may now be possible. The agent wrote to Daniel first " +
        "after re-reading the facts (1 read tool step). It had read tools only",
    );

    // Daniel's yes is his own turn, and that turn files the review.
    await user.click(await screen.findByRole("button", { name: "Send as Daniel" }));
    const chats = calls.filter((c) => c.url === "/api/chat");
    expect(chats.at(-1)!.body).toMatchObject({ message: "Yes, please request the 14:30 checkout." });
    await waitFor(() => expect(timeline()).toHaveTextContent("The agent filed a manager review."));

    await user.click(await screen.findByRole("button", { name: "Approve Daniel's review" }));
    const decision = calls.find((c) => c.url === "/api/operator/approvals/apr_1/decision")!;
    expect(decision.body).toMatchObject({ decision: "approve" });
    expect(await screen.findByTestId("scenario-done")).toBeInTheDocument();
    expect(timeline()).toHaveTextContent("You approved. The records still matched, so it was applied");
    expect(timeline()).toHaveTextContent("Hotel update: Late checkout until 14:30 approved.");
  });

  it("tells words from authority: a claimed manager gets a review, other lines change nothing", async () => {
    hotelSpeaks = true;
    requested = "15:30";
    const user = await start("authority");
    const timeline = () => screen.getByTestId("scenario-timeline");

    await user.click(screen.getByRole("button", { name: "Send as Emma" }));
    await waitFor(() =>
      expect(timeline()).toHaveTextContent("Emma asked for 15:30. The agent filed a manager review."),
    );
    await user.click(await screen.findByRole("button", { name: "Send as Emma" }));
    await waitFor(() =>
      expect(timeline()).toHaveTextContent(
        "Emma's message changed nothing in the hotel: the agent's turn left no receipt. No tool " +
          "takes a guest or a booking as input",
      ),
    );
    await user.click(await screen.findByRole("button", { name: "Send as Emma" }));
    await waitFor(() => expect(timeline()).toHaveTextContent("none of them runs SQL"));

    await user.click(await screen.findByRole("button", { name: "Reject Emma's review" }));
    const decision = calls.find((c) => c.url === "/api/operator/approvals/apr_1/decision")!;
    expect(decision.body).toMatchObject({ decision: "reject" });
    expect(await screen.findByTestId("scenario-done")).toBeInTheDocument();
    expect(timeline()).toHaveTextContent("You rejected the review. The checkout was not changed.");
    expect(timeline()).toHaveTextContent("Hotel update: Late checkout until 15:30 declined.");
  });

  it("waits at a hotel step until the hotel speaks, and says so if the update was not sent", async () => {
    notTold = "NO_PROVIDER";
    const user = await start("offer");
    await user.click(screen.getByRole("button", { name: "Send as Daniel" }));
    await user.click(await screen.findByRole("button", { name: "Make it happen" }));
    const step = () => screen.getAllByTestId("scenario-step")[2];
    await waitFor(() => expect(step()).toHaveAttribute("data-state", "current"));
    expect(within(step()).getByTestId("step-waiting")).toHaveTextContent(
      "Waiting for the hotel's message to Daniel",
    );
    expect(within(step()).queryByRole("button")).toBeNull();
    expect(await screen.findByTestId("scenario-note")).toHaveTextContent(
      "The hotel did not send this update to Daniel: no language model configured.",
    );
  });

  it("does not tick a step when the agent's result differs, and says why", async () => {
    forcedOutcome = "applied";
    const user = await start("offer");
    await user.click(await screen.findByRole("button", { name: "Send as Daniel" }));
    expect(await screen.findByTestId("scenario-note")).toHaveTextContent(
      "This step expects a refusal (the room must be ready for its next arrival). Instead: Daniel asked for 14:30. Inside the automatic window",
    );
    expect(screen.getAllByTestId("scenario-step")[0]).toHaveAttribute("data-state", "current");
    expect(screen.queryByTestId("scenario-done")).toBeNull();
  });

  it("shows a waiting review expire when the hotel clock reaches its time", async () => {
    const user = await start("clock");
    await user.click(await screen.findByRole("button", { name: "Send as Emma" }));
    expect(await screen.findByTestId("scenario-timeline")).toHaveTextContent(
      "Emma asked for 14:30. The agent filed a manager review.",
    );
    await user.click(await screen.findByRole("button", { name: "Move the hotel clock to 14:30" }));
    const moves = calls.filter((c) => c.url === "/api/operator/world/clock").map((c) => c.body);
    expect(moves).toEqual([
      expect.objectContaining({ advance_minutes: 120 }),
      expect.objectContaining({ advance_minutes: 120 }),
      expect.objectContaining({ advance_minutes: 30 }),
    ]);
    await screen.findByTestId("scenario-done");
    await waitFor(() =>
      expect(screen.getByTestId("scenario-timeline")).toHaveTextContent(
        "Emma's review for 14:30 expired: the time it asked for has arrived on the hotel clock",
      ),
    );
    expect(screen.getByTestId("demo-time")).toHaveTextContent("2026-09-22 14:30");
  });

  it("starts over only after confirmation, then drops the discarded world's guest sessions", async () => {
    const confirm = vi.spyOn(window, "confirm");
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByTestId("guest-G-002"));
    await screen.findByTestId("guest-context");
    const button = screen.getByTestId("start-over");

    confirm.mockReturnValueOnce(false);
    await user.click(button);
    expect(calls.some((c) => c.url === "/api/operator/world/reset")).toBe(false);

    confirm.mockReturnValueOnce(true);
    await user.click(button);
    await waitFor(() => expect(screen.queryByTestId("guest-context")).toBeNull());
    const post = calls.find((c) => c.url === "/api/operator/world/reset")!;
    expect(post).toMatchObject({ method: "POST", auth: null, body: { confirm: true } });
    expect(window.localStorage.getItem("hotel-demo-sessions")).toBeNull();
  });
});
