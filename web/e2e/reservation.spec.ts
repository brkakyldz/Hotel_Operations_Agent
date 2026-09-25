import { expect, test } from "@playwright/test";
import { API } from "./ports";

test.describe("Reservation read journey (offline adapter)", () => {
  test("Emma asks, sees running then completed state, reply and actual tool activity", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("simulation-label")).toContainText("fictional");
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
    await expect(page.getByTestId("demo-time")).toHaveText("2026-09-22 10:00");
    const box = page.getByLabel("Message");
    await expect(box).toBeFocused();
    await box.fill("When is my checkout?");
    await box.press("Enter");
    await expect(page.getByTestId("turn-status").first()).toContainText(/Sending|Queued|Working|Completed/);
    await expect(page.getByTestId("agent-reply")).toContainText("R-1042");
    await expect(page.getByTestId("agent-reply")).toContainText("2026-09-22T12:00:00+03:00");
    await expect(page.getByTestId("turn-status")).toContainText("Completed");
    const activity = page.getByTestId("activity-item");
    await expect(activity).toHaveCount(1);
    await expect(activity).toContainText("get_my_reservation");
    await expect(activity).toContainText("completed");
  });

  test("each guest keeps their own chat: switching shows Daniel's, returning restores Emma's", async ({
    page,
  }) => {
    let sessionPosts = 0;
    page.on("request", (r) => {
      if (r.url().endsWith("/api/demo/sessions") && r.method() === "POST") sessionPosts += 1;
    });
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("my room?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("R-1042");
    await page.getByTestId("guest-G-002").click();
    await expect(page.getByTestId("guest-context")).toContainText("Daniel Kim");
    await expect(page.getByTestId("turn")).toHaveCount(0);
    await expect(page.getByTestId("activity-item")).toHaveCount(0);
    await page.getByLabel("Message").fill("my room?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("R-1088");
    await expect(page.getByTestId("agent-reply")).not.toContainText("R-1042");
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
    await expect(page.getByTestId("turn")).toHaveCount(1);
    await expect(page.getByTestId("agent-reply")).toContainText("R-1042");
    await page.getByTestId("guest-G-001").click(); // already on screen: nothing happens
    await expect(page.getByTestId("turn")).toHaveCount(1);
    expect(sessionPosts).toBe(2);
  });

  test("Sofia is shown as arriving and can read her reservation", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-003").click();
    await expect(page.getByTestId("guest-context")).toContainText("Arriving today");
    await page.getByLabel("Message").fill("Rezervasyonum ne zaman?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("confirmed");
  });

  test("keyboard only: select a guest and send", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("guest-G-002")).toBeVisible();
    await page.getByTestId("guest-G-002").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("guest-context")).toContainText("Daniel Kim");
    await expect(page.getByLabel("Message")).toBeFocused();
    await page.keyboard.type("checkout?");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("R-1088");
  });

  test("progress streams over SSE with the capability in a header, never the URL", async ({ page }) => {
    const streams: { url: string; auth: string | null; type: string | null }[] = [];
    page.on("response", async (r) => {
      if (r.url().includes("/events")) {
        streams.push({
          url: r.url(),
          auth: await r.request().headerValue("authorization"),
          type: r.headers()["content-type"] ?? null,
        });
      }
    });
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("When is my checkout?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("R-1042");
    await expect(page.getByTestId("steps")).toContainText("1 tool step");
    expect(streams.length).toBeGreaterThan(0);
    expect(streams[0].type).toContain("text/event-stream");
    expect(streams[0].auth).toMatch(/^Bearer /);
    const token = (streams[0].auth ?? "").slice("Bearer ".length);
    expect(streams[0].url).not.toContain(token);
  });

  test("reload restores the transcript by polling, without resubmitting", async ({ page }) => {
    let chatPosts = 0;
    page.on("request", (r) => {
      if (r.url().endsWith("/api/chat") && r.method() === "POST") chatPosts += 1;
    });
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("checkout?");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("agent-reply")).toContainText("R-1042");
    await page.reload();
    await expect(page.getByTestId("agent-reply")).toContainText("R-1042");
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
    expect(chatPosts).toBe(1);
  });

  test("provider failure is shown as an error, never a fabricated answer", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await page.getByLabel("Message").fill("simulate provider outage please");
    await page.getByLabel("Message").press("Enter");
    await expect(page.getByTestId("run-error")).toContainText("PROVIDER_RATE_LIMITED");
    await expect(page.getByTestId("agent-reply")).toHaveCount(0);
  });

  test("a conversation the server closed asks to pick the guest again", async ({ page, request }) => {
    await page.goto("/");
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
    await request.post(`${API}/__e2e__/close-sessions`);
    await page.reload();
    await expect(page.getByTestId("session-notice")).toContainText(/no longer open/i);
    await page.getByTestId("guest-G-001").click();
    await expect(page.getByTestId("guest-context")).toContainText("Emma Wilson");
  });
});
