# A1 — PostgreSQL persistence, transactional outbox and tamper-evident audit: implementation specification

## 0. Agent instruction

You are the **A1 persistence and audit agent**. Read these files in order before editing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/contracts.md`
4. `Idea.md`

Your job is to make the frozen A0 contract objects durable, queryable and auditable in PostgreSQL. Implement exactly this specification. Do not change anything in `apps/api/app/contracts/`, change the public OpenAPI shape, implement webhook parsing, execute Razorpay calls, calculate risk features/scores, build API routes or build frontend UI. Those are owned by A2–A12.

This is a financial-risk demo. Prefer deterministic transactions, explicit constraints and traceable failures over clever abstractions.

## 1. Objective and definition of done

Create an async SQLAlchemy/Alembic persistence layer that:

- maps every durable A0 contract to PostgreSQL without storing raw VPA, raw IP, raw device fingerprint, customer name, payment credential or API secret;
- rejects duplicate provider webhook events and duplicate action requests at the database boundary;
- writes a business record, its audit event and its outbox message in one transaction where a caller requests them together;
- creates an append-only, independently verifiable SHA-256 audit chain per aggregate; and
- supplies small repository interfaces that later agents can consume without reaching into ORM tables.

Definition of done:

- `alembic upgrade head` creates the schema from an empty PostgreSQL 16 database.
- The integration test suite proves round trips, constraints, transactions, outbox claiming and audit-tamper detection.
- Replaying a provider event with the same `(provider, provider_event_id)` returns the original event and adds no second audit record.
- Two concurrent audit appends to the same aggregate result in ordered sequence numbers and an intact chain.
- A single business transaction either persists its entity, audit event and outbox row together, or persists none of them.
- `scripts/verify_audit_chain.py` verifies a database aggregate and returns non-zero when it finds corruption.

## 2. Scope and file ownership

### You own

```text
apps/api/alembic.ini
apps/api/migrations/env.py
apps/api/migrations/script.py.mako
apps/api/migrations/versions/001_initial_persistence.py
apps/api/app/persistence/__init__.py
apps/api/app/persistence/base.py
apps/api/app/persistence/session.py
apps/api/app/persistence/models.py
apps/api/app/persistence/outbox.py
apps/api/app/persistence/audit_chain.py
apps/api/app/persistence/types.py
apps/api/app/repositories/__init__.py
apps/api/app/repositories/risk_sessions.py
apps/api/app/repositories/mandate_events.py
apps/api/app/repositories/feature_snapshots.py
apps/api/app/repositories/risk_assessments.py
apps/api/app/repositories/actions.py
apps/api/app/repositories/audit.py
apps/api/app/repositories/dashboard_read.py
apps/api/tests/unit/test_audit_chain.py
apps/api/tests/integration/**
scripts/verify_audit_chain.py
docs/threat-model.md
```

You may add a narrowly scoped test helper under `apps/api/tests/` and repository protocol/data-transfer files under `apps/api/app/repositories/` if needed. Keep all persistence-specific code inside `app/persistence` or `app/repositories`.

### You may read but must not modify

```text
apps/api/app/contracts/**                 # frozen A0 source of truth
apps/api/app/main.py                      # A0/A2 integration point
apps/api/app/core/settings.py             # A0 configuration owner
apps/api/app/api/routes/**                # A2/A3/A8/A9
apps/api/app/domain/rules/**              # A5
apps/api/app/services/**                  # A2/A4/A5
apps/api/app/integrations/**              # A3/A6/A12
apps/api/app/workers/**                   # A6/A12
apps/web/**                               # A7/A8
docker-compose.yml                        # A0
```

### Explicit non-goals

- Do not modify Pydantic fields, enum values, validation, OpenAPI or fixtures created by A0.
- Do not store the full Razorpay JSON webhook payload. A3 may preserve a redacted, separately approved diagnostic payload later; it is not part of A1.
- Do not write raw identifiers to a JSONB "metadata" escape hatch.
- Do not implement Redis, velocity, feature extraction, rules, ML, LLM, checkout session routes, dashboard routes, revocation HTTP calls or workers.
- Do not use SQLite as the primary integration database: partial indexes, JSONB, advisory locks and `FOR UPDATE SKIP LOCKED` are intentionally PostgreSQL-specific.

## 3. Persistence principles

1. **Contracts in, contracts out.** Repository write methods accept A0 contracts or explicit internal command objects; repository reads return A0 contracts or a separately named, redacted read projection. ORM model objects must not cross package boundaries.
2. **No raw PII.** The only VPA/IP/device/customer fields in tables are the A0 hash fields. `vpa_handle` is allowed because it is a UPI handle suffix such as `upi`, but it is lower-case and has no username.
3. **UUID primary keys match contract IDs.** Never introduce a numeric surrogate alongside a contract UUID.
4. **UTC always.** Every time column is `TIMESTAMP WITH TIME ZONE`; use server UTC defaults only for persistence timestamps and pass contract event time explicitly.
5. **JSONB is bounded.** JSONB stores A0 nested structures (`mandate_intent`, `sources`, `rule_evaluations`, `redacted_payload`, outbox payload). It is not a dumping ground for raw provider data.
6. **Database constraints enforce safety.** Important idempotency and range checks must not live only in Python.
7. **One transaction for one outcome.** A caller owns an `async with session.begin()` transaction. Repositories never independently commit.

## 4. Database connection and Alembic setup

### 4.1 Async session factory

Create `app/persistence/session.py` with:

```text
create_async_engine(settings.postgres_url, pool_pre_ping=True, future=True)
async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
get_session_factory() cached per process
asynccontextmanager transaction() yielding AsyncSession
dispose_engine() for test cleanup
```

Read `Settings` through `get_settings()`; do not copy setting definitions. The module must not connect to the database during import or FastAPI startup. Connection happens only when a caller opens a session.

Use type annotations for `AsyncSession` and `async_sessionmaker[AsyncSession]`. Repositories receive `AsyncSession` by constructor injection. They must call `flush()` when a generated database value/constraint outcome is needed, but never `commit()`, `rollback()` or `close()`.

### 4.2 Alembic

Create a conventional async Alembic setup under `apps/api/migrations/`. `alembic.ini` uses `script_location = migrations`; `env.py` imports `Base.metadata` from `app.persistence.base` and obtains the runtime URL from `get_settings().postgres_url` unless `sqlalchemy.url` is explicitly supplied by a test command.

Create a single, hand-written initial revision:

```text
revision: 001_initial_persistence
down_revision: None
```

It must create all tables, check constraints, foreign keys, unique constraints and indexes listed in this document. Do not use Alembic autogenerate for the committed migration. `downgrade()` drops tables in reverse foreign-key dependency order.

## 5. ORM tables and exact schema

Declare models in one `app/persistence/models.py` module for this first revision. All table names are lower-case plural. All use `Base` from `base.py`; do not use SQLAlchemy's implicit declarative base in multiple files.

### 5.1 `risk_sessions`

| Column | Type / constraints |
|---|---|
| `risk_session_id` | UUID primary key; contract value |
| `schema_version` | varchar(16), non-null, default `1.0` |
| `merchant_namespace` | varchar(64), non-null, indexed |
| `checkout_order_ref` | varchar(128), nullable |
| `customer_reference_hash` | varchar(76), nullable, indexed |
| `ip_hash` | varchar(76), nullable, indexed |
| `device_fingerprint_hash` | varchar(76), nullable, indexed |
| `user_agent_hash` | varchar(76), nullable |
| `flow_started_at` / `flow_completed_at` | timestamptz, nullable |
| `mandate_intent` | JSONB, non-null, default `{}` |
| `status` | varchar(16), non-null, indexed |
| `created_at` / `updated_at` | timestamptz, non-null |

Add partial unique index `uq_risk_sessions_namespace_order_ref` on `(merchant_namespace, checkout_order_ref)` where `checkout_order_ref IS NOT NULL`. Add check `flow_completed_at IS NULL OR flow_started_at IS NULL OR flow_completed_at >= flow_started_at`.

### 5.2 `mandate_webhook_events`

| Column | Type / constraints |
|---|---|
| `mandate_event_id` | UUID primary key |
| `schema_version` | varchar(16), non-null |
| `provider` | varchar(32), non-null |
| `provider_event_id` | varchar(128), non-null |
| `event_type` | varchar(32), non-null, indexed |
| `token_id` | varchar(128), non-null, indexed |
| `risk_session_id` | UUID nullable FK to `risk_sessions`, indexed |
| `vpa_hash` | varchar(76), nullable, indexed |
| `vpa_handle` | varchar(100), nullable |
| `recurring_status` | varchar(64), nullable |
| `failure_reason` | varchar(500), nullable |
| `provider_created_at` / `received_at` | timestamptz nullable / non-null |
| `raw_payload_sha256` | varchar(71), non-null |
| `is_demo_event` | boolean, non-null, default false |
| `persisted_at` | timestamptz, non-null server default UTC now |

Add unique constraint `uq_mandate_webhook_events_provider_event` on `(provider, provider_event_id)`. This is the authoritative webhook idempotency guard. Add check that `provider='razorpay'` for v1. Do not persist `X-Razorpay-Signature`.

### 5.3 `feature_snapshots`

The scalar fields from the A0 `FeatureSnapshot` must be real typed columns to support later dashboard filtering and model training. Do not reduce the snapshot to one untyped blob.

| Column group | Required columns |
|---|---|
| Identity | `feature_snapshot_id` UUID PK, `schema_version`, nullable FKs `mandate_event_id` and `risk_session_id`, `feature_version`, `calculated_at`, `is_demo_simulation` |
| Velocity | `ip_velocity_5m`, `ip_velocity_1h`, `device_velocity_5m`, `device_velocity_1h`, `customer_velocity_1h`, `vpa_velocity_1h`, `shared_demo_merchant_count_1h` as nullable integers |
| Behaviour/network | `is_new_device_for_customer`, `flow_duration_seconds`, `user_agent_bot_suspected`, `network_vpn_or_proxy`, `network_reputation_score`, `npci_risk_flag`, `payer_bank_or_compliance_flag` |
| Mandate | `max_amount_paise`, `mandate_frequency`, `expiry_days_from_registration` |
| Provenance | `sources` JSONB non-null default `{}` |

Add check `mandate_event_id IS NOT NULL OR risk_session_id IS NOT NULL`; checks enforcing non-negative numeric fields and `network_reputation_score BETWEEN 0 AND 100`; and a check that `shared_demo_merchant_count_1h IS NULL OR is_demo_simulation = true`. Add partial unique indexes on `(mandate_event_id, feature_version)` where the event ID is non-null and on `(risk_session_id, feature_version)` where session ID is non-null. Add indexes on `calculated_at` and `is_demo_simulation`.

### 5.4 `risk_assessments` and `rule_evaluations`

`risk_assessments` stores the complete decision; `rule_evaluations` is normalised so the dashboard can filter/display reasons without JSON parsing.

`risk_assessments` columns:

```text
assessment_id UUID primary key
schema_version varchar(16) not null
risk_session_id UUID nullable FK to risk_sessions, indexed
mandate_event_id UUID nullable FK to mandate_webhook_events, indexed
feature_snapshot_id UUID not null FK to feature_snapshots, indexed
stage varchar(32) not null, indexed
score integer not null check score BETWEEN 0 AND 100
decision varchar(16) not null, indexed
engine_version varchar(64) not null
evaluation_latency_ms integer not null check evaluation_latency_ms BETWEEN 0 AND 60000
assessed_at timestamptz not null, indexed
persisted_at timestamptz not null server default UTC now
```

Add check `stage <> 'PRECHECK' OR risk_session_id IS NOT NULL`; add check `stage NOT IN ('POST_CONFIRMATION','REJECTION_AUDIT') OR mandate_event_id IS NOT NULL`. Add partial unique indexes:

```text
(risk_session_id, stage, engine_version) WHERE risk_session_id IS NOT NULL
(mandate_event_id, stage, engine_version) WHERE mandate_event_id IS NOT NULL
```

`rule_evaluations` columns:

```text
rule_evaluation_id UUID primary key
assessment_id UUID not null FK risk_assessments ON DELETE CASCADE, indexed
rule_id varchar(64) not null
triggered boolean not null
points integer not null check points BETWEEN 0 AND 100
reason_code varchar(80) nullable
reason_text varchar(240) nullable
position integer not null check position >= 0
```

Unique `(assessment_id, position)` and `(assessment_id, rule_id)`. A repository preserves the list position received from the A0 contract.

### 5.5 `action_requests`, `action_attempts` and `outbox_messages`

`action_requests` columns:

```text
action_request_id UUID primary key
schema_version varchar(16) not null
assessment_id UUID not null FK risk_assessments, indexed
mandate_event_id UUID not null FK mandate_webhook_events, indexed
action_type varchar(32) not null
token_id varchar(128) not null
idempotency_key varchar(128) not null unique
status varchar(16) not null, indexed
requested_at timestamptz not null
persisted_at timestamptz not null server default UTC now
```

Add unique `(mandate_event_id, action_type)` to prohibit two token-revoke requests for one received event. Do not add a foreign key to a future worker/job table.

`action_attempts` columns:

```text
action_attempt_id UUID primary key
schema_version varchar(16) not null
action_request_id UUID not null FK action_requests ON DELETE CASCADE, indexed
attempt_number integer not null check attempt_number BETWEEN 1 AND 100
status varchar(16) not null
provider_status_code smallint nullable check provider_status_code BETWEEN 100 AND 599
safe_error_code varchar(80) nullable
safe_error_message varchar(240) nullable
attempted_at timestamptz not null
completed_at timestamptz nullable
```

Add unique `(action_request_id, attempt_number)` and check `completed_at IS NULL OR completed_at >= attempted_at`.

`outbox_messages` is internal only; do not add it to A0 public contracts. Columns:

```text
outbox_message_id UUID primary key
aggregate_type varchar(32) not null
aggregate_id UUID not null
message_type varchar(100) not null
payload JSONB not null
idempotency_key varchar(128) not null unique
status varchar(16) not null default 'PENDING', indexed
attempt_count integer not null default 0 check attempt_count >= 0
available_at timestamptz not null, indexed
locked_at timestamptz nullable
locked_by varchar(128) nullable
processed_at timestamptz nullable
last_error_code varchar(80) nullable
created_at timestamptz not null server default UTC now
```

Permit only statuses `PENDING`, `PROCESSING`, `PROCESSED`, `DEAD_LETTER` using a database check. A6 will execute messages; A1 only makes them atomically durable and claimable. The first message type is exactly `token_revoke_requested`.

### 5.6 `audit_events`

```text
audit_event_id UUID primary key
schema_version varchar(16) not null
aggregate_type varchar(32) not null
aggregate_id UUID not null
sequence_number integer not null check sequence_number >= 1
event_type varchar(100) not null
actor_type varchar(16) not null
actor_id varchar(128) nullable
occurred_at timestamptz not null
redacted_payload JSONB not null
previous_event_hash varchar(71) nullable
event_hash varchar(71) not null
persisted_at timestamptz not null server default UTC now
```

Add unique `(aggregate_type, aggregate_id, sequence_number)` and unique `event_hash`; add descending index `(aggregate_type, aggregate_id, sequence_number DESC)` for tail lookup. Audit rows have no update/delete repository methods and no cascade foreign keys. They remain even if a business row is removed during local development.

## 6. ORM conversion and repositories

### 6.1 Conversions

Place model conversion helpers in `app/persistence/types.py`. Use only standard JSON-safe `model_dump(mode="json")` values for JSONB. Convert nested A0 models into their JSON object form and reconstruct them with `model_validate` on reads.

Do not use a generic reflection-based mapper. Write explicit conversion functions for each contract:

```text
risk_session_to_row / risk_session_from_row
mandate_event_to_row / mandate_event_from_row
feature_snapshot_to_row / feature_snapshot_from_row
risk_assessment_to_row / risk_assessment_from_row
action_request_to_row / action_request_from_row
action_attempt_to_row / action_attempt_from_row
audit_event_to_row / audit_event_from_row
```

### 6.2 Repository interfaces

Implement each repository as a small class accepting `AsyncSession`. Use these public methods and return types exactly:

```text
RiskSessionRepository
  create(session: RiskSession) -> RiskSession
  get(risk_session_id: UUID) -> RiskSession | None
  get_by_order_ref(merchant_namespace: str, checkout_order_ref: str) -> RiskSession | None
  update_telemetry(risk_session_id: UUID, customer_reference_hash: HashValue | None, ip_hash: HashValue | None, device_fingerprint_hash: HashValue | None, user_agent_hash: HashValue | None, flow_completed_at: datetime | None, status: RiskSessionStatus, updated_at: datetime) -> RiskSession
  update_status(risk_session_id: UUID, status: RiskSessionStatus, updated_at: datetime) -> RiskSession

MandateEventRepository
  create_or_get(event: MandateWebhookEvent) -> tuple[MandateWebhookEvent, bool]
  get(mandate_event_id: UUID) -> MandateWebhookEvent | None
  attach_risk_session(mandate_event_id: UUID, risk_session_id: UUID) -> MandateWebhookEvent

FeatureSnapshotRepository
  create(snapshot: FeatureSnapshot) -> FeatureSnapshot
  get(feature_snapshot_id: UUID) -> FeatureSnapshot | None
  get_latest_for_event(mandate_event_id: UUID, feature_version: str) -> FeatureSnapshot | None

RiskAssessmentRepository
  create(assessment: RiskAssessment) -> RiskAssessment
  get(assessment_id: UUID) -> RiskAssessment | None
  get_precheck_for_session(risk_session_id: UUID) -> RiskAssessment | None
  get_for_event(mandate_event_id: UUID, stage: DecisionStage, engine_version: str) -> RiskAssessment | None

ActionRepository
  create_request(request: ActionRequest, outbox_payload: dict[str, object] | None = None) -> ActionRequest
  get_request(action_request_id: UUID) -> ActionRequest | None
  create_attempt(attempt: ActionAttempt) -> ActionAttempt
  update_request_status(action_request_id: UUID, status: ActionStatus) -> ActionRequest

OutboxRepository
  enqueue(aggregate_type: str, aggregate_id: UUID, message_type: str, payload: dict[str, object], idempotency_key: str, available_at: datetime) -> UUID
  claim_batch(worker_id: str, limit: int, now: datetime) -> list[OutboxMessage]
  mark_processed(outbox_message_id: UUID, processed_at: datetime) -> None
  release_for_retry(outbox_message_id: UUID, available_at: datetime, safe_error_code: str) -> None
  mark_dead_letter(outbox_message_id: UUID, safe_error_code: str) -> None

AuditRepository
  append(aggregate_type: str, aggregate_id: UUID, event_type: str, actor_type: AuditActorType, actor_id: str | None, occurred_at: datetime, redacted_payload: dict[str, object]) -> AuditEvent
  list_for_aggregate(aggregate_type: str, aggregate_id: UUID) -> list[AuditEvent]
  verify_aggregate(aggregate_type: str, aggregate_id: UUID) -> AuditVerificationResult

DashboardReadRepository
  list_recent_assessments(limit: int, offset: int, decision: Decision | None = None) -> list[DashboardAssessmentRow]
```

Define `OutboxMessage`, `AuditVerificationResult` and `DashboardAssessmentRow` as internal immutable dataclasses/Pydantic models in the repository or persistence package, not as changes to A0 contracts. `DashboardAssessmentRow` exposes only: assessment ID, decision, score, stage, assessed time, token ID, VPA handle, `is_demo_event`, action status and rule reasons. It must never expose VPA hash, IP hash, device hash, customer hash or payload JSON.

For `update_telemetry`, a `None` hash/timestamp argument means retain the existing stored value. The method changes only supplied telemetry fields, always applies the supplied `status`/`updated_at`, and returns the complete updated contract. It must not erase a previously captured customer/device/network value because a later telemetry PATCH omitted it.

### 6.3 Transaction boundary rules

Callers open the transaction:

```python
async with transaction() as session:
    events = MandateEventRepository(session)
    audit = AuditRepository(session)
    event, created = await events.create_or_get(event_contract)
    if created:
        await audit.append(...)
# commit occurs here
```

Implement no hidden commit. Let `IntegrityError` propagate only from low-level methods where a caller needs to decide duplicate behaviour. `create_or_get` specifically must catch the unique-conflict race using a savepoint/nested transaction, then query and return the existing contract with `created=False`. It must not add an audit event in the duplicate branch.

`ActionRepository.create_request` creates the request plus an outbox message only when `outbox_payload` is supplied. It uses the same request idempotency key for the outbox idempotency key. If either insert fails, the caller's surrounding transaction must roll back both. It does **not** create audit events automatically; the calling service adds a deliberately named audit event in the same transaction.

## 7. Tamper-evident audit chain

### 7.1 Exact hash algorithm

`app/persistence/audit_chain.py` must use the following byte sequence for every event. This is the canonical algorithm used by write and verify paths:

```text
canonical_payload = json.dumps(
    redacted_payload,
    ensure_ascii=True,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
)
hash_input = "|".join([
    previous_event_hash or "GENESIS",
    aggregate_type,
    str(aggregate_id).lower(),
    str(sequence_number),
    event_type,
    actor_type.value,
    actor_id or "",
    occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    canonical_payload,
]).encode("utf-8")
event_hash = "sha256:" + hashlib.sha256(hash_input).hexdigest()
```

Do not use `repr`, database JSON order, a Python dict string, floating-point values, an HMAC or a random salt. This is an integrity-evidence chain, not a replacement for database access control.

### 7.2 Concurrent appends

Before reading an aggregate tail or inserting an audit event, acquire a PostgreSQL transaction-scoped advisory lock:

```sql
SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0));
```

`lock_key` is exactly `audit:{aggregate_type}:{aggregate_id-lowercase}`. Then query the latest row ordered by `sequence_number DESC`, calculate next sequence/hash, insert and flush. This gives a correct linear chain when two service calls append concurrently. The unique sequence constraint remains the final guard.

### 7.3 Verification result

`verify_aggregate` loads rows ascending by sequence and checks: sequence begins at 1 and increments by one; first `previous_event_hash` is null; every later previous hash equals the prior stored hash; every stored hash equals a recomputed canonical hash. Return:

```text
AuditVerificationResult
  valid: bool
  checked_events: int
  first_invalid_sequence: int | None
  reason: str | None
```

For a missing aggregate, return `valid=True`, `checked_events=0`, no reason. Do not throw merely because no rows exist.

## 8. Migration constraints and indexes

The initial migration must include every constraint above. Add these additional indexes because the demo dashboard and workers need them:

```text
ix_mandate_events_received_at_desc              (received_at DESC)
ix_risk_assessments_assessed_at_desc            (assessed_at DESC)
ix_action_requests_status_requested_at          (status, requested_at DESC)
ix_outbox_claimable                             (status, available_at)
ix_audit_events_aggregate_tail                  (aggregate_type, aggregate_id, sequence_number DESC)
```

Use PostgreSQL `UUID`, `JSONB`, `TIMESTAMP(timezone=True)` and partial unique indexes through SQLAlchemy dialect features. Name every constraint/index explicitly so error messages and migrations remain stable. Do not create application-owned tables under Alembic's version table.

## 9. Verification script

Replace `scripts/verify_audit_chain.py` with a CLI using only the A1 persistence package. It accepts:

```text
--aggregate-type  risk_session|mandate_event|risk_assessment|action_request
--aggregate-id    UUID
--database-url    optional; defaults to Settings.postgres_url
```

It prints one JSON line matching `AuditVerificationResult` and exits `0` if valid or `1` if invalid. Invalid CLI arguments exit `2`. It must never print database URLs, settings, payloads or secret values.

## 10. Required tests

### 10.1 Unit tests — no PostgreSQL

Implement `tests/unit/test_audit_chain.py` with fixed UUID/time/payload values. Assert the exact known SHA-256 output for at least a genesis event and a chained event, stable hash output despite input dictionary key order, UTC normalisation, and failure when stored hash/previous hash/sequence is altered. Do not test the hash algorithm only indirectly.

### 10.2 PostgreSQL integration setup

Create `tests/integration/conftest.py` that reads `TEST_POSTGRES_URL`. It fails with a helpful message if missing; never silently uses SQLite. It runs `alembic upgrade head` once per test session, truncates every A1 table between tests in FK-safe order, and provides an `AsyncSession` fixture that rolls back leftover transactions. `TEST_POSTGRES_URL` for local Compose is:

```text
postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian
```

### 10.3 Required integration cases

Implement these named tests:

```text
test_migration_creates_all_expected_tables_and_constraints
test_risk_session_round_trip_preserves_contract_without_raw_fields
test_duplicate_checkout_order_ref_is_rejected_per_merchant_namespace
test_same_order_ref_is_allowed_in_different_namespaces
test_webhook_create_or_get_is_idempotent_and_returns_original
test_feature_snapshot_requires_event_or_session_at_database_boundary
test_feature_snapshot_demo_counter_requires_demo_flag_at_database_boundary
test_risk_assessment_persists_ordered_rule_evaluations
test_action_request_and_outbox_commit_atomically
test_action_request_duplicate_idempotency_key_is_rejected
test_outbox_claim_batch_is_exclusive_between_two_sessions
test_outbox_release_retry_and_dead_letter_transitions
test_audit_append_builds_and_verifies_chain
test_audit_chain_detects_direct_database_tampering
test_concurrent_audit_appends_are_linear_and_valid
test_dashboard_projection_is_redacted_and_ordered
```

For the tampering test, change `audit_events.redacted_payload` with raw SQL after valid insertion, then assert `verify_aggregate` returns `valid=False`. For the concurrency test, start two independent sessions/tasks appending different event types to the same aggregate and assert sequence `[1, 2]` and valid verification. For the outbox exclusivity test, claim one message with two sessions and assert only one receives it. Use fixed, synthetic fake data only.

Add `tests/integration/test_migrations.py` to run upgrade from an empty test database/schema and check the expected table names. Do not merely check that `alembic_version` exists.

## 11. Merge and downstream handoff

### Inputs you consume

| Input | Producer | How A1 uses it |
|---|---|---|
| Pydantic models and enums | A0 | Exact public persistence input/output contracts; import only, never edit. |
| `Settings.postgres_url` | A0 | Builds the async engine lazily. |
| Contract fixtures | A0 | Used as safe, fixed integration-test input. |
| Docker PostgreSQL service | A0 | Local integration-test database. |

### Outputs future agents consume

| Consumer | What it imports/uses from A1 | Merge rule |
|---|---|---|
| A2 session API | `RiskSessionRepository`, transaction context | It may add a route/service, never writes ORM directly. |
| A3 webhook API | `MandateEventRepository.create_or_get` | If `created=False`, it returns an idempotent acknowledgement and creates no extra audit event. |
| A4 feature extractor | `FeatureSnapshotRepository` | Emits a contract snapshot; its Redis state stays outside A1. |
| A5 risk engine | `RiskAssessmentRepository` | Persists ordered rule evaluations exactly as produced. |
| A6 action worker | `ActionRepository`, `OutboxRepository` | Claims messages with `SKIP LOCKED`; it owns Razorpay calls/retry policy. |
| A8 dashboard | `DashboardReadRepository` | Receives redacted rows only, never ORM or raw hashes. |
| A9 simulator | migrations + repositories | Can seed/replay only fake contract fixtures. |
| A10 CI/security | Alembic + A1 tests + audit CLI | Runs the verification commands, scans for raw sensitive fields. |

### Required merge order

1. A0 must be merged and tagged `contracts-v1` first.
2. Rebase A1 on that commit. Confirm that all A0 fixtures validate before adding a migration.
3. Merge A1 before A2/A3/A4/A5/A6 begin integration against a live database.
4. A2–A6 may develop in parallel with fake repositories, but they must rebase and use the A1 interfaces before merging.
5. Do not accept schema changes from a later work package as direct edits to `001_initial_persistence.py`. They must be new numbered migrations and retain A0 compatibility.

### Pull-request handoff checklist

Include all of the following in the A1 PR description:

- migration revision and exact table list;
- every public repository method implemented;
- test commands and results, including PostgreSQL integration tests;
- audit hash algorithm test vector/hash results;
- proof that duplicate webhook/outbox claim and tamper tests pass;
- a statement that `apps/api/app/contracts/**` was unchanged; and
- known deferred ownership: webhook signature parsing (A3), feature calculation (A4), scoring (A5), external revocation (A6), dashboard HTTP route/UI (A8).

## 12. Required verification commands

Run these from repository root after A0 foundation files are complete. Do not mark the package done until all pass.

```powershell
docker compose config
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
uv --directory apps/api run alembic upgrade head
uv --directory apps/api run pytest tests/unit/test_audit_chain.py -q
uv --directory apps/api run pytest tests/integration -q
uv --directory apps/api run ruff check app/persistence app/repositories tests
uv --directory apps/api run ruff format --check app/persistence app/repositories tests
uv --directory apps/api run mypy app/persistence app/repositories
uv --directory apps/api run python ../../scripts/verify_audit_chain.py --aggregate-type risk_session --aggregate-id 00000000-0000-4000-8000-000000000001
docker compose down
```

The CLI may report a valid zero-event result for the final command; that verifies startup and argument handling. Add a test-created aggregate command to the PR evidence for a non-empty valid chain and a tampered invalid chain.

## 13. Failure conditions

The A1 package is incomplete or unsafe if any of these occur:

- Any raw VPA, IP, fingerprint, customer identifier, webhook body, signature or secret reaches a persistent row, JSONB payload, log or test fixture.
- Repositories commit internally or allow an entity/audit/outbox sequence to partially persist.
- The unique provider-event constraint is missing or `create_or_get` can create duplicate audit records on retry.
- Audit hashing uses non-canonical JSON, local time, non-deterministic data, or lacks concurrency control.
- Audit events are mutable through an A1 repository method or cascade-delete with business data.
- `claim_batch` lets two workers process the same outbox row.
- A1 changes frozen A0 public contracts, implements Razorpay/network logic, or edits another agent's owned area.
