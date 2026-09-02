# A10 — Security, observability and CI: implementation specification

## 0. Agent instruction

You are the **A10 security, observability and CI agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md` (sections 9, 10 and 11 in particular)
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A3_RAZORPAY_WEBHOOK_GATEWAY.md`
5. `docs/work-packages/A6_REVOKE_ADAPTER_AND_OUTBOX_WORKER.md`
6. `docs/work-packages/A8_DASHBOARD_AND_DEMO_UX.md`
7. `docs/work-packages/A9_SIMULATOR_AND_SCENARIOS.md`
8. `docs/contracts.md`

Implement exactly this work package. You build the GitHub Actions pipeline that enforces lint/type/test/contract/secret checks on every push, the redaction and secret-hygiene test suite that proves logs and code never carry raw VPAs or API secrets, a minimal zero-dependency metrics surface, and the three governance documents (threat model, limitations and claims, demo runbook).

Do **not** implement authentication, rate limiting or WAF features (they are documented deferrals), change any other agent's code, add runtime dependencies, or weaken any existing test. Security enforcement here is by test and pipeline, not by promise.

## 1. Why this package exists

The architecture's acceptance criterion for A10: *"Secret scan/lint/type/unit checks run in CI; logs do not contain raw VPAs or API secrets."* Everything before A10 built capabilities; A10 makes them continuously verifiable and the claims honest:

```text
every push / PR
  -> CI: lint + format + types + unit + contract + integration + security tests (API)
        : lint + unit + build (web) + contract freshness
        : gitleaks secret scan + dependency audit report
        : docker compose config + Playwright e2e on the real stack
  -> repo: /metrics endpoint + Prometheus config (bounded cardinality, no identifiers)
  -> docs: threat model, limitations-and-claims, demo runbook
```

The redaction guarantee is enforced where it can be tested: static source scans (no secret-printing patterns), dynamic log capture (a fabricated raw VPA sent through fail-closed webhook paths never appears in any log record), settings hygiene (`SecretStr` repr leakage), and the audit-chain scale target (1,000 events verified in under 5 seconds).

## 2. Objective and definition of done

Definition of done:

- `.github/workflows/ci.yml` runs on push and pull requests with jobs: `api` (lint/format/mypy/unit+contract, then integration+security against PostgreSQL/Redis service containers), `web` (lint/test/build), `contracts` (regenerate OpenAPI + browser types and fail on any diff), `security` (gitleaks + dependency audit report), `compose` (config validation), and `e2e` (full stack + Playwright).
- CI passes on the repository as merged; secrets in CI are dummy local values only (`local-test-secret`, `mandate_guardian`), never real credentials.
- `GET /metrics` exposes Prometheus text-format counters (`http_requests_total`, `http_request_duration_seconds_count/_sum`) with method/route-template/status labels only — no client IDs, no paths with embedded values, bounded cardinality.
- Logging is centrally configured (`core/logging.py`); no log statement anywhere in `apps/api/app` or `apps/web/src` can print a request body, signature, secret or raw identifier — proven by static and dynamic tests.
- `tests/security/` proves: settings repr leaks nothing, fabricated raw-VPA webhooks leave no trace in logs, the static scan patterns hold, and `.env.example` contains only placeholder values.
- `tests/performance/test_audit_verification_scale.py` proves 1,000 audit events verify in under 5 seconds locally.
- The three documents are complete and honest: threat model (STRIDE-lite over the real data flows), limitations and claims (the architecture's guardrail phrasing verbatim in spirit), and a demo runbook a judge can follow in 10 minutes.
- No file outside the owned paths changes except the four documented `main.py` lines in Section 3.

## 3. Scope and exact file ownership

### You own

```text
.github/workflows/ci.yml                  # replaces the A0 scaffold placeholder
docs/threat-model.md
docs/limitations-and-claims.md
docs/demo-runbook.md
infra/observability/prometheus.yml
infra/observability/README.md
apps/api/app/core/metrics.py
apps/api/app/core/logging.py
apps/api/app/api/routes/metrics.py
apps/api/tests/security/__init__.py
apps/api/tests/security/test_log_redaction.py
apps/api/tests/security/test_static_secret_scan.py
apps/api/tests/performance/test_audit_verification_scale.py
```

### New-file exceptions in other agents' directories (A6/A9 precedent)

Three **new** files that overwrite no existing file: `apps/api/app/core/metrics.py`, `apps/api/app/core/logging.py` (A0 reserved this exact name as a possible small addition), and `apps/api/app/api/routes/metrics.py`. None modifies an A0-owned file's content.

### You may update minimally

```text
apps/api/app/main.py   # only three documented additions in Section 5.3
```

### You may read but must not modify

```text
apps/api/app/**                          # all other packages' code; A10 tests may import and execute it
apps/web/**                              # scanned by A10's static tests, never edited
data/**, docs/contracts.md, docs/work-packages/**   # A0/A9
docker-compose.yml, Makefile, .env.example, .gitignore, pyproject.toml, package.json   # A0; no changes
```

### Explicit non-goals

- No authentication, authorisation, rate limiting, WAF or SIEM integration — each is listed in the threat model as a documented deferral with its residual risk.
- No new Python or npm dependencies: metrics are hand-rolled Prometheus text format; secret scanning in CI uses the gitleaks *action* (pipeline tooling, not a repo dependency).
- No changes to other agents' tests, services, routes or the dashboard; A10 observes and enforces, never rewrites.
- No real credentials anywhere: CI uses `local-test-secret` and `mandate_guardian` only.
- No per-request logging of bodies, headers (other than `X-Request-ID`), signatures or credentials — enforced, not assumed.

## 4. CI pipeline (`.github/workflows/ci.yml`)

Triggers: `on: push` (branches `main`) and `on: pull_request`. All jobs on `ubuntu-latest`. The exact job matrix:

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:

jobs:
  api:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env: { POSTGRES_USER: mandate_guardian, POSTGRES_PASSWORD: mandate_guardian, POSTGRES_DB: mandate_guardian }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U mandate_guardian" --health-interval 5s
          --health-timeout 5s --health-retries 10
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping" --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      TEST_POSTGRES_URL: postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian
      TEST_REDIS_URL: redis://localhost:6379/15
      RAZORPAY_WEBHOOK_SECRET: local-test-secret
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.12" }
      - run: uv --directory apps/api sync --all-groups
      - run: uv --directory apps/api run ruff check app tests
      - run: uv --directory apps/api run ruff format --check app tests
      - run: uv --directory apps/api run mypy app
      - run: uv --directory apps/api run pytest tests/contract tests/unit tests/security -q
      - run: uv --directory apps/api run pytest tests/integration tests/performance -q

  web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "22", cache: npm, cache-dependency-path: apps/web/package-lock.json }
      - run: npm --prefix apps/web ci
      - run: npm --prefix apps/web run lint
      - run: npm --prefix apps/web run test
      - run: npm --prefix apps/web run build

  contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.12" }
      - uses: actions/setup-node@v4
        with: { node-version: "22", cache: npm, cache-dependency-path: apps/web/package-lock.json }
      - run: npm --prefix apps/web ci
      - run: uv --directory apps/api sync --all-groups
      - run: uv --directory apps/api run python ../../scripts/export_openapi.py
      - run: npm --prefix apps/web run generate:api
      - run: git diff --exit-code apps/api/openapi.json apps/web/src/lib/api-types.ts

  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: gitleaks/gitleaks-action@v2
        env: { GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}" }
      - uses: actions/setup-node@v4
        with: { node-version: "22", cache: npm, cache-dependency-path: apps/web/package-lock.json }
      - run: npm --prefix apps/web ci
      - run: npm --prefix apps/web audit --omit=dev --audit-level=high
        continue-on-error: true     # reported, not blocking, for the buildathon

  compose:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker compose config -q

  e2e:
    runs-on: ubuntu-latest
    services:
      postgres: { ... same as api ... }
      redis: { ... same as api ... }
    env:
      TEST_POSTGRES_URL: ...same...
      RAZORPAY_WEBHOOK_SECRET: local-test-secret
      NEXT_PUBLIC_API_BASE_URL: http://127.0.0.1:8000
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.12" }
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - run: uv --directory apps/api sync --all-groups
      - run: uv --directory apps/api run alembic upgrade head
      - run: uv --directory apps/api run uvicorn app.main:app --port 8000 &
      - run: npm --prefix apps/web ci
      - run: npm --prefix apps/web run build
      - run: npm --prefix apps/web run start &
      - run: npx --prefix apps/web playwright install --with-deps chromium
      - run: npx --prefix apps/web playwright test
```

Rules:

- The `contracts` job's `git diff --exit-code` is the contract-freshness gate: a PR whose `openapi.json` or `api-types.ts` is stale fails.
- The API job runs `tests/security` in the fast phase (no DB needed for the redaction/static tests) and integration+performance in the same container afterwards.
- Long-running background servers in `e2e` need explicit waits for `/healthz` and `http://localhost:3000` (a small retry loop step); document this in the workflow with comments.
- No job prints environment values; secrets are only the dummy local ones. Optional A11/A12 packages must not be wired into CI until they exist.

## 5. Logging and metrics

### 5.1 `core/logging.py`

```python
def configure_logging(level: str | None = None) -> None:
    """Configure the root logger once per process: a StreamHandler with the fixed format
    "%(asctime)s %(levelname)s %(name)s %(message)s", level from the argument or settings.log_level.
    Idempotent (no duplicate handlers). Adds no request hooks and never touches message content."""
```

Module docstring rule (also enforced by tests): callers must never pass request bodies, signatures, credentials, raw VPAs or raw identifiers as log arguments; log only IDs, statuses and fixed strings.

### 5.2 `core/metrics.py`

Zero-dependency, thread-safe, in-process registry:

```python
class MetricsRegistry:
    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None
    def observe_seconds(self, name: str, labels: dict[str, str] | None, seconds: float) -> None
    def render_prometheus(self) -> str    # text/plain; version=0.0.4 exposition
    def reset(self) -> None               # tests only

REGISTRY = MetricsRegistry()              # process-wide singleton
```

- `inc` maintains `*_total` counters; `observe_seconds` maintains `<name>_count` and `<name>_sum`.
- Label validation: label keys are restricted to `{"method", "route", "status"}`; values are validated (method in a fixed set, route must be a path template without embedded values, status is an int string). A violation raises `ValueError` — this is the cardinality guard.
- `render_prometheus` escapes label values and emits `# TYPE` lines; it contains no application data beyond the counters.

### 5.3 `routes/metrics.py` and the three `main.py` additions

`GET /metrics` (tagged `metrics`, no auth, `Cache-Control: no-store`): returns `REGISTRY.render_prometheus()` as `text/plain; version=0.0.4`. It exposes counters only — never payloads, never request IDs, never client addresses.

The three `main.py` lines A10 adds inside `create_app()`:

```python
configure_logging()                                   # 1: central logging
app.include_router(metrics.router)                    # 2: /metrics (no /v1 prefix)
@app.middleware("http")                                # 3: request metrics (defined in core/metrics.py,
async def _request_metrics(request, call_next): ...   #    imported; counts + observes duration)
```

The middleware labels: `method` = request.method, `route` = `request.scope["route"].path` template when a route matched (else `"unmatched"`), `status` = response status code. It must never log and never raise (wrap in try/except and continue).

## 6. Security tests

### 6.1 `tests/security/test_log_redaction.py` (dynamic; no DB — uses fail-closed paths)

Uses `caplog` at DEBUG level against the real app via `TestClient`, with `app.state` overrides where needed.

```text
test_settings_repr_and_str_contain_no_secret_values
  -> build Settings with distinctive dummy credentials; assert str(settings) and repr(settings)
     contain neither the key secret nor the webhook secret values.

test_webhook_invalid_signature_logs_no_body_content
  -> POST /v1/webhooks/razorpay a body containing the marker "raw-vpa-marker@upi" with a wrong
     signature; assert 401 and the marker appears in no captured log record.

test_webhook_unconfigured_secret_logs_no_payload
  -> override app.state.webhook_signature_verifier to an unconfigured verifier; POST the same
     marker body; assert 503 and no marker, no signature, no secret in logs.

test_precheck_unavailable_logs_no_session_data
  -> POST precheck for a non-existent UUID; assert 404 and logs contain no raw values.

test_metrics_labels_contain_no_identifiers
  -> after several requests through the middleware, render /metrics and assert no UUID, no
     "token_", no "hmac-sha256" appears anywhere.

test_metrics_cardinality_guard_rejects_bad_labels
  -> registry.inc with a route label containing an embedded value raises ValueError.
```

### 6.2 `tests/security/test_static_secret_scan.py` (static; repository walk)

Walks `apps/api/app/**/*.py` and `apps/web/src/**/*.{ts,tsx}` and asserts the forbidden patterns are absent:

```text
test_no_print_statements_in_api_app
test_no_console_log_in_web_src                      # console.error/warn allowed
test_no_secret_value_logging_patterns               # e.g. logger.*get_secret_value, log(...signature)
test_no_live_razorpay_keys_anywhere_in_apps         # "rzp_live_" absent from apps/, data/, docs/
test_no_test_secret_in_production_source            # "local-test-secret" absent from apps/api/app and apps/web/src
test_env_example_contains_placeholder_values_only
  -> parse .env.example: credential keys (RAZORPAY_*, LITELLM_API_KEY, HMAC_PEPPER) must be empty
     or obvious placeholders; POSTGRES_URL/REDIS_URL must match the documented local dev values.
test_no_hardcoded_credentials_in_ci                 # ci.yml contains only local-test-secret/mandate_guardian
```

Keep patterns tight to avoid false positives; every pattern is documented in the test with its rationale.

### 6.3 `tests/performance/test_audit_verification_scale.py`

Integration test (TEST_POSTGRES_URL): append 1,000 audit events to one aggregate through `AuditRepository.append` (fixed synthetic payloads, real hash chain), then measure `verify_aggregate` and assert it completes in **under 5 seconds** and reports `valid=True, checked_events=1000`. Assert the architecture's number exactly; report the measured duration in the test output. If the append loop itself is slow, that is acceptable — only verification is time-asserted; batched insertion would require an A1 interface change and is out of scope.

## 7. Governance documents

### 7.1 `docs/threat-model.md` — required structure

1. **Scope and assets**: risk sessions, mandate events, assessments, audit chain, action/outbox integrity, API credentials; the architecture's data-flow diagram reproduced.
2. **Trust boundaries**: browser <-> API, Razorpay <-> webhook route, worker <-> Razorpay API, demo controls <-> production guard.
3. **STRIDE-lite table** — one row per threat with: threat, vector, existing control (name the package/test), residual risk:

```text
Spoofing          Forged webhook callbacks        A3 constant-time HMAC over raw bytes, fail-closed 503   Proxy allow-list deferred (A10 infra)
Spoofing          Fake checkout telemetry         A2 trust table (peer IP only, client claims ignored)    -
Tampering         Audit history modification      A1 SHA-256 chain + advisory locks + verify endpoint     DB access control out of scope
Repudiation       Disputing a decision            Append-only audit events per aggregate                  -
Information disc. Raw PII at rest or in logs      A0 hash contracts, A2/A3 redaction, A10 tests           -
Information disc. Dashboard leakage              A8 field-by-field mapping + no-hash tests               No auth (deferred; read-only, no raw data)
Denial of service Webhook flood                   DB-level idempotency; bounded transactions              Rate limiting deferred
Elevation         Demo controls in production     app_env guard returning 404                             -
Elevation         Worker double-execution         SKIP LOCKED + idempotency keys                          Stale PROCESSING reclaim deferred (A6)
```

4. **Deferred controls** with residual-risk statements (auth on dashboard/demo, rate limiting, proxy trust, stale-message reclamation, secret rotation) — each must reference the package that documented it first.
5. **Out of scope**: MuleHunter.AI/bank integrations, KYC, guaranteeing prevention of debits.

### 7.2 `docs/limitations-and-claims.md` — required content

Carry the architecture's honesty guardrails verbatim in spirit: the system detects merchant-visible risk at/around registration, challenges before redirect when possible, and requests post-confirmation revocation; it does **not** guarantee prevention of every debit, does not identify anyone as a mule account, does not call MuleHunter.AI, does not receive bank KYC, and has no Razorpay network-wide data. List explicitly: demo-only shared-merchant namespace (labelled `simulated` everywhere), Test-mode/no-real-money usage, mock revoke mode with scheduled failures, uncorrelated events score with reduced context, UA classification and reputation provider deferrals, dashboard/demo without auth, A11/A12 not part of the core loop. Each limitation names the owning work package.

### 7.3 `docs/demo-runbook.md` — required structure

A 10-minute judge script: prerequisites and stack start (exact commands); URL map (`/checkout`, `/challenge`, `/dashboard`, `/demo`, API docs, `/healthz`, `/metrics`); the fast path ("run all seven scenarios from `/demo` and show the dashboard"); the manual path (checkout ALLOW flow, forced CHALLENGE via repeated same-device sessions, signed webhook replay with the A0 fixture + `local-test-secret`, duplicate delivery, worker failure/recovery via A9's scheduled failure); what to point out on screen (masked VPA, reasons, latency, audit-chain verified badge, simulated badges); recovery notes (restart Redis for pristine windows, re-run any scenario, dashboard polling). Every step must reference the owning package's documented behaviour.

### 7.4 `infra/observability/prometheus.yml` and `README.md`

A minimal scrape config (one job scraping `host.docker.internal:8000/metrics`, 15s interval) and a README describing the exposed metric names, the cardinality rules, why identifiers are excluded, and the local run command (`docker run -p 9090:9090 -v .../prometheus.yml:/etc/prometheus/prometheus.yml prom/prometheus`). No dashboards are required; a Grafana JSON may be added later.

## 8. Verification commands

```powershell
docker compose config
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
$env:TEST_REDIS_URL = "redis://localhost:6379/15"
uv --directory apps/api run pytest tests/security -q
uv --directory apps/api run pytest tests/performance/test_audit_verification_scale.py -q
uv --directory apps/api run ruff check app/core/metrics.py app/core/logging.py app/api/routes/metrics.py tests/security tests/performance/test_audit_verification_scale.py
uv --directory apps/api run mypy app/core/metrics.py app/core/logging.py app/api/routes/metrics.py
uv --directory apps/api run uvicorn app.main:app --port 8000   # then verify GET /metrics renders counters and stop cleanly
docker compose down
```

Additionally, validate the workflow syntactically (e.g. `yamllint` or a local `act --list` if available) and state in the PR that CI must be observed green on the pull request itself — a CI file is only proven by the pipeline run.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A10 use |
|---|---|---|
| A0 | Make targets, `.env.example`, compose, scripts, contract flow | CI replays the exact documented commands; contract-freshness gates the generated artifacts. |
| A1-A6 | repositories, endpoints, tests | Executed by CI; security tests exercise their fail-closed paths; never edited. |
| A7/A8 | web lint/test/build/e2e scripts | Executed by CI and the e2e job. |
| A9 | demo scenarios, benchmark script | Referenced by the runbook; the e2e/CI runbooks link to them. |
| All | logging discipline | Verified by A10's dynamic and static tests. |

### Outputs produced

| Consumer | Receives from A10 | Integration rule |
|---|---|---|
| All future PRs | CI gates (lint/type/tests/contract freshness/secrets) | No package may merge with red CI; contract changes regenerate artifacts in the same PR. |
| Judges/reviewers | threat model, limitations and claims, runbook | The submission's trust and honesty evidence. |
| Operators | `/metrics` + Prometheus config | Optional local scraping; no dashboards required. |
| A11/A12 (optional) | CI scaffold and governance docs | They extend, never weaken, the pipeline and the limitations list. |

### Merge order

1. A10 requires A0 (commands, compose) and at least A1-A3 merged so its redaction tests exercise real routes; it merges last among the core packages or in parallel once those exist.
2. The `e2e` job should be enabled only after A7-A9 merge; before that it must be marked `continue-on-error` or guarded so CI stays green — document which choice was made in the PR.
3. A10's PR must show a green pipeline run on itself; that run is the package's primary acceptance evidence.

### Pull-request handoff checklist

State in the A10 PR: the CI job list and what each gate prevents; the contract-freshness mechanism; the gitleaks result; the three new-file exceptions and the three `main.py` lines; the metric names and cardinality rules; the dynamic redaction test evidence (marker-based); the static scan patterns with rationale; the 1,000-event verification duration measured; screenshots/links of the three documents; results for all commands in Section 8; and known deferred work (auth, rate limiting, proxy trust, stale-PROCESSING reclamation, secret rotation, Grafana dashboards, SIEM).

## 10. Failure conditions

The A10 work is unsafe or incomplete if any of these occur:

- CI can pass while the generated `openapi.json`/`api-types.ts` are stale, tests are skipped, or lint/type checks are softened.
- A raw VPA, signature, credential or payload reaches a log record in any tested path, or a fabricated marker sent through fail-closed routes appears in captured logs.
- `Settings` stringification leaks a secret value, or `rzp_live_` credentials exist anywhere in the repository.
- `/metrics` exposes request IDs, client addresses, paths with embedded values, or unbounded label cardinality.
- The middleware or metrics code can raise into the request path or log request content.
- The 1,000-event audit verification misses the 5-second target and the miss is hidden rather than reported.
- The threat model, limitations or runbook contradict the architecture's honesty guardrails, or claim capabilities the system does not have (MuleHunter.AI integration, KYC, guaranteed prevention).
- A10 edits other packages' code, adds runtime dependencies, implements authentication/rate limiting, or weakens any existing test to make CI green.
