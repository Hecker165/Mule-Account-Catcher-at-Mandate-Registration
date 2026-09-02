# A5 — Deterministic rule engine, explanations and evaluation orchestration: implementation specification

## 0. Agent instruction

You are the **A5 rule engine and evaluation agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A2_SESSION_CAPTURE_AND_PRECHECK_API.md`
5. `docs/work-packages/A3_RAZORPAY_WEBHOOK_GATEWAY.md`
6. `docs/work-packages/A4_REDIS_FEATURE_STORE_AND_EXTRACTION.md`
7. `docs/contracts.md`

Implement exactly this work package. You build the versioned deterministic rule ensemble that converts an A0 `FeatureSnapshot` into a `RiskAssessment` (score, decision, ordered rule evaluations, human-readable reasons), plus the two evaluation services that compose A4's `FeatureExtractor` with this engine and persist snapshot + assessment + audit + revoke outbox through A1 in the caller's transaction.

Do **not** change A0 contracts, do not call Redis or PostgreSQL outside A4's store and A1's repositories, do not implement revoke HTTP calls or a worker (A6), do not touch webhook/session routes, and do not build UI, ML or LLM features.

The engine is a pure, synchronous function of `(snapshot, stage, config)`. No clock, no I/O, no randomness inside the engine — determinism is the product.

## 1. Why this package exists

The enforcement-critical path must be explainable and reproducible. This package completes the runtime join:

```text
webhook event (A3) / ready session (A2)
  -> A4 FeatureExtractor -> FeatureSnapshot (persisted by A5)
  -> A5 RuleEngine.evaluate(snapshot, stage)
       -> score (0..100), decision, RuleEvaluation list with reason codes and text
  -> RiskAssessment persisted (A1) + audit event
  -> BLOCK + token.confirmed -> one revoke ActionRequest + outbox message (A1, same transaction)
  -> A6 worker executes the revoke; A8 dashboard displays reasons
```

Score-to-decision mapping (architecture-mandated, configuration-owned):

| Stage | Score | Decision |
|---|---|---|
| `PRECHECK` | 0–29 | `ALLOW` |
| `PRECHECK` | 30–100 | `CHALLENGE` (never `BLOCK`; pre-check may only challenge before the UPI redirect) |
| `POST_CONFIRMATION` | 0–69 | `ALLOW` |
| `POST_CONFIRMATION` | 70–100 | `BLOCK` (queues exactly one revoke) |
| `REJECTION_AUDIT` | 0–69 | `ALLOW` |
| `REJECTION_AUDIT` | 70–100 | `BLOCK` (recorded and displayed; **no revoke is ever queued** because the mandate was already rejected/cancelled) |

`CHALLENGE` is impossible outside `PRECHECK`; `BLOCK` at `REJECTION_AUDIT` never triggers an action. Both invariants are engine-level and tested.

## 2. Objective and definition of done

Build the rule catalogue, engine, explanation renderer and evaluation services with golden-fixture proof.

Definition of done:

- Golden fixtures yield the exact expected score, decision and triggered rule IDs, including threshold-edge cases at scores 29/30/69/70 and clamping above 100.
- Every rule evaluation lists `rule_id`, `triggered`, `points`, `reason_code` and `reason_text`; untriggered rules report `points=0` and no reason; catalogue order is preserved.
- A missing (`None`) feature never triggers a rule and never produces a guessed score.
- `CHALLENGE` can never be produced for `POST_CONFIRMATION`/`REJECTION_AUDIT`; a revoke request can only be created for a `POST_CONFIRMATION` `BLOCK`.
- Both evaluator ports from A2/A3 are implemented and wired in `main.py`, replacing the two `Unavailable*` placeholders exactly at the documented seams — nothing else in routes/services changes.
- A blocked confirmed event produces, in one transaction: one snapshot, one assessment, one assessment audit event, one action request and one outbox message. A rejected event produces snapshot + assessment + audit and no action.
- `evaluation_latency_ms` is measured (extraction + scoring), clamped to 0..60000.
- Unit tests are pure (no Redis/Postgres); integration tests prove persistence and atomicity against real services.

## 3. Scope and exact file ownership

### You own

```text
apps/api/app/domain/rules/__init__.py
apps/api/app/domain/rules/config.py
apps/api/app/domain/rules/catalogue.py
apps/api/app/domain/rules/engine.py
apps/api/app/domain/rules/renderer.py
apps/api/app/services/risk_evaluation.py
apps/api/tests/unit/test_rule_engine.py
apps/api/tests/unit/test_rule_golden_fixtures.py
apps/api/tests/unit/test_risk_evaluation_services.py
apps/api/tests/integration/test_evaluation_against_persistence.py
data/fixtures/rules/**
```

### You may update minimally

```text
apps/api/app/main.py   # only: replace the two Unavailable* evaluator placeholders and set the two A4 state keys, exactly as in Section 7
```

Do not alter A0 metadata, health endpoints, settings, request-ID behaviour, or the A2/A3 route wiring.

### You may read but must not modify

```text
apps/api/app/contracts/**                  # frozen A0 contracts
apps/api/app/core/settings.py              # A0 settings owner
apps/api/app/persistence/**                # A1 transaction/audit-chain implementation
apps/api/app/repositories/**               # A1 repositories, injected/constructed by the services
apps/api/app/services/features/**          # A4 FeatureExtractor, RedisFeatureStore, reputation port
apps/api/app/services/mandate_evaluator_port.py          # A3 port; A5 implements, never edits
apps/api/app/services/precheck_provider.py               # A2 port; A5 implements, never edits
apps/api/app/services/session_capture.py, precheck_orchestrator.py  # A2
apps/api/app/services/webhook_*.py         # A3
apps/api/app/api/**                        # A2/A3 routes; A5 adds no routes
data/fixtures/contracts/**, data/fixtures/webhooks/**   # A0
apps/web/**, data/scenarios/**             # A7/A8/A9
docker-compose.yml, Makefile, pyproject.toml  # A0; A5 needs no dependency changes
```

### Explicit non-goals

- No changes to any A0 Pydantic model, enum, fixture or the OpenAPI shape of existing routes.
- No new routes, no `X-` headers, no middleware.
- No Redis access other than through the injected A4 `FeatureExtractor`; A5 never opens its own Redis connection.
- No Razorpay HTTP calls and no worker: A5 only writes the `ActionRequest` row and the outbox message through A1; A6 executes them.
- No ML scoring, no LLM calls, no UI.
- No edits to A2/A3 service modules, repositories or persistence — the composition happens only via constructor injection and the `main.py` state block.

## 4. Rule configuration and catalogue (`config.py`, `catalogue.py`)

### 4.1 Configuration objects

Plain frozen dataclasses (not Pydantic — predicates are code, not data). Thresholds live **only** here; no frontend or route code may contain a score boundary.

```python
@dataclass(frozen=True)
class DecisionThresholds:
    challenge_min: int = 30    # PRECHECK: score >= 30 -> CHALLENGE, else ALLOW
    block_min: int = 70        # POST_CONFIRMATION / REJECTION_AUDIT: score >= 70 -> BLOCK, else ALLOW

@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str               # ^[a-z][a-z0-9_]{2,63}$ (A0 RuleEvaluation pattern)
    points: int                # 0..100
    stages: frozenset[DecisionStage]   # stages where the rule may fire
    predicate: Callable[[FeatureSnapshot], bool]   # must be None-safe
    reason_code: str           # UPPER_SNAKE, 1..80 chars
    reason_template: str       # str.format template over snapshot fields, <= 240 chars when rendered

@dataclass(frozen=True)
class RuleSetConfig:
    thresholds: DecisionThresholds
    rules: tuple[RuleDefinition, ...]
    engine_version: str = "rules-v1"

    @classmethod
    def default(cls) -> "RuleSetConfig": ...   # catalogue.DEFAULT_RULES + default thresholds
```

### 4.2 The rules-v1 catalogue (exact)

Predicates are None-safe: if any referenced feature is `None`, the rule does **not** trigger. `velocity` conditions read the record-then-count values (a first-ever event has velocity 1).

| # | rule_id | points | stages | triggers when | reason_code |
|---|---|---|---|---|---|
| 1 | `velocity_ip_burst` | 25 | all | `ip_velocity_5m >= 5` or `ip_velocity_1h >= 20` | `IP_VELOCITY_BURST` |
| 2 | `velocity_device_burst` | 25 | all | `device_velocity_5m >= 5` or `device_velocity_1h >= 20` | `DEVICE_VELOCITY_BURST` |
| 3 | `velocity_customer_burst` | 15 | all | `customer_velocity_1h >= 10` | `CUSTOMER_VELOCITY_BURST` |
| 4 | `velocity_vpa_burst` | 15 | all | `vpa_velocity_1h >= 10` | `VPA_VELOCITY_BURST` |
| 5 | `new_device_for_established_customer` | 20 | all | `is_new_device_for_customer is True` | `NEW_DEVICE_FOR_CUSTOMER` |
| 6 | `implausible_flow_duration` | 20 | all | `flow_duration_seconds <= 3` | `IMPLAUSIBLE_FLOW_DURATION` |
| 7 | `network_anonymity` | 20 | all | `network_vpn_or_proxy is True` or `network_reputation_score <= 30` | `NETWORK_ANONYMITY` |
| 8 | `npci_risk_rejection` | 100 | `REJECTION_AUDIT` only | `npci_risk_flag is True` | `NPCI_RISK_REJECTION` |
| 9 | `payer_bank_compliance_rejection` | 60 | `REJECTION_AUDIT` only | `payer_bank_or_compliance_flag is True` | `PAYER_BANK_COMPLIANCE_REJECTION` |
| 10 | `demo_shared_merchant_velocity` | 30 | all | `shared_demo_merchant_count_1h >= 3` | `DEMO_SHARED_MERCHANT_VELOCITY` |
| 11 | `unusual_mandate_terms` | 10 | all | `max_amount_paise >= 5_000_000` or `expiry_days_from_registration >= 1095` | `UNUSUAL_MANDATE_TERMS` |

"all" = `{PRECHECK, POST_CONFIRMATION, REJECTION_AUDIT}`. This catalogue covers the architecture's required rule families: velocity, new device, impossible flow duration, network reputation, NPCI/payer-bank rejection wording, unusual mandate terms and demo shared-merchant velocity. (Bot-like user agent is absent because A4 v1 cannot classify it from a hash — the catalogue is where a future rule slots in.)

### 4.3 Reason templates

Each template uses only feature values (counts, booleans, seconds, paise) — never hashes, IDs, VPAs or IPs. Exact v1 templates:

```text
IP_VELOCITY_BURST:            "IP made {ip_velocity_5m} registrations in 5 minutes ({ip_velocity_1h} in 1 hour)."
DEVICE_VELOCITY_BURST:        "Device made {device_velocity_5m} registrations in 5 minutes ({device_velocity_1h} in 1 hour)."
CUSTOMER_VELOCITY_BURST:      "Customer made {customer_velocity_1h} registrations in 1 hour."
VPA_VELOCITY_BURST:           "Destination VPA handle received {vpa_velocity_1h} registrations in 1 hour."
NEW_DEVICE_FOR_CUSTOMER:      "Registration came from a device not seen for this customer in 24 hours."
IMPLAUSIBLE_FLOW_DURATION:    "Checkout completed in only {flow_duration_seconds} seconds."
NETWORK_ANONYMITY:            "Network signals indicated VPN/proxy or a poor reputation score."
NPCI_RISK_REJECTION:          "Mandate was rejected with NPCI risk wording: {npci_flag_present}."
PAYER_BANK_COMPLIANCE_REJECTION: "Mandate was rejected with payer-bank or compliance wording."
DEMO_SHARED_MERCHANT_VELOCITY:"Device registered at {shared_demo_merchant_count_1h} simulated shared test merchants in 1 hour (demo data)."
UNUSUAL_MANDATE_TERMS:        "Mandate amount or expiry is unusual for registration risk."
```

`{npci_flag_present}` renders as the literal string `"flagged"`. The renderer truncates to 240 characters (A0 limit) and never raises on missing values — templates must only reference features the rule's own predicate required.

## 5. Rule engine and renderer (`engine.py`, `renderer.py`)

### 5.1 Engine

```python
@dataclass(frozen=True)
class RuleEngineResult:
    score: int
    decision: Decision
    rule_evaluations: tuple[RuleEvaluation, ...]

class RuleEngine:
    def __init__(self, config: RuleSetConfig | None = None) -> None:
        self._config = config or RuleSetConfig.default()

    def evaluate(self, snapshot: FeatureSnapshot, stage: DecisionStage) -> RuleEngineResult: ...
```

Exact algorithm, in order:

1. For each `RuleDefinition` in catalogue order: if `stage not in rule.stages`, emit `RuleEvaluation(rule_id, triggered=False, points=0, reason_code=None, reason_text=None)` and continue.
2. Otherwise evaluate `predicate(snapshot)`. On `True`: `points=rule.points`, `reason_code=rule.reason_code`, `reason_text=renderer.render(rule, snapshot)`. On `False`: the untriggered shape from step 1.
3. `score = max(0, min(100, sum(evaluation.points)))` — explicit clamping.
4. Decision by stage (Section 1 table) using `config.thresholds`:
   - `PRECHECK`: `CHALLENGE if score >= challenge_min else ALLOW`
   - `POST_CONFIRMATION`, `REJECTION_AUDIT`: `BLOCK if score >= block_min else ALLOW`
5. Return the `RuleEngineResult`.

Rules:

- The engine is synchronous, pure and free of I/O, clock access and randomness. Same input + same config ⇒ byte-identical result.
- `rule_evaluations` always contains every catalogue rule in catalogue order (satisfies the A0 `min_length=1` and gives the dashboard full transparency).
- Points are reported per rule; the clamped total is the only score. Individual rule points stay `0..100`.
- The engine never raises on any valid `FeatureSnapshot` (predicates are None-safe).

### 5.2 Renderer

```python
def render_reason(rule: RuleDefinition, snapshot: FeatureSnapshot) -> str:
    """Fill rule.reason_template from snapshot fields; truncate to 240 chars; never raise."""
```

- Fill via `str.format_map` with a mapping that returns the literal `"unavailable"` for missing fields.
- Truncate to the A0 limit of 240 characters.
- Defensive guard: if the rendered text would contain `hmac-sha256:` or `sha256:`, replace the offending substring with `"[redacted]"` (this can only happen through a template bug and must fail loudly in tests).
- No other module builds `reason_text`.

## 6. Evaluation services (`risk_evaluation.py`)

### 6.1 Interfaces

```python
class EvaluationInvariantError(RuntimeError):
    """The engine/extractor produced an assessment violating A0 stage invariants. Triggers rollback."""

def _default_repositories(db_session: AsyncSession) -> ...   # used unless overridden for tests

class MandateEvaluationService:      # implements the A3 MandateEventEvaluator protocol
    def __init__(
        self,
        extractor: FeatureExtractor,
        engine: RuleEngine,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        uuid_factory: Callable[[], UUID] = uuid4,
        snapshot_repository: Callable[[AsyncSession], FeatureSnapshotRepository] = FeatureSnapshotRepository,
        assessment_repository: Callable[[AsyncSession], RiskAssessmentRepository] = RiskAssessmentRepository,
        action_repository: Callable[[AsyncSession], ActionRepository] = ActionRepository,
        audit_repository: Callable[[AsyncSession], AuditRepository] = AuditRepository,
        session_repository: Callable[[AsyncSession], RiskSessionRepository] = RiskSessionRepository,
    ) -> None: ...

    async def evaluate(self, db_session: AsyncSession, event: MandateWebhookEvent) -> None: ...

class PrecheckEvaluationService:     # implements the A2 PrecheckEvaluator protocol
    def __init__(self, extractor, engine, clock, uuid_factory, snapshot_repository, assessment_repository, audit_repository) -> None: ...
    async def assess(self, db_session: AsyncSession, risk_session: RiskSession) -> RiskAssessment: ...
```

Repository callables (class references by default, fakes in tests) let unit tests inject fake repositories without touching A1. The services never commit; the caller (A2/A3 route) owns the transaction.

### 6.2 `MandateEvaluationService.evaluate` — exact steps

Stage mapping: `token.confirmed` -> `POST_CONFIRMATION`; `token.rejected`/`token.cancelled` -> `REJECTION_AUDIT`.

1. Start the latency timer (`time.perf_counter()`).
2. Load the correlated session: if `event.risk_session_id` is not `None`, `risk_session = await session_repository(db_session).get(event.risk_session_id)`; else `None`. A missing row is treated like no correlation (A3 already handles the FK safely).
3. `snapshot = await extractor.extract_for_event(event, risk_session, now=clock())`; persist with `snapshot_repository(db_session).create(snapshot)`. An A1 `IntegrityError` here means the same event was evaluated twice — let it propagate (rollback, HTTP 500) rather than swallowing it.
4. `result = engine.evaluate(snapshot, stage)`.
5. Build the A0 `RiskAssessment`:
   - `assessment_id=uuid_factory()`, `risk_session_id=event.risk_session_id`, `mandate_event_id=event.mandate_event_id`, `feature_snapshot_id=snapshot.feature_snapshot_id`, `stage=stage`, `score=result.score`, `decision=result.decision`, `engine_version="rules-v1"`, `rule_evaluations=list(result.rule_evaluations)`, `evaluation_latency_ms=clamp(int((perf_counter()-start)*1000), 0, 60000)`, `assessed_at=clock()`.
6. Validate stage invariants (A0 rules): `PRECHECK` requires `risk_session_id`; `POST_CONFIRMATION`/`REJECTION_AUDIT` require `mandate_event_id`; the decision is legal for the stage (no `CHALLENGE` outside `PRECHECK`). On violation raise `EvaluationInvariantError` — never persist a broken assessment.
7. Persist with `assessment_repository(db_session).create(assessment)`.
8. Append audit event `risk_assessment.completed`: aggregate `risk_assessment` / `assessment.assessment_id`, actor `SYSTEM` / `risk_engine`, `occurred_at=clock()`, payload exactly:

```json
{
  "assessment_id": "<uuid>",
  "stage": "POST_CONFIRMATION",
  "decision": "BLOCK",
  "score": 100,
  "engine_version": "rules-v1",
  "evaluation_latency_ms": 12,
  "triggered_rule_count": 3
}
```

No feature values, hashes or reason texts in the audit payload — the assessment row already carries them.

9. Queue the revoke **only** when `stage == POST_CONFIRMATION and decision == BLOCK` (Section 6.4). Never for `REJECTION_AUDIT`, never for `ALLOW`.
10. Return `None` (the A3 protocol contract).

### 6.3 `PrecheckEvaluationService.assess` — exact steps

1. Start the latency timer.
2. `snapshot = await extractor.extract_for_session(risk_session, now=clock())`; persist it.
3. `result = engine.evaluate(snapshot, DecisionStage.PRECHECK)`; build the `RiskAssessment` with `stage=PRECHECK`, `risk_session_id=risk_session.risk_session_id`, `mandate_event_id=None`, `feature_snapshot_id`, latency and `assessed_at` as above.
4. Validate invariants (step 6 above); persist; append the same `risk_assessment.completed` audit payload (with `"stage": "PRECHECK"`).
5. Return the persisted assessment. A2's orchestrator consumes the session and appends its own `risk_session.precheck_completed` audit — A5 must not touch the session row.

### 6.4 Revoke queueing (confirmed BLOCK only)

Build the A0 `ActionRequest`:

```python
ActionRequest(
    action_request_id=uuid_factory(),
    assessment_id=assessment.assessment_id,
    mandate_event_id=event.mandate_event_id,
    action_type=ActionType.TOKEN_REVOKE,
    token_id=event.token_id,
    idempotency_key=f"revoke-{event.mandate_event_id}",   # matches ^[a-z0-9_-]{16,128}$
    status=ActionStatus.QUEUED,
    requested_at=clock(),
)
```

Persist atomically through A1 with the outbox message:

```python
outbox_payload = {
    "action_request_id": str(request.action_request_id),
    "mandate_event_id": str(event.mandate_event_id),
    "token_id": event.token_id,
    "idempotency_key": request.idempotency_key,
    "action_type": "TOKEN_REVOKE",
    "requested_at": request.requested_at.isoformat().replace("+00:00", "Z"),
}
await action_repository(db_session).create_request(request, outbox_payload)
```

- The outbox row uses `aggregate_type="action_request"`, `aggregate_id=request.action_request_id`, `message_type="token_revoke_requested"`, `idempotency_key` equal to the request key, `available_at=request.requested_at`. A6 consumes it later; A5 makes no HTTP call.
- A1's unique `(mandate_event_id, action_type)` constraint guarantees at most one revoke per event even under races; a second attempt raises `IntegrityError` and rolls back the whole webhook transaction.
- Then append audit event `action_request.created`: aggregate `action_request` / `request.action_request_id`, actor `SYSTEM` / `risk_engine`, payload exactly:

```json
{
  "action_type": "TOKEN_REVOKE",
  "token_id": "token_demo123",
  "idempotency_key": "revoke-<mandate_event_id>",
  "assessment_id": "<uuid>"
}
```

### 6.5 Error-handling contract

| Situation | Behaviour |
|---|---|
| A4 raises `FeatureStoreUnavailable` | Propagates. A3/A2 roll back and the route maps it (A3: HTTP 500; A2: 503 per its own spec). No partial rows. |
| Duplicate snapshot/assessment `IntegrityError` | Propagates; whole transaction rolls back. |
| `EvaluationInvariantError` | Propagates; nothing persisted. |
| Any other unexpected exception | Propagates. The services never catch-and-continue. |

The rule engine itself never raises (Section 5.1); all failure surfaces come from persistence or the extractor.

## 7. Application wiring (`main.py`)

A5 replaces the two placeholder evaluators and registers A4's extractor. The complete A5 block in `create_app()`:

```python
store = RedisFeatureStore.from_settings()
reputation = UnavailableNetworkReputationProvider()
extractor = FeatureExtractor(store=store, reputation=reputation)
engine = RuleEngine()  # rules-v1 defaults

app.state.feature_extractor = extractor                    # A4 seam
app.state.network_reputation_provider = reputation         # A4 seam
app.state.precheck_evaluator = PrecheckEvaluationService(  # replaces A2's UnavailablePrecheckEvaluator
    extractor=extractor, engine=engine,
)
app.state.mandate_event_evaluator = MandateEvaluationService(  # replaces A3's UnavailableMandateEvaluator
    extractor=extractor, engine=engine,
)
```

This is the entire runtime integration: the webhook path (A3) and pre-check path (A2) now produce real persisted assessments. If the A5 services raise, A2/A3's documented error mapping handles it — no route edits. Tests override any `app.state` attribute directly.

## 8. Golden fixtures and tests

### 8.1 Golden fixture bundle (`data/fixtures/rules/`)

Each file is a JSON object: `{"description": str, "stage": "PRECHECK"|"POST_CONFIRMATION"|"REJECTION_AUDIT", "snapshot": <FeatureSnapshot JSON>, "expected": {"score": int, "decision": str, "triggered_rule_ids": [str]}}`. Snapshots use fixed UUIDs, `2026-01-15T10:00:00Z` timestamps, `hmac-sha256:` + `"ab"*32` fake hashes and no real data. `manifest.json` maps filename -> `{"valid": true}` and is the test parameter source.

```text
manifest.json
allow_clean.json                    # all features None or low; score 0, ALLOW
challenge_velocity.json             # device_velocity_5m=7 -> +25; POST score 25 vs PRECHECK CHALLENGE (two cases or two files)
block_bot_burst.json                # device burst + flow 2s + demo counter 3 -> 75, BLOCK
block_npci_rejection.json           # npci_risk_flag=true, REJECTION_AUDIT -> 100, BLOCK
payer_bank_rejection.json           # payer_bank_or_compliance_flag=true -> 60, ALLOW (post) / CHALLENGE (precheck)
clamped_over_100.json               # multiple rules exceeding 100 -> score exactly 100
threshold_29.json                   # single-rule config, 29 points
threshold_30.json                   # 30 points
threshold_69.json                   # 69 points
threshold_70.json                   # 70 points
```

The four `threshold_*` files use a snapshot that triggers a single synthetic rule; the test pairs them with a one-rule `RuleSetConfig` whose points equal the filename value, then asserts the decision for **all three stages** from one file. This is the architecture's 29/30/69/70 edge proof.

### 8.2 Unit tests — `tests/unit/test_rule_engine.py` (pure, no I/O)

```text
test_clean_snapshot_scores_zero_and_allows_everywhere
test_every_catalogue_rule_triggers_on_its_documented_condition
test_every_rule_is_none_safe_and_never_raises
test_missing_features_never_trigger_rules
test_score_is_clamped_to_100
test_rule_evaluations_preserve_catalogue_order
test_untriggered_rules_report_zero_points_and_no_reason
test_triggered_rules_report_points_reason_code_and_text
test_threshold_29_allows_in_every_stage
test_threshold_30_challenges_in_precheck_only
test_threshold_69_challenges_in_precheck_and_allows_post_confirmation
test_threshold_70_blocks_post_confirmation_and_rejection_audit
test_challenge_never_appears_outside_precheck
test_npci_rule_ignored_outside_rejection_audit
test_payer_bank_rule_ignored_outside_rejection_audit
test_reason_text_never_exceeds_240_chars
test_reason_text_never_contains_hashes
test_engine_version_is_rules_v1
test_same_inputs_give_identical_results
```

`test_threshold_*` tests use the synthetic single-rule config; `test_every_catalogue_rule_triggers_on_its_documented_condition` builds one minimal snapshot per catalogue entry with exactly the triggering values from the Section 4.2 table.

### 8.3 Unit tests — `tests/unit/test_rule_golden_fixtures.py`

```text
test_manifest_lists_every_fixture
test_every_golden_fixture_matches_expected_score_decision_and_rules
test_every_golden_snapshot_round_trips_through_a0_contract
```

Load via the manifest, `FeatureSnapshot.model_validate`, run `RuleEngine(config)` (default config except the threshold files), and compare score, decision and the ordered list of triggered `rule_id`s exactly.

### 8.4 Unit tests — `tests/unit/test_risk_evaluation_services.py`

Fake repositories (in-memory), a fake extractor returning a fixed snapshot, the real `RuleEngine`, a fixed clock and UUID sequence. Same fake pattern as A2's `test_precheck_orchestrator.py`.

```text
test_mandate_evaluation_persists_snapshot_assessment_and_one_audit_event
test_blocked_confirmed_event_queues_exactly_one_revoke_request_and_outbox_message
test_allowed_confirmed_event_queues_no_revoke
test_rejected_block_event_records_assessment_but_never_queues_a_revoke
test_cancelled_event_is_treated_as_rejection_audit
test_uncorrelated_event_produces_assessment_with_null_session_link
test_precheck_assessment_has_stage_precheck_and_session_link
test_precheck_service_never_touches_the_session_row
test_audit_payloads_contain_fixed_keys_only
test_evaluation_latency_ms_is_present_and_bounded
test_invariant_violation_raises_and_persists_nothing
test_duplicate_snapshot_integrity_error_propagates
```

### 8.5 Integration tests — `tests/integration/test_evaluation_against_persistence.py`

Use A1's `TEST_POSTGRES_URL` setup and the A4 Redis test helper. Construct the real services with real repositories and the real Redis-backed `FeatureExtractor`.

```text
test_full_webhook_evaluation_persists_and_reloads_assessment_with_rule_rows
test_revoke_request_and_outbox_commit_atomically
test_rejected_event_creates_no_action_rows
test_precheck_assessment_links_session_and_snapshot
test_rule_evaluation_rows_preserve_position_and_reasons
```

`test_full_webhook_evaluation_persists_and_reloads_assessment_with_rule_rows`: create a session and event via A1 repositories, run `MandateEvaluationService.evaluate`, then reload via `RiskAssessmentRepository.get_for_event` and assert score/decision/rule rows and that `verify_aggregate` reports a valid audit chain for the assessment and action aggregates.

### 8.6 Manual acceptance (before A6/A9 exist)

1. Start the stack; create a session, patch telemetry, call pre-check — a real `RiskAssessment` now replaces the previous 503 behaviour, and a second call replays via A2's `X-Idempotent-Replay`.
2. Sign and POST the confirmed webhook fixture with elevated Redis velocity (repeat with fresh event IDs) — the response shows `"evaluation":"completed"` and the dashboard-visible assessment reaches `CHALLENGE`/`BLOCK` according to the catalogue.
3. Replay the NPCI-risk rejection fixture — a score-100 `REJECTION_AUDIT` assessment with `NPCI_RISK_REJECTION` reason exists, and **no** `action_requests` or `outbox_messages` rows.
4. Inspect `audit_events` for the assessment aggregate: hash chain verifies via A1's `verify_aggregate`.

### 8.7 Verification commands

```powershell
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
$env:TEST_REDIS_URL = "redis://localhost:6379/15"
uv --directory apps/api run pytest tests/unit/test_rule_engine.py tests/unit/test_rule_golden_fixtures.py tests/unit/test_risk_evaluation_services.py -q
uv --directory apps/api run pytest tests/integration/test_evaluation_against_persistence.py -q
uv --directory apps/api run ruff check app/domain/rules app/services/risk_evaluation.py tests/unit/test_rule_engine.py tests/unit/test_rule_golden_fixtures.py tests/unit/test_risk_evaluation_services.py
uv --directory apps/api run ruff format --check app/domain/rules app/services/risk_evaluation.py
uv --directory apps/api run mypy app/domain/rules app/services/risk_evaluation.py
docker compose down
```

Do not mark the package done until all commands pass. Performance context: the engine adds microseconds; the 200 ms webhook budget is dominated by Redis pipelining and two inserts — keep the service free of extra I/O.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A5 use |
|---|---|---|
| A0 | `RiskAssessment`, `RuleEvaluation`, `ActionRequest`, enums, decision-stage invariants | Exact public shapes; import only, never edit. |
| A1 | `transaction()`, snapshot/assessment/action/audit/session repositories, outbox rules | All persistence, atomic request+outbox, audit chain. |
| A4 | `FeatureExtractor`, `RedisFeatureStore`, `UnavailableNetworkReputationProvider` | Extraction behind constructor injection; A5 never opens Redis itself. |
| A2 | `PrecheckEvaluator` protocol | `PrecheckEvaluationService` implements it; A2's orchestrator keeps session lifecycle. |
| A3 | `MandateEventEvaluator` protocol | `MandateEvaluationService` implements it; A3 keeps ingestion, idempotency and correlation. |

### Outputs produced

| Consumer | Receives from A5 | Integration rule |
|---|---|---|
| A6 revoke worker | outbox message `token_revoke_requested` + `ActionRequest` rows | Claims with `SKIP LOCKED`; owns all Razorpay HTTP calls and retry policy. A5 creates no jobs beyond the outbox row. |
| A8 dashboard | persisted assessments, rule rows, reasons via A1 | Displays reason codes/text and decisions; thresholds remain server-side config. |
| A9 simulator | catalogue thresholds + golden fixtures | Designs borderline cases against the documented points; may not change them. |
| A10 CI/security | pure engine tests + redaction tests | Verifies no hashes/PII in reason text and audit payloads. |
| A11 ML shadow model | `FeatureSnapshot` + `RiskAssessment` rows via A1 | Writes `model_score` beside the rule decision; never changes enforcement. |

### Merge order

1. A0 (`contracts-v1`) and A1 merge first; A4 must be merged (or its interface stubbed) before A5's integration tests run.
2. A5 rebases on A4 and merges after it; A2/A3 may merge before or after — A5 touches neither.
3. A5's only runtime integration is the Section 7 `main.py` block; conflicts there are resolved in favour of this document's exact block.
4. A6, A8 and A9 integrate against A5's persisted outputs without modification.

### Pull-request handoff checklist

State in the A5 PR: the rule catalogue table and engine version; the golden-fixture results (score/decision per file, including 29/30/69/70 and clamping); the stage-scope invariants with test names; the exact audit payload schemas; the atomic revoke+outbox proof; results for all commands in Section 8.7; confirmation that only owned paths plus the Section 7 `main.py` block changed; and known deferred work (bot-like-UA rule pending A2 capture-time classification, real reputation provider, A6 worker execution, A11 shadow model).

## 10. Failure conditions

The A5 work is unsafe or incomplete if any of these occur:

- A decision, score or threshold is hardcoded outside `domain/rules/config.py`, duplicated in routes/frontend, or derived from anything other than the snapshot and config.
- The engine is non-deterministic: clock, randomness, I/O or environment-dependent results.
- `CHALLENGE` is ever produced outside `PRECHECK`, or a revoke request/outbox row is created for a `REJECTION_AUDIT` or `ALLOW` outcome.
- A missing (`None`) feature triggers a rule, invents a value, or is silently treated as a passing condition.
- Score is not clamped to 0..100, rule evaluations lose catalogue order, or reasons contain hashes, raw identifiers or template placeholders.
- Snapshot, assessment, audit and outbox rows are not written in the caller's single transaction, or the service commits/rolls back on its own.
- A5 modifies A0 contracts, A1/A2/A3/A4-owned files, adds routes, calls Razorpay, or performs worker/revoke execution.
- Golden fixtures disagree with the catalogue, or a threshold-edge test is missing.
