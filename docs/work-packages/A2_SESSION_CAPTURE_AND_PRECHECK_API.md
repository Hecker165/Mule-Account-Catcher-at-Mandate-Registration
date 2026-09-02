# A2 — Checkout session capture and pre-registration pre-check API: implementation specification

## 0. Agent instruction

You are the **A2 session capture and pre-check API agent**. Read these documents before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/contracts.md`
5. `Idea.md`

Implement exactly this work package. You create the API layer that captures customer-controlled checkout context before UPI redirection, pseudonymises it, makes it durable, and orchestrates a pre-registration risk decision through a future A5 evaluator.

Do **not** implement fraud rules, Redis velocity counters, Razorpay Orders, Razorpay webhooks, revocation calls, database migrations, a dashboard, frontend pages, ML, LLM calls or external IP reputation calls. You must use A0 contracts and A1 repositories unchanged.

## 1. Why this package exists

`token.confirmed` is a post-registration Razorpay signal. A merchant can only challenge a customer **before** they leave for their UPI app if checkout telemetry is captured and evaluated first. A2 creates the correlation record that connects both moments:

```text
browser checkout
  -> POST /v1/risk-sessions
  -> risk_session_id returned
  -> frontend gives risk_session_id to later Razorpay Order notes/mapping (A7 + Razorpay integration)
  -> PATCH telemetry when checkout is ready
  -> POST /precheck
  -> A5 evaluator returns ALLOW or CHALLENGE
  -> later webhook A3 joins event to the same risk_session_id
```

The webhook source IP is Razorpay's server IP, **not** the customer IP. A2 must therefore obtain the customer network address only from the browser-to-API request and must never try to reconstruct it from a later webhook.

## 2. Objective and definition of done

Build a small, deterministic FastAPI module with three session endpoints, HMAC pseudonymisation and a pluggable pre-check evaluator port.

Definition of done:

- A browser/client can create a `RiskSession`, receive its UUID, record telemetry and request a pre-check without raw identifiers being returned or logged.
- Browser-supplied IP and VPN claims are never trusted for risk data; the API uses only the direct request peer address and request User-Agent.
- Creating the same `(merchant_namespace, checkout_order_ref)` twice returns the original persisted session and does not duplicate its audit event.
- Telemetry transitions a session from `CREATED` to `READY`; a successful pre-check transitions it to `CONSUMED` in the same transaction as the returned assessment/audit event.
- The route works with an injected fake evaluator in tests and returns a safe `503` when no real evaluator is configured.
- Every route has unit/API tests, integration tests with A1 PostgreSQL repositories, and no external network dependency.

## 3. Scope and exact file ownership

### You own

```text
apps/api/app/api/routes/risk_sessions.py
apps/api/app/api/routes/__init__.py
apps/api/app/api/dependencies.py
apps/api/app/services/session_capture.py
apps/api/app/services/precheck_orchestrator.py
apps/api/app/services/precheck_provider.py
apps/api/app/services/privacy_hashing.py
apps/api/tests/unit/test_privacy_hashing.py
apps/api/tests/unit/test_precheck_orchestrator.py
apps/api/tests/integration/test_risk_session_api.py
apps/api/tests/integration/test_risk_session_service.py
```

You may update `apps/api/app/main.py` **only** to register the A2 router and install the explicit unavailable evaluator described below. Do not alter A0 metadata, health endpoints, settings or request-ID behaviour.

### You may import but must not modify

```text
apps/api/app/contracts/**               # A0 frozen public interfaces
apps/api/app/core/settings.py           # A0 settings owner
apps/api/app/persistence/**             # A1 database/session implementation
apps/api/app/repositories/**            # A1 repository implementation
apps/api/app/domain/rules/**            # A5 owns rules/evaluator implementation
apps/api/app/integrations/**            # A3/A6/A12
apps/api/app/workers/**                 # A6/A12
apps/web/**                             # A7/A8
```

### Explicit non-goals

- Do not change any A0 Pydantic model or create duplicate request/response TypeScript/Python contracts.
- Do not store raw `customer_reference`, IP, device fingerprint, user agent, VPA or payment data.
- Do not use `X-Forwarded-For`, `X-Real-IP`, browser `client_ip`, `is_vpn_claimed` or browser User-Agent body values as trusted risk signals.
- Do not add fallback scoring rules such as "always allow". If the A5 evaluator is absent, return a safe, visible error.
- Do not create Razorpay orders or put the risk session ID into Razorpay yourself. A7/A3/A6 own their respective UI/provider integration paths.

## 4. Public API

All routes are tagged `risk-sessions` in FastAPI. They use A0 contract response models and FastAPI's normal validation responses. Set `Cache-Control: no-store` on every response from this router.

### 4.1 Create a session

```text
POST /v1/risk-sessions
Request body: RiskSessionCreateRequest
Success: 201 Created, body RiskSession
Idempotent replay: 200 OK, body original RiskSession, header X-Idempotent-Replay: true
```

Processing steps, in this order:

1. Read `merchant_namespace`, `checkout_order_ref`, `customer_reference`, `device_fingerprint`, `flow_started_at` and `mandate_intent` from `RiskSessionCreateRequest`.
2. Read the direct peer IP from `request.client.host`, if available; read `request.headers.get("user-agent")`.
3. Pseudonymise each available sensitive value using Section 5. Never log raw request data.
4. Build an A0 `RiskSession` with a newly generated UUID4, `status=CREATED`, current UTC `created_at`/`updated_at`, and only hash fields in the persisted model.
5. If `checkout_order_ref` is absent, persist this new session through `RiskSessionRepository.create` and append one audit event `risk_session.created`.
6. If `checkout_order_ref` is present, call `get_by_order_ref(merchant_namespace, checkout_order_ref)` first. If an existing session is found, return it as the idempotent replay; do not update it and do not append audit data.
7. If no session exists, create and audit it in one A1 transaction. If a concurrent create reaches the unique constraint, re-query and return the winner with the replay header.

The create audit payload is exactly:

```json
{
  "merchant_namespace": "demo_merchant",
  "checkout_order_ref_present": true,
  "customer_reference_present": true,
  "device_fingerprint_present": true,
  "network_peer_ip_present": true,
  "request_user_agent_present": true,
  "flow_started_at_present": true,
  "max_amount_paise_present": true,
  "frequency_present": true
}
```

The values are booleans only. The actor is `SYSTEM`, actor ID `checkout_api`, event type `risk_session.created`, aggregate type `risk_session`, aggregate ID equal to `risk_session_id`.

### 4.2 Record telemetry

```text
PATCH /v1/risk-sessions/{risk_session_id}/telemetry
Request body: RiskSessionTelemetryUpdateRequest
Success: 200 OK, body RiskSession
Not found: 404 {"detail":"risk session not found"}
Consumed: 409 {"detail":"risk session is already consumed"}
```

Process exactly as follows:

1. Parse `risk_session_id` as UUID4 in the path. Load it through `RiskSessionRepository.get`.
2. Reject a missing session with the stated 404. Reject a `CONSUMED` or `EXPIRED` session with the stated 409 before any write.
3. Derive a new peer IP hash from `request.client.host`, and a new request User-Agent hash from the HTTP header. Ignore body `client_ip`, body `client_user_agent` and body `is_vpn_claimed` for persistence and decisions. They remain contract-compatible client hints only and are not trusted.
4. If a non-null body `device_fingerprint` is supplied, pseudonymise it and replace the existing device hash. If null, retain the stored hash.
5. Retain existing customer hash; A2 never accepts customer reference in this PATCH contract.
6. Use body `flow_completed_at` when supplied; otherwise retain the stored value. The A0 model validation prevents an end time before the start time.
7. Call A1 `RiskSessionRepository.update_telemetry(...)` with `status=READY`, passing `None` only for fields that must remain unchanged. A1 must treat nullable telemetry inputs as "retain current value", not "erase". Do this in one transaction.
8. Append one `risk_session.telemetry_recorded` audit event in that same transaction. Use the boolean-only payload below and actor `SYSTEM` / `checkout_api`.

```json
{
  "network_peer_ip_present": true,
  "request_user_agent_present": true,
  "device_fingerprint_updated": true,
  "flow_completed_at_present": true,
  "untrusted_client_ip_ignored": true,
  "untrusted_vpn_claim_ignored": true
}
```

### 4.3 Request a pre-registration assessment

```text
POST /v1/risk-sessions/{risk_session_id}/precheck
Request body: none
Success: 200 OK, body RiskAssessment
Not found: 404 {"detail":"risk session not found"}
Not ready: 409 {"detail":"risk session telemetry is not ready"}
Risk engine unavailable: 503 {"detail":"precheck evaluator is unavailable"}
```

Processing steps:

1. Load session. Return 404 if absent.
2. If status is `CONSUMED`, call `RiskAssessmentRepository.get_precheck_for_session(risk_session_id)`. If one exists, return it with `X-Idempotent-Replay: true`; if it does not, return 409 `{"detail":"risk session has inconsistent consumed state"}` and write no data.
3. If status is not `READY`, return the stated 409. A client must record final telemetry before pre-checking.
4. Open one A1 transaction. Invoke the injected `PrecheckEvaluator.assess(db_session, risk_session)` protocol. A5's implementation is responsible for producing/persisting one `FeatureSnapshot` and one `RiskAssessment` in the same transaction. It must return an assessment whose `stage=PRECHECK`, `risk_session_id` matches the route, and `mandate_event_id is None`.
5. If the evaluator raises `PrecheckEvaluatorUnavailable`, roll back and return 503. Do not mark the session consumed and do not silently allow.
6. Validate the returned assessment invariants above. If invalid, raise/log a server error using only assessment IDs/versions, roll back, return 500 `{"detail":"precheck evaluator returned an invalid assessment"}`.
7. Set session status to `CONSUMED` through `RiskSessionRepository.update_status` and append audit event `risk_session.precheck_completed` in the same transaction.
8. Return the persisted assessment. No external service, redirect or Razorpay call occurs in this endpoint.

The pre-check audit payload is exactly:

```json
{
  "assessment_id": "00000000-0000-4000-8000-000000000010",
  "decision": "CHALLENGE",
  "score": 45,
  "engine_version": "rules-v1",
  "evaluation_latency_ms": 18
}
```

It contains no feature values, hashes or raw telemetry. Actor: `SYSTEM` / `precheck_api`; aggregate is the risk session.

## 5. HMAC pseudonymisation implementation

Create `app/services/privacy_hashing.py`. It must expose an injected `HmacPseudonymizer` rather than a module-level secret.

```text
HmacPseudonymizer(pepper: SecretStr | str)
  hash_customer_reference(value: str | None) -> HashValue | None
  hash_ip(value: str | None) -> HashValue | None
  hash_device_fingerprint(value: str | None) -> HashValue | None
  hash_user_agent(value: str | None) -> HashValue | None
```

Use `hmac.new(pepper_bytes, domain + b"\x00" + normalised_bytes, hashlib.sha256).hexdigest()` and prepend `hmac-sha256:`. Each field uses these exact domain labels and normalisation:

| Method | Domain | Normalisation |
|---|---|---|
| customer reference | `customer_reference` | Unicode NFKC then `strip()`. Preserve case because an internal customer ID can be case-sensitive. |
| IP | `ip_address` | Parse with `ipaddress.ip_address`; use its compressed representation. Invalid/missing input returns `None`. |
| device fingerprint | `device_fingerprint` | Unicode NFKC then `strip()`. |
| user agent | `user_agent` | Unicode NFKC, split all whitespace and join with one ASCII space. Preserve case. |

Return `None` for a missing/empty normalised value. Never put a raw normalised value in an exception, log entry, metric label or audit payload. A HMAC output is stable for the same pepper/domain/value but must differ for the same value under different domain labels. Unit-test all those properties with a fixed test-only pepper.

### 5.1 Trusted versus untrusted request context

| Input | Trust/use rule |
|---|---|
| `request.client.host` | Use only this as the direct peer address. In the local demo it is expected to be localhost. |
| `User-Agent` request header | Use as captured browser context. It is not proof of browser identity, but it is stronger than a body claim. |
| `X-Forwarded-For`, `X-Real-IP`, `Forwarded` | Ignore entirely. A trusted-proxy allow-list is a later infrastructure change, not an A2 assumption. |
| Body `client_ip`, `client_user_agent`, `is_vpn_claimed` | Accept only because frozen A0 contracts contain them; ignore for persistence/risk/audit. |
| Body `device_fingerprint` | Capture as an opaque client signal; HMAC it before persistence. |
| Body `customer_reference` | Capture only on session creation; HMAC it before persistence. |

## 6. Service and dependency design

### 6.1 `SessionCaptureService`

Create a class with injected `RiskSessionRepository`, `AuditRepository`, `HmacPseudonymizer`, clock function (`Callable[[], datetime]`) and UUID factory. The clock and UUID factory make tests deterministic. It exposes:

```text
create(request_contract, peer_ip, request_user_agent) -> CreateSessionResult
record_telemetry(risk_session_id, request_contract, peer_ip, request_user_agent) -> RiskSession
```

`CreateSessionResult` is an internal immutable dataclass with `session: RiskSession` and `is_replay: bool`. It is not an OpenAPI model.

The service owns transactions only through a supplied session-factory/unit-of-work abstraction from A1. Route functions stay thin: parse HTTP, call service, map named domain errors to HTTP status, set headers and return models.

### 6.2 Pre-check port

Create `app/services/precheck_provider.py` with only this extension interface:

```python
class PrecheckEvaluator(Protocol):
    async def assess(self, db_session: AsyncSession, risk_session: RiskSession) -> RiskAssessment: ...

class PrecheckEvaluatorUnavailable(RuntimeError):
    """Raised when the real A5 evaluator has not been installed."""

class UnavailablePrecheckEvaluator:
    async def assess(...) -> RiskAssessment:
        raise PrecheckEvaluatorUnavailable
```

Create a `get_precheck_evaluator(request: Request) -> PrecheckEvaluator` FastAPI dependency. It reads `request.app.state.precheck_evaluator`. If missing, return `UnavailablePrecheckEvaluator`; do not create a permissive fallback.

Create `PrecheckOrchestrator` in `precheck_orchestrator.py` with injected A1 repositories, `AuditRepository` and `PrecheckEvaluator`. Its `assess(risk_session_id)` method implements Section 4.3 and raises named errors `RiskSessionNotFound`, `RiskSessionNotReady`, `RiskSessionInconsistent`, `PrecheckUnavailable`, `InvalidPrecheckAssessment` for the route to map.

### 6.3 App wiring

In `app/main.py`:

- import and `include_router(risk_sessions.router, prefix="/v1")`;
- set `app.state.precheck_evaluator = UnavailablePrecheckEvaluator()` during app creation; and
- leave all later evaluator wiring explicitly to A5.

When A5 merges, it replaces only that one state assignment with its rules evaluator during application composition. This is the documented, intentionally small integration seam. A5 must not edit A2 route logic.

## 7. Session lifecycle, timing and correlation contract

```text
CREATED --PATCH telemetry--> READY --POST precheck succeeds--> CONSUMED
   |                              |                                  |
   +-- no Razorpay call ----------+                                  +-- future order/webhook correlation

EXPIRED is reserved for a later cleanup worker and is read-only in A2.
```

The frontend future integration must keep `risk_session_id` in its local checkout state and associate it with the internal order record. When the order is created, it passes this opaque UUID in the merchant-side Razorpay order `notes.risk_session_id` or persists an equivalent internal `order_id -> risk_session_id` mapping. A3 reads that mapping when normalising a later webhook. A2 does not call Razorpay and does not assume Razorpay will echo a custom field in every event.

`CHALLENGE` response handling is owned by A7: show a local step-up screen and do not redirect to UPI until it succeeds. `ALLOW` may continue to checkout. A2 returns the assessment only; it never issues a browser redirect.

## 8. Tests and acceptance criteria

### 8.1 Unit tests

`test_privacy_hashing.py` must prove:

- exact deterministic HMAC output for a fixed test pepper and input;
- identical canonical IP forms produce the same hash;
- different domains produce different hashes for the same string;
- blank/malformed IP produces `None` and is not raised/logged;
- whitespace/user-agent canonicalisation is deterministic; and
- no raw sample string occurs in the serialised result/repr.

`test_precheck_orchestrator.py` uses fake repositories/evaluator and a fixed clock. It must prove:

- missing session -> named not-found error;
- `CREATED` session -> named not-ready error;
- unavailable evaluator leaves session `READY` and writes no audit event;
- valid `PRECHECK` assessment consumes session and creates one audit event;
- wrong stage, wrong session ID or non-null mandate event from an evaluator causes invalid-assessment error and rollback; and
- consumed session returns the persisted existing pre-check without running the evaluator again.

### 8.2 PostgreSQL/API integration tests

Use A1's `TEST_POSTGRES_URL` fixture setup. Add these named tests:

```text
test_create_risk_session_returns_hashes_not_raw_request_values
test_create_same_namespace_order_ref_is_an_idempotent_replay
test_create_same_order_ref_in_different_namespace_creates_two_sessions
test_telemetry_uses_direct_peer_and_ignores_client_claims
test_telemetry_marks_ready_and_appends_redacted_audit_event
test_telemetry_rejects_consumed_session
test_precheck_requires_ready_session
test_precheck_returns_503_when_evaluator_is_unavailable
test_precheck_persists_valid_assessment_consumes_session_and_audits
test_precheck_replay_returns_same_assessment_without_re_evaluation
test_all_session_responses_set_no_store_and_preserve_request_id
```

For tests that need an assessment, install a fake evaluator through `app.state.precheck_evaluator`. The fake must create a valid synthetic `FeatureSnapshot` and `RiskAssessment` using A1 repositories in the supplied transaction, then return the persisted assessment. Do not fake this by returning an unpersisted contract: replay behaviour must exercise the real A1 read path.

Run the following as part of A2 verification:

```powershell
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
uv --directory apps/api run pytest tests/unit/test_privacy_hashing.py tests/unit/test_precheck_orchestrator.py -q
uv --directory apps/api run pytest tests/integration/test_risk_session_api.py tests/integration/test_risk_session_service.py -q
uv --directory apps/api run ruff check app/api app/services tests
uv --directory apps/api run ruff format --check app/api app/services tests
uv --directory apps/api run mypy app/api app/services
docker compose down
```

### 8.3 Visual/manual acceptance test

Until A7's UI exists, use FastAPI `/docs` and an HTTP client:

1. Create a session with fake `customer_reference="demo-customer-001"` and `device_fingerprint="demo-device-001"`.
2. Confirm the response contains only `hmac-sha256:` values—not either raw string—and note its `risk_session_id`.
3. Patch telemetry using intentionally false body `client_ip="198.51.100.99"`, `is_vpn_claimed=true`; inspect the audit event through A1 test tooling and confirm it contains booleans only.
4. Call precheck without an A5 evaluator and visibly receive 503; the session remains `READY`.
5. Run the integration test fake evaluator path; call precheck twice and confirm the second response uses `X-Idempotent-Replay: true` and the same assessment ID.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A2 use |
|---|---|---|
| A0 | `RiskSession*`, `MandateIntent`, `RiskAssessment`, enums, settings | Exact public data shape, status/decision values and HMAC pepper source. |
| A1 | async transaction/session, session/assessment/audit repositories | Durable writes, idempotency, audit chain and replay lookup. |
| A5, later | `PrecheckEvaluator` implementation | Evaluation/persistence behind the A2 protocol; no route rewrite. |

### Outputs produced

| Consumer | Receives from A2 | Integration rule |
|---|---|---|
| A7 checkout UI | endpoints and `risk_session_id` | Create session before redirect; telemetry then precheck; branch on returned decision. |
| A3 webhook gateway | durable session/order correlation record | Join via merchant's saved order mapping, never webhook client IP. |
| A4 features | ready/consumed session context | Reads hashed session context through A1; no raw telemetry is available. |
| A5 rules | `PrecheckEvaluator` port and ready session | Persists snapshot/assessment in A2's provided transaction, then returns the assessment. |
| A8 dashboard | redacted audit/session/assessment data | Uses A1 projection/API later; does not expose raw client inputs. |
| A9 simulator | endpoint lifecycle and fake evaluator test pattern | Can create deterministic demo sessions. |

### Merge order

1. A0 contracts and A1 persistence must be complete first. Confirm A1 exposes `update_telemetry` and `get_precheck_for_session` exactly as specified.
2. Implement and merge A2 router/services/tests.
3. A7 can build UI against A2 endpoint/OpenAPI shapes immediately, using a test evaluator only in local integration.
4. A4/A5 merge their extractor/evaluator later. The integration change is limited to replacing `app.state.precheck_evaluator` in `main.py`; no A2 endpoint or contract changes are permitted.
5. A3 later uses `risk_session_id` only through merchant-side order correlation. It must not modify A2's hashing/trust rules.

### Pull-request handoff checklist

The A2 PR must state:

- all routes, exact status codes and response contracts;
- raw fields accepted, HMAC-hashed and intentionally ignored;
- test results including 503 unavailable, idempotency and no-raw-data tests;
- A1 methods consumed and the A5 extension seam;
- no modified files in `contracts/`, `persistence/`, `repositories/`, `domain/rules/`, `integrations/`, `workers/` or `apps/web/`; and
- known deferred work: A5 real evaluator, A7 challenge UI, A3 order/webhook correlation and A4 feature extraction.

## 10. Failure conditions

A2 is unsafe or incomplete if any of the following is true:

- It uses webhook source data or untrusted forwarding/client body headers as the customer IP.
- It persists or returns a raw customer reference, IP, device fingerprint or user-agent; a generic JSON field does not make this acceptable.
- It lets an unavailable evaluator silently `ALLOW` a customer.
- A pre-check result can be duplicated or session status can be consumed without a persisted assessment/audit event.
- It alters frozen A0 contracts or directly accesses ORM tables rather than A1 repositories.
- It implements a scoring rule, external reputation call, Razorpay call, UI or queue/worker.
- It accepts a session `CREATED` state as ready for pre-check, or lets telemetry overwrite a consumed session.
