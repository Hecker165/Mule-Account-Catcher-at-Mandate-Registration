# A0 — Foundation and Canonical Contracts: implementation specification

## 0. Agent instruction

You are the **A0 foundation agent** for Mule Account Catcher at Mandate Registration. Read these two files before changing anything:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `Idea.md`

Then implement **only** this specification. Do not redesign the system, substitute the stack, add a fraud model, implement Razorpay API calls, add a database schema, build UI pages, or change a contract based on a future agent's convenience. Your work becomes the frozen interface for all other agents.

The repository has already been scaffolded with placeholders. Replace only the A0-owned placeholders below; leave files labelled as owned by A1–A12 untouched except where this document explicitly instructs otherwise.

## 1. Objective and definition of done

Create a reproducible local development foundation and the **version 1 canonical contracts**. A later agent must be able to start the stack, import the Pydantic models, validate fixture JSON, obtain `openapi.json`, generate browser types, and start a FastAPI health endpoint without needing to infer any data shape.

Definition of done:

- Python 3.12 API dependencies and Node 22 web tooling are declared and install cleanly.
- `docker compose up -d postgres redis` starts the backing services; `docker compose config` is valid.
- `uv run uvicorn app.main:app --reload --port 8000` starts `GET /healthz`.
- Every valid contract fixture validates against the named model; every invalid fixture fails validation.
- `apps/api/openapi.json` is generated from FastAPI and `apps/web/src/lib/api-types.ts` is generated from it, not hand-authored.
- The listed commands in `Makefile` work on Windows PowerShell through Docker/uv/npm (avoid Bash-only syntax).
- No source file, fixture or generated artifact contains a real API key, VPA, IP address, bank account number or customer name.

## 2. Scope and ownership

### You own

```text
README.md
.env.example
.gitignore
docker-compose.yml
Makefile
apps/api/pyproject.toml
apps/api/uv.lock
apps/api/Dockerfile
apps/api/app/main.py
apps/api/app/core/__init__.py
apps/api/app/core/settings.py
apps/api/app/contracts/**
apps/api/tests/contract/**
apps/api/tests/unit/test_health.py
apps/api/openapi.json                       # generated
apps/web/package.json
apps/web/package-lock.json
apps/web/Dockerfile
apps/web/src/lib/api-types.ts               # generated
data/fixtures/contracts/**
data/fixtures/webhooks/**
scripts/export_openapi.py
docs/contracts.md
```

You may add small, directly related files under those paths (for example `apps/api/app/core/logging.py` or `apps/web/tsconfig.json`).

### You must create but not implement beyond a placeholder

The already-reserved directories below must remain available for their future owners. Do not add domain logic to them:

```text
apps/api/app/api/routes/        # A2/A3/A8/A9
apps/api/app/domain/rules/      # A5
apps/api/app/repositories/      # A1
apps/api/app/services/          # A2/A4/A5
apps/api/app/integrations/      # A3/A6/A12
apps/api/app/workers/           # A6/A12
apps/api/migrations/            # A1
apps/web/src/app/               # A7/A8
apps/web/src/components/        # A7/A8
data/scenarios/                 # A9
infra/                          # A6/A10/A12
```

### Explicit non-goals

- No Razorpay SDK/client, webhook route, signature verification or token-revoke code.
- No SQLAlchemy models, Alembic migration, Redis client, queue/outbox or database access.
- No scoring rules, LightGBM/XGBoost, SHAP, LLM, LiteLLM call or provider key.
- No checkout, dashboard or challenge UI.
- No `requirements.txt`: this project uses `pyproject.toml` plus `uv.lock` as the one Python dependency authority.

## 3. Required local stack and commands

Use this fixed stack; do not replace it with Express, Django, Poetry, pnpm, a monorepo manager or microservices.

| Area | Required choice |
|---|---|
| API runtime | Python 3.12, FastAPI, Uvicorn, Pydantic v2 |
| Python packages | `uv` and `pyproject.toml` |
| Web runtime | Node 22, Next.js, React, TypeScript, npm |
| Database for later agents | PostgreSQL 16 Alpine |
| Feature/idempotency store for later agents | Redis 7 Alpine |
| Local orchestration | Docker Compose v2 |
| Contract transfer | FastAPI OpenAPI JSON plus generated `openapi-typescript` types |

### 3.1 `apps/api/pyproject.toml`

Set project name `mandate-guardian-api`, version `0.1.0`, Python `>=3.12,<3.13` and an MIT license. Use these dependency ranges:

```toml
dependencies = [
  "fastapi>=0.115,<1.0",
  "uvicorn[standard]>=0.30,<1.0",
  "pydantic>=2.9,<3.0",
  "pydantic-settings>=2.6,<3.0",
  "httpx>=0.27,<1.0",
  "sqlalchemy>=2.0,<3.0",
  "asyncpg>=0.29,<1.0",
  "alembic>=1.13,<2.0",
  "redis>=5.0,<7.0"
]

[dependency-groups]
dev = [
  "pytest>=8.0,<9.0",
  "pytest-asyncio>=0.24,<1.0",
  "pytest-cov>=5.0,<7.0",
  "ruff>=0.8,<1.0",
  "mypy>=1.13,<2.0",
  "types-redis>=4.6,<5.0"
]
```

Configure Ruff for line length 100 and rules `E`, `F`, `I`, `UP`, `B`; configure mypy with `python_version = "3.12"`, `strict = true`, and package roots including `app`. Run `uv lock` and commit the resulting `uv.lock`.

### 3.2 Docker Compose

Implement `docker-compose.yml` with exactly these four named services:

| Service | Image/build | Required configuration |
|---|---|---|
| `postgres` | `postgres:16-alpine` | Port `5432:5432`, database/user/password all `mandate_guardian` for local development only, named volume `postgres_data`, healthcheck using `pg_isready`. |
| `redis` | `redis:7-alpine` | Port `6379:6379`, named volume `redis_data`, healthcheck `redis-cli ping`. |
| `api` | build `./apps/api` | Port `8000:8000`; depends on healthy Postgres and Redis; source bind mount for development; starts `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`. |
| `web` | build `./apps/web` | Port `3000:3000`; depends on API start; `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`; source bind mount; starts `npm run dev -- --hostname 0.0.0.0`. |

Use `POSTGRES_URL=postgresql+asyncpg://mandate_guardian:mandate_guardian@postgres:5432/mandate_guardian` and `REDIS_URL=redis://redis:6379/0` inside Compose. Use no real secret and no `container_name`. Add named volumes `postgres_data` and `redis_data`. Do not make API startup require a database connection yet; only configuration is required in A0.

Create small Dockerfiles that install dependencies from lock files before copying the source. The API Dockerfile uses `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`; the web Dockerfile uses `node:22-bookworm-slim`. Both must support the Compose development command above.

### 3.3 Root commands and environment template

Create these Make targets, each calling a cross-platform tool command and printing no secrets:

| Target | Required behaviour |
|---|---|
| `make bootstrap` | runs `uv sync --all-groups` in `apps/api` and `npm install` in `apps/web`. |
| `make up` / `make down` | starts/stops Compose. |
| `make api` | starts the API from `apps/api`. |
| `make web` | starts the web app from `apps/web`. |
| `make contracts` | exports OpenAPI then generates browser types. |
| `make test-contract` | runs only contract and health tests. |
| `make lint` | runs Ruff check/format-check and mypy for API plus `npm run lint` for web. |
| `make test` | runs all currently implemented tests. |
| `make check` | runs `contracts`, `lint` and `test` in that order. |

For Windows compatibility, use `uv --directory apps/api ...` and `npm --prefix apps/web ...`, never `cd ... && ...` or Unix-only shell features inside Make recipes. Document equivalent direct PowerShell commands in the README because Make may not be preinstalled on Windows.

`.env.example` must contain names and safe local defaults only:

```dotenv
APP_ENV=local
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8000
POSTGRES_URL=postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian
REDIS_URL=redis://localhost:6379/0
HMAC_PEPPER=replace-with-a-local-development-secret
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
LITELLM_BASE_URL=
LITELLM_API_KEY=
LITELLM_MODEL=
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
```

State in README that Test Mode uses test API keys and no real money; the code must work with no Razorpay credentials because integration is A3/A6 work.

## 4. FastAPI baseline

Implement `create_app() -> FastAPI` in `apps/api/app/main.py`, then expose `app = create_app()`.

Set these exact metadata values:

```text
title: Mandate Guardian API
version: 0.1.0
description: Merchant-layer, explainable UPI mandate-risk controls.
openapi_url: /openapi.json
docs_url: /docs
redoc_url: /redoc
```

Add only these routes in A0:

```text
GET /healthz -> 200 {"status":"ok","service":"mandate-guardian-api","version":"0.1.0"}
GET /readyz  -> 200 {"status":"ready","service":"mandate-guardian-api","version":"0.1.0"}
```

The readiness route is a configuration-only readiness response in A0. A1/A4 may later add actual Postgres/Redis checks without changing its response shape. Add the `X-Request-ID` response header: reuse a valid incoming UUID request ID if supplied; otherwise create a UUID4. Do not log request bodies, authorization headers, fingerprint headers or any secret-setting value.

Implement `Settings` in `app/core/settings.py` using `pydantic-settings`, `.env` support, `extra="ignore"`, cached `get_settings()`, and these fields: `app_env`, `log_level`, `api_host`, `api_port`, `postgres_url`, `redis_url`, `hmac_pepper`, `razorpay_key_id`, `razorpay_key_secret`, `razorpay_webhook_secret`, `litellm_base_url`, `litellm_api_key`, `litellm_model`. Mark credential fields as `SecretStr | None`; never serialize them.

## 5. Contract rules that apply to every model

Create exactly these modules:

```text
app/contracts/__init__.py
app/contracts/common.py
app/contracts/risk_session.py
app/contracts/mandate_event.py
app/contracts/feature_snapshot.py
app/contracts/risk_assessment.py
app/contracts/action.py
app/contracts/audit.py
```

All public models must:

- inherit a single shared `ContractModel` from `common.py` with `extra="forbid"`, `populate_by_name=True`, `str_strip_whitespace=True`, JSON serialization of UUIDs/datetimes, and `schema_version="1.0"` as a required/defaulted field where listed below;
- use timezone-aware UTC datetimes only; reject naive datetime input;
- use UUID4 for internal IDs, lowercase UUID serialization, and no database IDs;
- use `snake_case` JSON field names; no undocumented aliases;
- use `Decimal` only if a fraction is needed. Monetary values are **integers in paise**, never float rupees;
- contain only pseudonymised fields at rest. A hash must match `hmac-sha256:` followed by 64 lowercase hexadecimal characters;
- include `model_config` examples so FastAPI OpenAPI shows a usable example; and
- be re-exported from `app/contracts/__init__.py` in a stable, alphabetised `__all__`.

Use literal enum values exactly as specified. Future agents may add a new optional field only in a backwards-compatible contract version; they may not rename/remove a field or enum value.

### 5.1 `common.py`

Implement:

```text
Decision: ALLOW | CHALLENGE | BLOCK
DecisionStage: PRECHECK | POST_CONFIRMATION | REJECTION_AUDIT
MandateEventType: token.confirmed | token.rejected | token.cancelled
ActionType: TOKEN_REVOKE
ActionStatus: QUEUED | IN_PROGRESS | SUCCEEDED | RETRYING | FAILED | NOT_REQUIRED
Availability: AVAILABLE | MISSING | NOT_APPLICABLE | DEMO_SIMULATED
AuditActorType: SYSTEM | WEBHOOK | WORKER | OPERATOR | DEMO
RiskSessionStatus: CREATED | READY | CONSUMED | EXPIRED
```

Also implement `ensure_utc(value: datetime) -> datetime`, the constrained hash string type, `ContractModel`, and `FeatureSource`. `FeatureSource` fields are `availability: Availability`, `source: str` (1–80 characters), and `captured_at: datetime | None`. `captured_at` is required when availability is `AVAILABLE` or `DEMO_SIMULATED`, otherwise it must be `None`.

### 5.2 `risk_session.py`

Implement these request/response contracts. A2 will provide the routes and hash raw telemetry; A0 must only define the shape.

```text
MandateIntent
  max_amount_paise: int | None, ge=0, le=10_000_000
  frequency: str | None, max_length=32
  expire_at: datetime | None

RiskSessionCreateRequest
  merchant_namespace: str, pattern ^[a-z][a-z0-9_-]{2,63}$
  checkout_order_ref: str | None, 1..128 chars
  customer_reference: str | None, 1..256 chars, write-only/sensitive example only
  device_fingerprint: str | None, 8..512 chars, write-only/sensitive example only
  flow_started_at: datetime | None
  mandate_intent: MandateIntent

RiskSessionTelemetryUpdateRequest
  device_fingerprint: str | None, 8..512 chars
  flow_completed_at: datetime | None
  client_user_agent: str | None, 1..512 chars
  client_ip: str | None, max_length=64
  is_vpn_claimed: bool | None

RiskSession
  schema_version: "1.0"
  risk_session_id: UUID4
  merchant_namespace: str
  checkout_order_ref: str | None
  customer_reference_hash: HashValue | None
  ip_hash: HashValue | None
  device_fingerprint_hash: HashValue | None
  user_agent_hash: HashValue | None
  flow_started_at: datetime | None
  flow_completed_at: datetime | None
  mandate_intent: MandateIntent
  status: RiskSessionStatus
  created_at: datetime
  updated_at: datetime
```

Validate that `flow_completed_at` is not before `flow_started_at`, and `expire_at` is later than `flow_started_at` when both exist. Never add raw `customer_reference`, raw IP, raw device fingerprint or raw user-agent to `RiskSession`.

### 5.3 `mandate_event.py`

The raw Razorpay body is handled only in memory by A3. The normalised contract has no raw payload or raw VPA.

```text
MandateWebhookEvent
  schema_version: "1.0"
  mandate_event_id: UUID4
  provider: Literal["razorpay"]
  provider_event_id: str, 1..128 chars       # x-razorpay-event-id when provided
  event_type: MandateEventType
  token_id: str, pattern ^token_[A-Za-z0-9]+$
  risk_session_id: UUID4 | None
  vpa_hash: HashValue | None
  vpa_handle: str | None, 1..100 chars, lower-case validated
  recurring_status: str | None, 1..64 chars
  failure_reason: str | None, 1..500 chars
  provider_created_at: datetime | None
  received_at: datetime
  raw_payload_sha256: str, pattern ^sha256:[a-f0-9]{64}$
  is_demo_event: bool
```

`provider_event_id` is the idempotency identity used by A3. Keep `risk_session_id` optional because correlation can fail safely. Do not derive it from the webhook source IP.

### 5.4 `feature_snapshot.py`

Feature calculations must be explicit, typed and named. Implement this model without computation logic:

```text
FeatureSnapshot
  schema_version: "1.0"
  feature_snapshot_id: UUID4
  mandate_event_id: UUID4 | None
  risk_session_id: UUID4 | None
  feature_version: str, default "rules-v1"
  calculated_at: datetime
  ip_velocity_5m: int | None, ge=0
  ip_velocity_1h: int | None, ge=0
  device_velocity_5m: int | None, ge=0
  device_velocity_1h: int | None, ge=0
  customer_velocity_1h: int | None, ge=0
  vpa_velocity_1h: int | None, ge=0
  shared_demo_merchant_count_1h: int | None, ge=0
  is_new_device_for_customer: bool | None
  flow_duration_seconds: int | None, ge=0, le=86_400
  user_agent_bot_suspected: bool | None
  network_vpn_or_proxy: bool | None
  network_reputation_score: int | None, ge=0, le=100
  npci_risk_flag: bool | None
  payer_bank_or_compliance_flag: bool | None
  max_amount_paise: int | None, ge=0
  mandate_frequency: str | None, max_length=32
  expiry_days_from_registration: int | None, ge=0
  sources: dict[str, FeatureSource]
  is_demo_simulation: bool
```

Require at least one of `mandate_event_id` or `risk_session_id`. If `shared_demo_merchant_count_1h` has a value, require `is_demo_simulation=True` and sources entry `shared_demo_merchant_count_1h` with `DEMO_SIMULATED`. This prevents a demo capability being passed off as production data.

### 5.5 `risk_assessment.py`

```text
RuleEvaluation
  rule_id: str, pattern ^[a-z][a-z0-9_]{2,63}$
  triggered: bool
  points: int, ge=0, le=100
  reason_code: str | None, 1..80 chars
  reason_text: str | None, 1..240 chars

RiskAssessment
  schema_version: "1.0"
  assessment_id: UUID4
  risk_session_id: UUID4 | None
  mandate_event_id: UUID4 | None
  feature_snapshot_id: UUID4
  stage: DecisionStage
  score: int, ge=0, le=100
  decision: Decision
  engine_version: str, default "rules-v1"
  rule_evaluations: list[RuleEvaluation], min_length=1
  evaluation_latency_ms: int, ge=0, le=60_000
  assessed_at: datetime
```

Require `risk_session_id` for `PRECHECK`; require `mandate_event_id` for `POST_CONFIRMATION` and `REJECTION_AUDIT`. The document does not encode business score thresholds: A5 owns scoring logic. It only guarantees the output shape.

### 5.6 `action.py`

```text
ActionRequest
  schema_version: "1.0"
  action_request_id: UUID4
  assessment_id: UUID4
  mandate_event_id: UUID4
  action_type: ActionType
  token_id: str, pattern ^token_[A-Za-z0-9]+$
  idempotency_key: str, pattern ^[a-z0-9_-]{16,128}$
  status: ActionStatus
  requested_at: datetime

ActionAttempt
  schema_version: "1.0"
  action_attempt_id: UUID4
  action_request_id: UUID4
  attempt_number: int, ge=1, le=100
  status: ActionStatus
  provider_status_code: int | None, ge=100, le=599
  safe_error_code: str | None, 1..80 chars
  safe_error_message: str | None, 1..240 chars
  attempted_at: datetime
  completed_at: datetime | None
```

`safe_error_message` must never be a raw upstream body. A6 owns action execution and retry policy.

### 5.7 `audit.py`

```text
AuditEvent
  schema_version: "1.0"
  audit_event_id: UUID4
  aggregate_type: Literal["risk_session", "mandate_event", "risk_assessment", "action_request"]
  aggregate_id: UUID4
  sequence_number: int, ge=1
  event_type: str, pattern ^[a-z][a-z0-9_.]{2,100}$
  actor_type: AuditActorType
  actor_id: str | None, 1..128 chars
  occurred_at: datetime
  redacted_payload: dict[str, object]
  previous_event_hash: str | None, pattern ^sha256:[a-f0-9]{64}$
  event_hash: str, pattern ^sha256:[a-f0-9]{64}$
```

The first event in an aggregate has `previous_event_hash=None`; later events require it. A1 determines how hashes are generated and persisted. A0 only locks the portable audit event shape.

## 6. Fixture bundle

Create JSON fixtures under `data/fixtures/contracts/` with fixed UUIDs, UTC timestamps from `2026-01-15T10:00:00Z` onward, obviously fake hash values and no personally identifiable information. Use these exact names:

```text
risk_session_create.valid.json
risk_session.valid.json
risk_session_invalid_namespace.json
mandate_event_confirmed.valid.json
mandate_event_rejected_npci_risk.valid.json
feature_snapshot_bot_burst.valid.json
feature_snapshot_invalid_demo_flag.json
risk_assessment_block.valid.json
risk_assessment_invalid_score.json
action_request_revoke.valid.json
action_attempt_retry.valid.json
audit_event_first.valid.json
audit_event_chained.valid.json
manifest.json
```

`manifest.json` maps every fixture filename to its fully qualified model name and `valid: true|false`. The confirmed event uses `token_demo123`; the rejection event uses `token_demo456`, `event_type="token.rejected"` and a bounded fake `failure_reason` containing the phrase `NPCI risk flag`. The bot-burst snapshot must show IP/device velocity above 10, a 2-second flow, `network_vpn_or_proxy=true`, `shared_demo_merchant_count_1h=3`, `is_demo_simulation=true`, and its matching source availability `DEMO_SIMULATED`. The blocking assessment must include at least three triggered evaluations and a score of 100.

Create these webhook fixtures under `data/fixtures/webhooks/`:

```text
token_confirmed_body.json
token_confirmed_headers.json
token_rejected_npci_risk_body.json
token_rejected_npci_risk_headers.json
README.md
```

They are payload-shape examples for A3, not valid live signatures. Use `x-razorpay-event-id` values `evt_demo_confirmed_001` and `evt_demo_rejected_001`; use `x-razorpay-signature: fixture-not-a-real-signature`; say that clearly in the README. Include only the minimal `event`, `created_at` and `payload.token.entity` fields needed by the architecture: token ID, method `upi`, VPA username `demo.user`, handle `upi`, recurring status and optional failure reason. No real VPA may appear.

## 7. Contract tests

Implement `apps/api/tests/contract/test_contract_fixtures.py` and use the manifest as the test parameter source. It must:

1. map manifest model names to imported models from `app.contracts`;
2. validate all `valid=true` JSON using `model_validate`;
3. assert all `valid=false` JSON raises `pydantic.ValidationError`;
4. assert every serialised valid model round-trips through JSON;
5. assert every generated JSON schema has `additionalProperties: false` on its top-level object; and
6. assert the required `schema_version` serialises as `"1.0"` for event/output models.

Implement `apps/api/tests/unit/test_health.py` using FastAPI `TestClient`. It checks status code, exact JSON body and a parseable UUID4 `X-Request-ID` for both health routes. It also sends a UUID4 request header and checks that the response preserves it.

Do not use a database, Redis or network in A0 tests. They must finish in under five seconds on a normal laptop.

## 8. OpenAPI and browser type generation

Implement `scripts/export_openapi.py` to import `create_app`, serialise `app.openapi()` as deterministic UTF-8 JSON with `indent=2` and a trailing newline to `apps/api/openapi.json`, then exit non-zero if no schemas are present.

In `apps/web/package.json`, declare these exact scripts:

```json
{
  "dev": "next dev",
  "build": "next build",
  "start": "next start",
  "lint": "eslint .",
  "generate:api": "openapi-typescript ../api/openapi.json -o src/lib/api-types.ts"
}
```

Add `next`, `react`, `react-dom`, `typescript`, `@types/node`, `@types/react`, `@types/react-dom`, `eslint`, `eslint-config-next` and `openapi-typescript` with compatible current major ranges. Create `eslint.config.mjs` using the Next core-web-vitals configuration; do not use the removed `next lint` command. Provide minimum valid `tsconfig.json`, `next.config.ts`, `src/app/layout.tsx` and `src/app/page.tsx` so `npm run build` works and displays only `Foundation ready`.

The contract command sequence is exactly:

```text
uv --directory apps/api run python ../../scripts/export_openapi.py
npm --prefix apps/web run generate:api
```

Run it and commit both generated artifacts. Future frontend agents import types from `src/lib/api-types.ts`; they do not create duplicate TypeScript interfaces for backend payloads.

## 9. Documentation that you must update

Update `README.md` with:

- a one-paragraph scope statement and the honest Test Mode note;
- prerequisites: Docker Desktop, Python 3.12, `uv`, Node 22/npm;
- copy `.env.example` to `.env` command for PowerShell;
- the direct PowerShell equivalent of each key Make target;
- local URLs: API docs `http://localhost:8000/docs`, health `http://localhost:8000/healthz`, web `http://localhost:3000`;
- a short directory ownership map pointing readers to this A0 document and the architecture plan.

Replace `docs/contracts.md` with a concise contract catalogue: model name, producer, consumers and one sentence of responsibility. State that `apps/api/app/contracts/` is the only canonical source and generated TypeScript must not be hand-edited.

## 10. Integration contract and merge handoff

Your output is consumed without modification as follows:

```text
A2 session API        imports RiskSession* + MandateIntent
A3 webhook gateway    imports MandateWebhookEvent + webhook fixtures
A4 feature extractor  emits FeatureSnapshot
A5 rule engine         consumes FeatureSnapshot, emits RiskAssessment
A1 persistence/audit  persists all contracts, creates AuditEvent chain
A6 revoke worker       consumes ActionRequest, emits ActionAttempt
A7/A8 web apps         import generated api-types.ts; do not duplicate contracts
A9 simulator           replays fixture/scenario shapes
A10 CI/security        invokes Make targets and contract tests
A11/A12 optional work  reads FeatureSnapshot/RiskAssessment only
```

Before you finish:

1. Run the full A0 verification commands below.
2. Add `## Contract compatibility` to your pull-request description: `contracts-v1`, list every model, and state `no breaking changes`.
3. Do not change contract files after handoff. If a later agent discovers a missing field, it must open an interface-change request rather than silently editing A0 output.
4. Deliver the exact file list changed, every command run with its result, and the two known deferred areas: database persistence (A1) and Razorpay integration (A3/A6).

## 11. Required verification commands

Run these commands from repository root. Resolve failures; do not claim completion with a skipped command.

```powershell
docker compose config
uv --directory apps/api sync --all-groups
npm --prefix apps/web install
uv --directory apps/api run pytest tests/contract tests/unit/test_health.py -q
uv --directory apps/api run ruff check app tests
uv --directory apps/api run ruff format --check app tests
uv --directory apps/api run mypy app
uv --directory apps/api run python ../../scripts/export_openapi.py
npm --prefix apps/web run generate:api
npm --prefix apps/web run build
docker compose up -d postgres redis
docker compose ps
docker compose down
```

For the FastAPI smoke test, run `uv --directory apps/api run uvicorn app.main:app --port 8000`, request `/healthz` and `/openapi.json`, then stop it cleanly. Do not start a real Razorpay flow in A0.

## 12. Failure conditions

The A0 work is not acceptable if any of these occur:

- A contract leaks raw VPA, raw IP, customer name, secret or payment credential into a persisted/output model or fixture.
- A Pydantic model accepts undeclared fields, naive timestamps, float money, an unknown enum value or an invalid hash prefix.
- Browser types are manually maintained, stale, or cannot be regenerated from OpenAPI.
- Any application path requires Razorpay credentials, Redis or PostgreSQL merely to answer `/healthz`.
- The agent implements files allocated to A1–A12, causing a conflict with parallel work.
- The project claims a live Razorpay transaction/API is free. Only development Test Mode is treated as no-real-money demo usage.
