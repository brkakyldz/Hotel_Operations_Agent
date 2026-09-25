import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ManagerPanel from "./ManagerPanel";
import { useHotelDesk } from "./useHotelDesk";

function item(overrides: Record<string, unknown> = {}) {
  return {
    approval_id: "apr_1",
    status: "pending",
    version: 1,
    guest_name: "Emma Wilson",
    room_number: "305",
    reservation_reference: "R-1042",
    requested_checkout_local: "2026-09-22T15:00:00+03:00",
    current_checkout_local: "2026-09-22T12:00:00+03:00",
    guest_reason: "late flight",
    created_at: "2026-09-22T07:00:00+00:00",
    expires_at: "2026-09-22T15:00:00+03:00",
    fresh: true,
    human_decision: null,
    decided_by: null,
    decided_at: null,
    status_reason: null,
    ...overrides,
  };
}

function task(overrides: Record<string, unknown> = {}) {
  return {
    kind: "housekeeping",
    task_id: "hk_1",
    status: "pending",
    version: 1,
    guest_name: "Emma Wilson",
    room_number: "305",
    reservation_reference: "R-1042",
    details: { item: "towels", quantity: 2, notes: null },
    created_at: "2026-09-22T07:00:00+00:00",
    actions: ["start", "cancel"],
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

let calls: { url: string; auth: string | null; body: unknown }[] = [];
let decisionOutcome = "executed";

beforeEach(() => {
  calls = [];
  decisionOutcome = "executed";
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const auth = new Headers(init.headers).get("Authorization");
      calls.push({ url, auth, body: init.body ? JSON.parse(String(init.body)) : null });
      if (url === "/api/operator/world") {
        return json({
          source: "Simulated world event (fictional)",
          demo_time: "2026-09-22T10:00:00+03:00",
          clock_limit: "2026-09-22T18:00:00+03:00",
          clock_steps: [30, 60, 120],
          events: [],
        });
      }
      if (url.includes("/transition")) {
        return json({ task: task({ status: "in_progress", version: 2, actions: ["complete", "cancel"] }) });
      }
      if (url.startsWith("/api/operator/tasks")) {
        const moved = calls.some((c) => c.url.includes("/transition"));
        return json({
          tasks: [moved ? task({ status: "in_progress", version: 2, actions: ["complete", "cancel"] }) : task()],
          truncated: false,
        });
      }
      if (url.includes("/decision")) {
        const body = JSON.parse(String(init.body));
        return json({
          approval: item({
            status: decisionOutcome,
            version: 2,
            status_reason: decisionOutcome === "stale" ? "CHANGED:operational" : null,
          }),
          decision: body.decision,
          outcome: decisionOutcome,
          applied: decisionOutcome === "executed",
          receipt_id: decisionOutcome === "executed" ? "rcp_9" : null,
          replayed: false,
        });
      }
      const decided = calls.some((c) => c.url.includes("/decision"));
      return json({
        approvals: decided
          ? [item({ status: decisionOutcome, version: 2, human_decision: "approve" })]
          : [item()],
        next_cursor: null,
      });
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

/** The Manager tab over a real desk hook, as the main screen mounts it. */
function Manager({ onChanged = vi.fn() }: { onChanged?: () => void }) {
  const desk = useHotelDesk(0);
  return <ManagerPanel desk={desk} onChanged={onChanged} />;
}

describe("Manager tab", () => {
  it("approves with the queue version, no credential, and shows intent apart from the result", async () => {
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<Manager onChanged={onChanged} />);
    const entry = await screen.findByTestId("operator-item");
    expect(entry).toHaveTextContent("Emma Wilson");
    expect(entry).toHaveTextContent("Late checkout 12:00 → 15:00");
    // It lapses when the hotel clock reaches 15:00: the time is said once, not twice.
    expect(entry).not.toHaveTextContent("lapses");
    expect(within(entry).queryByText("pending")).toBeNull(); // every card here is waiting
    await user.click(within(entry).getByRole("button", { name: "Approve" }));
    expect(await screen.findByTestId("operator-result")).toHaveTextContent(
      "Approved: Emma's checkout is now 15:00 (hotel time).",
    );
    const decision = calls.find((c) => c.url.includes("/decision"))!;
    expect(decision.body).toMatchObject({ decision: "approve", expected_version: 1 });
    expect(decision.body).not.toHaveProperty("actor");
    expect(decision.auth).toBeNull();
    expect(onChanged).toHaveBeenCalledOnce(); // the guest's side re-reads on the same screen
    expect(await screen.findByTestId("decided-item")).toHaveTextContent("approved and applied");
  });

  it("reports a stale approve attempt as not executed", async () => {
    decisionOutcome = "stale";
    const user = userEvent.setup();
    render(<Manager />);
    await user.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByTestId("operator-result")).toHaveTextContent(
      "Approve recorded, but hotel data changed since the request (This room's bookings and plans). Nothing was changed",
    );
  });

  it("moves a task with its version and shows the simulated outcome", async () => {
    const user = userEvent.setup();
    render(<Manager />);
    const card = await screen.findByTestId("operator-task");
    expect(card).toHaveTextContent("towels × 2");
    await user.click(within(card).getByRole("button", { name: /^Start/ }));
    expect(await screen.findByTestId("operator-result")).toHaveTextContent(
      "towels × 2 (room 305): In progress (simulated staff).",
    );
    const move = calls.find((c) => c.url.includes("/transition"))!;
    expect(move.url).toBe("/api/operator/tasks/housekeeping/hk_1/transition");
    expect(move.body).toEqual({ action: "start", expected_version: 1 });
    expect(
      await within(await screen.findByTestId("operator-task")).findByRole("button", {
        name: /^Mark completed/,
      }),
    ).toBeInTheDocument();
  });
});
