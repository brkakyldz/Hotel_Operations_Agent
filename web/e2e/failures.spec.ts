import { expect, test } from "@playwright/test";
import { API } from "./ports";

// Fault-injection journeys (offline adapter): the UI must show what actually happened.
test.describe("Failure outcomes (offline adapter)", () => {
  test("a committed action survives a failed reply and a reload", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-002").click();
    await expect(page.getByTestId("guest-context")).toContainText("Daniel Kim");
    await page.getByLabel("Message").fill("one towel please, simulate narration failure");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("run-error")).toBeVisible();
    await expect(page.getByTestId("committed-warning")).toBeVisible();
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toContainText("towels × 1");
    await expect(page.getByTestId("agent-reply")).toHaveCount(0); // no fabricated answer
    const taskId = (await receipt.locator("code").getAttribute("title"))!;
    await expect(page.getByTestId("request-item").first().locator(`code[title="${taskId}"]`)).toHaveCount(1);
    await page.reload();
    await expect(page.getByTestId("committed-warning")).toBeVisible();
    await expect(page.getByTestId("receipt").locator(`code[title="${taskId}"]`)).toHaveCount(1);
    await expect(page.getByTestId("request-item").first().locator(`code[title="${taskId}"]`)).toHaveCount(1);
  });

  test("a backend restart mid-run interrupts it, keeps the committed task and never replays it", async ({
    page,
    request,
  }) => {

    let chatPosts = 0;
    page.on("request", (r) => {
      if (r.url().endsWith("/api/chat") && r.method() === "POST") chatPosts += 1;
    });
    // One reload: if the dev proxy still answers 502 from a stale connection, the UI
    // itself must keep the stored session and retry (a guest would not reload twice).
    const reloadAndRestore = async () => {
      await page.reload();
      await expect(page.getByTestId("turn-status")).toContainText("Interrupted", { timeout: 20_000 });
    };
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
    await page.waitForLoadState("networkidle");
    const requestsBefore = await page.getByTestId("request-item").count();
    await page.getByLabel("Message").fill("one towel please, simulate process restart");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("turn-status")).toContainText("Working");

    // The run has committed its task and is still narrating when the backend stops.
    const restart = await (await request.post(`${API}/__e2e__/restart`)).json();
    expect(restart).toEqual({ restarting: true, run_in_flight: true });
    await expect.poll(async () => (await request.get(`${API}/api/health`).catch(() => null))?.ok(), {
      timeout: 20_000,
    }).toBe(true);

    await reloadAndRestore();
    await expect(page.getByTestId("committed-warning")).toBeVisible();
    await expect(page.getByTestId("agent-reply")).toHaveCount(0);
    const receipt = page.getByTestId("receipt");
    await expect(receipt).toContainText("towels × 1");
    const taskId = (await receipt.locator("code").getAttribute("title"))!;
    await page.waitForTimeout(500); // a replay would have created a second task by now
    await reloadAndRestore();
    await expect(page.getByTestId("request-item")).toHaveCount(requestsBefore + 1);
    await expect(page.getByTestId("request-item").first().locator(`code[title="${taskId}"]`)).toHaveCount(1);
    expect(chatPosts).toBe(1);
  });
});
