import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { OperatorApproval } from "./api";
import { blockingText, FreshnessDetail } from "./WorldPanel";

describe("blockingText", () => {
  it("reads the server's reason codes in plain language", () => {
    expect(blockingText("CHANGED:operational,room")).toBe(
      "hotel data changed since the request (This room's bookings and plans, Room)",
    );
    expect(blockingText("NO_LONGER_ELIGIBLE:INVALID_TIME")).toBe(
      "the policy no longer allows it: the requested time has already passed on the hotel clock",
    );
    expect(blockingText(null)).toBeNull();
  });
});

describe("FreshnessDetail", () => {
  const base: OperatorApproval = {
    approval_id: "apr_1",
    status: "pending",
    version: 1,
    guest_name: "Emma Wilson",
    room_number: "305",
    reservation_reference: "R-1042",
    requested_checkout_local: "2026-09-22T15:00:00+03:00",
    current_checkout_local: "2026-09-22T12:00:00+03:00",
    guest_reason: null,
    created_at: null,
    expires_at: null,
    fresh: false,
    human_decision: null,
    decided_by: null,
    decided_at: null,
    status_reason: null,
    observed_versions: { reservation: 1, room: 1, policy: 1, room_day_plan: 1, operational: 1 },
    current_versions: { reservation: 1, room: 1, policy: 1, room_day_plan: 1, operational: 2 },
    changed_components: ["operational"],
    blocking_reason: "CHANGED:operational",
    world_changes: [
      {
        kind: "event",
        key: "arrival_305_early",
        title: "Room 305's next guest arrives early (15:30 instead of 18:00)",
        components: ["operational"],
        before: "next arrival 18:00",
        after: "next arrival 15:30",
        wall_time: "2026-09-22T07:05:00+00:00",
        demo_time: "2026-09-22T10:00:00+03:00",
      },
    ],
  };

  it("leads with the plain verdict and what changed, and keeps the versions as detail", async () => {
    const user = userEvent.setup();
    render(<FreshnessDetail item={base} />);
    expect(
      screen.getByText(
        /If you approve now, nothing will change: hotel data changed since the request \(This room's bookings and plans\)\. Emma would have to ask again/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("What changed in the hotel since Emma asked")).toBeInTheDocument();
    expect(screen.getByText(/Room 305's next guest arrives early/)).toHaveTextContent(
      "hotel time 10:00",
    );
    await user.click(screen.getByText("How this is checked (version comparison)"));
    const row = screen.getByRole("row", { name: /This room's bookings/ });
    expect(row).toHaveTextContent("v1v2changed");
    expect(screen.getByRole("row", { name: /Room/ })).toHaveTextContent("same");
  });

  it("says an unchanged review would apply", () => {
    render(
      <FreshnessDetail
        item={{
          ...base,
          fresh: true,
          current_versions: base.observed_versions,
          changed_components: [],
          blocking_reason: null,
          world_changes: [],
        }}
      />,
    );
    expect(
      screen.getByText("If you approve now, the checkout changes to 15:00 (hotel time)."),
    ).toBeInTheDocument();
  });
});
