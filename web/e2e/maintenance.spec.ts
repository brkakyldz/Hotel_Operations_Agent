import { expect, test } from "@playwright/test";

// Relative assertions only: the E2E database persists across tests in one run.
test.describe("Maintenance (offline adapter)", () => {
  test("a leak report becomes one pending maintenance receipt that survives reload", async ({
    page,
  }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-002").click();
    await expect(page.getByTestId("guest-context")).toContainText("Daniel Kim");
    await page.getByLabel("Message").fill("The shower in my bathroom is leaking");
    await page.getByLabel("Message").press("Enter");
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toHaveCount(1);
    await expect(receipt).toContainText("Maintenance report");
    await expect(receipt).toContainText("Plumbing");
    await expect(page.getByTestId("receipt-status")).toContainText("not yet repaired");
    const taskId = (await receipt.locator("code").getAttribute("title"))!;
    await expect(page.getByTestId("agent-reply")).toContainText(taskId);
    const newest = page.getByTestId("request-item").first();
    await expect(newest.locator(`code[title="${taskId}"]`)).toHaveCount(1);
    await expect(newest).toContainText("The shower in my bathroom is leaking");
    await expect(newest).toContainText("not yet repaired");
    await page.reload();
    await expect(page.getByTestId("request-item").first().locator(`code[title="${taskId}"]`)).toHaveCount(1);
    await expect(page.getByTestId("request-item").first()).toContainText("Pending");
  });

  test("an arriving guest cannot report an in-room issue", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-003").click();
    await expect(page.getByTestId("guest-context")).toContainText("Sofia Rossi");
    await page.getByLabel("Message").fill("The air conditioning is too loud");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("NOT_CHECKED_IN");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
    await expect(page.getByTestId("request-item")).toHaveCount(0);
  });
});
