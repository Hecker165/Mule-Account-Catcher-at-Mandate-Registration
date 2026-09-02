# A3 — Razorpay webhook gateway: raw-body signature verification, idempotent ingestion and event normalisation: implementation specification

## 0. Agent instruction

You are the **A3 webhook gateway agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A2_SESSION_CAPTURE_AND_PRECHECK_API.md`
5. `docs/contracts.md`
6. `data/fixtures/webhooks/README.md`

Implement exactly this work package. You build the only public entry point that Razorpay servers call: `POST /v1/webhooks/razorpay`. You verify the signature over untouched raw request bytes, normalise the payload into the frozen A0 `MandateWebhookEvent` contract, correlate the pre-registration risk session, persist idempotently through A1 repositories, and append the receipt audit event.

Do **not** implement feature extraction, Redis velocity counters, rules, scoring, outbox enqueueing, revocation calls, Razorpay order creation, frontend pages, ML or LLM work. Use A0 contracts and A1 repositories unchanged, and follow the pseudonymisation and trust conventions established by A2.

This is payment-security code. Fail closed on every signature or configuration problem, and never let raw provider payloads, signatures or VPAs reach logs, responses or persistence.

## 1. Why this package exists

`token.confirmed` arrives from Razorpay after NPCI has already registered the mandate, so it is the merchant's last chance to act before the next value debit. A3 is the gateway that turns that raw HTTP callback into a durable, auditable, idempotent domain event:

```text
Razorpay servers
  -> POST /v1/webhooks/razorpay (raw body + x-razorpay-signature)
  -> A3 verifies HMAC-SHA256 over the exact bytes (fail closed)
  -> parser normalises payload into MandateWebhookEvent (raw body kept in memory only)
  -> correlator joins the pre-registration RiskSession (order notes/mapping; never webhook source IP)
  -> A1 MandateEventRepository.create_or_get (database-level idempotency guard)
  -> audit event mandate_event.received (same transaction)
  -> MandateEventEvaluator port (A4 features + A5 rules install the real implementation later)
  -> 200 acknowledgement to Razorpay
```

Runtime join with the other packages (from the architecture plan):

```text
checkout telemetry (A7) -> RiskSession (A2/A1) -> risk_session_id into order notes/mapping
Razorpay webhook (A3) -> stored event (A1) -> features (A4) -> score/reasons (A5)
                         -> assessment + audit/outbox (A1) -> revoke worker (A6)
                         -> dashboard (A8)
```

Two hard product rules that shape this package:

1. `CHALLENGE` can never be produced by this webhook. It arrives post-registration; the evaluator port here may only produce `ALLOW`, `BLOCK` (confirmed stage) or a rejection-audit outcome.
2. The webhook request IP belongs to Razorpay. A3 never records, hashes or reasons about the webhook source IP; all customer network/device context was already captured by A2 and is joined through correlation.

## 2. Objective and definition of done

Build a small, deterministic FastAPI webhook module with signature verification, payload normalisation, session correlation and transactional idempotent ingestion.

Definition of done:

- A validly signed `token.confirmed` fixture is accepted exactly once: one `mandate_webhook_events` row, one `mandate_event.received` audit event, one evaluation attempt.
- Replay of the same signed payload returns an idempotent acknowledgement and creates no second event row, no second audit event, no rescore and no second revoke job.
- An invalid, missing or tampered signature is rejected with HTTP 401 before any parsing or persistence.
- With no webhook secret configured, every webhook delivery fails closed with HTTP 503; nothing is parsed, persisted or evaluated.
- Signed but unrelated Razorpay event types (for example `payment.captured`) are acknowledged with `{"status":"ignored"}` and leave no database rows.
- A webhook whose order notes carry `risk_session_id` joins that session; one without correlation data is still accepted with `correlation_status="unavailable"` and `risk_session_id=None`.
- The route works with an injected fake evaluator in tests; when the real evaluator is absent the event is still persisted and audited with `evaluation_status="not_configured"`.
- Every route-level behaviour has unit and PostgreSQL integration tests; no test performs a real network call.

## 3. Scope and exact file ownership

### You own

```text
apps/api/app/api/routes/webhooks.py
apps/api/app/services/webhook_signature.py
apps/api/app/services/webhook_pseudonymisation.py
apps/api/app/services/webhook_event_parser.py
apps/api/app/services/mandate_evaluator_port.py
apps/api/app/services/webhook_ingestion.py
apps/api/tests/unit/test_webhook_signature.py
apps/api/tests/unit/test_webhook_event_parser.py
apps/api/tests/unit/test_webhook_ingestion.py
apps/api/tests/integration/test_webhook_api.py
apps/api/tests/integration/test_webhook_ingestion.py
apps/api/tests/helpers/__init__.py
apps/api/tests/helpers/webhook_signing.py
```

### You may update minimally

```text
apps/api/app/main.py   # only: include the A3 router and set the app.state objects listed in Section 10.2
```

Do not alter A0 metadata, health endpoints, settings, request-ID behaviour, or the A2 router and evaluator wiring already present.

### You may read but must not modify

```text
apps/api/app/contracts/**                  # frozen A0 contracts (MandateWebhookEvent, enums)
apps/api/app/core/settings.py              # A0 settings owner (razorpay_webhook_secret, hmac_pepper)
apps/api/app/persistence/**                # A1 transaction/session/audit-chain implementation
apps/api/app/repositories/**               # A1 repositories (MandateEventRepository, AuditRepository, RiskSessionRepository)
apps/api/app/api/routes/risk_sessions.py   # A2-owned
apps/api/app/api/routes/__init__.py        # A2-owned; router registration happens in main.py
apps/api/app/api/dependencies.py           # A2-owned
apps/api/app/services/session_capture.py   # A2-owned
apps/api/app/services/privacy_hashing.py   # A2-owned
apps/api/app/services/precheck_*.py        # A2-owned
apps/api/app/domain/rules/**               # A5
apps/api/app/integrations/**               # A6/A12
apps/api/app/workers/**                    # A6/A12
data/fixtures/webhooks/**                  # A0-owned payload-shape examples
apps/web/**                                # A7/A8
docker-compose.yml, Makefile               # A0
```

### Explicit non-goals

- Do not change any A0 Pydantic model, enum value, fixture, or the OpenAPI shapes of other agents' routes.
- Do not store the raw webhook body, the `x-razorpay-signature` header, or a raw VPA anywhere: the only permitted artifacts are `raw_payload_sha256`, `vpa_hash` and `vpa_handle`.
- Do not derive customer identity, IP, device or velocity data from the webhook. Customer context comes only from the correlated `RiskSession`.
- Do not call Razorpay HTTP APIs (orders, tokens, revoke). A6 owns the token-revoke client; order creation is out of scope for the buildathon core.
- Do not enqueue outbox messages, create `ActionRequest`s, or decide scores. The evaluator port (Section 10) hands off to A4/A5, which persist their own snapshot/assessment/audit/outbox through A1 in the same transaction.
- Do not add rate limiting, Razorpay IP allow-listing or retry queues: those belong to A10 infrastructure work.

## 4. Public webhook API

All route code lives in `apps/api/app/api/routes/webhooks.py` and is tagged `razorpay-webhooks` in FastAPI. The route must be declared as `async def razorpay_webhook(request: Request) -> JSONResponse` with **no Pydantic body parameter**, so the exact raw bytes are read with `await request.body()` and are never re-serialised by FastAPI before verification. Add `Cache-Control: no-store` to every response from this route.

```text
POST /v1/webhooks/razorpay
Required header when processing: x-razorpay-signature
Optional headers: x-razorpay-event-id, x-demo-event
```

### 4.1 Processing order (mandatory)

1. Read the configured verifier from `app.state.webhook_signature_verifier`. If `verifier.is_configured` is false, return `503 {"detail":"webhook signature verification is not configured"}`. This check comes before reading the body.
2. Read `raw_body = await request.body()` and `signature = request.headers.get("x-razorpay-signature")`.
3. If `verifier.verify(raw_body, signature)` is false (this covers a missing header), return `401 {"detail":"invalid webhook signature"}`. Persist and log nothing on this path except the request ID.
4. Classify and parse with `app.state.webhook_parser` (Section 7). The route supplies `provider_event_id` from the `x-razorpay-event-id` header and `is_demo_event` computed per Section 4.3.
5. If the classification is `ignored` (unrelated event type or unsupported method), return `200 {"status":"ignored","event_type":"<event>","reason":"<reason>"}`. No database writes.
6. If parsing raises `WebhookParseError`, return `400 {"detail":"<fixed safe message>"}`. No database writes. The message is one of the fixed strings in Section 7.4; it never contains payload fragments.
7. Correlate the risk session (Section 8) and run ingestion (Section 9) inside one A1 transaction.
8. Map the ingestion result to the responses in Section 4.2.

### 4.2 Success and idempotent responses

| Situation | Status | Body | Header |
|---|---|---|---|
| First delivery, evaluation completed | 200 | `{"status":"processed","mandate_event_id":"<uuid>","duplicate":false,"correlation":"correlated\|unresolved\|unavailable","evaluation":"completed"}` | — |
| First delivery, real evaluator not installed | 200 | same shape with `"evaluation":"not_configured"` | — |
| Repeat delivery of a known provider event | 200 | `{"status":"duplicate","mandate_event_id":"<original uuid>","duplicate":true}` | `X-Idempotent-Replay: true` |

### 4.3 Demo event flag

`is_demo_event=True` only when the request header `X-Demo-Event: true` is present **and** `get_settings().app_env != "production"`. Otherwise the header is ignored and the flag is `False`. This is how the A9 simulator and local demo fixtures mark traffic; production deployments silently reject the flag. The flag is never taken from the payload body.

### 4.4 Logging rules

Log at most: request ID, provider event ID (header value or derived prefix), classified event type, `mandate_event_id`, correlation status and evaluation status. Never log the raw body, the signature header, the webhook secret, VPAs, notes content or full payload JSON.

## 5. Signature verification (`webhook_signature.py`)

```python
class WebhookSignatureVerifier:
    def __init__(self, secret: SecretStr | str | None) -> None: ...
    @property
    def is_configured(self) -> bool: ...
    def verify(self, raw_body: bytes, signature_header: str | None) -> bool: ...
```

Exact algorithm — Razorpay's documented webhook signature scheme:

```python
expected = hmac.new(secret_bytes, raw_body, hashlib.sha256).hexdigest()
provided = (signature_header or "").strip().lower()
valid = hmac.compare_digest(expected.encode("ascii"), provided.encode("ascii"))
```

Rules:

- `secret_bytes` is `secret.get_secret_value().encode("utf-8")` when a `SecretStr` was supplied. An empty or `None` secret means `is_configured == False` and `verify` always returns `False`.
- Comparison must be constant time via `hmac.compare_digest`. Do not compare with `==`, do not decode the hex into bytes first, and do not short-circuit on length mismatch.
- Lowercase the header value only after stripping whitespace; the computed digest is already lowercase.
- `verify` returns `bool` only. It raises nothing and logs nothing. Malformed or absent headers are simply `False`.
- The verifier never stores the raw body or the signature after the call.

Unit tests must include a fixed test vector: for secret `local-test-secret` and body `b'{"event":"token.confirmed"}'`, compute the expected digest once in the test with the same algorithm and assert it, plus a tampered-body case and a tampered-signature case.

## 6. VPA pseudonymisation (`webhook_pseudonymisation.py`)

A2 owns `privacy_hashing.py` and its four domains. A3 owns the `vpa` domain so the two packages never edit the same file. Use exactly the same construction as A2 so all hashes are interchangeable and the single `HMAC_PEPPER` from A0 settings is used:

```python
class VpaPseudonymizer:
    def __init__(self, pepper: SecretStr | str) -> None: ...
    def hash_vpa(
        self, username: str | None, handle: str | None
    ) -> tuple[HashValue | None, str | None]: ...
```

Normalisation and algorithm:

- Concatenate `username` and `handle` as `f"{username}@{handle}"` only when both are present after normalisation; if `handle` is missing but `username` is present, hash `username` alone and return `handle=None`.
- Normalisation: Unicode NFKC, `strip()`, then lowercase (VPA identifiers are case-insensitive).
- Digest: `hmac.new(pepper_bytes, b"vpa" + b"\x00" + normalised.encode("utf-8"), hashlib.sha256).hexdigest()` prefixed with `hmac-sha256:`.
- Return `(None, None)` when the normalised username is empty. Never raise on odd input and never include the raw VPA in an error, log or audit payload.
- The `vpa_handle` returned to the parser is the lowercased handle (for example `upi`); it is not a secret and A1 stores it as a plain column.

## 7. Event parsing and normalisation (`webhook_event_parser.py`)

```python
class WebhookParseError(ValueError):
    """Raised with a fixed safe message only; never carries payload fragments."""

@dataclass(frozen=True)
class ParsedClassification:
    kind: str                                  # "mandate" | "ignored"
    event: MandateWebhookEvent | None
    notes_risk_session_id: UUID | None
    notes_merchant_namespace: str | None
    notes_checkout_order_ref: str | None
    ignore_reason: str | None

class RazorpayWebhookParser:
    def __init__(
        self,
        vpa_pseudonymizer: VpaPseudonymizer,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None: ...
    def classify_and_parse(
        self, raw_body: bytes, provider_event_id: str | None, is_demo_event: bool
    ) -> ParsedClassification: ...
```

### 7.1 Classification

- `payload.event` must be a string. Map only `token.confirmed`, `token.rejected` and `token.cancelled` to the A0 `MandateEventType` enum. Any other value yields `kind="ignored"` with `ignore_reason="unrelated event type"`.
- `payload.token.entity.method` (when present) must be `"upi"`; any other method yields `kind="ignored"` with `ignore_reason="unsupported method"`. A missing method is accepted.

### 7.2 Field mapping

| Contract field | Source | Rule |
|---|---|---|
| `schema_version` | constant | `"1.0"` |
| `mandate_event_id` | `uuid_factory()` | fresh UUID4 per parsed delivery attempt |
| `provider` | constant | `"razorpay"` |
| `provider_event_id` | argument | header value when supplied (1..128 chars after strip); otherwise `sha256(raw_body).hexdigest()[:32]` |
| `event_type` | `payload.event` | mapped enum value |
| `token_id` | `payload.token.entity.id` | required; must match `^token_[A-Za-z0-9]+$` |
| `vpa_hash`, `vpa_handle` | `entity.vpa` | accepts object `{"username":"...","handle":"..."}` or string `"user@handle"`; delegate to Section 6 |
| `recurring_status` | `entity.recurring_status` | optional string, 1..64 chars |
| `failure_reason` | `entity.failure_reason` | optional string, 1..500 chars |
| `provider_created_at` | `payload.created_at` | Unix epoch seconds; convert to timezone-aware UTC |
| `received_at` | `clock()` | timezone-aware UTC |
| `raw_payload_sha256` | computed | `"sha256:" + sha256(raw_body).hexdigest()` |
| `is_demo_event` | argument | from Section 4.3; never from the body |
| `risk_session_id` | not set here | filled by the correlator (Section 8) |

### 7.3 Notes extraction

From `entity.notes` (when present and a dict): read `risk_session_id` (must parse as a UUID, otherwise ignored), `merchant_namespace` and `checkout_order_ref` (plain strings, truncated to 64/128 chars). Notes content is never logged or persisted beyond these three correlation fields.

### 7.4 Fixed error messages

`WebhookParseError` is raised with exactly one of:

```text
"webhook body is not valid JSON"
"webhook payload is missing required token entity"
"webhook token id is invalid"
```

The route maps these one-to-one to HTTP 400 bodies. Never append received values, payload fragments or repr output to the message. Construct the `MandateWebhookEvent` with `model_validate` so A0 validation is the final authority; catch a `pydantic.ValidationError` and re-raise it as `WebhookParseError("webhook token id is invalid")` only when the failing field is `token_id`, and as `"webhook payload is missing required token entity"` otherwise.

## 8. Session correlation

Correlation runs inside the ingestion service, after parsing and before persistence. Strategy, in order:

1. **Notes UUID (preferred).** If `notes_risk_session_id` is present, load it through `RiskSessionRepository.get`. If found, use it (`correlation_status="correlated"`). If not found, leave `risk_session_id=None` with `correlation_status="unresolved"` — a provider-attested UUID that does not exist locally must not be stored as a foreign key value.
2. **Order mapping.** Otherwise, when `notes_merchant_namespace` and `notes_checkout_order_ref` are both present, call `RiskSessionRepository.get_by_order_ref(merchant_namespace, checkout_order_ref)`. If found, `correlation_status="correlated"`. This is the convention A2/A7 follow when the merchant uses its own order reference in Razorpay order notes.
3. **Unavailable.** Otherwise `risk_session_id=None`, `correlation_status="unavailable"`.

Hard rules:

- The webhook client IP and all forwarding headers are never used for correlation (the A2 Section 5.1 trust table applies unchanged).
- Correlation never raises. A missing mapping is a safe, auditable outcome, not an error: the architecture acceptance criterion is "absent mapping is safely marked `context_unavailable`".
- The A0 contract keeps `risk_session_id` optional; downstream features/scoring treat an uncorrelated event exactly like a first-seen customer.

## 9. Ingestion service and transaction rules (`webhook_ingestion.py`)

```python
@dataclass(frozen=True)
class IngestResult:
    mandate_event_id: UUID
    is_duplicate: bool
    correlation_status: str          # "correlated" | "unresolved" | "unavailable"
    evaluation_status: str           # "completed" | "not_configured" | "skipped_duplicate"

class WebhookIngestionService:
    def __init__(
        self,
        mandate_events: MandateEventRepository,
        audit: AuditRepository,
        risk_sessions: RiskSessionRepository,
        evaluator: MandateEventEvaluator,
        clock: Callable[[], datetime],
    ) -> None: ...
    async def ingest(
        self,
        event: MandateWebhookEvent,
        notes_risk_session_id: UUID | None,
        notes_merchant_namespace: str | None,
        notes_checkout_order_ref: str | None,
    ) -> IngestResult: ...
```

The route opens the transaction; the service never commits:

```python
async with transaction() as session:
    service = WebhookIngestionService(
        mandate_events=MandateEventRepository(session),
        audit=AuditRepository(session),
        risk_sessions=RiskSessionRepository(session),
        evaluator=request.app.state.mandate_event_evaluator,
        clock=lambda: datetime.now(timezone.utc),
    )
    result = await service.ingest(...)
```

### 9.1 Exact processing steps

1. Correlate per Section 8; when correlated, set the link with `event.model_copy(update={"risk_session_id": ...})`. Contracts are immutable — never mutate in place.
2. `stored, created = await mandate_events.create_or_get(event)`.
3. If `created` is false: return `IngestResult(mandate_event_id=stored.mandate_event_id, is_duplicate=True, correlation_status="unavailable", evaluation_status="skipped_duplicate")`. Do not append an audit event, do not run the evaluator, and write nothing else. This is the A1-mandated duplicate branch.
4. Append exactly one audit event: `event_type="mandate_event.received"`, `aggregate_type="mandate_event"`, `aggregate_id=stored.mandate_event_id`, `actor_type=WEBHOOK`, `actor_id="razorpay_webhook"`, `occurred_at=clock()`, with exactly this payload shape (values from the stored event):

```json
{
  "event_type": "token.confirmed",
  "provider": "razorpay",
  "token_id": "token_demo123",
  "correlation_status": "correlated",
  "risk_session_id": "00000000-0000-4000-8000-000000000001",
  "is_demo_event": false,
  "vpa_handle": "upi",
  "failure_reason_present": false,
  "evaluation_status": "completed"
}
```

`risk_session_id` is the correlated UUID string or `null`; `evaluation_status` reflects the outcome of step 5.

5. Invoke the evaluator in the same transaction:

```python
try:
    await evaluator.evaluate(db_session, stored)
    evaluation_status = "completed"
except MandateEvaluatorUnavailable:
    evaluation_status = "not_configured"   # event + audit stay committed
```

Any other exception propagates: the route lets the transaction roll back and returns HTTP 500 so the provider retries later. No partial rows may survive.

6. Return the `IngestResult`.

### 9.2 Why the unavailable evaluator does not fail the webhook

Before A4/A5 merge, every real delivery would otherwise roll back and Razorpay would retry forever. The event and its receipt audit are durable facts regardless of evaluation readiness, so they are committed with `evaluation_status="not_configured"`. Known limitation (state it in the PR): a duplicate replay of such an event is never re-evaluated, because the duplicate branch short-circuits. A9 can replay the scenario with a fresh provider event ID after A5 is installed.

### 9.3 Outbox boundary

A3 never writes `outbox_messages` or `ActionRequest` rows. For a `BLOCK` decision, A5's evaluator persists the feature snapshot, risk assessment, its own audit event and the revoke outbox message through A1 repositories inside the same transaction A3 opened. The A1 guarantee "action request + outbox commit atomically" therefore holds for the whole webhook call.

## 10. Evaluator port and application wiring

### 10.1 Port (`mandate_evaluator_port.py`)

```python
class MandateEventEvaluator(Protocol):
    async def evaluate(self, db_session: AsyncSession, event: MandateWebhookEvent) -> None: ...

class MandateEvaluatorUnavailable(RuntimeError):
    """Raised when the real A4/A5 evaluator has not been installed."""

class UnavailableMandateEvaluator:
    async def evaluate(self, db_session: AsyncSession, event: MandateWebhookEvent) -> None:
        raise MandateEvaluatorUnavailable
```

The A4/A5 implementation must, inside the supplied transaction: extract features, persist a `FeatureSnapshot`, evaluate rules, persist a `RiskAssessment` (`stage=POST_CONFIRMATION` for confirmed events, `REJECTION_AUDIT` for rejected/cancelled events, with `mandate_event_id` set and `risk_session_id` matching the correlated session), append its own audit event and — for a confirmed `BLOCK` — create the revoke `ActionRequest` with its outbox payload. It returns `None`; A3 owns nothing about the outcome beyond `evaluation_status`.

### 10.2 Wiring in `main.py`

A3 adds exactly this to `create_app()`:

```python
app.state.webhook_signature_verifier = WebhookSignatureVerifier(settings.razorpay_webhook_secret)
app.state.webhook_vpa_pseudonymizer = VpaPseudonymizer(settings.hmac_pepper)
app.state.webhook_parser = RazorpayWebhookParser(app.state.webhook_vpa_pseudonymizer)
app.state.mandate_event_evaluator = UnavailableMandateEvaluator()
app.include_router(webhooks.router, prefix="/v1")
```

When A4/A5 merge, they replace only the `mandate_event_evaluator` state assignment — the same intentionally small seam A2 established for the pre-check. A4/A5 must not edit A3 route logic. Tests override any of the four `app.state` objects directly; no env-var patching of the cached settings object is needed.

## 11. Tests and acceptance criteria

All tests use fake/synthetic data and fixed clocks; unit tests must not touch PostgreSQL, and integration tests must not touch the network. Never commit a real webhook secret; the test secret is `local-test-secret`.

### 11.1 Test helper (`tests/helpers/webhook_signing.py`)

```python
TEST_WEBHOOK_SECRET = "local-test-secret"

def sign_body(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """Return the hex HMAC-SHA256 signature of the raw body."""

def load_webhook_fixture(filename: str) -> bytes:
    """Load a file from data/fixtures/webhooks/ as raw bytes."""
```

### 11.2 Unit tests — `tests/unit/test_webhook_signature.py`

```text
test_known_secret_and_body_produce_expected_hmac_hex
test_verify_accepts_exact_body_and_signature
test_verify_rejects_tampered_body
test_verify_rejects_tampered_signature
test_verify_rejects_missing_and_malformed_header_values
test_verify_uses_constant_time_compare
test_empty_or_none_secret_means_not_configured_and_always_fails
```

### 11.3 Unit tests — `tests/unit/test_webhook_event_parser.py`

Load the A0 fixtures `token_confirmed_body.json` and `token_rejected_npci_risk_body.json` as the primary inputs.

```text
test_confirmed_fixture_parses_to_valid_mandate_event
test_rejected_fixture_preserves_npci_risk_failure_reason
test_provider_event_id_header_wins_over_derived_prefix
test_missing_provider_event_id_derives_body_digest_prefix
test_vpa_object_and_string_forms_hash_identically
test_vpa_hash_uses_vpa_domain_and_pepper
test_epoch_created_at_becomes_timezone_aware_utc
test_unrelated_event_type_is_classified_ignored
test_non_upi_method_is_classified_ignored
test_missing_token_entity_raises_fixed_parse_error
test_invalid_token_id_raises_fixed_parse_error
test_malformed_json_raises_fixed_parse_error
test_notes_fields_are_extracted_and_invalid_uuid_ignored
test_demo_flag_comes_from_argument_not_payload
test_raw_body_and_signature_never_appear_in_event_or_error_text
```

### 11.4 Unit tests — `tests/unit/test_webhook_ingestion.py`

Use fake repositories, a fake evaluator and a fixed clock (same pattern as A2's `test_precheck_orchestrator.py`).

```text
test_first_delivery_persists_event_and_appends_exactly_one_audit_event
test_duplicate_delivery_returns_original_without_audit_or_evaluation
test_unavailable_evaluator_still_persists_event_with_not_configured_status
test_unexpected_evaluator_error_marks_transaction_for_rollback
test_notes_uuid_correlation_links_existing_session
test_unknown_notes_uuid_yields_unresolved_and_null_link
test_absent_notes_yields_unavailable_correlation
test_audit_payload_contains_fixed_keys_only_and_no_raw_values
```

### 11.5 Integration tests — `tests/integration/test_webhook_api.py`

Use A1's `TEST_POSTGRES_URL` fixture setup and FastAPI `TestClient` (or httpx `ASGITransport`) against the real app with `app.state` overrides.

```text
test_valid_signed_fixture_is_accepted_exactly_once
test_replay_of_same_signed_payload_is_idempotent_with_replay_header
test_replay_creates_no_second_assessment_or_revoke_job
test_invalid_signature_is_rejected_401_before_persistence
test_missing_signature_is_rejected_401
test_tampered_body_after_signing_is_rejected_401
test_unconfigured_secret_fails_closed_503
test_malformed_json_with_valid_signature_is_400
test_unrelated_event_type_is_acked_ignored_with_no_rows
test_non_upi_method_is_acked_ignored_with_no_rows
test_notes_risk_session_id_correlates_to_risk_session
test_absent_correlation_is_persisted_with_null_session
test_responses_set_no_store_and_preserve_request_id
test_no_raw_vpa_signature_or_body_in_any_response_or_log
```

For `test_replay_creates_no_second_assessment_or_revoke_job`, install a fake evaluator that records each call and (for confirmed events) creates one assessment plus one outbox message via the real A1 repositories; assert the evaluator ran once and outbox rows equal one after two identical deliveries.

### 11.6 Integration tests — `tests/integration/test_webhook_ingestion.py`

```text
test_create_or_get_is_idempotent_against_real_database
test_audit_event_chain_is_built_for_mandate_event_aggregate
test_rollback_on_evaluator_error_leaves_no_event_and_no_audit_rows
test_raw_payload_sha256_matches_body_and_body_is_never_persisted
```

The rollback test intentionally raises inside the evaluator and asserts zero rows in `mandate_webhook_events` and `audit_events` afterwards.

### 11.7 Manual acceptance (before the A9 simulator exists)

1. Start the stack with `RAZORPAY_WEBHOOK_SECRET=local-test-secret` in `.env`.
2. Sign `token_confirmed_body.json` with the helper and POST it with `x-razorpay-signature` and `x-razorpay-event-id: evt_demo_confirmed_001`; expect `{"status":"processed",...,"evaluation":"not_configured"}`.
3. Replay it; expect `{"status":"duplicate",...}` with `X-Idempotent-Replay: true` and unchanged database counts.
4. POST the same body with a wrong signature; expect 401 and unchanged counts.
5. POST `token_rejected_npci_risk_body.json` signed; expect processed with `failure_reason_present: true` in the audit event and no action rows.

### 11.8 Verification commands

```powershell
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
uv --directory apps/api run pytest tests/unit/test_webhook_signature.py tests/unit/test_webhook_event_parser.py tests/unit/test_webhook_ingestion.py -q
uv --directory apps/api run pytest tests/integration/test_webhook_api.py tests/integration/test_webhook_ingestion.py -q
uv --directory apps/api run ruff check app/api/routes/webhooks.py app/services/webhook_signature.py app/services/webhook_pseudonymisation.py app/services/webhook_event_parser.py app/services/mandate_evaluator_port.py app/services/webhook_ingestion.py
uv --directory apps/api run ruff format --check app/api/routes/webhooks.py app/services/webhook_signature.py app/services/webhook_pseudonymisation.py app/services/webhook_event_parser.py app/services/mandate_evaluator_port.py app/services/webhook_ingestion.py
uv --directory apps/api run mypy app/services/webhook_signature.py app/services/webhook_pseudonymisation.py app/services/webhook_event_parser.py app/services/mandate_evaluator_port.py app/services/webhook_ingestion.py
docker compose down
```

Do not mark the package done until all commands pass. Performance note: the architecture target is webhook-to-persisted-assessment p95 under 200 ms; A3's own work (verify, parse, correlate, one insert, one audit append) is local database work only — never add an HTTP call inside the webhook transaction.

## 12. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A3 use |
|---|---|---|
| A0 | `MandateWebhookEvent`, `MandateEventType`, `HashValue`, enums, settings, webhook fixtures | Exact public shape; import only, never edit. Fixtures are the canonical test payloads. |
| A1 | `transaction()`, `MandateEventRepository.create_or_get`, `AuditRepository`, `RiskSessionRepository` | Durable idempotency, audit chain, correlation lookup. |
| A2 | Pseudonymisation conventions and trust table | Same HMAC construction, same "never trust client claims" rules. |
| A4/A5, later | `MandateEventEvaluator` implementation | Feature/scoring/outbox behind the A3 port; installed via one `app.state` assignment. |

### Outputs produced

| Consumer | Receives from A3 | Integration rule |
|---|---|---|
| A4 feature extractor | persisted `MandateWebhookEvent` via A1 repositories | Reads event fields and the correlated session; computes Redis velocity features inside the evaluator transaction. |
| A5 rule engine | event + session context | Persists snapshot/assessment/audit/outbox in A3's transaction; may only produce ALLOW/BLOCK post-confirmation. |
| A6 revoke worker | outbox message created by A5 | A3 creates no actions; A6 consumes the outbox with `SKIP LOCKED`. |
| A8 dashboard | redacted event/assessment/audit rows via A1 | Never receives raw payloads or signatures. |
| A9 simulator | endpoint contract and signing helper pattern | Sends signed fixtures with fresh `x-razorpay-event-id` values; uses `X-Demo-Event: true` locally. |
| A10 CI/security | webhook tests + redaction rules | Verifies no signature/secret/VPA leakage in logs. |

### Merge order

1. A0 (`contracts-v1`) and A1 must be merged first; A3 also requires A2's merge for the pseudonymisation conventions, `app.state` pattern and `RiskSessionRepository` semantics.
2. Implement and merge A3 with fake-evaluator tests.
3. A4/A5 develop in parallel against the frozen `MandateEventEvaluator` protocol, then rebase; their only integration change is the `app.state.mandate_event_evaluator` assignment.
4. A6 integration happens after A5 produces real outbox messages from webhook-driven assessments.
5. A9's simulator and A10's CI consume the A3 endpoint and tests without modification.

### Pull-request handoff checklist

State in the A3 PR: routes and exact status codes; the fail-closed matrix; signature algorithm and the constant-time test; idempotency evidence (replay produces one row, one audit event, no evaluation); correlation strategy and the safe `context_unavailable` behaviour; the audit payload schema; results for all commands in Section 11.8; confirmation that `contracts/`, `persistence/`, `repositories/`, `domain/`, `integrations/`, `workers/`, `apps/web/` and A2-owned services were not modified; and known deferred work (real evaluator A4/A5, simulator A9, Razorpay allow-list/observability A10).

## 13. Failure conditions

The A3 work is unsafe or incomplete if any of these occur:

- Any code path parses, persists or evaluates a webhook whose signature failed, or proceeds when the secret is unconfigured.
- The signature check is not constant-time over the exact raw bytes, or the body is re-serialised before verification.
- The same provider event can create two rows, two audit events, two assessments or two revoke jobs.
- The raw body, signature header, secret or a raw VPA reaches a database row, JSONB payload, log line, response body, audit payload or test fixture.
- Correlation uses the webhook source IP, forwarding headers, or any client-controlled claim other than the signature-verified order notes.
- A missing correlation mapping raises instead of safely persisting `risk_session_id=None`.
- A3 writes outbox/action rows, computes scores, calls Razorpay APIs, or modifies A0/A1/A2-owned files.
- A failing evaluator leaves partial rows instead of rolling back the whole transaction.
- The webhook returns an HTTP error for a successfully persisted event when only the optional evaluator was missing.
