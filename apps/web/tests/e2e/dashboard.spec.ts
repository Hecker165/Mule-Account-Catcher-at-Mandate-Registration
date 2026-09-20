import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext } from "@playwright/test";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";
const WEBHOOK_SECRET = "local-test-secret";

async function apiHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE_URL}/healthz`);
    return response.ok;
  } catch {
    return false;
  }
}

test.beforeEach(async () => {
  if (!(await apiHealth())) {
    test.skip(true, "API is not running");
  }
});

function fixtureBytes(name: string): Buffer {
  const file = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "..",
    "data",
    "fixtures",
    "webhooks",
    name
  );
  return readFileSync(file);
}

function sign(body: Buffer): string {
  return createHmac("sha256", WEBHOOK_SECRET).update(body).digest("hex");
}

async function postWebhook(
  request: APIRequestContext,
  body: Buffer,
  eventId: string,
  extraHeaders: Record<string, string> = {}
): Promise<import("@playwright/test").APIResponse> {
  return request.post(`${API_BASE_URL}/v1/webhooks/razorpay`, {
    data: body.toString("utf-8"),
    headers: {
      "content-type": "application/json",
      "x-razorpay-signature": sign(body),
      "x-razorpay-event-id": eventId,
      "X-Demo-Event": "true",
      ...extraHeaders,
    },
  });
}

async function createReadySession(
  request: APIRequestContext,
  namespace: string,
  deviceFingerprint: string,
  customerReference: string,
  orderRef: string,
  runPrecheck = true
): Promise<string> {
  const created = await request.post(`${API_BASE_URL}/v1/risk-sessions`, {
    data: {
      merchant_namespace: namespace,
      checkout_order_ref: orderRef,
      customer_reference: customerReference,
      device_fingerprint: deviceFingerprint,
      flow_started_at: "2026-01-15T10:00:00Z",
      mandate_intent: {
        max_amount_paise: 100000,
        frequency: "monthly",
        expire_at: "2027-01-15T10:00:00Z",
      },
    },
    headers: { "user-agent": "playwright-seed/1.0" },
  });
  expect(created.ok()).toBeTruthy();
  const session = await created.json();
  const telemetry = await request.patch(
    `${API_BASE_URL}/v1/risk-sessions/${session.risk_session_id}/telemetry`,
    {
      data: { flow_completed_at: "2026-01-15T10:00:00Z" },
      headers: { "user-agent": "playwright-seed/1.0" },
    }
  );
  expect(telemetry.ok()).toBeTruthy();
  if (runPrecheck) {
    const precheck = await request.post(
      `${API_BASE_URL}/v1/risk-sessions/${session.risk_session_id}/precheck`
    );
    expect(precheck.ok()).toBeTruthy();
  }
  return session.risk_session_id as string;
}

test("test_dashboard_renders_seeded_assessments_with_masked_fields", async ({
  page,
  request,
}) => {
  const body = fixtureBytes("token_confirmed_body.json");
  const eventId = `evt_e2e_dash_${Date.now()}`;
  const delivered = await postWebhook(request, body, eventId);
  expect(delivered.ok()).toBeTruthy();

  await page.goto("/dashboard");
  const row = page.getByRole("row", { name: /•••@upi/ }).first();
  await expect(row).toBeVisible();
  await expect(row.getByText("•••@upi")).toBeVisible();
  await expect(row.getByText("ALLOW")).toBeVisible();
  await expect(row.getByText(/\d+ ms/)).toBeVisible();
  const html = await page.content();
  const uuids = html.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi) ?? [];
  expect(uuids).toEqual([]);
});

test("test_dashboard_hides_hashes_and_secrets", async ({ page, request }) => {
  const body = fixtureBytes("token_confirmed_body.json");
  await postWebhook(request, body, `evt_e2e_hide_${Date.now()}`);
  await page.goto("/dashboard");
  await expect(page.getByText("Risk dashboard (demo)")).toBeVisible();
  const html = await page.content();
  expect(html).not.toContain("hmac-sha256");
  expect(html).not.toContain("sha256:");
  expect(html).not.toContain("local-test-secret");
  expect(html).not.toContain("demo.user");
});

test("test_dashboard_polls_and_updates", async ({ page, request }) => {
  await page.goto("/dashboard");
  await expect(page.getByText("Risk dashboard (demo)")).toBeVisible();
  const before = await page.getByRole("row").count();
  const body = fixtureBytes("token_confirmed_body.json");
  await postWebhook(request, body, `evt_e2e_poll_${Date.now()}`);
  await expect
    .poll(async () => page.getByRole("row").count(), { timeout: 8000 })
    .toBeGreaterThan(before);
});

test("test_dashboard_shows_block_with_reasons_and_revoke_lifecycle", async ({
  page,
  request,
}) => {
  const namespace = "demo_merchant_e2e_dash";
  const deviceFingerprint = `e2e-dash-device-${Date.now()}-fp`;
  const runSuffix = Date.now();
  for (let index = 0; index < 10; index += 1) {
    await createReadySession(
      request,
      namespace,
      deviceFingerprint,
      `e2e-dash-customer-${runSuffix}`,
      `order_e2e_dash_${runSuffix}_${index}`
    );
  }
  // The final session skips precheck: its snapshot row is reserved for the
  // webhook evaluation (one snapshot per session per version).
  const sessionId = await createReadySession(
    request,
    namespace,
    deviceFingerprint,
    `e2e-dash-customer-${runSuffix}`,
    `order_e2e_dash_${runSuffix}_final`,
    false
  );
  const parsed = JSON.parse(fixtureBytes("token_confirmed_body.json").toString("utf-8"));
  parsed.payload.token.entity.notes = { risk_session_id: sessionId };
  const body = Buffer.from(JSON.stringify(parsed));
  const delivered = await postWebhook(request, body, `evt_e2e_block_${runSuffix}`);
  expect(delivered.ok()).toBeTruthy();
  if ((await delivered.json()).evaluation === "not_configured") {
    test.skip(true, "A5 evaluator is not installed");
  }

  await page.goto("/dashboard");
  const row = page.getByRole("row", { name: /BLOCK/ }).first();
  await expect(row).toBeVisible();
  await expect(row.getByText("DEVICE_VELOCITY_BURST")).toBeVisible();
  await expect(row.getByText("simulated")).toBeVisible();
  await expect(row.getByText("SUCCEEDED")).toBeVisible({ timeout: 30000 });
});

test("test_audit_chain_panel_verifies_for_an_assessment", async ({ page, request }) => {
  const body = fixtureBytes("token_confirmed_body.json");
  await postWebhook(request, body, `evt_e2e_audit_${Date.now()}`);
  await page.goto("/dashboard");
  const row = page.getByRole("row", { name: /•••@upi/ }).first();
  await expect(row).toBeVisible();
  await row.click();
  await expect(page.getByText(/Chain verified/)).toBeVisible();
});

test("test_demo_page_shows_checklist_and_working_controls", async ({ page }) => {
  await page.goto("/demo");
  for (const name of [
    "Legitimate flow",
    "Bot burst",
    "NPCI-risk rejection",
    "Confirmed high-risk block",
    "Duplicate webhook",
    "Worker failure and recovery",
    "Shared demo merchant velocity",
  ]) {
    await expect(page.getByText(name, { exact: false }).first()).toBeVisible();
  }
  // A9's demo control API is live behind the same-origin proxy: the page's
  // feature detection must report the controls as installed, and the
  // "not installed" notice must be gone.
  await expect(page.getByText("Automated demo controls are installed")).toBeVisible();
  await expect(page.getByText("Automated demo controls are not installed yet")).toHaveCount(0);
});
