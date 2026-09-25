import { expect, type Page, test } from "@playwright/test";
import { API } from "./ports";

// Explore mode's hotel board, and the guided
// tour's own read-only layout (story | chat | the story's room and its reviews). Offline
// adapter: this proves wiring and state, not real-model behaviour.

test.beforeEach(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-checkouts`);
  await request.post(`${API}/__e2e__/restore-world`);
});

test.afterAll(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-world`);
  await request.post(`${API}/__e2e__/restore-checkouts`);
});

async function scenarios(page: Page, id: string) {
  await page.goto("/");
  await page.getByTestId("mode-guided").click();
  await page.getByTestId(`scenario-${id}`).getByRole("button", { name: /^Start/ }).click();
  await expect(page.getByTestId("scenario-guide")).toBeVisible();
  await expect(page.getByTestId("room-checkout")).toHaveText("12:00");
  await expect(page.getByTestId("demo-time")).toContainText("2026-09-22 10:00"); // a clean hotel
}

function step(page: Page, i: number) {
  return page.getByTestId("scenario-step").nth(i);
}

async function act(page: Page, name: string, i: number) {
  await step(page, i).getByRole("button", { name }).click();
  await expect(step(page, i)).toHaveAttribute("data-state", "done", { timeout: 15_000 });
}

/** A hotel step has no button: it is ticked by the turn the hotel starts. */
async function hotelSpeaks(page: Page, i: number) {
  await expect(step(page, i).getByRole("button")).toHaveCount(0);
  await expect(step(page, i)).toHaveAttribute("data-state", "done", { timeout: 15_000 });
}

function hotelTurns(page: Page) {
  return page.getByTestId("turn").filter({ has: page.getByTestId("hotel-update") });
}

test.describe("Hotel board and guided tour (offline adapter)", () => {
  test("a waiting review meets a changed hotel: approve applies nothing, then 15:00 is refused", async ({
    page,
  }) => {
    await scenarios(page, "stale");

    await act(page, "Send as Emma", 0);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "Emma asked for 15:00. The agent filed a manager review.",
    );
    // The tour is read only: no board, no tabs, no hand-driven decision.
    await expect(page.getByTestId("hotel-board")).toBeHidden();
    await expect(page.getByTestId("tab-manager")).toBeHidden();
    const card = page.getByTestId("operator-item");
    await expect(card).toHaveCount(1);
    await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);

    await act(page, "Make it happen", 1);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "Simulated hotel change: Room 305's next guest arrives early",
    );
    // The room card shows the cause and its effect: the arrival moved, the latest checkout fell.
    await expect(page.getByTestId("changed-305:next_arrival")).toHaveText("18:00 15:30");
    await expect(page.getByTestId("changed-305:latest")).toHaveText("16:00 14:30");
    // And the waiting review says, in plain words, that an approve would change nothing.
    await expect(card.getByTestId("freshness")).toContainText("If you approve now, nothing will change");
    await expect(card.getByTestId("freshness")).toContainText("What changed in the hotel since Emma asked");

    await act(page, "Approve Emma's review", 2);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "You approved, but nothing was changed",
    );

    await act(page, "Send as Emma", 3);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "the room must be ready for its next arrival",
    );
    await expect(page.getByTestId("scenario-done")).toContainText("In this system");
    await expect(page.getByTestId("room-checkout")).toHaveText("12:00");

    // Leaving the tour keeps the hotel as the story left it.
    await page.getByRole("button", { name: "Keep exploring from here" }).click();
    await expect(page.getByTestId("board-room-305")).toContainText("15:30");
    await expect(page.getByTestId("demo-time")).toContainText("2026-09-22 10:00");
  });

  test("the hotel clock keeps moving: a review whose time arrives expires by itself", async ({
    page,
  }) => {
    await scenarios(page, "clock");
    await act(page, "Send as Emma", 0);
    await act(page, "Move the hotel clock to 14:30", 1);
    await expect(page.getByTestId("demo-time")).toContainText("2026-09-22 14:30");
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "Emma's review for 14:30 expired",
    );
    await expect(page.getByTestId("scenario-done")).toBeVisible();
    await expect(page.getByTestId("operator-item")).toHaveCount(0);
    await expect(page.getByTestId("decided-item")).toContainText("expired on the hotel clock");
    await expect(page.getByTestId("room-checkout")).toHaveText("12:00");
  });

  test("the hotel offers, the guest asks: the agent writes first, and only Daniel's yes files a review", async ({
    page,
  }) => {
    await scenarios(page, "offer");
    await act(page, "Send as Daniel", 0);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "Daniel asked for 14:30. The checkout rules refused it",
    );
    await act(page, "Make it happen", 1);

    await hotelSpeaks(page, 2);
    const offer = hotelTurns(page).first();
    await expect(offer.getByTestId("hotel-update")).toContainText(
      "Checkout at 14:30 may now be possible",
    );
    await expect(offer.getByTestId("agent-reply")).toContainText("Shall I request it for you?");
    // The hotel's turn could only read: it filed nothing.
    await expect(page.getByTestId("operator-item")).toHaveCount(0);

    await act(page, "Send as Daniel", 3);
    await expect(page.getByTestId("operator-item")).toHaveCount(1);
    await act(page, "Approve Daniel's review", 4);
    await hotelSpeaks(page, 5);
    await expect(hotelTurns(page)).toHaveCount(2);
    await expect(hotelTurns(page).last().getByTestId("agent-reply")).toContainText(
      "a manager approved your late checkout; your checkout is now 14:30.",
    );
    await expect(page.getByTestId("scenario-done")).toContainText("In this system");
    // The room card marks the value the approve moved: 12:00 before, 14:30 now.
    await expect(page.getByTestId("room-checkout")).toHaveText("12:00 14:30");
  });

  test("words are not authority: a claimed manager files only a review, and the other lines change nothing", async ({
    page,
  }) => {
    await scenarios(page, "authority");
    await act(page, "Send as Emma", 0);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "Emma asked for 15:30. The agent filed a manager review.",
    );
    await expect(page.getByTestId("room-checkout")).toHaveText("12:00");

    await act(page, "Send as Emma", 1);
    await act(page, "Send as Emma", 2);
    await expect(page.getByTestId("scenario-timeline")).toContainText(
      "none of them runs SQL or edits a record directly",
    );
    await expect(page.getByTestId("operator-item")).toHaveCount(1); // still only the one review

    await act(page, "Reject Emma's review", 3);
    await hotelSpeaks(page, 4);
    await expect(hotelTurns(page).getByTestId("agent-reply")).toContainText(
      "A manager declined your late checkout request for 15:30; your checkout stays at 12:00.",
    );
    await expect(page.getByTestId("scenario-done")).toContainText("In this system");
    await expect(page.getByTestId("room-checkout")).toHaveText("12:00");

    // The SQL line named Daniel's room: his stay is still there, untouched.
    await page.getByRole("button", { name: "Keep exploring from here" }).click();
    await expect(page.getByTestId("board-room-412")).toContainText("Daniel Kim");
  });

  test("restart and start over put the whole hotel back", async ({ page }) => {
    await scenarios(page, "clock");
    await act(page, "Send as Emma", 0);
    await act(page, "Move the hotel clock to 14:30", 1);
    await expect(page.getByTestId("demo-time")).toContainText("14:30");

    await page.getByTestId("scenario-guide").getByRole("button", { name: "Restart" }).click();
    await expect(page.getByTestId("demo-time")).toContainText("2026-09-22 10:00");
    await expect(step(page, 0)).toHaveAttribute("data-state", "current");
    await act(page, "Send as Emma", 0);
    await expect(page.getByTestId("operator-item")).toHaveCount(1);

    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByTestId("start-over").click();
    await expect(page.getByTestId("guest-G-001")).toBeVisible(); // back to Explore's board
    await expect(page.getByTestId("mode-explore")).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("guest-context")).toBeHidden();
    await expect(page.getByTestId("manager-count")).toBeHidden();
    await expect(page.getByTestId("demo-time")).toContainText("2026-09-22 10:00");
  });

  test("the hotel board changes the simulated hotel by hand and shows the effect at once", async ({
    page,
  }) => {
    await page.goto("/");
    const daniel = page.getByTestId("board-room-412");
    await expect(daniel).toContainText("15:00");
    await expect(daniel).toContainText("14:00automatic");
    await page.getByTestId("world-event-arrival_412_cancelled").click();
    await expect(page.getByTestId("world-result")).toContainText(
      "Simulated: Room 412's next arrival is cancelled.",
    );
    await expect(page.getByTestId("changed-412:next_arrival")).toHaveText("15:00 None");
    await expect(page.getByTestId("changed-412:latest")).toHaveText("14:00 16:00");
    await expect(page.getByTestId("world-event-arrival_412_cancelled")).toHaveCount(0);
    // The shortened cleaning window stays in the catalog but is not offered on the board.
    await expect(page.getByTestId("world-event-cleaning_305_shortened")).toHaveCount(0);
  });
});
