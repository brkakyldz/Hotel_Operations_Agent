import { expect, test } from "@playwright/test";

import { API } from "./ports";

test.beforeEach(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-checkouts`);
});

test.describe("Early check-in (offline adapter)", () => {
  test("Sofia gets arrival examples, asks how early, then checks in early once", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-003").click();
    await expect(page.getByTestId("check-in-time")).toContainText("2026-09-22 15:00");
    const examples = page.getByRole("group", { name: "Example requests" });
    await expect(examples).not.toContainText("towels");

    // Asking is advisory: nothing changes, and the earliest possible time is offered.
    await examples.getByRole("button", { name: "How early could I check in today?" }).click();
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("Earliest possible: 12:00");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
    await expect(page.getByTestId("check-in-time")).toContainText("2026-09-22 15:00");

    await page.getByLabel("Message").fill("Can I check in at 13:00?");
    await page.getByLabel("Message").press("Enter");
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toContainText("Check-in moved earlier");
    await expect(receipt).toContainText("2026-09-22 15:00 → 2026-09-22 13:00");
    await expect(page.getByTestId("check-in-time")).toContainText("2026-09-22 13:00");
  });

  test("a checked-in guest is told early check-in does not apply", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("Can I check in at 13:00?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("NOT_ARRIVING");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
  });
});
