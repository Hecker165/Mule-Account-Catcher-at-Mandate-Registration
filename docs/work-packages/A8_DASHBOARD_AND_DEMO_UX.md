# A8 — Dashboard and live demo UX: implementation specification

## 0. Agent instruction

You are the **A8 dashboard and demo UX agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A5_RULE_ENGINE_AND_EVALUATION.md`
5. `docs/work-packages/A6_REVOKE_ADAPTER_AND_OUTBOX_WORKER.md`
6. `docs/work-packages/A7_CHECKOUT_AND_CHALLENGE_UI.md`
7. `docs/contracts.md`
8. `apps/web/src/lib/api-types.ts` (generated; read-only)

Implement exactly this work package. You build the operator-facing surfaces: a read-only backend dashboard API that exposes A1's redacted projections over HTTP, and the web dashboard plus demo-suite pages that display masked VPA handles, decisions, scores, reasons, lifecycle and latency for the latest 50 events.

Do **not** implement demo scenario generation or control endpoints (A9), change any A0 contract, edit A1-owned files, or expose any hash, raw identifier or payload JSON in an API response or UI pixel. The dashboard is read-only: it must not be able to trigger decisions, actions or writes.

## 1. Why this package exists

The architecture's acceptance criterion for A8: *"Visual page shows masked VPA, score, reasons, lifecycle and latency for latest 50 events."* The dashboard is the proof surface for the entire loop:

```text
A1 PostgreSQL (system of record)
  -> A8 dashboard API (redacted projection, no-store)
     -> /dashboard  : latest 50 assessments, reasons, revoke lifecycle, audit-chain verify
     -> /demo       : the 7-scenario demonstration suite checklist + live counters
                       (run controls are A9's; A8 feature-detects and renders a placeholder)
```

The seven architecture demo scenarios (legitimate flow, bot burst, NPCI-risk rejection, confirmed high-risk block, duplicate webhook, worker failure/recovery, shared demo merchant velocity) are *displayed* here as a checklist with live counters; A9 automates their execution.

## 2. Objective and definition of done

Build one backend route module, six frontend files and deterministic tests.

Definition of done:

- `GET /v1/dashboard/assessments` returns A1's redacted `DashboardAssessmentRow` projection (assessment ID, decision, score, stage, assessed time, token ID, VPA handle, `is_demo_event`, action status, rule reasons) **plus** `evaluation_latency_ms` enriched from the A0 `RiskAssessment` contract, with `limit` clamped 1..100 (default 50) and an optional `decision` filter.
- No response ever contains a VPA hash, IP hash, device hash, customer hash, webhook body, signature or raw payload; verified by tests at route and UI level.
- `GET /v1/dashboard/audit-events` lists an aggregate's audit chain and `GET /v1/dashboard/audit-events/verify` returns A1's `AuditVerificationResult` — enabling the dashboard's tamper-evidence badge.
- All dashboard routes set `Cache-Control: no-store` and are read-only (GET only).
- The web `/dashboard` page polls every 2 seconds (paused when the tab is hidden, manual refresh included) and renders: masked VPA handle (`•••@upi`), truncated token/assessment IDs, decision badge, score, stage, reason chips with human text, revoke lifecycle status and latency; demo-simulated rows carry a visible "simulated" badge.
- The web `/demo` page renders the seven architecture scenarios as a checklist with expected outcomes, live counters derived from the dashboard API, and a feature-detected slot for A9's run controls (disabled with a clear message while A9 is absent).
- Types come only from the regenerated `api-types.ts`; no hand-written backend payload interfaces.
- Route integration tests (pytest, real PostgreSQL) and Playwright e2e (real stack, seeded through the signed-webhook path) all pass, including the no-hash/no-secret assertions.

## 3. Scope and exact file ownership

### You own

```text
apps/api/app/api/routes/dashboard.py
apps/api/tests/integration/test_dashboard_api.py
apps/web/src/app/dashboard/page.tsx
apps/web/src/app/demo/page.tsx
apps/web/src/components/dashboard/AssessmentTable.tsx
apps/web/src/components/dashboard/ReasonChips.tsx
apps/web/src/components/dashboard/StatusBadges.tsx
apps/web/src/components/dashboard/AuditChainPanel.tsx
apps/web/src/lib/dashboard-api.ts
apps/web/tests/unit/dashboard-api.test.ts
apps/web/tests/e2e/dashboard.spec.ts
```

The backend route module is a **new** file in the routes directory (reserved for A8 by A0). Response models are declared locally in that module (they are A8-specific read projections, not A0 canonical contracts).

### You may update minimally

```text
apps/api/app/main.py   # only: include_router(dashboard.router, prefix="/v1")
```

### Regenerated artifacts (sanctioned flow, never hand-edited)

Because `dashboard.py` adds OpenAPI schemas, you must run the exact A0 contract sequence and commit the regenerated artifacts:

```powershell
uv --directory apps/api run python ../../scripts/export_openapi.py
npm --prefix apps/web run generate:api
```

`apps/api/openapi.json` and `apps/web/src/lib/api-types.ts` change only through these commands. Any other regeneration diff must be reviewed before committing.

### You may read but must not modify

```text
apps/api/app/contracts/**            # frozen A0 contracts
apps/api/app/persistence/**          # A1 transaction/session implementation
apps/api/app/repositories/**         # A1 repositories (DashboardReadRepository, AuditRepository, RiskAssessmentRepository)
apps/api/app/services/**, app/domain/**, app/integrations/**, app/workers/**   # A2-A6
apps/api/app/api/routes/**           # A2/A3 routes; A8 adds no other routes
apps/web/src/lib/api-client.ts, risk-session-state.ts, masking.ts   # A7-owned; A8 adds its own module instead
apps/web/src/app/checkout/**, challenge/**, components/checkout/**  # A7
data/**, docs/**, docker-compose.yml, Makefile, pyproject.toml, package.json   # A0/A9; A8 needs no dependency changes
```

### Explicit non-goals

- No write endpoints, no demo scenario generation or run-control endpoints (A9), no simulator (A9), no CI changes (A10).
- No direct ORM access in the route: everything goes through A1 repositories.
- No authentication/authorisation on the dashboard (a documented buildathon limitation, deferred to A10; state it in the PR and the dashboard footer).
- No new npm/pip dependencies: polling is plain `setInterval` + `fetch`; styling reuses A7's Tailwind setup.
- No edits to A7-owned frontend files; the landing page already links to `/dashboard`.
- No hash values, full UUIDs (only truncated display), raw identifiers or payload JSON in any response or rendered page.

## 4. Backend dashboard API (`routes/dashboard.py`)

All routes are tagged `dashboard`, GET-only, and set `Cache-Control: no-store`. The router is included under `/v1`. There is no auth (documented limitation). Route functions stay thin: parse query, open `async with transaction() as session`, call A1 repositories, map to the local response models.

### 4.1 Local response models (declared in the module)

```python
class DashboardReasonItem(BaseModel):
    rule_id: str
    reason_code: str | None
    reason_text: str | None
    points: int

class DashboardAssessmentItem(BaseModel):
    assessment_id: UUID
    decision: Decision
    score: int
    stage: DecisionStage
    assessed_at: datetime
    evaluation_latency_ms: int
    token_id: str | None
    vpa_handle: str | None          # handle suffix only ("upi") - never a username
    is_demo_event: bool
    action_status: ActionStatus | None
    reasons: list[DashboardReasonItem]
    model_config = {"json_schema_extra": {"examples": [...]}}   # one realistic fake example

class DashboardAssessmentsResponse(BaseModel):
    items: list[DashboardAssessmentItem]
    limit: int
    offset: int
```

### 4.2 `GET /v1/dashboard/assessments`

Query parameters: `limit: int = Query(50, ge=1, le=100)`, `offset: int = Query(0, ge=0)`, `decision: Decision | None = None`.

Processing:

1. `rows = await DashboardReadRepository(session).list_recent_assessments(limit=limit, offset=offset, decision=decision)` — A1's redacted projection, newest first.
2. Enrich each row with latency: `contract = await RiskAssessmentRepository(session).get(row.assessment_id)`; `evaluation_latency_ms = contract.evaluation_latency_ms`. This is a bounded N+1 (max 100 lookups, primary-key reads, local database) — acceptable for the demo and it uses only A1 interfaces; document the tradeoff in the module docstring.
3. Map reasons from the row's triggered rule reasons (A1 exposes only triggered entries with `rule_id`, `reason_code`, `reason_text`, `points`).
4. Return `DashboardAssessmentsResponse(items=..., limit=limit, offset=offset)`.

Hard redaction rule: build the response **only** from the explicitly listed fields. If a future A1 row carries more fields, they are ignored — never forwarded wholesale (`model_dump` of the row is forbidden; field-by-field mapping is mandatory and tested).

### 4.3 `GET /v1/dashboard/audit-events`

Query parameters: `aggregate_type` (Literal `risk_session|mandate_event|risk_assessment|action_request`, required), `aggregate_id: UUID` (required), `limit: int = Query(100, ge=1, le=500)`.

Returns, newest first: a list of

```python
class DashboardAuditItem(BaseModel):
    audit_event_id: UUID
    sequence_number: int
    event_type: str
    actor_type: AuditActorType
    actor_id: str | None
    occurred_at: datetime
    redacted_payload: dict[str, object]   # A1 already guarantees this payload is redacted
```

mapped from `AuditRepository.list_for_aggregate`. `redacted_payload` is forwarded as-is because A1 owns its redaction guarantee; A8 adds nothing to it.

### 4.4 `GET /v1/dashboard/audit-events/verify`

Same query parameters. Returns A1's `AuditRepository.verify_aggregate(...)` result mapped 1:1:

```python
class AuditVerificationResponse(BaseModel):
    valid: bool
    checked_events: int
    first_invalid_sequence: int | None
    reason: str | None
```

Unknown aggregate types are rejected by FastAPI validation (422). Missing aggregates return the valid/zero result from A1 (never an error).

## 5. Web dashboard (`/dashboard`)

### 5.1 Client (`lib/dashboard-api.ts`)

Typed fetchers built only on the regenerated `api-types.ts` (reuse of A7's `API_BASE_URL` pattern is copied, not imported — A7's file is untouched):

```typescript
export function maskVpa(handle: string | null): string | null   // "upi" -> "•••@upi"; null -> null
export function truncateId(id: string): string                   // first 8 chars + "…" (local copy, tested)
export async function fetchAssessments(params?: { limit?: number; offset?: number; decision?: string }):
  Promise<components["schemas"]["DashboardAssessmentsResponse"]>
export async function fetchAuditEvents(aggregateType: string, aggregateId: string):
  Promise<components["schemas"]["DashboardAuditItem"][]>
export async function verifyAuditChain(aggregateType: string, aggregateId: string):
  Promise<components["schemas"]["AuditVerificationResponse"]>
```

Same call discipline as A7: `X-Request-ID` UUID4 per call, `cache: "no-store"`, 5s abort, `ApiError`-style error kinds, fixed error messages.

### 5.2 Page behaviour (`/dashboard/page.tsx`)

- Polls `fetchAssessments({ limit: 50 })` every **2000 ms**; the interval pauses when `document.hidden` and a manual "Refresh" button always works. Polling errors show a status line ("Dashboard updates paused — API unreachable") without destroying the last good table.
- Renders `AssessmentTable`: one row per assessment, newest first, columns: assessed time (local), decision badge, score, stage, masked VPA (`maskVpa`), truncated token ID, truncated assessment ID, latency badge ("12 ms"), revoke lifecycle badge (action status; `null` renders "—"), demo badge ("simulated" when `is_demo_event`), reason chips.
- Selecting a row opens a drill-down panel: full reason list (`ReasonChips`) and the `AuditChainPanel` for `aggregate_type=risk_assessment`, `aggregate_id=<assessment_id>` — showing the event chain and the verification badge ("Chain verified · N events" in green, or the invalid reason in red).
- Footer (static, honest-claims): "Demo build: data is simulated and local-only. Dashboard has no authentication and shows no raw customer data." plus the architecture's limitations pointer.
- No client-side computation of risk; no filters beyond the API's; no write actions anywhere on the page.

### 5.3 Components

| Component | Responsibility | Rules |
|---|---|---|
| `AssessmentTable` | rows + drill-down selection | renders only mapped fields; masked/truncated IDs; `reasons` passed to `ReasonChips`; empty state "No assessments yet" |
| `ReasonChips` | triggered reasons as chips | chip label = `reason_code`, tooltip/body = `reason_text`, suffix `+points`; never renders a rule that is not triggered |
| `StatusBadges` | decision / action lifecycle / demo / latency badges | fixed colour mapping: ALLOW green, CHALLENGE amber, BLOCK red; QUEUED/IN_PROGRESS/RETRYING amber, SUCCEEDED green, FAILED red; "simulated" badge always visible on demo rows |
| `AuditChainPanel` | chain + verify badge | loads once per selection; shows sequence, event type, actor, time per event; verification result via the verify endpoint; a failed verification is a prominent red panel |

## 6. Web demo page (`/demo`)

- Renders the **seven architecture demonstration scenarios** as a checklist with their expected outcomes (legitimate flow, bot burst, NPCI-risk rejection, confirmed high-risk block, duplicate webhook, worker failure/recovery, shared demo merchant velocity), each with a one-line "what you should see" description drawn from the architecture plan.
- **Live counters** derived from `fetchAssessments({ limit: 100 })`: totals by decision, count of `simulated` rows, average and p95 of `evaluation_latency_ms`, count of rows with a revoke lifecycle (and how many `SUCCEEDED`). Recomputed on every poll; never claimed to be a full-table statistic (the header says "last 100 assessments").
- **A9 control slot (feature-detected):** on mount, `GET /v1/demo/scenarios`. If it responds 200, render its scenario buttons and the documented A9 contract (A9's specification owns the exact shape; A8 renders whatever it declares there and must not parse deeper than the documented fields). If it responds 404 or errors, render the disabled panel: "Automated demo controls are not installed yet — provided by the A9 simulator package." The page must never break because A9 is absent.
- Links to `/checkout` ("drive a scenario manually") and back to `/dashboard`.
- Footer: the same honest demo labelling as the dashboard.

## 7. Backend route integration tests (`tests/integration/test_dashboard_api.py`)

Use A1's `TEST_POSTGRES_URL` setup and FastAPI `TestClient` against the real app. Seed data through A1 repositories (sessions, events, snapshots, assessments with rule rows, action requests/attempts) using fixed synthetic values — including one assessment with a tamper-broken audit chain for the verify-badge test.

```text
test_dashboard_assessments_route_returns_redacted_projection
test_dashboard_route_maps_only_listed_fields
test_dashboard_route_includes_latency_from_assessment_contract
test_dashboard_limit_is_validated_1_to_100
test_dashboard_decision_filter_filters
test_dashboard_offset_paginates
test_dashboard_route_never_contains_hashes_or_payload_json
test_audit_events_route_lists_chain_newest_first
test_audit_verify_route_reports_valid_chain
test_audit_verify_route_reports_tampered_chain
test_unknown_aggregate_type_is_rejected_422
test_missing_aggregate_verifies_as_valid_zero
test_dashboard_routes_are_get_only_and_set_no_store
```

`test_dashboard_route_maps_only_listed_fields` asserts the exact response key set for an item (no extra keys), which is the automated guard for the field-by-field mapping rule. `test_dashboard_route_never_contains_hashes_or_payload_json` string-searches the full response body for `hmac-sha256:`, `sha256:`, and the seeded raw fixture values (they must not appear).

## 8. Web tests

### 8.1 Unit tests — `tests/unit/dashboard-api.test.ts` (Vitest, fetch stubbed)

```text
test_fetch_assessments_calls_the_dashboard_endpoint_with_query_params
test_mask_vpa_renders_bullet_handle_format
test_mask_vpa_passes_through_null
test_truncate_id_shows_first_eight_chars
test_verify_audit_chain_maps_the_verification_result
test_audit_events_error_kinds_match_api_error_taxonomy
test_every_request_sends_uuid_x_request_id_and_no_store
```

### 8.2 Playwright e2e — `tests/e2e/dashboard.spec.ts`

Prerequisites: stack running (`docker compose up -d postgres redis`; API started with `RAZORPAY_WEBHOOK_SECRET=local-test-secret` on :8000; web on :3000). Data is seeded through the real signed-webhook path, proving the full loop end to end:

1. Load `data/fixtures/webhooks/token_confirmed_body.json` via `node:fs`; sign the exact bytes with `node:crypto` HMAC-SHA256 using `local-test-secret`.
2. POST it to `/v1/webhooks/razorpay` with `x-razorpay-signature`, a fresh `x-razorpay-event-id: evt_e2e_<Date.now()>` per delivery, and `X-Demo-Event: true`.
3. For the full-loop test, first create ~21 sessions via the A2 API in namespace `demo_merchant_e2e_dash` sharing one device fingerprint and run each to pre-check (this raises IP/device 5m+1h velocity so the correlated webhook assessment scores 100 → `BLOCK`), then send the confirmed webhook with `entity.notes.risk_session_id` pointing at a 22nd session — A3 correlates, A5 scores 100/BLOCK and queues the revoke, A6's mock worker completes it.

```text
test_dashboard_renders_seeded_assessments_with_masked_fields
  -> after one signed webhook (evaluation completed), /dashboard shows the row with a
     masked VPA handle, truncated token/assessment IDs, an ALLOW decision badge and a
     latency badge; no full UUID anywhere.

test_dashboard_hides_hashes_and_secrets
  -> assert the page HTML never contains "hmac-sha256", "sha256:", the webhook secret
     string, or the seeded raw fixture values.

test_dashboard_polls_and_updates
  -> count rows, seed another webhook, expect the count to grow within ~4 seconds
     (two poll intervals) without a manual reload.

test_dashboard_shows_block_with_reasons_and_revoke_lifecycle
  -> after the full-loop seed: a BLOCK row with reason chips (velocity reasons),
     action lifecycle badge progressing to SUCCEEDED, and the "simulated" badge.
     Skips when the webhook answers evaluation "not_configured" (A5 absent).

test_audit_chain_panel_verifies_for_an_assessment
  -> select the seeded row, expect the audit panel with the chain and the green
     "Chain verified" badge.

test_demo_page_shows_checklist_and_missing_controls_message
  -> /demo lists all seven scenarios with expected outcomes and shows the A9
     "not installed yet" panel while /v1/demo/scenarios returns 404.
```

Playwright config follows A7's (`baseURL http://localhost:3000`, retries 0, health-check skip when the API is down).

### 8.3 Verification commands

```powershell
uv --directory apps/api sync --all-groups
npm --prefix apps/web install
uv --directory apps/api run ruff check app/api/routes/dashboard.py tests/integration/test_dashboard_api.py
uv --directory apps/api run ruff format --check app/api/routes/dashboard.py
uv --directory apps/api run mypy app/api/routes/dashboard.py
uv --directory apps/api run python ../../scripts/export_openapi.py
npm --prefix apps/web run generate:api
npm --prefix apps/web run lint
npm --prefix apps/web run build
npm --prefix apps/web run test
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
uv --directory apps/api run pytest tests/integration/test_dashboard_api.py -q
# then start API (with RAZORPAY_WEBHOOK_SECRET=local-test-secret) and web, and run:
npm --prefix apps/web run test:e2e
docker compose down
```

Do not mark the package done until all pass, including the regenerated-artifact commit.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A8 use |
|---|---|---|
| A0 | contracts, fixtures, OpenAPI export flow, generated types | Types only from `api-types.ts`; regenerates via the sanctioned commands for its own endpoints. |
| A1 | `DashboardReadRepository`, `AuditRepository`, `RiskAssessmentRepository`, `transaction()` | All reads; redaction guarantee inherited, field-by-field mapping enforced. |
| A5 | persisted assessments, rule rows, `evaluation_latency_ms` | Displayed as score/reasons/latency; never recomputed. |
| A6 | `ActionAttempt`/request statuses via A1 | Revoke lifecycle badges; no worker interaction. |
| A7 | Tailwind setup, page conventions, landing link | Visual consistency; no file edits. |
| A9, later | `GET /v1/demo/scenarios` (+ its documented run contract) | Feature-detected control slot; A8 renders, A9 executes. |

### Outputs produced

| Consumer | Receives from A8 | Integration rule |
|---|---|---|
| Reviewers/operators | The visual proof surface for the architecture's demo suite | The 7 scenarios are checklisted with live counters and expected outcomes. |
| A9 simulator | The demo page shell and the feature-detection contract | A9 implements the control endpoint; no A8 edits required. |
| A10 CI/security | route redaction tests + e2e no-secret assertions | Added to CI as-is; A10 also owns the deferred dashboard authentication. |
| A12 narrative (optional) | dashboard read patterns | Read-only consumer of the same A1 data. |

### Merge order

1. A0 and A1 must be merged; A8's route tests also need seeded A5/A6-shaped data (created via A1 repositories in tests, so A5/A6 need not be merged first).
2. A8 merges in parallel with A3–A7; the only shared backend file is `main.py` (one `include_router` line).
3. The full BLOCK/lifecycle e2e completes once A3–A6 are merged; until then those tests skip by design while the redaction and rendering tests pass.

### Pull-request handoff checklist

State in the A8 PR: the three endpoints with exact response schemas; the field-by-field redaction mapping and its automated guard; the latency-enrichment tradeoff (bounded N+1); polling behaviour and interval; the masked/truncated display rules; the demo checklist content and A9 feature-detection; screenshots of `/dashboard` (with ALLOW and BLOCK rows) and `/demo`; the regenerated `openapi.json`/`api-types.ts` diff; results for all commands in Section 8.3; confirmation that no A0/A1/A7-owned files were modified beyond the sanctioned `main.py` line and generated artifacts; and known deferred work (dashboard authentication — A10, A9 run controls, real-time push instead of polling).

## 10. Failure conditions

The A8 work is unsafe or incomplete if any of these occur:

- Any hash value, webhook body, signature, raw identifier or payload JSON appears in an API response or rendered page; or a response is built by forwarding a repository row wholesale instead of field-by-field mapping.
- The dashboard exposes a write path or can trigger decisions/actions.
- Latency, reasons, lifecycle or the demo/simulated badge are missing from the latest-50 view — the architecture's core A8 acceptance criterion.
- The demo page renders A9 controls as available when they are absent, or breaks when A9 is not installed.
- The audit-chain verification badge is shown without calling the verify endpoint, or a tampered chain renders as verified.
- A8 edits A0/A1/A7-owned files (beyond the sanctioned `main.py` line and regenerated artifacts), hand-edits generated types, adds dependencies, or implements A9's simulator/control endpoints.
