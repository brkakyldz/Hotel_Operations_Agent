import { expect, test } from "@playwright/test";


// Each test uses its own guest session; the E2E database persists across tests in a run,
// so assertions are relative (new receipt IDs), never absolute counts across tests.
test.describe("Policy and housekeeping (offline adapter)", () => {
  test("breakfast answer creates no receipt", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-002").click();
    await page.getByLabel("Message").fill("When is breakfast served?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("07:00-10:30");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
    await expect(page.getByTestId("activity-item")).toContainText("get_hotel_policy");
  });

  test("two towels produce one pending receipt and a pending request", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
    await page.getByLabel("Message").fill("Could I get two towels?");
    await page.getByLabel("Message").press("Enter");
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toHaveCount(1);
    await expect(receipt).toContainText("towels × 2");
    await expect(page.getByTestId("receipt-status")).toContainText("Pending");
    await expect(receipt).not.toContainText(/delivered|completed/i);
    const taskId = (await receipt.locator("code").getAttribute("title"))!;
    await expect(page.getByTestId("agent-reply")).toContainText(taskId);
    // Newest first: the just-created task heads the reservation's request list.
    await expect(page.getByTestId("request-item").first().locator(`code[title="${taskId}"]`)).toHaveCount(1);
    await expect(page.getByTestId("request-item").first()).toContainText("Pending");
  });

  test("simulated staff move the task on the manager's board and the guest sees it", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("Could I get two towels?");
    await page.getByLabel("Message").press("Enter");
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toContainText("towels × 2");
    const taskId = (await receipt.locator("code").getAttribute("title"))!;

    await page.getByTestId("tab-manager").click();
    const card = page.locator(`[data-testid="operator-task"][data-id="${taskId}"]`);
    await expect(card).toContainText("Emma Wilson");
    await card.getByRole("button", { name: /^Start/ }).click();
    await expect(card).toContainText("In progress");
    await card.getByRole("button", { name: /^Mark completed/ }).click();
    await expect(page.getByTestId("operator-result")).toContainText("Completed");
    // The hotel tells Emma on its own, in a turn marked as the hotel's.
    await expect(page.getByTestId("hotel-update")).toContainText("Towels marked completed");
    await page.getByTestId("hotel-board").getByText("Activity log (audit trail)").click();
    await expect(
      page.getByTestId("operator-event").filter({ hasText: "task status changed" }).first(),
    ).toContainText("→ completed");

    await page.getByTestId("tab-requests").click();
    const request = page.getByTestId("request-item").filter({ has: page.locator(`code[title="${taskId}"]`) });
    await expect(request).toContainText("Completed");
  });

  test("arriving guest is refused and another guest's requests never show", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("one pillow please");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("receipt")).toHaveCount(1);
    await page.getByTestId("guest-G-003").click();
    await expect(page.getByTestId("guest-context")).toContainText("Sofia Rossi");
    await expect(page.getByTestId("request-item")).toHaveCount(0);
    await page.getByLabel("Message").fill("two towels please");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("NOT_CHECKED_IN");
    await expect(page.getByTestId("receipt")).toHaveCount(0);
    await expect(page.getByTestId("request-item")).toHaveCount(0);
  });
});
