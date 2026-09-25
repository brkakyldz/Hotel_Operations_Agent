import { expect, type Page, test } from "@playwright/test";

import { API } from "./ports";

test.beforeEach(async ({ request }) => {
  await request.post(`${API}/__e2e__/restore-checkouts`);
  await request.post(`${API}/__e2e__/restore-world`); // no review left waiting by another spec
});

async function requestReview(page: Page) {
  await page.goto("/");
  await page.getByTestId("guest-G-001").click();
  await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  await page.getByLabel("Message").fill("Please extend my checkout to 15:00");
  await page.getByLabel("Message").press("Enter");
  const receipt = page.getByTestId("receipt");
  await expect(receipt).toContainText("Late checkout review");
  await expect(page.getByTestId("receipt-status")).toContainText("Pending manager review");
  await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
  return (await receipt.locator("code").getAttribute("title"))!;
}

async function decideAsManager(page: Page, approvalId: string, action: "Approve" | "Reject") {
  // The manager's desk is a tab on the same screen, with no sign-in.
  await page.getByTestId("tab-manager").click();
  const item = page.locator(`[data-testid="operator-item"][data-id="${approvalId}"]`);
  await expect(item).toContainText("Emma Wilson");
  await item.getByRole("button", { name: action }).click();
  await expect(page.getByTestId("operator-result")).toBeVisible();
  const result = (await page.getByTestId("operator-result").textContent())!;
  await page.getByTestId("tab-requests").click();
  return result;
}

test.describe("Human approval (offline adapter)", () => {
  test("the manager's approval changes the checkout once, on the same screen", async ({ page }) => {
    const approvalId = await requestReview(page);
    await expect(page.getByTestId("manager-count")).toBeVisible();
    const result = await decideAsManager(page, approvalId, "Approve");
    expect(result).toContain("Emma's checkout is now 15:00");
    // The guest card beside the desk shows the effect without a reload or a second page.
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 15:00");
    const review = page.getByTestId("request-item").filter({ has: page.locator(`code[title="${approvalId}"]`) });
    await expect(review).toContainText("Approved by a manager");
  });

  test("the manager's rejection leaves the checkout unchanged", async ({ page }) => {
    const approvalId = await requestReview(page);
    const result = await decideAsManager(page, approvalId, "Reject");
    expect(result).toContain("not changed");
    await expect(page.getByTestId("checkout-time")).toContainText("2026-09-22 12:00");
    const review = page.getByTestId("request-item").filter({ has: page.locator(`code[title="${approvalId}"]`) });
    await expect(review).toContainText("Declined by a manager");
  });
});
