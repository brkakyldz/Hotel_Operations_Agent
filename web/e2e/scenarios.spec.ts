import { expect, type Page, test } from "@playwright/test";

// Checkout outcomes around a review: no_change, stale and expired reviews, plus the
// mobile-width layout. Offline adapter, not live
// evidence. The stale condition is injected through a labelled test-only route; expiry is
// the real hotel clock moving.
import { API } from "./ports";

test.beforeEach(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-checkouts`);
  await request.post(`${API}/__e2e__/restore-world`); // clock at 10:00, no review left over
});

test.afterAll(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-world`);
});

async function ask(page: Page, guestId: string, message: string) {
  await page.goto("/");
  await page.getByTestId(`guest-${guestId}`).click();
  await expect(page.getByTestId("guest-context")).toBeVisible();
  await page.getByLabel("Message").fill(message);
  await page.getByLabel("Message").press("Enter");
  await expect(page.getByTestId("turn-status").last()).toContainText("Completed");
}

async function requestReview(page: Page): Promise<string> {
  await ask(page, "G-001", "Please extend my checkout to 15:00");
  const receipt = page.getByTestId("receipt");
  await expect(page.getByTestId("receipt-status")).toContainText("Pending manager review");
  return (await receipt.locator("code").getAttribute("title"))!;
}

function reviewCard(page: Page, approvalId: string) {
  return page.locator(`[data-testid="operator-item"][data-id="${approvalId}"]`);
}

test.describe("Review outcomes and mobile layout (offline adapter)", () => {
  test("an earlier checkout is no_change and nothing is shortened", async ({ page }) => {
    await ask(page, "G-001", "Please change my checkout to 11:00");
    await expect(page.getByTestId("agent-reply")).toContainText("no_change");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  });

  test("a data change makes an approved review stale; nothing executes", async ({
    page,
    request,
  }) => {
    const approvalId = await requestReview(page);
    await request.post(`${API}/__e2e__/change-operational-data`); // injected condition
    await page.getByTestId("tab-manager").click();
    const item = reviewCard(page, approvalId);
    await expect(item).toContainText("hotel data changed since the request");
    await item.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByTestId("operator-result")).toContainText(
      "Approve recorded, but hotel data changed",
    );
    await page.getByTestId("tab-requests").click();
    const review = page
      .getByTestId("request-item")
      .filter({ has: page.locator(`code[title="${approvalId}"]`) });
    await expect(review).toContainText("Hotel data changed before a decision — checkout not changed");
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  });

  test("a review the hotel clock overtakes expires and changes nothing", async ({ page }) => {
    const approvalId = await requestReview(page); // until 15:00
    await page.getByTestId("tab-manager").click();
    await expect(reviewCard(page, approvalId)).toContainText("12:00 → 15:00");
    for (const step of ["+2 h", "+2 h", "+1 h"]) {
      await page.getByTestId("board-clock").getByRole("button", { name: step }).click();
      await expect(page.getByTestId("world-result")).toBeVisible();
    }
    await expect(page.getByTestId("demo-time")).toContainText("2026-09-22 15:00");
    await page.getByTestId("tab-manager").click();
    await expect(reviewCard(page, approvalId)).toHaveCount(0);
    await expect(
      page.locator(`[data-testid="decided-item"][data-id="${approvalId}"]`),
    ).toContainText("expired on the hotel clock");
    await page.getByTestId("tab-requests").click();
    const review = page
      .getByTestId("request-item")
      .filter({ has: page.locator(`code[title="${approvalId}"]`) });
    await expect(review).toContainText("Expired before a decision — checkout not changed");
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  });

  test("mobile width: chat first, context below, no horizontal scrolling", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await ask(page, "G-002", "When is breakfast?");
    await expect(page.getByTestId("agent-reply")).toContainText("Breakfast is served");
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
    const chat = await page.locator("section.chat").boundingBox();
    const side = await page.locator("aside.side").boundingBox();
    expect(chat && side && side.y >= chat.y + chat.height - 1).toBeTruthy();
    await expect(page.getByLabel("Message")).toBeVisible();
    await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
  });

  test("mobile width: the page follows each reply down to the message box", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await ask(page, "G-002", "When is breakfast?");
    await expect(page.getByTestId("agent-reply")).toContainText("Breakfast is served");
    await expect(page.getByTestId("agent-reply")).toBeInViewport();
    await expect(page.getByLabel("Message")).toBeInViewport();
    await page.getByLabel("Message").fill("And is there a gym?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toHaveCount(2);
    await expect(page.getByTestId("agent-reply").last()).toBeInViewport();
    await expect(page.getByLabel("Message")).toBeInViewport();
  });
});
