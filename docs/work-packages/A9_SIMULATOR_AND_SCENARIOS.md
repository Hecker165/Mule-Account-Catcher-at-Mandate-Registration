# A9 — Simulator, labelled scenarios and demo control API: implementation specification

## 0. Agent instruction

You are the **A9 simulator and scenarios agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A2_SESSION_CAPTURE_AND_PRECHECK_API.md`
5. `docs/work-packages/A3_RAZORPAY_WEBHOOK_GATEWAY.md`
6. `docs/work-packages/A5_RULE_ENGINE_AND_EVALUATION.md`
7. `docs/work-packages/A6_REVOKE_ADAPTER_AND_OUTBOX_WORKER.md`
8. `docs/work-packages/A8_DASHBOARD_AND_DEMO_UX.md`
9. `docs/contracts.md`

Implement exactly this work package. You build the seven architecture demonstration scenarios as declarative definitions in `data/scenarios/`, the runner that executes them reproducibly against the real running stack, the demo control API that A8's demo page feature-detects, and the two offline scripts (`generate_demo_events.py`, `benchmark_pipeline.py`).

Do **not** implement risk logic, change contracts, edit A1-owned files, build UI (A8 renders your API), or touch the dashboard pages. Every scenario result must be honest: a check that cannot pass is reported as failed with a clear detail, never papered over.

## 1. Why this package exists

The architecture's demonstration suite is the buildathon's proof:

1. **Legitimate flow** — 20 distinct normal sessions, all `ALLOW`, zero false positives.
2. **Bot burst** — registrations sharing device/IP with a sub-3-second flow; at least 18 of 24 challenged, borderline cases explained.
3. **NPCI-risk rejected event** — score 100 with the strongest risk reason, clearly not revoked (the mandate was already rejected).
4. **Confirmed high-risk event** — `BLOCK`, audit chain, exactly one queued revocation.
5. **Duplicate webhook** — replay of the same signed payload leaves one event, one assessment, one revoke.
6. **Worker failure and recovery** — first revoke call fails, retry succeeds, no duplicate revocation.
7. **Shared demo merchant velocity** — one device at three simulated merchants; the third session is challenged with the labelled demo counter.

A9 makes these reproducible with one click (via A8's demo page) or one command, and proves the performance targets with the benchmark script.

## 2. Objective and definition of done

Definition of done:

- `GET /v1/demo/scenarios` lists exactly the seven scenarios with name, title, description and expected outcome; `POST /v1/demo/scenarios/{name}/run` executes synchronously and returns a per-check verdict; unknown names 404; concurrent runs of the same scenario 409; the whole router is hidden (404) when `app_env == "production"`.
- All seven scenarios run green against the real stack (PostgreSQL + Redis + API + mock revoke client) and re-runs are safe or honestly explain window contamination.
- Scenario definitions are declarative JSON in `data/scenarios/` — the single source consumed by both the runner and the generator script.
- Webhook scenarios sign payloads **server-side** with the configured `RAZORPAY_WEBHOOK_SECRET`; with no secret configured they fail with an explicit "webhook signature verification is not configured" check instead of pretending.
- `scripts/generate_demo_events.py` writes reproducible signed webhook/session files from a scenario definition; `scripts/benchmark_pipeline.py` reports webhook and pre-check p50/p95 against the architecture targets without failing on slower machines.
- No raw VPA/IP/secret appears in scenario results, generated files, logs or the demo API responses; the webhook secret never leaves the server process.
- Route integration tests and scenario integration tests all pass against the real stack.

## 3. Scope and exact file ownership

### You own

```text
data/scenarios/legitimate_flow.json
data/scenarios/bot_burst.json
data/scenarios/npci_risk_rejection.json
data/scenarios/confirmed_high_risk_block.json
data/scenarios/duplicate_webhook.json
data/scenarios/worker_failure_recovery.json
data/scenarios/shared_demo_merchant_velocity.json
data/scenarios/README.md
scripts/generate_demo_events.py          # replaces the A0 scaffold placeholder
scripts/benchmark_pipeline.py            # replaces the A0 scaffold placeholder
apps/api/app/services/demo_runner.py
apps/api/app/api/routes/demo.py
apps/api/app/repositories/demo_read.py
apps/api/tests/integration/test_demo_scenarios.py
apps/api/tests/integration/test_demo_route.py
```

### The one new file in A1's directory (documented exception, A6 precedent)

`apps/api/app/repositories/demo_read.py` is a **new** read-only repository file — it modifies no A1-owned file. It provides the action-request lookups the scenarios need (A1 has no lookup by mandate event). Style: constructor takes `AsyncSession`, `select`-only reads, never writes, never commits.

### You may update minimally

```text
apps/api/app/main.py   # only: include_router(demo.router, prefix="/v1")
```

### You may read but must not modify

```text
apps/api/app/contracts/**, app/core/**, app/persistence/**       # A0/A1
apps/api/app/repositories/**            # A1 interfaces + A6's ActionAttemptReadRepository (imported, not edited)
apps/api/app/services/**                # A2-A6, A8-related services (imported, not edited)
apps/api/app/api/routes/**              # A2/A3/A8 routes
apps/api/app/state                      # app.state.token_revoke_client (A6) is READ via the route
data/fixtures/**, apps/web/**, docs/**  # A0/A7/A8
docker-compose.yml, Makefile, pyproject.toml, package.json   # A0; no dependency changes
```

### Explicit non-goals

- No dashboard UI (A8 renders your API), no CI changes (A10), no ML/LLM work.
- No new dependencies (`httpx` is already declared; scenario HTTP calls use it).
- No writes through the ORM: scenario verification reads go through A1 repositories (including `get_for_event`, `verify_aggregate`) and the new read-only `demo_read.py`.
- No secrets in results, generated files or logs; the webhook secret and signing happen only inside the runner process.
- No scenario "cheats": the runner uses the same public HTTP endpoints a real integration would use (plus server-side signing), never internal shortcuts that skip signature verification or idempotency.

## 4. Demo control API (`routes/demo.py`)

Tagged `demo`, all responses `Cache-Control: no-store`. Registered under `/v1` via `main.py`. **Production guard:** every handler returns 404 when `get_settings().app_env == "production"` — the demo surface simply does not exist there.

### 4.1 `GET /v1/demo/scenarios`

```json
{
  "scenarios": [
    {
      "name": "bot_burst",
      "title": "Bot burst",
      "description": "24 registrations sharing one device and the same peer IP with a 2-second flow.",
      "expected_outcome": "At least 18 of 24 sessions challenged; the first 4 borderline ALLOWs are explained."
    }
  ]
}
```

The list is loaded from the JSON definitions in `data/scenarios/` (repository root resolved relative to the runner module; overridable via constructor for tests). A missing/unreadable definitions directory is a 500 with a fixed message, never a silent empty list.

### 4.2 `POST /v1/demo/scenarios/{name}/run`

Synchronous execution (the longest scenario, worker recovery, takes about 6-10 seconds locally).

```json
{
  "name": "bot_burst",
  "status": "SUCCEEDED",
  "started_at": "2026-01-15T10:00:00Z",
  "finished_at": "2026-01-15T10:00:03Z",
  "duration_ms": 3120,
  "checks": [
    { "check": "at_least_18_challenged", "passed": true, "detail": "20/24 sessions challenged." },
    { "check": "borderline_allows_explained", "passed": true, "detail": "Sessions 1-4 scored 20 (flow duration only) and are intentionally below the challenge threshold." }
  ]
}
```

- Unknown `name` -> `404 {"detail":"unknown demo scenario"}`.
- Same scenario already running -> `409 {"detail":"scenario run already in progress"}`. Implemented with a module-level `dict[str, asyncio.Lock]` exposed on the runner as `runner.locks` so tests can hold a lock deterministically.
- Any unexpected exception -> `status: "FAILED"` with a check named `unexpected_error` (fixed message, exception class name only) — the route itself still returns 200; scenario failure is data, not an HTTP error.
- This endpoint creates real sessions/events (the one sanctioned demo write surface); the production guard above is what keeps it out of real deployments.

## 5. Scenario definitions (`data/scenarios/*.json`)

Common schema: `{"name", "title", "description", "expected_outcome", "params": {...}}`. These files are the single source for the runner and the generator script. Exact contents:

**legitimate_flow.json** — 20 distinct normal customers; each isolated in its own demo namespace (`demo_legit_01`..`demo_legit_20`, valid A0 pattern) so the shared localhost peer IP of the demo does not create artificial IP velocity. This is the documented, honest design satisfying "zero false positives for this controlled fixture set"; in production, IP velocity across one merchant's customers is a true signal.

```json
"params": { "namespace_prefix": "demo_legit_", "sessions": 20, "flow_seconds": 30 }
```

**bot_burst.json** — 24 registrations sharing one device and the same peer IP with a 2-second flow.

```json
"params": { "namespace": "demo_bot_burst", "sessions": 24, "shared_device": true, "flow_seconds": 2 }
```

Math (A5 catalogue): sessions 1-4 score 20 (`implausible_flow_duration` only) -> ALLOW; from session 5 the device and IP 5-minute velocity rules add 25+25 -> 70 -> `CHALLENGE`. Sessions 5-24 = 20 of 24 challenged, satisfying "at least 18" with the borderline cases explainable.

**npci_risk_rejection.json**

```json
"params": { "fixture": "token_rejected_npci_risk_body.json", "event_id_prefix": "evt_demo_rejected_", "demo_header": true }
```

**confirmed_high_risk_block.json** — velocity seeding in one namespace, then a confirmed webhook correlated via order notes:

```json
"params": {
  "fixture": "token_confirmed_body.json", "event_id_prefix": "evt_demo_block_", "demo_header": true,
  "seed": { "namespace": "demo_high_risk", "sessions": 21, "shared_device": true, "flow_seconds": 30 }
}
```

**duplicate_webhook.json** — the same signed payload delivered twice:

```json
"params": { "fixture": "token_confirmed_body.json", "event_id_prefix": "evt_demo_dup_", "demo_header": true, "deliveries": 2 }
```

**worker_failure_recovery.json** — one scheduled mock revoke failure, then retry and success:

```json
"params": {
  "fixture": "token_confirmed_body.json", "event_id_prefix": "evt_demo_recovery_", "demo_header": true,
  "seed": { "namespace": "demo_worker_recovery", "sessions": 21, "shared_device": true, "flow_seconds": 30 },
  "scheduled_failures": 1, "poll_timeout_seconds": 30
}
```

**shared_demo_merchant_velocity.json** — one device registering at three simulated demo merchants:

```json
"params": { "namespaces": ["demo_shared_one", "demo_shared_two", "demo_shared_three"], "shared_device": true, "flow_seconds": 30 }
```

## 6. Scenario runner (`services/demo_runner.py`)

### 6.1 Interface

```python
@dataclass(frozen=True)
class ScenarioCheck:
    check: str
    passed: bool
    detail: str

@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: str                  # "SUCCEEDED" | "FAILED"
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    checks: tuple[ScenarioCheck, ...]

@dataclass(frozen=True)
class ScenarioSummary:
    name: str; title: str; description: str; expected_outcome: str

class DemoScenarioRunner:
    def __init__(
        self,
        http: httpx.AsyncClient,             # base_url = http://127.0.0.1:{settings.api_port}; ASGITransport in tests
        webhook_secret: str | None,
        revoke_client: TokenRevokeClient | None,
        scenarios_dir: Path | None = None,   # default: repository root /data/scenarios
        fixtures_dir: Path | None = None,    # default: repository root /data/fixtures/webhooks
        clock: Callable[[], datetime] = ...,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None: ...

    locks: dict[str, asyncio.Lock]
    def list_scenarios(self) -> list[ScenarioSummary]
    async def run(self, name: str) -> ScenarioResult
```

`run` dispatches on the definition's `name` to one of seven private methods. Every result includes at least the checks listed in Section 6.3. `SUCCEEDED` only when all checks pass.

### 6.2 Building blocks (used by the scenario methods)

- **Session driving**: `POST /v1/risk-sessions` (namespace, fresh `checkout_order_ref="demo-"+uuid8`, customer reference `demo-customer-<n>`, device fingerprint per params, `flow_started_at` = now - flow_seconds), `PATCH .../telemetry` (`flow_completed_at` = now), `POST .../precheck`. The pre-check response *is* the `RiskAssessment` used for session-based checks.
- **Webhook driving**: load the fixture file as raw bytes, inject `"notes": {"risk_session_id": "<uuid>"}` into `payload.token.entity` when a seed/correlation is needed (the body is signed *after* modification, exactly as A3 requires), sign `HMAC-SHA256(secret, body).hexdigest()`, POST to `/v1/webhooks/razorpay` with `x-razorpay-signature`, a fresh `x-razorpay-event-id` = `{event_id_prefix}{uuid}` and `X-Demo-Event: true` when `demo_header` is set.
- **Signing guard**: when `webhook_secret is None`, webhook scenarios immediately return `FAILED` with check `webhook_secret_configured: false, detail "webhook signature verification is not configured"` — an honest, visible outcome (this mirrors A3's fail-closed behaviour).
- **Verification reads** (inside `async with transaction() as session`): `RiskAssessmentRepository.get_for_event(mandate_event_id, stage, "rules-v1")`, `AuditRepository.verify_aggregate(...)`, `DemoReadRepository.get_action_request_for_mandate_event(...)`, `count_action_requests_for_mandate_event(...)`, and A6's `ActionAttemptReadRepository.count_attempts(...)`.
- **Failure injection**: `worker_failure_recovery` calls `revoke_client.schedule_failures(params["scheduled_failures"])` **before** sending the webhook (so the worker's first attempt always hits the injected failure). If the client is not a `MockTokenRevokeClient` (no `schedule_failures`), the check `failure_injection_available` fails honestly with "real Razorpay client configured".

### 6.3 Per-scenario checks

| Scenario | Checks (all must pass for SUCCEEDED) |
|---|---|
| `legitimate_flow` | `all_sessions_allowed` (20/20 pre-checks returned ALLOW) · `zero_false_positives` (no CHALLENGE/BLOCK among them) |
| `bot_burst` | `at_least_18_challenged` (>= 18 of 24) · `borderline_allows_explained` (the ALLOW sessions are exactly the low-velocity prefix, with their scores in the detail) |
| `npci_risk_rejection` | `webhook_processed` (response `status=="processed"`) · `score_100_block` (`get_for_event` -> score 100, decision BLOCK, stage REJECTION_AUDIT) · `npci_reason_present` (rule `npci_risk_rejection` in `rule_evaluations`) · `no_revoke_queued` (`count_action_requests_for_mandate_event == 0`) |
| `confirmed_high_risk_block` | `webhook_processed` · `score_100_block` · `exactly_one_revoke` (count == 1) · `revoke_lifecycle_started` (status in QUEUED/IN_PROGRESS/RETRYING/SUCCEEDED) · `audit_chain_valid` (`verify_aggregate` on the assessment aggregate) |
| `duplicate_webhook` | `first_delivery_processed` · `second_delivery_duplicate` (status `duplicate` + `X-Idempotent-Replay: true` header) · `single_event_and_assessment` (provider event lookup returns one row; `get_for_event` returns one assessment) |
| `worker_failure_recovery` | `failure_injection_available` · `first_attempt_retried` (attempt 1 RETRYING with `UPSTREAM_SERVER_ERROR` via audit/attempt reads) · `final_status_succeeded` (poll request status every 500 ms up to `poll_timeout_seconds`) · `exactly_two_attempts` · `no_duplicate_revocation` (one action request, one successful attempt) |
| `shared_demo_merchant_velocity` | `third_session_challenged` · `demo_counter_is_three` (the third pre-check assessment's `shared_demo_merchant_count_1h == 3`) · `demo_source_labelled` (`sources["shared_demo_merchant_count_1h"].availability == DEMO_SIMULATED`) |

Re-run safety: session counts per namespace stay below the velocity thresholds for legitimate/shared scenarios only if Redis windows have drained; each run uses fresh device fingerprints (UUID) for the shared-device scenarios' shared value only where the scenario requires sharing — the runner uses a **fresh shared device per run** (`demo-device-<uuid8>`) so shared-merchant counts are exactly 3, and documents in the result detail that re-running within 5 minutes may raise velocity counts (bot burst remains green; legitimate flow stays green up to four runs per 5-minute window).

## 7. Offline scripts

### 7.1 `scripts/generate_demo_events.py`

CLI:

```text
uv --directory apps/api run python ../../scripts/generate_demo_events.py --scenario bot_burst --out-dir data/scenarios/generated
uv --directory apps/api run python ../../scripts/generate_demo_events.py --scenario all
Flags: --scenario {name|all}, --out-dir (default data/scenarios/generated),
       --secret (default "local-test-secret"; REFUSES any value starting with "rzp_live"),
       --base-time (ISO UTC, default 2026-01-15T10:00:00Z, used for deterministic timestamps)
```

Behaviour: loads the scenario definition from `data/scenarios/`, and writes one directory per scenario containing:

```text
sessions.jsonl        # one RiskSessionCreateRequest body per line, in execution order
telemetry.jsonl       # the matching PATCH bodies (same order)
webhooks.jsonl        # {"body": "<exact raw body string>", "signature": "<hex>", "headers": {...}} per delivery
manifest.json         # scenario name, generated-at, counts, secret fingerprint (first 4 chars only)
```

Webhook bodies are built exactly as the runner does (fixture bytes, optional notes injection, signed after modification). The script never calls the API and never touches Redis/Postgres. It prints a fixed warning that outputs are for local demo replay only. Determinism: same `--base-time` and same script version produce identical files apart from the random UUID/event-id suffixes, which are documented in the manifest.

### 7.2 `scripts/benchmark_pipeline.py`

CLI:

```text
uv --directory apps/api run python ../../scripts/benchmark_pipeline.py --base-url http://127.0.0.1:8000 --samples 100
```

Prerequisites: full stack running (API + PostgreSQL + Redis) with `RAZORPAY_WEBHOOK_SECRET` configured; `--secret` flag as in the generator.

Measurements (architecture targets from its Section 9):

1. **Webhook-to-persisted assessment**: send `--samples` (default 100) signed `token_confirmed_body.json` deliveries with unique event IDs and `X-Demo-Event: true`; measure the POST round-trip (the assessment is persisted synchronously inside the webhook). Report n, p50, p95 in milliseconds; compare against the **200 ms** target.
2. **Pre-check**: create + telemetry + pre-check for `--samples` sessions in namespace `demo_benchmark` (fresh device per session); measure only the pre-check call; compare against **150 ms**.

Output: one JSON object to stdout (`{"webhook": {"n", "p50_ms", "p95_ms", "target_ms", "within_target"}, "precheck": {...}}`) plus a human-readable summary. Exit code 0 even when a target is missed — the machine context is printed and the architecture explicitly requires showing real measured numbers instead of failing. Exclude third-party network latency by construction (mock revoke client, local stack). Never include secrets or full request bodies in the output.

## 8. Tests and acceptance criteria

Route tests use `httpx.ASGITransport` over the real `create_app()` (no running server needed); scenario tests need the full stack (PostgreSQL + Redis + mock worker). Both read `TEST_POSTGRES_URL` per A1's setup. The app under test must be started with `RAZORPAY_WEBHOOK_SECRET=local-test-secret` (monkeypatched env before `create_app`) so webhook scenarios exercise real signing.

### 8.1 Route tests — `tests/integration/test_demo_route.py`

```text
test_demo_scenarios_route_lists_seven_scenarios
test_demo_scenario_list_matches_definition_files
test_demo_route_unknown_scenario_returns_404
test_demo_route_is_hidden_when_app_env_is_production
test_demo_route_concurrent_run_returns_409
test_demo_route_sets_no_store
test_scenario_result_shape_has_checks_and_timings
```

`test_demo_route_concurrent_run_returns_409` acquires `runner.locks["legitimate_flow"]` directly, POSTs the run, and expects 409 — deterministic without timing races.

### 8.2 Scenario tests — `tests/integration/test_demo_scenarios.py`

These are the architecture demonstration suite as automated tests. Order-independent; each uses the runner exactly as the route does.

```text
test_legitimate_flow_all_allow_zero_false_positives
test_bot_burst_challenges_at_least_18_of_24
test_bot_burst_borderline_allows_are_explained
test_npc_i_risk_rejection_scores_100_without_revoke
test_confirmed_high_risk_block_queues_exactly_one_revoke
test_confirmed_high_risk_block_audit_chain_verifies
test_duplicate_webhook_yields_single_event_and_assessment
test_worker_failure_recovery_retries_then_succeeds
test_worker_failure_recovery_has_exactly_two_attempts
test_shared_demo_merchant_velocity_challenges_third_session
test_webhook_scenarios_fail_honestly_without_secret
test_scenario_results_carry_checks_and_timings
```

`test_npc_i_risk_rejection_scores_100_without_revoke` is intentionally spelled with the underscore-safe test-name tokenisation of `NPCI` (`test_npc_i_...`); keep it exactly as written. `test_worker_failure_recovery_*` asserts the ~5 s backoff path and therefore may be the slowest test in the suite; do not reduce A6's backoff configuration to speed it up.

### 8.3 Script tests (inside `test_demo_scenarios.py`)

```text
test_generate_demo_events_writes_signed_files_for_a_scenario
test_generate_demo_events_refuses_live_secrets
test_benchmark_pipeline_reports_percentiles_json
```

The generator test runs the script via `subprocess` against a temp out-dir and verifies the signature in `webhooks.jsonl` recomputes correctly over the body bytes. The benchmark test runs it with `--samples 5` against the live stack and asserts the JSON keys exist.

### 8.4 Verification commands

```powershell
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
uv --directory apps/api run ruff check app/api/routes/demo.py app/services/demo_runner.py app/repositories/demo_read.py scripts/generate_demo_events.py scripts/benchmark_pipeline.py
uv --directory apps/api run ruff format --check app/api/routes/demo.py app/services/demo_runner.py app/repositories/demo_read.py scripts/generate_demo_events.py scripts/benchmark_pipeline.py
uv --directory apps/api run mypy app/api/routes/demo.py app/services/demo_runner.py app/repositories/demo_read.py
uv --directory apps/api run pytest tests/integration/test_demo_route.py -q
$env:RAZORPAY_WEBHOOK_SECRET = "local-test-secret"
uv --directory apps/api run pytest tests/integration/test_demo_scenarios.py -q
uv --directory apps/api run python ../../scripts/generate_demo_events.py --scenario all --out-dir data/scenarios/generated
uv --directory apps/api run python ../../scripts/benchmark_pipeline.py --samples 100
docker compose down
```

Do not mark the package done until all pass. The benchmark output (p50/p95 for webhook and pre-check) must be pasted into the PR — it is the architecture's performance evidence.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A9 use |
|---|---|---|
| A0 | contracts, webhook fixtures, settings | Fixture bytes for signing; `app_env`, `api_port`, `razorpay_webhook_secret` from settings. |
| A1 | repositories, `verify_aggregate`, `transaction()` | Read-only verification inside scenarios; `demo_read.py` is additive. |
| A2/A3 | session lifecycle and webhook endpoints | The runner drives the same public HTTP contract as a real integration. |
| A5 | rule catalogue maths | Scenario thresholds/expectations are derived from the documented points; A9 never changes them. |
| A6 | `app.state.token_revoke_client`, attempt reads, backoff config | Failure injection and lifecycle assertions; imported, never edited. |
| A8 | the feature-detected control slot | A9's `GET /v1/demo/scenarios` is the contract A8 renders; A8 needs no changes when A9 lands. |

### Outputs produced

| Consumer | Receives from A9 | Integration rule |
|---|---|---|
| A8 demo page | scenario list + synchronous run results with per-check verdicts | Rendered as buttons and check lists; A8's feature detection already expects this shape. |
| A10 CI/security | scenario suite + benchmark evidence | The scenario tests are the end-to-end regression suite; benchmark output is pasted into PRs/submission. |
| Reviewers/judges | reproducible demo (`one click` via A8, or scripts) | The 7 architecture scenarios become executable proof. |

### Merge order

1. A0, A1 and A2 are required; the webhook scenarios additionally require A3, A5 and A6 to be merged (or they fail honestly per Section 6.2's signing/evaluator guards — tests distinguish "not installed" from "broken").
2. A9 merges in parallel with A7/A8; the only shared file is `main.py` (one `include_router` line).
3. Add `data/scenarios/generated/` to `.gitignore` — a one-line additive change to A0's `.gitignore`, documented in the PR like the earlier exceptions.

### Pull-request handoff checklist

State in the A9 PR: the demo control API contract; the seven scenario definitions and their expected-outcome maths (including the namespace-isolation rationale for the legitimate flow and the borderline-ALLOW explanation for the bot burst); the honest-failure behaviour without a webhook secret or with a real revoke client; the benchmark p50/p95 results against the 200 ms/150 ms targets; results for all commands in Section 8.4; confirmation that `demo_read.py` is new and read-only and no A1/A5/A6/A8-owned files were modified; and known deferred work (longer-running soak scenarios, replay of generated JSONL against a fresh stack, A10 CI integration).

## 10. Failure conditions

The A9 work is unsafe or incomplete if any of these occur:

- A scenario reports success while a check failed, or hides a failing check behind an HTTP error.
- The runner bypasses signature verification, idempotency or any public API contract to make a scenario pass (internal service calls that skip A3's pipeline are forbidden).
- The webhook secret or any raw VPA/IP appears in scenario results, generated files, logs or the benchmark output.
- Scenario parameters diverge from the architecture's demonstration suite (e.g. fewer than 24 burst sessions without explanation, or a legitimate flow that tolerates false positives).
- The demo control API is reachable when `app_env == "production"`.
- A9 edits A0/A1/A5/A6/A8-owned files (beyond the sanctioned `main.py` line and the `.gitignore` line), changes thresholds, or implements dashboard UI.
- Re-running a scenario is destructive (duplicated revocations, duplicated assessments) rather than idempotent or honestly explained.
