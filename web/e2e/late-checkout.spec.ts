import { expect, test } from "@playwright/test";

import { API } from "./ports";

// Checkout writes change fixture state; restore it so journeys do not depend on file order.
test.beforeEach(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-checkouts`);
});

test.describe("Late checkout (offline adapter)", () => {
  test("Emma's 14:00 request applies once and the context panel refreshes", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
    await page.getByLabel("Message").fill("Please extend my checkout to 14:00");
    await page.getByLabel("Message").press("Enter");
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toHaveCount(1);
    await expect(receipt).toContainText("Checkout changed");
    await expect(receipt).toContainText("2026-09-22 12:00 → 2026-09-22 14:00");
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 14:00");
    await expect(page.getByTestId("activity-item")).toContainText("request_late_checkout");
  });

  test("Daniel's 15:00 request is denied and nothing changes", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-002").click();
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
    await page.getByLabel("Message").fill("Please extend my checkout to 15:00");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("denied");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  });

  test("Emma's 15:00 request waits for review and changes nothing yet", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("Please extend my checkout to 15:00");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("pending_approval");
    await expect(page.getByTestId("receipt")).toContainText("Late checkout review");
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  });
});
