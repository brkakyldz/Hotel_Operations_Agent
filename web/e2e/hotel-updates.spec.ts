import { expect, type Page, test } from "@playwright/test";
import { API } from "./ports";

// The agent writes first when the hotel changes something that concerns the
// guest. Each journey starts from a clean hotel so earlier journeys' reviews and tasks do not
// add turns of their own. Offline adapter: this proves wiring and state, not model quality.

test.beforeEach(async ({ request }) => {
  const r = await request.post(`${API}/api/operator/world/reset`, { data: { confirm: true } });
  expect(r.ok()).toBeTruthy();
});

test.afterAll(async ({ request }) => {
  await request.post(`${API}/api/operator/world/reset`, { data: { confirm: true } });
});

async function asEmma(page: Page, message: string) {
  await page.goto("/");
  await page.getByTestId("guest-G-001").click();
  const input = page.getByLabel("Message");
  await input.fill(message);
  await input.press("Enter");
  await expect(page.getByTestId("turn-status").first()).toContainText("Completed", {
    timeout: 15_000,
  });
}

async function clock(page: Page, ...steps: string[]) {
  for (const step of steps) {
    const button = page.getByRole("button", { name: step, exact: true });
    await expect(button).toBeEnabled();
    await button.click();
    await expect(page.getByTestId("world-result")).toContainText("Simulated");
  }
}

function hotelTurn(page: Page) {
  return page.getByTestId("turn").filter({ has: page.getByTestId("hotel-update") });
}

test.describe("Hotel updates (offline adapter)", () => {
  test("when the hotel clock reaches the requested time, the agent writes first", async ({ page }) => {
    await asEmma(page, "Can I check out at 14:30?");
    await expect(page.getByTestId("board-requests-305")).toContainText(
      "Late checkout until 14:30: waiting for a manager",
    );
    await clock(page, "+2 h", "+2 h");
    await expect(hotelTurn(page)).toHaveCount(0); // 14:00: still waiting, nothing to say
    await clock(page, "+30 min");

    const turn = hotelTurn(page);
    await expect(turn.getByTestId("hotel-update")).toContainText("Hotel update · 14:30");
    await expect(turn.getByTestId("hotel-update")).toContainText(
      "Late checkout review for 14:30 expired",
    );
    await expect(turn.getByTestId("agent-reply")).toContainText(
      "Your late checkout request for 14:30 expired before a manager decided; your checkout stays at 12:00.",
      { timeout: 15_000 },
    );
    // The agent re-read the facts before it wrote, and it only read.
    await expect(turn.getByTestId("steps")).toContainText("2 tool steps");
    await expect(page.getByTestId("board-update-305")).toContainText(
      "Agent told Emma: Late checkout review for 14:30 expired (14:30)",
    );
    await expect(page.getByTestId("board-requests-305")).not.toContainText("waiting for a manager");
    // The guest can answer as soon as the hotel's turn has finished.
    await expect(page.getByLabel("Message")).toBeEnabled();
  });

  test("a manager's decision reaches the guest without their asking", async ({ page }) => {
    await asEmma(page, "Can I check out at 15:00?");
    await page.getByTestId("tab-manager").click();
    await page.getByTestId("operator-item").getByRole("button", { name: "Reject" }).click();

    const turn = hotelTurn(page);
    await expect(turn.getByTestId("hotel-update")).toContainText(
      "Late checkout until 15:00 declined",
    );
    await expect(turn.getByTestId("agent-reply")).toContainText(
      "A manager declined your late checkout request for 15:00; your checkout stays at 12:00.",
      { timeout: 15_000 },
    );
  });

  test("the end of cleaning hours cancels a cleaning nobody started, on the board and in the chat", async ({
    page,
  }) => {
    await asEmma(page, "Please clean my room.");
    await expect(page.getByTestId("board-requests-305")).toContainText("Room cleaning: waiting for staff");
    await clock(page, "+2 h", "+2 h", "+2 h"); // 16:00

    const turn = hotelTurn(page);
    await expect(turn.getByTestId("hotel-update")).toContainText("Hotel update · 16:00");
    await expect(turn.getByTestId("hotel-update")).toContainText(
      "Room cleaning cancelled: cleaning hours ended",
    );
    await expect(turn.getByTestId("agent-reply")).toContainText(
      "Cleaning hours ended before your room cleaning",
      { timeout: 15_000 },
    );
    await expect(page.getByTestId("board-requests-305")).toContainText("None");
    await expect(page.getByTestId("board-update-305")).toContainText(
      "Agent told Emma: Room cleaning cancelled: cleaning hours ended (16:00)",
    );
    await expect(page.getByTestId("world-result")).toContainText(
      "Cleaning hours ended: 1 room cleaning nobody started cancelled.",
    );
  });
});
