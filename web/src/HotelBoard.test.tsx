import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BoardRoom, WorldState } from "./api";
import HotelBoard, { RoomCard, sideEffectText, useBoardChanges } from "./HotelBoard";
import { useHotelDesk } from "./useHotelDesk";

/** The board over a real desk hook, as the main screen mounts it in Explore mode. */
function Board({
  onChanged = vi.fn(),
  onSelect = vi.fn(),
  selected = null,
}: {
  onChanged?: () => void;
  onSelect?: (id: string) => void;
  selected?: string | null;
}) {
  const desk = useHotelDesk(0);
  const changes = useBoardChanges(desk.world);
  return (
    <HotelBoard
      desk={desk}
      changes={changes}
      guests={null}
      selected={selected}
      selectDisabled={false}
      loadError={null}
      onSelect={onSelect}
      onChanged={onChanged}
    />
  );
}

/** The guided tour's read-only card for one room. */
function Card() {
  const desk = useHotelDesk(0);
  const changes = useBoardChanges(desk.world);
  return <RoomCard world={desk.world} room="305" changes={changes} />;
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

const T = (hhmm: string) => `2026-09-22T${hhmm}:00+03:00`;

function room(overrides: Partial<BoardRoom>): BoardRoom {
  return {
    room_number: "305",
    guest_id: "G-001",
    guest_name: "Emma Wilson",
    status: "checked_in",
    check_in: T("09:00"),
    checkout: T("12:00"),
    in_service: true,
    housekeeping_state: "dirty",
    open_tasks: [],
    pending_review: null,
    next_arrival: T("18:00"),
    latest_checkout: T("16:00"),
    latest_needs_review: true,
    latest_blocked_by: null,
    ...overrides,
  };
}

function worldState(early = false): WorldState {
  const event = (key: string, extra: object) => ({
    key,
    title: key,
    effect: `${key} effect.`,
    components: ["operational"],
    applied: false,
    applied_at: null,
    available: true,
    unavailable_reason: null,
    room: null,
    fact: null,
    action: null,
    ...extra,
  });
  return {
    source: "Simulated world event (fictional)",
    demo_time: T("10:00"),
    timezone: "Europe/Istanbul",
    clock_limit: T("18:00"),
    clock_steps: [30, 60, 120],
    events: [
      event("arrival_305_early", {
        title: "Room 305's next guest arrives early (15:30 instead of 18:00)",
        room: "305",
        fact: "next_arrival",
        action: "Arrives early (15:30)",
        applied: early,
        available: !early,
      }),
      event("room_305_out_of_service", {
        room: "305",
        fact: "in_service",
        action: "Take out of service",
        available: false,
        unavailable_reason: "Room 305 is already out of service.",
      }),
      event("cleaning_305_shortened", { action: null }),
    ],
    rooms: [
      room({ room_number: "218", guest_id: "G-003", guest_name: "Sofia Rossi", status: "confirmed",
        check_in: T("15:00"), checkout: T("12:00"), next_arrival: null, latest_checkout: null,
        latest_needs_review: false }), // prettier-ignore
      room(early ? { next_arrival: T("15:30"), latest_checkout: T("14:30") } : {}),
    ],
    policy: { version: 1, standard_checkout: "12:00", automatic_until: "14:00", manager_until: "16:00" },
  };
}

let calls: { url: string; auth: string | null; body: unknown }[] = [];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const auth = new Headers(init.headers).get("Authorization");
      calls.push({ url, auth, body: init.body ? JSON.parse(String(init.body)) : null });
      if (url.endsWith("/world/events")) {
        return json({
          kind: "event",
          key: "arrival_305_early",
          title: "Room 305's next guest arrives early (15:30 instead of 18:00)",
          components: ["operational"],
          before: "next arrival 18:00",
          after: "next arrival 15:30",
          demo_time: T("10:00"),
          replayed: false,
        });
      }
      if (url.startsWith("/api/operator/approvals")) return json({ approvals: [], next_cursor: null });
      if (url.startsWith("/api/operator/tasks")) return json({ tasks: [], truncated: false });
      if (url.startsWith("/api/operator/events")) {
        return json({
          events: [
            {
              id: 7, event_type: "world_event_applied", actor_type: "operator", run_id: null,
              entity_type: "world", entity_id: "arrival_305_early", tool_name: null, outcome: "applied",
              reason_codes: [], wall_time: "2026-09-24T17:00:00+00:00",
              demo_time: T("12:30"), payload: {},
            },
          ],
          next_before_id: null,
        });
      }
      return json(worldState(calls.some((c) => c.url.endsWith("/world/events"))));
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

describe("Hotel board", () => {
  it("shows every room as the rules see it now", async () => {
    render(<Board />);
    const emma = await screen.findByTestId("board-room-305");
    expect(emma).toHaveTextContent("Emma Wilson");
    expect(emma).toHaveTextContent("12:00");
    expect(emma).toHaveTextContent("18:00");
    expect(emma).toHaveTextContent("16:00with a manager");
    const sofia = screen.getByTestId("board-room-218");
    expect(sofia).toHaveTextContent("Arrives 15:00");
    expect(sofia).toHaveTextContent("after check-in");
    expect(screen.getByTestId("board-policy")).toHaveTextContent(
      "Checkout policy: automatic until 14:00, with a manager until 16:00",
    );
  });

  it("shows each room whole: open requests, a waiting review, and cleaning only where a rule reads it", async () => {
    const whole = worldState();
    whole.rooms[1] = room({
      open_tasks: [
        { kind: "housekeeping", task_id: "hk_1", what: "2 towels", status: "pending" },
        { kind: "housekeeping", task_id: "hk_2", what: "room_cleaning", status: "in_progress" },
        { kind: "maintenance", task_id: "mt_1", what: "hvac", status: "pending" },
      ],
      pending_review: {
        approval_id: "apr_1",
        requested_checkout: T("15:00"),
        blocking_reason: "CHANGED:operational",
      },
      latest_update: {
        kind: "task_closed", title: "Towels marked completed", demo_time: T("11:30"),
        status: "sent", skip_reason: null,
      }, // prettier-ignore
    });
    whole.rooms[0] = {
      ...whole.rooms[0],
      latest_update: {
        kind: "review_closed", title: "Late checkout until 13:00 declined", demo_time: T("10:30"),
        status: "skipped", skip_reason: "NO_CONVERSATION",
      }, // prettier-ignore
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.startsWith("/api/operator/approvals")) return json({ approvals: [], next_cursor: null });
        if (url.startsWith("/api/operator/tasks")) return json({ tasks: [], truncated: false });
        return json(whole);
      }),
    );
    render(<Board />);
    const emma = await screen.findByTestId("board-room-305");
    // An occupied room's cleaning state never changes in the demo, so it is not shown; an
    // arriving guest's room shows it, because early check-in needs a ready room.
    expect(emma).not.toHaveTextContent("Needs cleaning");
    expect(screen.getByTestId("board-room-218")).toHaveTextContent("Needs cleaning");
    // One review waits per stay: the latest time is available once it is decided.
    expect(emma).toHaveTextContent("once the waiting review is decided");
    const requests = screen.getByTestId("board-requests-305");
    expect(requests).toHaveTextContent("Late checkout until 15:00: waiting for a manager");
    expect(requests).toHaveTextContent("would not apply now");
    expect(requests).toHaveTextContent("2 towels: waiting for staff");
    expect(requests).toHaveTextContent("Room cleaning: staff working on it");
    expect(requests).toHaveTextContent("Air conditioning or heating: reported");
    expect(screen.getByTestId("board-requests-218")).toHaveTextContent("None");
    // What the hotel last had to tell each guest, and whether the agent did.
    expect(screen.getByTestId("board-update-305")).toHaveTextContent(
      "Agent told Emma: Towels marked completed (11:30)",
    );
    expect(screen.getByTestId("board-update-218")).toHaveTextContent(
      "Not told (no open conversation): Late checkout until 13:00 declined (10:30)",
    );
  });

  it("simulates a change from the cell it changes and marks what moved", async () => {
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<Board onChanged={onChanged} />);
    await user.click(await screen.findByTestId("world-event-arrival_305_early"));
    const post = calls.find((c) => c.url.endsWith("/world/events"))!;
    expect(post.auth).toBeNull(); // the manager's side has no token
    expect(post.body).toMatchObject({ event: "arrival_305_early" });
    expect((post.body as { client_request_id: string }).client_request_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(await screen.findByTestId("world-result")).toHaveTextContent(
      "Simulated: Room 305's next guest arrives early (15:30 instead of 18:00). arrival_305_early effect.",
    );
    // The cause and its effect, side by side: the old value struck through beside the new one.
    expect(await screen.findByTestId("changed-305:next_arrival")).toHaveTextContent("18:00 15:30");
    expect(screen.getByTestId("changed-305:latest")).toHaveTextContent("16:00 14:30");
    expect(screen.queryByTestId("changed-305:checkout")).toBeNull();
    expect(screen.queryByTestId("world-event-arrival_305_early")).toBeNull(); // it happened
  });

  it("explains why a change is not possible instead of offering it", async () => {
    render(<Board />);
    const emma = await screen.findByTestId("board-room-305");
    // The reason is on the screen, not only in a tooltip.
    within(emma).getByText("Take out of service: not possible now (Room 305 is already out of service)");
    expect(screen.queryByTestId("world-event-room_305_out_of_service")).toBeNull();
  });

  it("does not offer a catalog event that has no place on the board", async () => {
    render(<Board />);
    await screen.findByTestId("board-room-305");
    expect(screen.queryByTestId("world-event-cleaning_305_shortened")).toBeNull();
    // The checkout policy is fixed: the board shows it and offers no change to it.
    expect(within(screen.getByTestId("board-policy")).queryByRole("button")).toBeNull();
  });

  it("picks a guest from their row", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<Board onSelect={onSelect} selected="G-001" />);
    expect(await screen.findByTestId("guest-G-001")).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByTestId("guest-G-003"));
    expect(onSelect).toHaveBeenCalledWith("G-003");
  });

  it("offers only the clock steps the server allows", async () => {
    const user = userEvent.setup();
    render(<Board />);
    await user.click(await screen.findByRole("button", { name: "+2 h" }));
    const post = calls.find((c) => c.url.endsWith("/world/clock"))!;
    expect(post.body).toMatchObject({ advance_minutes: 120 });
  });

  it("says what the clock move itself changed", async () => {
    const user = userEvent.setup();
    const base = (globalThis.fetch as unknown as (u: string, i?: RequestInit) => Promise<Response>);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit = {}) => {
        if (url.endsWith("/world/clock")) {
          return json({
            kind: "clock", key: "advance_120", title: "The demo clock moved from 14:00 to 16:00",
            components: [], before: "14:00", after: "16:00", demo_time: T("16:00"), replayed: false,
            side_effects: { expired_reviews: ["apr_1"], cancelled_tasks: ["hk_1", "hk_2"] },
          }); // prettier-ignore
        }
        return base(url, init);
      }),
    );
    render(<Board />);
    await user.click(await screen.findByRole("button", { name: "+2 h" }));
    const result = await screen.findByTestId("world-result");
    expect(result).toHaveTextContent("1 waiting review expired: the requested time came.");
    expect(result).toHaveTextContent("Cleaning hours ended: 2 room cleanings nobody started cancelled.");
    expect(sideEffectText({ expired_reviews: [], cancelled_tasks: [] })).toBe("");
    expect(sideEffectText(undefined)).toBe("");
  });

  it("reads the audit log only when opened, with hotel times", async () => {
    const user = userEvent.setup();
    render(<Board />);
    await screen.findByTestId("board-room-305");
    expect(calls.some((c) => c.url.startsWith("/api/operator/events"))).toBe(false);
    await user.click(screen.getByText("Activity log (audit trail)"));
    const row = await screen.findByTestId("operator-event");
    expect(row).toHaveTextContent("12:30");
    expect(row).toHaveTextContent("world event applied");
  });
});

describe("an API that does not send the board", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.startsWith("/api/operator/approvals")) return json({ approvals: [], next_cursor: null });
        if (url.startsWith("/api/operator/tasks")) return json({ tasks: [], truncated: false });
        const { rooms: _rooms, policy: _policy, ...older } = worldState();
        return json(older); // a server started before the board existed
      }),
    );
  });

  it("says so instead of reading forever", async () => {
    render(<Board />);
    expect(await screen.findByTestId("board-outdated")).toHaveTextContent("restart the API");
    expect(screen.queryByText("Reading the hotel…")).toBeNull();
  });

  it("says so on the tour's room card too", async () => {
    render(<Card />);
    expect(await screen.findByTestId("board-outdated")).toBeInTheDocument();
  });
});

describe("Room card (guided tour)", () => {
  it("shows the story's room read only", async () => {
    render(<Card />);
    const card = await screen.findByTestId("room-card");
    expect(card).toHaveTextContent("Room 305 now · Emma Wilson");
    expect(card).toHaveTextContent("Latest checkout if Emma asked now16:00 with a manager");
    expect(card).toHaveTextContent("move only through the story's steps");
    expect(card).toHaveTextContent("still goes to the agent");
    expect(card).not.toHaveTextContent("cleaning"); // an occupied room's state is not shown
    expect(within(card).queryByRole("button")).toBeNull();
  });
});
