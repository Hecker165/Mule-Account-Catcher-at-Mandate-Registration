# A6 — Razorpay token-revoke adapter and transactional outbox worker: implementation specification

## 0. Agent instruction

You are the **A6 revoke adapter and worker agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A3_RAZORPAY_WEBHOOK_GATEWAY.md`
5. `docs/work-packages/A5_RULE_ENGINE_AND_EVALUATION.md`
6. `docs/contracts.md`

Implement exactly this work package. You build the typed Razorpay token-revoke client (with a mock mode for credential-free development) and the outbox worker that executes the `token_revoke_requested` messages A5 enqueues, with idempotency, transient retry/backoff and dead-lettering.

Do **not** change A0 contracts, edit A1-owned files, re-score anything, implement reputation enrichment or narrative workers (A12), add routes, or build UI. You consume A1's outbox/action repositories and A5's outbox payloads exactly as specified.

Security rules: the API secret never appears in logs, results, exceptions, audit payloads or test fixtures; upstream response bodies never reach the database; the webhook request path never waits on Razorpay.

## 1. Why this package exists

A `BLOCK`ed confirmed mandate must be revoked before the next value debit. A5 queues the intent atomically with the assessment; A6 executes it asynchronously so the webhook stays fast and provider outages never lose an action:

```text
A5 (same transaction as assessment)
  -> action_requests row (status QUEUED) + outbox_messages row (token_revoke_requested, PENDING)

A6 worker (background task, independent of the request path)
  -> claim_batch (SKIP LOCKED, exclusive between workers)
  -> POST /v1/tokens/{token_id}/revoke  (HTTP Basic auth + Idempotency-Key header)
  -> ActionAttempt row per attempt + audit event
  -> SUCCEEDED  -> request SUCCEEDED, outbox PROCESSED
  -> transient  -> request RETRYING,  outbox released with backoff (2^attempt * 5s, cap 300s)
  -> permanent  -> request FAILED,    outbox DEAD_LETTER (no retry)
  -> max attempts (8) exceeded -> request FAILED, outbox DEAD_LETTER
```

Runtime join (from the architecture plan):

```text
assessment + audit/outbox (A1/A5) -> revoke worker (A6) -> Razorpay revoke API
                                     -> dashboard shows REVOKED / RETRYING / FAILED (A8)
```

Operator-visible outcomes are exactly the A0 `ActionStatus` values `SUCCEEDED`, `RETRYING` and `FAILED` (`QUEUED`/`IN_PROGRESS` are transitional; `NOT_REQUIRED` is reserved and unused in v1).

## 2. Objective and definition of done

Build the adapter, the mock mode and the worker with deterministic, testable retry behaviour.

Definition of done:

- The real client sends `POST {base_url}/tokens/{token_id}/revoke` with HTTP Basic auth from `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` and an `Idempotency-Key` header that is stable across all attempts of one request.
- `2xx` classifies `SUCCEEDED`; all `4xx` (except 408/429) classify permanent; `408`, `429`, all `5xx`, timeouts and connection errors classify retryable.
- The mock client works with zero credentials, records every call with its idempotency key, deduplicates after a recorded success, and supports scheduled failures for the demo recovery scenario.
- Transient failures retry with exponential backoff (5s base, doubling, 300s cap, 8 attempts max) and then dead-letter; permanent failures dead-letter immediately with no retry.
- Every attempt appends one `ActionAttempt` row (unique `(action_request_id, attempt_number)`) and one audit event; retry never duplicates the revocation.
- The worker claims outbox rows exclusively (`SKIP LOCKED`): two workers can never process the same message.
- The worker runs as a background lifespan task in the API process and never blocks webhook acknowledgement; tests drive it deterministically through `process_pending_once`.
- No secret, no raw upstream body and no raw VPA appears in any log line, `safe_error_message`, audit payload or test fixture.
- Unit tests use `httpx.MockTransport` and fake repositories (no network, no PostgreSQL); integration tests run against real PostgreSQL with the mock/transport clients.

## 3. Scope and exact file ownership

### You own

```text
apps/api/app/integrations/razorpay/__init__.py
apps/api/app/integrations/razorpay/types.py
apps/api/app/integrations/razorpay/client.py
apps/api/app/integrations/razorpay/mock_client.py
apps/api/app/workers/__init__.py
apps/api/app/workers/config.py
apps/api/app/workers/outbox_worker.py
apps/api/app/repositories/action_attempt_read.py
apps/api/tests/unit/test_revoke_client.py
apps/api/tests/unit/test_outbox_worker.py
apps/api/tests/integration/test_outbox_worker_persistence.py
```

### The one new file in A1's directory (documented exception)

`apps/api/app/repositories/action_attempt_read.py` is a **new** file in the repositories package — it does not modify any A1-owned file. It is a read-only query repository over A1's ORM models (`app.persistence.models`), used only to derive attempt numbers. Style rules: constructor takes `AsyncSession`, reads only, never commits, never writes. If you find yourself wanting to edit an A1 file instead, stop and open an interface-change request.

### You may update minimally

```text
apps/api/app/main.py   # only: build the revoke client, start/stop the worker via a lifespan, per Section 7
```

### You may read but must not modify

```text
apps/api/app/contracts/**                # frozen A0 contracts (ActionRequest, ActionAttempt, enums)
apps/api/app/core/settings.py            # A0 settings owner (razorpay credentials)
apps/api/app/persistence/**              # A1 transaction/session/outbox implementation
apps/api/app/repositories/**             # A1 repositories (ActionRepository, OutboxRepository, AuditRepository)
apps/api/app/services/**                 # A2/A3/A4/A5 modules
apps/api/app/api/**                      # A2/A3 routes; A6 adds no routes
data/fixtures/**, apps/web/**            # A0/A7/A8/A9
docker-compose.yml, Makefile, pyproject.toml  # A0; A6 needs no dependency changes (httpx is already declared)
```

### Explicit non-goals

- No edits to A1's `OutboxRepository`, `ActionRepository` or any other owned file; no new migrations; no schema changes.
- No re-scoring, no feature work, no webhook/session logic — the worker consumes outbox payloads only.
- No reputation enrichment, notification or LLM narrative jobs (A12 optional work).
- No dashboard/UI and no new routes.
- No new dependencies: `httpx` (declared by A0) covers the client and `httpx.MockTransport` covers tests.
- The worker must never be called from inside a webhook/precheck request path; it runs only as a background task or under direct test control.

## 4. Revoke types and the real client (`types.py`, `client.py`)

### 4.1 Shared types (`types.py`)

```python
class RevokeOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"

@dataclass(frozen=True)
class RevokeResult:
    outcome: RevokeOutcome
    provider_status_code: int | None     # 100..599 when an HTTP response existed, else None
    safe_error_code: str | None          # fixed codes below; None on success
    safe_error_message: str | None       # fixed strings; never upstream body content

def classify_status(status_code: int) -> RevokeOutcome:
    """2xx -> SUCCEEDED; 408/429/5xx -> RETRYABLE; other 4xx -> PERMANENT; 1xx/3xx -> RETRYABLE."""
```

Fixed safe error codes and messages (never append upstream body text, URLs or credentials):

| Situation | `safe_error_code` | `safe_error_message` |
|---|---|---|
| 2xx | `None` | `None` |
| 4xx permanent | `UPSTREAM_REJECTED` | `"Razorpay rejected the revoke request (HTTP {status})."` |
| 408 / 429 | `UPSTREAM_TRANSIENT` | `"Razorpay asked to retry later (HTTP {status})."` |
| 5xx | `UPSTREAM_SERVER_ERROR` | `"Razorpay server error (HTTP {status})."` |
| Connect/transport error | `UPSTREAM_UNREACHABLE` | `"Could not reach Razorpay."` |
| Timeout | `UPSTREAM_TIMEOUT` | `"Razorpay call timed out."` |
| Malformed 2xx body | `UPSTREAM_INVALID_RESPONSE` | `"Razorpay returned an unexpected response shape."` |

### 4.2 `RazorpayTokenRevokeClient`

```python
class TokenRevokeClient(Protocol):
    async def revoke_token(self, token_id: str, idempotency_key: str) -> RevokeResult: ...
    async def aclose(self) -> None: ...

class RazorpayTokenRevokeClient:
    def __init__(
        self,
        key_id: SecretStr,
        key_secret: SecretStr,
        base_url: str = "https://api.razorpay.com/v1",
        timeout_seconds: float = 10.0,
        http_client: httpx.AsyncClient | None = None,   # injectable for MockTransport tests
    ) -> None: ...
```

Exact request:

```text
POST {base_url}/tokens/{token_id}/revoke
Authorization: Basic base64(key_id:key_secret)
Idempotency-Key: {idempotency_key}
Content-Type: application/json
body: {}
```

Rules:

- Validate `token_id` against `^token_[A-Za-z0-9]+$` and `idempotency_key` against `^[a-z0-9_-]{16,128}$` **before** any network call; violations raise `ValueError` with a fixed message.
- Build the `httpx.AsyncClient` lazily with `timeout=timeout_seconds`; `auth=(key_id, key_secret)`; never stringify the secret.
- Every `httpx.HTTPStatusError`/`TransportError`/timeout is converted into a `RevokeResult` — the method **returns** failures, it never raises for provider conditions. Only programming errors (invalid arguments) raise.
- The client logs nothing; `RevokeResult` is safe to serialise. `aclose()` closes the owned client only.
- Test-mode note for the PR: the real path is exercised with Razorpay Test-mode keys, which involve no real money.

## 5. Mock client and attempt reads (`mock_client.py`, `action_attempt_read.py`)

### 5.1 `MockTokenRevokeClient`

Used whenever Razorpay credentials are absent (the A0 rule: the code must work with no credentials) and by the demo/tests.

```python
@dataclass(frozen=True)
class MockRevokeCall:
    token_id: str
    idempotency_key: str
    result_outcome: RevokeOutcome

class MockTokenRevokeClient:
    def __init__(self) -> None: ...
    def schedule_failures(self, count: int, outcome: RevokeOutcome = RevokeOutcome.RETRYABLE,
                          status_code: int = 503) -> None
    @property
    def recorded_calls(self) -> tuple[MockRevokeCall, ...]: ...
    async def revoke_token(self, token_id: str, idempotency_key: str) -> RevokeResult: ...
    async def aclose(self) -> None: ...
```

Behaviour:

- Default: every call returns `SUCCEEDED` with `provider_status_code=200`.
- `schedule_failures(n)`: the next `n` calls return the configured failure outcome; afterwards calls succeed. This is exactly the demo scenario "force the first revoke call to fail, then show retry and final success".
- Records every invocation in `recorded_calls` (token id, idempotency key, outcome) so tests and the demo can prove exactly-once semantics.
- Deduplication: if the same `idempotency_key` has already produced a recorded `SUCCEEDED`, return that success **without recording a new call** — mirroring the provider-side idempotency guarantee.
- Same argument validation and no-logging rules as the real client.

### 5.2 Attempt-count reads (`action_attempt_read.py`)

```python
class ActionAttemptReadRepository:
    def __init__(self, db_session: AsyncSession) -> None: ...
    async def count_attempts(self, action_request_id: UUID) -> int: ...
    async def latest_attempt_number(self, action_request_id: UUID) -> int | None: ...
```

Implemented against A1's `action_attempts` ORM model, read-only (`select` + scalar), no writes, no commit. The worker uses `count_attempts(...) + 1` as the next `attempt_number`; the DB unique `(action_request_id, attempt_number)` stays the final guard.

## 6. Worker configuration and flow (`config.py`, `outbox_worker.py`)

### 6.1 Configuration (`config.py`)

```python
@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str = "outbox-worker-1"
    poll_interval_seconds: float = 2.0
    batch_size: int = 5
    max_attempts: int = 8
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 300.0

    def backoff_for(self, attempt_number: int) -> float:
        """min(base * 2**(attempt-1), cap) -> 5, 10, 20, 40, 80, 160, 300, 300."""
```

These are server-side configuration constants (the architecture's "thresholds are configuration" rule applies equally here); they are not exposed to the frontend.

### 6.2 Worker interface

```python
class OutboxWorker:
    def __init__(
        self,
        revoke_client: TokenRevokeClient,
        config: WorkerConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        outbox_repository: Callable[[AsyncSession], OutboxRepository] = OutboxRepository,
        action_repository: Callable[[AsyncSession], ActionRepository] = ActionRepository,
        audit_repository: Callable[[AsyncSession], AuditRepository] = AuditRepository,
        attempt_reads: Callable[[AsyncSession], ActionAttemptReadRepository] = ActionAttemptReadRepository,
    ) -> None: ...

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...
    async def process_pending_once(self, limit: int | None = None) -> int: ...
```

`run_forever` loops `process_pending_once` then sleeps `poll_interval_seconds`, exiting promptly when `stop_event` is set; it never lets an exception kill the loop (log-and-continue per cycle). `process_pending_once` is the deterministic entry point for tests and the demo.

### 6.3 Per-message processing (exact steps)

Each claimed message must have `message_type == "token_revoke_requested"`; anything else is dead-lettered with `safe_error_code="UNKNOWN_MESSAGE_TYPE"` (defensive; A5 emits only the known type).

1. Parse the payload: `action_request_id`, `mandate_event_id`, `token_id`, `idempotency_key`, `requested_at`. A payload missing any key is dead-lettered with `INVALID_PAYLOAD` and no provider call.
2. Load the request via `ActionRepository.get_request`. If it is missing, dead-letter with `ACTION_REQUEST_MISSING` and no provider call.
3. Set request status `IN_PROGRESS` via `update_request_status`.
4. `attempt_number = await attempt_reads(db_session).count_attempts(action_request_id) + 1`. If `attempt_number > config.max_attempts`: create a terminal `ActionAttempt` (`status=FAILED`, `safe_error_code="ATTEMPTS_EXHAUSTED"`, `attempted_at=now`, `completed_at=now`), set request `FAILED`, `mark_dead_letter`, audit, and continue to the next message — no provider call.
5. Call `revoke_client.revoke_token(token_id, idempotency_key)`. The idempotency key is identical on every attempt (A5 built it as `revoke-{mandate_event_id}`), so the provider/mock deduplicates.
6. Branch on `result.outcome`:

| Outcome | ActionAttempt | Request status | Outbox | Audit event |
|---|---|---|---|---|
| `SUCCEEDED` | `SUCCEEDED`, `provider_status_code`, `completed_at=now` | `SUCCEEDED` | `mark_processed` | `action_attempt.succeeded` |
| `RETRYABLE`, attempts remain | `RETRYING`, `safe_error_code`, `completed_at=None` | `RETRYING` | `release_for_retry(available_at=now+backoff_for(attempt), safe_error_code)` | `action_attempt.retrying` |
| `RETRYABLE`, attempts exhausted | `FAILED`, `safe_error_code`, `completed_at=now` | `FAILED` | `mark_dead_letter` | `action_attempt.failed` |
| `PERMANENT` | `FAILED`, `safe_error_code`, `completed_at=now` | `FAILED` | `mark_dead_letter` (no retry) | `action_attempt.failed` |

7. Every audit event: aggregate `action_request` / `request.action_request_id`, actor `WORKER` / `config.worker_id`, `occurred_at=now`, payload exactly:

```json
{
  "attempt_number": 2,
  "attempt_status": "RETRYING",
  "safe_error_code": "UPSTREAM_SERVER_ERROR",
  "provider_status_code": 503,
  "retry_available_at": "2026-01-15T10:00:35Z"
}
```

`retry_available_at` is present only on the retrying branch; `provider_status_code` is `null` for transport errors. No raw bodies, no secrets, no VPAs.

8. All writes for one message happen in one `async with transaction() as session` per message: attempt insert + status update + outbox transition + audit either all commit or all roll back. If the transaction fails, the message stays claimable (PENDING with `available_at` passed) and will be retried on a later cycle — the provider-side idempotency key keeps this safe.

### 6.4 Known limitations (state in the PR)

- A worker crash **between claim and commit** leaves a message in `PROCESSING`; v1 does not reclaim stale `PROCESSING` rows. Recovery is manual (`UPDATE outbox_messages SET status='PENDING'`) and documented for A10 ops; automated reclamation is deferred.
- The worker runs inside the API process. A dedicated worker container is a compose/infra change owned by A0/A10 coordination, not A6.

## 7. Application wiring (`main.py`)

A6 adds exactly this to `create_app()` (and a lifespan if A0 did not define one):

```python
if settings.razorpay_key_id and settings.razorpay_key_secret:
    revoke_client: TokenRevokeClient = RazorpayTokenRevokeClient(
        key_id=settings.razorpay_key_id, key_secret=settings.razorpay_key_secret,
    )
else:
    revoke_client = MockTokenRevokeClient()          # credential-free local/demo mode
app.state.token_revoke_client = revoke_client

@asynccontextmanager
async def lifespan(application: FastAPI):
    worker = OutboxWorker(revoke_client=revoke_client)
    stop_event = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop_event))
    yield
    stop_event.set()
    await task
```

The worker starts only via the lifespan — never at import. A9's demo controls may reach `app.state.token_revoke_client` (e.g. `schedule_failures`) to reproduce the failure-and-recovery scenario; A9 must not modify A6 files. Tests override `app.state.token_revoke_client` or construct `OutboxWorker` directly.

## 8. Tests and acceptance criteria

Unit tests are hermetic (`httpx.MockTransport`, fake repositories, fixed clock); integration tests run against real PostgreSQL with the mock/transport clients. Never commit real keys; test credentials are `test_key_id`/`test_key_secret` and exist only to prove the auth header construction.

### 8.1 Unit tests — `tests/unit/test_revoke_client.py`

Build `RazorpayTokenRevokeClient(key_id=SecretStr("test_key_id"), key_secret=SecretStr("test_key_secret"), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))` with a handler that records requests and returns configurable responses.

```text
test_2xx_returns_succeeded_with_status_code
test_request_uses_post_revoke_endpoint_and_basic_auth
test_idempotency_key_header_is_sent_on_every_call
test_429_and_5xx_are_classified_retryable
test_408_is_classified_retryable
test_4xx_other_than_408_429_is_classified_permanent
test_1xx_and_3xx_are_classified_retryable
test_timeout_is_classified_retryable_with_safe_code
test_connection_error_is_classified_retryable_with_safe_code
test_invalid_token_id_raises_before_any_http_call
test_invalid_idempotency_key_raises_before_any_http_call
test_upstream_body_never_appears_in_safe_error_message
test_secret_never_appears_in_result_or_exception_repr
test_client_returns_failures_instead_of_raising

test_mock_default_succeeds_and_records_call
test_mock_scheduled_failures_then_success
test_mock_dedupes_same_idempotency_key_after_success
test_mock_rejects_invalid_arguments
```

The auth test asserts the handler saw an `Authorization` header whose decoded value equals `test_key_id:test_key_secret` — and, critically, that no test output or repr leaks it.

### 8.2 Unit tests — `tests/unit/test_outbox_worker.py`

Fake in-memory repositories implementing only the A1 methods the worker uses, a `MockTokenRevokeClient` (or a scripted fake), and a fixed clock advanced explicitly.

```text
test_success_flow_marks_attempt_request_and_outbox
test_transient_failure_releases_with_backoff
test_backoff_sequence_is_5_10_20_40_80_160_300_300
test_retry_attempt_uses_same_idempotency_key
test_permanent_failure_dead_letters_without_retry
test_max_attempts_ends_in_failed_request_and_dead_letter
test_attempt_number_increments_per_retry
test_request_status_transitions_queued_to_in_progress_to_terminal
test_one_audit_event_per_attempt
test_audit_payload_contains_fixed_keys_only
test_unknown_message_type_is_dead_lettered_without_call
test_missing_action_request_is_dead_lettered_without_call
test_invalid_payload_is_dead_lettered_without_call
test_processed_message_is_not_processed_again
test_exception_during_processing_leaves_message_claimable
test_run_forever_stops_promptly_on_stop_event
```

### 8.3 Golden demo scenario (unit-level proof)

```text
test_demo_failure_and_recovery_scenario
```

Queue one request/outbox pair (A5 shapes), `schedule_failures(1)`, run `process_pending_once` three times with the clock advanced past each backoff: attempt 1 is `RETRYING` (503), attempt 2 is `SUCCEEDED` (200), `recorded_calls` shows the same idempotency key on both attempts and no third call, request status is `SUCCEEDED`, outbox `PROCESSED`.

### 8.4 Integration tests — `tests/integration/test_outbox_worker_persistence.py`

Use A1's `TEST_POSTGRES_URL` setup. Seed requests/outbox rows through the real `ActionRepository`/`OutboxRepository` (mimicking A5's exact payload shape from its Section 6.4), then run the real `OutboxWorker` with the `MockTokenRevokeClient`.

```text
test_end_to_end_revoke_success_flow
test_retry_then_success_flow_across_cycles
test_two_workers_never_process_the_same_message
test_dead_letter_after_permanent_failure
test_max_attempts_dead_letters_after_eight_attempts
test_audit_chain_for_action_aggregate_verifies
test_worker_cycle_after_success_is_a_noop
```

`test_two_workers_never_process_the_same_message`: build two workers with distinct `worker_id`s and separate sessions, run `process_pending_once` on both concurrently over a batch of 10 messages, and assert every message was claimed exactly once (no double attempts).

### 8.5 Manual acceptance (the architecture's demo scenario 6)

1. Start the stack with no Razorpay credentials (mock mode) and A5 merged.
2. Trigger a blocked confirmed webhook so A5 queues a revoke; the dashboard/request row shows `QUEUED`.
3. Via a small test script (or A9's control when merged), call `app.state.token_revoke_client.schedule_failures(1)`.
4. Within the poll interval the worker attempts: first attempt `RETRYING` (`UPSTREAM_SERVER_ERROR`, 503), then after backoff a second attempt `SUCCEEDED`.
5. Prove exactly-once: the mock's `recorded_calls` contains the idempotency key twice (two attempts, same key) with no duplicate success, the request shows `SUCCEEDED`, the outbox shows `PROCESSED`, and the audit chain for the aggregate verifies.

### 8.6 Verification commands

```powershell
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
uv --directory apps/api run pytest tests/unit/test_revoke_client.py tests/unit/test_outbox_worker.py -q
uv --directory apps/api run pytest tests/integration/test_outbox_worker_persistence.py -q
uv --directory apps/api run ruff check app/integrations/razorpay app/workers app/repositories/action_attempt_read.py tests/unit/test_revoke_client.py tests/unit/test_outbox_worker.py
uv --directory apps/api run ruff format --check app/integrations/razorpay app/workers app/repositories/action_attempt_read.py
uv --directory apps/api run mypy app/integrations/razorpay app/workers app/repositories/action_attempt_read.py
docker compose down
```

Do not mark the package done until all commands pass. No test may call the real Razorpay API; network access in tests is a failure condition.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A6 use |
|---|---|---|
| A0 | `ActionRequest`, `ActionAttempt`, `ActionStatus` enums, settings credentials | Exact public shapes; import only, never edit. |
| A1 | `transaction()`, `OutboxRepository` (claim/release/dead-letter), `ActionRepository`, `AuditRepository` | All persistence and exclusive claiming; A6 never edits these files. |
| A5 | outbox payload `token_revoke_requested` (Section 6.4 of A5's spec) and idempotency key | The worker's parsing contract; A5 creates, A6 consumes. |

### Outputs produced

| Consumer | Receives from A6 | Integration rule |
|---|---|---|
| A8 dashboard | `ActionAttempt` rows + request statuses via A1 | Shows `SUCCEEDED`/`RETRYING`/`FAILED` per attempt with safe error codes only. |
| A9 simulator | `app.state.token_revoke_client.schedule_failures(...)` and worker test pattern | Reproduces the failure-and-recovery demo without touching A6 files. |
| A10 CI/security | client/worker tests + redaction rules | Verifies no secrets or upstream bodies in logs, DB rows or payloads. |
| A12 narrative (optional) | audit/action history via A1 | Read-only; unrelated to the worker. |

### Merge order

1. A0 (`contracts-v1`) and A1 must be merged first; A6 also requires A5's outbox payload shape (merged or stubbed in tests).
2. A6 merges in parallel with A2–A5 — the only shared file is `main.py`, and A6's block is independent of A2/A3/A5's router/state blocks.
3. After A6, the end-to-end loop is complete: webhook -> assessment -> revoke -> worker -> terminal status. A7/A8/A9 then visualise and automate it.

### Pull-request handoff checklist

State in the A6 PR: the exact endpoint/auth/idempotency contract; the outcome-classification table; backoff curve and max attempts; the per-message write set (attempt + status + outbox + audit in one transaction); the demo failure-and-recovery proof (`recorded_calls`, statuses, audit chain valid); the stale-`PROCESSING` limitation; results for all commands in Section 8.6; confirmation that no A1-owned file was modified and `action_attempt_read.py` is additive and read-only; and known deferred work (separate worker container, stale-message reclamation, enrichment/notification jobs).

## 10. Failure conditions

The A6 work is unsafe or incomplete if any of these occur:

- The API secret, key id or authorization header appears in any log line, exception, result, audit payload, database row or test output.
- An upstream response body reaches `safe_error_message`, the database, an audit payload or a log.
- The same idempotency key can produce two successful provider revocations, or a retry uses a different key.
- Permanent 4xx failures are retried indefinitely, transient failures retry without backoff, or attempts exceed the configured maximum.
- The worker executes inside a webhook/precheck request path or blocks an HTTP response on a provider call.
- A claimed message can be processed by two workers, or a processed/failed message is re-executed.
- Attempt, status, outbox and audit writes for one message are not atomic.
- A6 edits A1/A5-owned files, changes frozen contracts, performs scoring, or adds routes/UI.
