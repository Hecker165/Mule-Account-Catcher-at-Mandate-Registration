import { expect, test } from "@playwright/test";

const API_BASE_URL = process.env.API_BASE_URL ?? "http://localhost:8000";

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

async function createSession(
  request: import("@playwright/test").APIRequestContext,
  merchantNamespace: string,
  deviceFingerprint: string,
  customerReference: string,
  orderRef: string
): Promise<string> {
  const response = await request.post(`${API_BASE_URL}/v1/risk-sessions`, {
    data: {
      merchant_namespace: merchantNamespace,
      checkout_order_ref: orderRef,
      customer_reference: customerReference,
      device_fingerprint: deviceFingerprint,
      flow_started_at: new Date().toISOString(),
      mandate_intent: {
        max_amount_paise: 100000,
        frequency: "monthly",
        expire_at: new Date(Date.now() + 365 * 24 * 60 * 60 * 1000).toISOString(),
      },
    },
    headers: { "user-agent": "playwright-seed/1.0" },
  });
  expect(response.ok()).toBeTruthy();
  const body = await response.json();
  return body.risk_session_id as string;
}

test("test_allow_flow_completes_without_challenge", async ({ page }) => {
  await page.goto("/checkout?merchant=demo_merchant_e2e_allow");
  await page.getByLabel("Customer reference (demo)").fill(`e2e-allow-customer-${Date.now()}`);
  await page.getByRole("button", { name: "Save details" }).click();
  await expect(page.getByText("Details saved — continue to risk check")).toBeVisible();
  // A real checkout takes longer than 3 seconds; rushing would trip the
  // implausible-flow-duration rule and turn this ALLOW case into a CHALLENGE.
  await page.waitForTimeout(4000);
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Risk check passed")).toBeVisible();
  expect(page.url()).not.toContain("/challenge");
  await page.getByRole("button", { name: "Continue to UPI app (simulated)" }).click();
  await expect(page.getByText("Simulated hand-off ready")).toBeVisible();
});

test("test_suspicious_flow_is_challenged_before_redirect", async ({ page, request }) => {
  const deviceFingerprint = `e2e-challenge-device-${Date.now()}`;
  const runSuffix = Date.now();
  for (let index = 0; index < 5; index += 1) {
    await createSession(
      request,
      "demo_merchant_e2e_challenge",
      deviceFingerprint,
      `e2e-seed-customer-${runSuffix}-${index}`,
      `order_e2e_seed_${runSuffix}_${index}`
    );
  }
  await page.addInitScript((fingerprint: string) => {
    sessionStorage.setItem("mg:demo_device_fingerprint", fingerprint);
  }, deviceFingerprint);
  await page.goto("/checkout?merchant=demo_merchant_e2e_challenge");
  await page.getByLabel("Customer reference (demo)").fill(`e2e-challenge-customer-${runSuffix}`);
  await page.getByRole("button", { name: "Save details" }).click();
  await page.getByRole("button", { name: "Continue" }).click();
  await page.waitForURL("**/challenge?session=*");
  await expect(page.getByText("Additional verification is required")).toBeVisible();
  await expect(page.getByText("Simulated hand-off ready")).toHaveCount(0);
});

test("test_challenge_gate_blocks_progress_until_stepup_succeeds", async ({ page }) => {
  await page.goto("/challenge");
  await expect(page.getByText("No challenge in progress")).toBeVisible();

  await page.goto("/checkout");
  await page.evaluate(() => {
    sessionStorage.setItem(
      "mg:challenge_context",
      JSON.stringify({
        risk_session_id: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        score: 45,
        engine_version: "rules-v1",
        assessment_id: "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
      })
    );
  });
  await page.goto("/challenge?session=a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d");
  const verifyButton = page.getByRole("button", { name: "Verify and continue" });
  await expect(verifyButton).toBeDisabled();
  await page.getByText("I confirm the mandate details above are correct").click();
  await expect(verifyButton).toBeDisabled();
  await page.getByLabel("Enter the 6-digit demo code").fill("000000");
  await expect(verifyButton).toBeEnabled();
  await verifyButton.click();
  await expect(page.getByText("The verification code does not match")).toBeVisible();
  await expect(page.getByText("Verification complete.")).toHaveCount(0);

  const code = (await page.getByTestId("demo-code").textContent()) ?? "";
  expect(code).toMatch(/^\d{6}$/);
  await page.getByLabel("Enter the 6-digit demo code").fill(code);
  await verifyButton.click();
  await expect(page.getByText("Verification complete.")).toBeVisible();
});

test("test_challenge_context_survives_reload", async ({ page }) => {
  await page.goto("/checkout");
  await page.evaluate(() => {
    sessionStorage.setItem(
      "mg:challenge_context",
      JSON.stringify({
        risk_session_id: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
        score: 45,
        engine_version: "rules-v1",
        assessment_id: "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
      })
    );
  });
  await page.goto("/challenge?session=a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d");
  await expect(page.getByText("Additional verification is required")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Additional verification is required")).toBeVisible();
});

test("test_no_raw_identifiers_or_secrets_in_page", async ({ page }) => {
  const customerReference = "e2e-secrecy-customer-xyz";
  await page.goto("/checkout");
  await page.getByLabel("Customer reference (demo)").fill(customerReference);
  await page.getByRole("button", { name: "Save details" }).click();
  await expect(page.getByText("Details saved — continue to risk check")).toBeVisible();
  const html = await page.content();
  expect(html).not.toContain(customerReference);
  expect(html).not.toContain("hmac-sha256");
  expect(html).not.toContain("razorpay_key");
  expect(html).not.toContain("webhook_secret");
  const uuids = html.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi) ?? [];
  expect(uuids).toEqual([]);
});

test("test_api_unreachable_blocks_the_flow", async ({ page }) => {
  await page.route("**/v1/risk-sessions**", (route) => route.abort("failed"));
  await page.goto("/checkout");
  await page.getByLabel("Customer reference (demo)").fill("e2e-offline-customer");
  await page.getByRole("button", { name: "Save details" }).click();
  await expect(page.getByText("Network error.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue" })).toHaveCount(0);
});
