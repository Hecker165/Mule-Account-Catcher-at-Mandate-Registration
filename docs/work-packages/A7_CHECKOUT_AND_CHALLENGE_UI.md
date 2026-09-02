# A7 — Checkout, telemetry and step-up challenge experience: implementation specification

## 0. Agent instruction

You are the **A7 checkout and challenge UI agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A2_SESSION_CAPTURE_AND_PRECHECK_API.md`
4. `docs/work-packages/A3_RAZORPAY_WEBHOOK_GATEWAY.md`
5. `docs/contracts.md`
6. `apps/web/src/lib/api-types.ts` (generated; read-only)

Implement exactly this work package. You build the browser experience that captures checkout context, creates the risk session, records telemetry, requests the pre-registration check and gates the UPI redirect behind an `ALLOW` decision or a completed step-up challenge.

Do **not** build the dashboard or demo pages (A8), implement risk logic in the browser, call Razorpay from the browser, add API routes, or change contracts. The browser never makes a final fraud decision and never sees or stores a secret.

## 1. Why this package exists

`CHALLENGE` can only happen before the customer leaves for their UPI app. A7 is the only component that experiences that moment:

```text
/checkout (browser)
  1. POST /v1/risk-sessions            -> RiskSession (status CREATED)   [A2]
  2. PATCH /v1/risk-sessions/{id}/telemetry -> READY
  3. POST /v1/risk-sessions/{id}/precheck   -> RiskAssessment (stage PRECHECK)
       ALLOW     -> show "continue" state
       CHALLENGE -> /challenge route: step-up screen BEFORE any redirect
  4. risk_session_id is kept in local checkout state and later travels into the
     merchant-side order mapping (Razorpay order notes / internal mapping) so the
     later webhook (A3) can correlate.
```

Hard product rules that shape this package:

1. The UI is a **gobetween, not a decision-maker**: it renders the API's decision; it never computes, overrides or hides one.
2. **Fail closed**: if the pre-check cannot be completed (network error, 5xx, evaluator unavailable 503), the customer is not sent onward; a visible retry state is shown.
3. A completed step-up challenge does **not** change the stored assessment. It is a local UI gate for the redirect; the assessment stays `CHALLENGE` in the audit trail.
4. No secret, API key, signature or raw identifier is ever rendered, logged or persisted by the web app. All display data is already pseudonymised by the API (hashes, masked values, booleans).

## 2. Objective and definition of done

Build two pages, three checkout components and four lib modules with typed API access and deterministic tests.

Definition of done:

- A browser can complete: create session -> telemetry -> pre-check -> `ALLOW` continue state, entirely against the real A2 API, with `risk_session_id` retained across the flow.
- A `CHALLENGE` decision routes to `/challenge`, which requires an explicit two-part step-up (confirm mandate details + enter the on-screen demo verification code) before the simulated UPI hand-off is shown; wrong code shows an inline error and never proceeds.
- All request/response handling uses the generated `apps/web/src/lib/api-types.ts` types — zero hand-written duplicates of backend payloads.
- Every API call sends a fresh `X-Request-ID` (UUID4) and uses `cache: "no-store"` with a 5-second `AbortController` timeout; failures surface as visible, specific UI states (network, 4xx, 409 consumed, 503 unavailable).
- No `console.log` of request/response payloads; no raw `customer_reference`/`device_fingerprint` values rendered anywhere (the API never returns them, and the UI never echoes its own inputs into decision states).
- Vitest unit tests cover the lib modules; Playwright e2e proves the `ALLOW` flow, the challenge-before-redirect flow, and a no-secrets assertion, using the velocity-isolation strategy in Section 8.3.
- `npm run build`, `npm run lint`, `npm run test` and `npm run test:e2e` all pass; no file outside the owned paths changes except the additive `package.json` dev-dependency/script block and the two minimal layout edits in Section 3.

## 3. Scope and exact file ownership

### You own

```text
apps/web/src/app/checkout/page.tsx
apps/web/src/app/challenge/page.tsx
apps/web/src/components/checkout/MandateSummary.tsx
apps/web/src/components/checkout/DecisionBanner.tsx
apps/web/src/components/checkout/StepUpChallenge.tsx
apps/web/src/lib/api-client.ts
apps/web/src/lib/risk-session-state.ts
apps/web/src/lib/masking.ts
apps/web/src/app/globals.css
apps/web/tailwind.config.ts
apps/web/postcss.config.mjs
apps/web/vitest.config.ts
apps/web/playwright.config.ts
apps/web/tests/unit/api-client.test.ts
apps/web/tests/unit/masking.test.ts
apps/web/tests/unit/risk-session-state.test.ts
apps/web/tests/e2e/checkout.spec.ts
```

### The one permitted edit outside your paths

`apps/web/package.json` (A0-owned): add **only** these dev dependencies — `tailwindcss`, `postcss`, `autoprefixer` (current major), `vitest`, `@vitejs/plugin-react`, `jsdom`, `@playwright/test` — and these scripts: `"test": "vitest run"`, `"test:e2e": "playwright test"`. Nothing else in the file changes; run `npm install` and commit the updated lock file. This mirrors the A4 `fakeredis` exception: additive, dev-only, documented.

### You may update minimally

```text
apps/web/src/app/layout.tsx   # only: import "./globals.css"
apps/web/src/app/page.tsx     # only: replace "Foundation ready" content with a plain landing that links to /checkout and /dashboard (A8 will own the dashboard link target)
```

### You may read but must not modify

```text
apps/web/src/lib/api-types.ts            # A0-generated; never hand-edit; regenerate only via "npm run generate:api"
apps/web/package.json, tsconfig.json, next.config.ts, eslint.config.mjs, Dockerfile
apps/api/**                              # A0-A6 backend; A7 adds no API code
data/fixtures/**, data/scenarios/**      # A0/A9
docs/**                                  # shared docs
```

### Explicit non-goals

- No dashboard, demo control or event-feed pages (A8), no simulator (A9), no CI changes (A10).
- No risk logic, scoring, thresholds or decision overrides in the browser.
- No Razorpay Checkout script, no API keys in browser code or env vars exposed beyond `NEXT_PUBLIC_API_BASE_URL`. The "UPI hand-off" is a clearly labelled simulated state in A7; the real hand-off wiring belongs to the demo/simulator work (A9) and is out of scope here.
- No hand-written TypeScript interfaces for backend payloads; import everything from `api-types.ts`.
- No fingerprinting libraries: the demo device fingerprint is a locally generated random UUID (Section 6.2), never a real fingerprinting mechanism.
- No `localStorage` persistence of checkout inputs; only the opaque `risk_session_id` travels via `sessionStorage` (Section 6.3).

## 4. Checkout flow (`/checkout`)

### 4.1 Fixed client-side demo inputs

| Value | Source | Notes |
|---|---|---|
| `merchant_namespace` | constant `demo_merchant` in `lib/risk-session-state.ts` | matches the A0 pattern; `demo_*` also flags demo traffic for A4's simulated counters |
| `checkout_order_ref` | `demo-` + first 8 chars of a fresh UUID, generated once per checkout | shown truncated, e.g. `demo-3f2a9c1b` |
| `customer_reference` | text input, label "Customer reference (demo)", required, max 256 | echoed back **nowhere**; sent once at creation |
| `device_fingerprint` | `crypto.randomUUID()` generated once per browser tab and kept in `sessionStorage` key `mg:demo_device_fingerprint` | a demo stand-in, not real fingerprinting |
| `flow_started_at` | ISO UTC timestamp captured when the checkout form first mounts | sent at creation |
| `mandate_intent` | fixed demo values: `max_amount_paise: 100000` (₹1,000), `frequency: "monthly"`, `expire_at` = now + 365 days (ISO UTC) | rendered by `MandateSummary` |

### 4.2 Exact API sequence (the state machine in `lib/risk-session-state.ts`)

```text
idle
  -> [user submits the small form]           POST /v1/risk-sessions
       body: RiskSessionCreateRequest (fields from 4.1)
       success 201/200 -> RiskSession; store risk_session_id; state = "ready"
       200 + X-Idempotent-Replay: true is treated as success (idempotent replay)
  -> [user presses "Continue"]                PATCH /v1/risk-sessions/{id}/telemetry
       body: RiskSessionTelemetryUpdateRequest { device_fingerprint, flow_completed_at }
       (client_ip, client_user_agent and is_vpn_claimed are NOT sent: A2 ignores them)
       success 200 -> state = "checking"
  ->                                          POST /v1/risk-sessions/{id}/precheck
       body: none
       200 RiskAssessment:
         decision ALLOW     -> state = "allowed"
         decision CHALLENGE -> state = "challenged" (route to /challenge)
         decision BLOCK     -> render verbatim as "blocked" (defensive; the rules-v1
                               pre-check never produces it, but the UI must not hide it)
       503 -> state = "error_precheck_unavailable" (retry button; never auto-continue)
       404/409 -> state = "error_session" with the specific message
       network/timeout -> state = "error_network"
```

Every request: `cache: "no-store"`, header `X-Request-ID: crypto.randomUUID()`, 5-second `AbortController` timeout. Responses are parsed with the generated types (`components["schemas"]["RiskSession"]` and `RiskAssessment`); unexpected shapes become a generic error state, never a silent pass.

### 4.3 UI states (rendered by `DecisionBanner`)

| State | What the customer sees |
|---|---|
| `idle` | The small checkout form (customer reference) and the mandate summary |
| `ready` | "Details saved — continue to risk check" button; truncated `risk_session_id` for demo correlation |
| `checking` | "Checking registration signals…" spinner |
| `allowed` | Green banner "Risk check passed" + button **"Continue to UPI app (simulated)"** — the simulated hand-off shows a final panel with the truncated `risk_session_id` and the text "Hand-off to Razorpay is wired by the demo simulator (not part of A7)" |
| `challenged` | Immediately route to `/challenge?session=<risk_session_id>` |
| `error_precheck_unavailable` | Amber banner "Risk pre-check is unavailable — you cannot continue yet" + Retry |
| `error_session` / `error_network` | Red banner with the specific cause + Retry / Start over |

Rules: the continue button is disabled until the state machine reaches `allowed`; there is no bypass, query-parameter override or "skip risk check" affordance; the state machine lives in `risk-session-state.ts` (pure, testable) and the page only renders it.

## 5. Challenge page (`/challenge`)

### 5.1 Entry and context

- Route: `/challenge?session=<risk_session_id>`. If the query parameter is missing or not a UUID, show "No challenge in progress — return to checkout" with a link to `/checkout`; do not invent a session.
- The page reads the decision context from `sessionStorage` key `mg:challenge_context` (written by the checkout page before routing): `{ risk_session_id, score, engine_version, assessment_id }`. If absent, render the fallback above. The step-up never re-calls pre-check and never mutates the assessment.

### 5.2 The step-up gate (`StepUpChallenge`), exact behaviour

1. Render the mandate summary (same component as checkout) and the banner: "Additional verification is required before we can send you to your UPI app."
2. Require both of:
   - a checkbox "I confirm the mandate details above are correct"; and
   - the 6-digit **demo verification code** displayed on-screen in a bordered box (labelled "Demo verification code — displayed here because no SMS provider is configured"). The code is `crypto.randomUUID()`'s first 6 digits, generated once per challenge view.
3. "Verify and continue" is enabled only when the box is ticked and the input is 6 digits. Wrong code: inline error "The verification code does not match", attempt counter shown, never proceeds. Three wrong attempts show "Verification failed — return to checkout" (link), still with no data written.
4. On success: show the final panel — "Verification complete. In the live demo this is where you would be handed to your UPI app." + truncated `risk_session_id` + the same A9 simulator note. No API call is made on success; the assessment remains `CHALLENGE` server-side.
5. "Cancel" clears the challenge context from `sessionStorage` and returns to `/checkout`.

Data rules: the page displays only data the API returned (decision metadata) plus locally generated values. It renders no hashes, sends no telemetry from the challenge page, and never writes to the API.

## 6. Lib modules

### 6.1 `lib/api-client.ts`

```typescript
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export async function apiFetch<Input, Output>(...): Promise<Output>   // internal helper
export function createRiskSession(body: components["schemas"]["RiskSessionCreateRequest"]):
  Promise<{ session: components["schemas"]["RiskSession"]; idempotentReplay: boolean }>
export function recordTelemetry(riskSessionId: string,
  body: components["schemas"]["RiskSessionTelemetryUpdateRequest"]): Promise<components["schemas"]["RiskSession"]>
export function requestPrecheck(riskSessionId: string): Promise<components["schemas"]["RiskAssessment"]>
export class ApiError extends Error { status: number; kind: "network" | "timeout" | "unavailable" | "client" | "server" }
```

Rules: every call attaches `X-Request-ID`, `cache: "no-store"`, 5s abort; `503` maps to `kind: "unavailable"`, `4xx` to `"client"`, `5xx` to `"server"`, abort to `"timeout"`, fetch rejection to `"network"`. Error messages are fixed strings — never response bodies. The client imports types only from `./api-types`.

### 6.2 `lib/risk-session-state.ts`

A framework-free reducer/state machine (usable in tests without React): `CheckoutState = { kind: "idle" | "ready" | "checking" | "allowed" | "challenged" | "blocked" | "error_precheck_unavailable" | "error_session" | "error_network", session?, assessment?, message? }` plus pure transition functions `startCheckout`, `sessionCreated`, `telemetryRecorded`, `precheckResolved`, `precheckFailed`, `reset`. Constants: `MERCHANT_NAMESPACE`, `makeCheckoutOrderRef()`, `makeDeviceFingerprint()` (sessionStorage-backed), `makeMandateIntent()`, `makeFlowTimestamps()`. React binding is a thin `useReducer` in the page.

### 6.3 `lib/masking.ts`

```typescript
export function formatPaise(paise: number): string        // 100000 -> "₹1,000.00"
export function truncateId(id: string): string             // first 8 chars + "…"
export function maskOrderRef(ref: string): string          // keeps "demo-" + last 4
export function isUuid(value: string): boolean
```

Only these helpers may format identifiers for display. No component may render an untruncated UUID or an unmasked order reference.

## 7. Privacy, trust and display rules

| Rule | Implementation |
|---|---|
| No secrets in the browser | The web app reads only `NEXT_PUBLIC_API_BASE_URL`; there is no Razorpay key, webhook secret or HMAC pepper anywhere in `apps/web`. |
| No raw identifiers rendered | `customer_reference` is used only as an input value and never displayed after submission; `device_fingerprint` is never displayed; API responses contain only hashes, so nothing sensitive can leak from them. |
| Truncated IDs only | Every UUID/order-ref rendered through `truncateId`/`maskOrderRef`. |
| Honest demo labelling | Every simulated element (UPI hand-off, demo verification code, demo namespace) carries a visible "demo/simulated" label, per the architecture's honesty requirements. |
| No sensitive logging | No `console.log`/`console.info` of request bodies, responses, tokens or customer input anywhere in `apps/web/src`. `console.error` is allowed only with fixed messages. |
| Fail closed | No pre-check answer means no forward path; no query-param or storage-based bypass exists. |
| Assessment immutability | The challenge flow performs no writes; the stored `CHALLENGE` assessment is never softened to `ALLOW` by any UI action. |

## 8. Tests and acceptance criteria

### 8.1 Unit tests — `tests/unit/api-client.test.ts` (Vitest, fetch stubbed)

```text
test_create_risk_session_posts_expected_body_and_headers
test_create_risk_session_detects_idempotent_replay_header
test_record_telemetry_patches_the_telemetry_endpoint
test_request_precheck_posts_with_no_body
test_every_request_sends_a_uuid_x_request_id_and_no_store
test_timeout_maps_to_timeout_error_kind
test_503_maps_to_unavailable_error_kind
test_404_maps_to_client_error_kind
test_500_maps_to_server_error_kind
test_fetch_rejection_maps_to_network_error_kind
test_error_message_never_contains_response_body
```

### 8.2 Unit tests — `tests/unit/masking.test.ts` and `tests/unit/risk-session-state.test.ts`

```text
test_format_paise_renders_rupees_from_integer_paise        # masking.test.ts
test_truncate_id_shows_first_eight_chars_only
test_mask_order_ref_keeps_prefix_and_last_four_only
test_is_uuid_rejects_non_uuids

test_checkout_starts_idle_and_transitions_to_ready          # risk-session-state.test.ts
test_telemetry_then_precheck_transitions_to_allowed_on_allow
test_precheck_challenge_transitions_to_challenged
test_precheck_503_transitions_to_error_precheck_unavailable
test_blocked_assessment_is_rendered_not_hidden
test_reset_returns_to_idle_and_clears_context
test_no_transition_skips_the_telemetry_step                 # fail-closed ordering
```

State-machine tests use fixed UUIDs and timestamps; no network and no React rendering.

### 8.3 Playwright e2e — `tests/e2e/checkout.spec.ts`

Prerequisites: the stack runs locally (`docker compose up -d postgres redis`, API on :8000, web on :3000). The e2e hits the real A2 API. Two preconditions handled explicitly:

- If `GET /healthz` on the API base URL fails, `test.skip(true, "API is not running")`.
- If the pre-check answers 503 (A5 evaluator not yet installed), the CHALLENGE test skips with that message; the ALLOW-then-503 behaviour becomes its own assertion instead.

**Determinism strategy (velocity isolation).** Each test uses its own merchant namespace, and the API counters (A4) key velocity per namespace:

- `demo_merchant_e2e_allow` — one clean session through the UI; nothing has been recorded in this namespace, so the assessment is `ALLOW` (score 0).
- `demo_merchant_e2e_challenge` — first create **five** sessions via Playwright's `request` fixture (same `device_fingerprint`, distinct customer references), so the UI session becomes the 6th recording: `device_velocity_5m = 6 >= 5` triggers `velocity_device_burst` (+25) and the decision is `CHALLENGE`. Use a distinct device fingerprint per run (`Date.now()` suffix inside the test) so repeated runs stay deterministic within the 5-minute window.

```text
test_allow_flow_completes_without_challenge
  -> fill customer reference, submit, continue, expect the green ALLOW banner and the
     simulated hand-off panel; assert no navigation to /challenge.

test_suspicious_flow_is_challenged_before_redirect
  -> seed five same-device sessions in demo_merchant_e2e_challenge via the request fixture;
     complete the UI flow; expect navigation to /challenge and the step-up screen
     rendered BEFORE any simulated hand-off is reachable.

test_challenge_gate_blocks_progress_until_stepup_succeeds
  -> on /challenge: "Verify and continue" disabled until checkbox + code; wrong code shows
     the inline error and stays; correct code shows the completion panel.

test_challenge_context_survives_reload
  -> reload /challenge mid-flow; the step-up is still shown from the stored context.

test_no_raw_identifiers_or_secrets_in_page
  -> after both flows: assert the page HTML contains neither the typed customer reference,
     nor any full UUID (only truncated forms), nor the strings "hmac-sha256", "secret",
     "razorpay_key", "webhook_secret".

test_api_unreachable_blocks_the_flow
  -> with the API base URL pointed at a closed port (route interception), the checkout
     shows a network error state and the continue path never becomes available.
```

Playwright config: `testDir: "tests/e2e"`, `baseURL: "http://localhost:3000"`, retries 0 (determinism over flakiness), and `API_BASE_URL` read from the environment (default `http://localhost:8000`) for the health check and request-fixture seeding.

### 8.4 Verification commands

```powershell
npm --prefix apps/web install
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/web run test
docker compose up -d postgres redis
# start API: uv --directory apps/api run uvicorn app.main:app --port 8000
# start web: npm --prefix apps/web run dev
npm --prefix apps/web run test:e2e
docker compose down
```

Do not mark the package done until all pass. `npm run generate:api` must be re-run only if the OpenAPI contract changed (A0/A2 responsibility); `api-types.ts` itself is never hand-edited.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A7 use |
|---|---|---|
| A0 | generated `api-types.ts`, `.env.example` (`NEXT_PUBLIC_API_BASE_URL`), web scaffold | Sole type source for all payloads; never edited by hand. |
| A2 | `POST /v1/risk-sessions`, `PATCH .../telemetry`, `POST .../precheck` with exact status codes and the `CREATED -> READY -> CONSUMED` lifecycle | The only backend surface A7 calls; status mapping per Section 4.2. |
| A3 (convention) | order-notes correlation (`notes.risk_session_id`) | A7 keeps `risk_session_id` in checkout state for the later order mapping; it does not call Razorpay itself. |
| A4/A5, later | real pre-check decisions | The UI needs no change: it renders whatever `RiskAssessment.decision` arrives. |
| A8, later | dashboard routes | A7's landing page links to `/dashboard`; A8 owns the target. |

### Outputs produced

| Consumer | Receives from A7 | Integration rule |
|---|---|---|
| A3 webhook gateway | `risk_session_id` retained in checkout state | The demo/simulator passes it into the merchant-side order mapping so the webhook can correlate; A7 never calls Razorpay. |
| A8 dashboard | none directly | A8 reads persisted sessions/assessments via A1; the landing link is the only touchpoint. |
| A9 simulator | this package's page structure and the challenge gate | The simulator may automate the same UI flow or call the API directly; it must not modify A7 files. |
| A10 CI | `npm run lint/build/test`, Playwright suite | Added to the web CI job as-is. |

### Merge order

1. A0 must be merged (`contracts-v1`, generated types, working `npm run build`); A2 must be merged before e2e tests can run.
2. A7 merges in parallel with A3–A6 — no shared backend files. The only shared frontend file is `package.json` (additive block) and the two minimal layout edits.
3. The full UI demo (including deterministic CHALLENGE) completes once A4/A5 are merged; until then the e2e challenge test skips by design and the 503 path is asserted instead.

### Pull-request handoff checklist

State in the A7 PR: the two routes and every UI state; the exact API sequence with status-code mapping; the fail-closed rules; the step-up gate behaviour and assessment-immutability rule; the velocity-isolation determinism strategy for e2e; screenshots or a short recording of the ALLOW and CHALLENGE flows; the additive `package.json` block diff; results for all commands in Section 8.4; confirmation that `api-types.ts` was not hand-edited and no backend files changed; and known deferred work (real Razorpay hand-off wiring via the demo/simulator, dashboard link target owned by A8).

## 10. Failure conditions

The A7 work is unsafe or incomplete if any of these occur:

- The browser computes, overrides or hides a risk decision, or offers any bypass of the pre-check.
- The customer can reach the simulated UPI hand-off without an `ALLOW` decision or a completed step-up challenge.
- The UI proceeds after a network error, timeout, 5xx or 503 instead of showing a retry state.
- A secret, API key, signature, raw customer reference or raw device fingerprint is rendered, logged or persisted in `apps/web`.
- Backend payload types are hand-written instead of imported from the generated `api-types.ts`.
- The challenge flow writes to the API or implies the stored `CHALLENGE` assessment was changed.
- A7 implements dashboard/demo pages (A8), adds backend code, or edits A0-owned frontend config beyond the documented additive block.
