import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Handbook } from "./api";
import PolicyPanel, { topicLines } from "./PolicyPanel";

const handbook: Handbook = {
  source: "Simulated hotel policy",
  hotel_name: "Demo Hotel (fictional)",
  hotel_timezone: "Europe/Istanbul",
  demo_time: "2026-09-22T10:00:00+03:00",
  topics: [
    {
      topic: "facilities",
      available: true,
      data: {
        topic: "facilities",
        venues: [{ name: "Pool", opens_local: "08:00", closes_local: "20:00", notes: "Heated." }],
        booking: "Booked at the front desk.",
      },
    },
    {
      topic: "internet",
      available: true,
      data: {
        topic: "internet",
        wifi_network: "DemoHotel-Guest",
        price: "Free.",
        coverage: "Everywhere.",
        password_location: "On the key-card sleeve.",
      },
    },
    { topic: "parking", available: false, data: null },
  ],
};

afterEach(() => vi.unstubAllGlobals());

describe("PolicyPanel", () => {
  it("shows every stored topic and marks the one the agent read", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(handbook), { status: 200 })),
    );
    render(<PolicyPanel readTopics={["facilities"]} />);
    const pool = await screen.findByTestId("policy-facilities");
    expect(pool).toHaveTextContent("Pool: 08:00–20:00. Heated.");
    expect(within(pool).getByTestId("policy-read-facilities")).toBeInTheDocument();
    expect(screen.getByTestId("policy-internet")).toHaveTextContent("Password: On the key-card sleeve.");
    expect(screen.queryByTestId("policy-read-internet")).toBeNull();
    // A topic with no stored data says so, as the tool does; nothing is filled in.
    expect(screen.getByTestId("policy-parking")).toHaveTextContent("No stored policy");
  });
});

describe("topicLines", () => {
  it("reads only stored values", () => {
    expect(
      topicLines("checkout", {
        standard_checkout_local: "12:00",
        automatic_extension_until_local: "14:00",
        reviewed_extension_until_local: "16:00",
      }),
    ).toEqual([
      "Standard checkout 12:00.",
      "Later checkout is automatic until 14:00 and needs a manager until 16:00, if the room's next arrival allows it.",
    ]);
    expect(topicLines("unknown_topic", { a: 1 })).toEqual([]);
  });
});
