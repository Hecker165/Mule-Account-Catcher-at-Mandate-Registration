# A4 — Redis feature store and deterministic feature extraction: implementation specification

## 0. Agent instruction

You are the **A4 feature store and extraction agent**. Read these documents in order before writing code:

1. `PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md`
2. `docs/work-packages/A0_FOUNDATION_AND_CONTRACTS.md`
3. `docs/work-packages/A1_PERSISTENCE_AND_AUDIT.md`
4. `docs/work-packages/A2_SESSION_CAPTURE_AND_PRECHECK_API.md`
5. `docs/work-packages/A3_RAZORPAY_WEBHOOK_GATEWAY.md`
6. `docs/contracts.md`

Implement exactly this work package. You build the Redis windowed-counter store and the deterministic extractor that turns a webhook mandate event and/or a pre-registration risk session into the frozen A0 `FeatureSnapshot` contract.

Do **not** implement rules, points, thresholds or scoring (A5), do not persist snapshots or assessments from application code (A5 orchestrates persistence), do not touch webhook or session routes, and do not build UI, ML or LLM features. You must consume A0 contracts and A2/A3 conventions unchanged.

Redis is a velocity and short-lived-state store only. It is never the audit store, and no key or value may contain a raw VPA, raw IP, raw device fingerprint, customer identifier or API secret — only HMAC hashes, merchant namespaces and synthetic demo identifiers.

## 1. Why this package exists

Feature extraction is the bridge between stored events and the rule engine. Both decision paths consume it:

```text
Path A (post-confirmation, A3 -> A5):
  webhook event stored (A3/A1)
    -> A5 evaluator calls FeatureExtractor.extract_for_event(event, risk_session, now)
    -> Redis velocity windows updated and counted
    -> FeatureSnapshot contract returned (unpersisted)
    -> A5 persists snapshot + assessment + audit/outbox via A1

Path B (pre-registration, A2 -> A5):
  READY risk session (A2/A1)
    -> A5 PrecheckEvaluator calls FeatureExtractor.extract_for_session(risk_session, now)
    -> same Redis windows and contract shape (vpa/demo fields unavailable)
    -> A5 persists snapshot + PRECHECK assessment
```

Runtime join (from the architecture plan):

```text
Razorpay webhook (A3) -> stored event (A1) -> features (A4) -> score/reasons (A5)
                          -> assessment + audit/outbox (A1) -> revoke worker (A6)
checkout telemetry (A7) -> RiskSession (A2/A1) -^ (session context feeds both paths)
```

Why Redis: sliding-window velocity counters with exact expiry, isolated per merchant and per signal, plus short-lived state (known devices, demo shared-merchant sets). Redis must never be required to answer health endpoints and never outlives its TTLs.

## 2. Objective and definition of done

Build a small, fully deterministic feature layer: every output is a pure function of (stored contract inputs, Redis state, injected clock, injected UUID factory).

Definition of done:

- With a frozen clock, a member recorded at time `T` is still counted at `T + window - 1ms` and is excluded exactly at `T + window` for 5-minute and 1-hour windows.
- Two distinct events recorded in the same second both count (no overwrites); recording the same member twice counts once.
- Counter keys are isolated per merchant namespace and per signal; demo keys are separate; every written key carries a TTL.
- The extractor emits `FeatureSnapshot` objects that validate against the A0 contract for: a fully correlated confirmed event, an uncorrelated event, a pre-check session, and a demo event with the shared-merchant counter.
- `shared_demo_merchant_count_1h` is only ever set with `is_demo_simulation=True` and a `DEMO_SIMULATED` source entry; non-demo traffic never touches demo keys.
- Every unavailable input produces a `None` feature with a `MISSING` source entry — never a zero, never a guess.
- A Redis failure raises a named `FeatureStoreUnavailable` error; there is no silent-zeros or permissive fallback.
- An integration test proves an extracted snapshot round-trips through A1 persistence.
- No file outside the owned paths is modified except the single additive dev dependency in Section 3.

## 3. Scope and exact file ownership

### You own

```text
apps/api/app/services/features/__init__.py
apps/api/app/services/features/keys.py
apps/api/app/services/features/redis_store.py
apps/api/app/services/features/reputation.py
apps/api/app/services/features/extractor.py
apps/api/tests/unit/test_feature_windows.py
apps/api/tests/unit/test_feature_extractor.py
apps/api/tests/integration/test_feature_store_redis.py
apps/api/tests/integration/test_feature_extractor_persistence.py
apps/api/tests/helpers/redis_test_client.py
```

### The one permitted edit outside your paths

`apps/api/pyproject.toml`: add `"fakeredis>=2.24,<3.0"` to the existing `[dependency-groups] dev` list. This is additive and dev-only; it is required for hermetic frozen-clock unit tests. Do not touch any other dependency, the runtime dependency list, or any other section. Run `uv lock` after the edit and commit the updated `uv.lock`.

### You may read but must not modify

```text
apps/api/app/contracts/**            # frozen A0 contracts (FeatureSnapshot, RiskSession, MandateWebhookEvent, FeatureSource)
apps/api/app/core/settings.py        # A0 settings owner (redis_url)
apps/api/app/persistence/**          # A1 transaction/session implementation (integration tests only)
apps/api/app/repositories/**         # A1 repositories (FeatureSnapshotRepository, RiskSessionRepository in tests only)
apps/api/app/services/session_capture.py, privacy_hashing.py  # A2 hash conventions
apps/api/app/services/webhook_*.py, mandate_evaluator_port.py # A3 event contract use and evaluator seam
apps/api/app/api/**                  # A2/A3 routes; A4 adds no routes
apps/api/app/main.py                 # A5 wires app.state; A4 does not touch it
apps/api/pyproject.toml              # only the dev-group exception above
apps/web/**, data/scenarios/**       # A7/A8/A9
docker-compose.yml, Makefile         # A0
```

### Explicit non-goals

- No rules, points, thresholds, decisions or reason codes: that is entirely A5. A4 never produces a `RiskAssessment`.
- No persistence from application code: the extractor returns an unpersisted `FeatureSnapshot`; A5 persists it through A1's `FeatureSnapshotRepository` in its own transaction. Only A4's integration tests may call repositories.
- No edits to `main.py`, `app.state`, routes, `docker-compose.yml` or the `redis` Compose service definition.
- No new runtime dependencies (the `redis` async client is already declared by A0).
- No SCAN/KEYS pattern queries, no persistent structures without TTL, no pub/sub, no Lua scripts beyond simple pipelining.
- No raw identifier ever reaches a Redis key, value, log line, metric label or test fixture.

## 4. Redis feature store (`redis_store.py`, `keys.py`)

### 4.1 Key catalogue

`keys.py` builds every key; nothing else formats keys. All hash arguments must match the A0 `HashValue` pattern (`hmac-sha256:` + 64 lowercase hex) and every namespace must match `^[a-z][a-z0-9_-]{2,63}$`; violations raise `ValueError` with a fixed safe message (never the offending value).

```python
SIGNALS = frozenset({"ip", "device", "customer", "vpa"})
WINDOWS = {"5m": 300, "1h": 3600}          # label -> window seconds
KNOWN_DEVICES_TTL_SECONDS = 86_400         # 24h
DEMO_SHARED_TTL_SECONDS = 3_660            # 1h window + 60s buffer

def velocity_key(key_prefix: str, merchant_namespace: str, signal: str, window_label: str, hash_value: str) -> str
    # "{prefix}:vel:{merchant_namespace}:{signal}:{window_label}:{hash_value}"
def known_devices_key(key_prefix: str, customer_hash: str) -> str
    # "{prefix}:dev:{customer_hash}"
def demo_shared_merchants_key(key_prefix: str, device_hash: str) -> str
    # "{prefix}:demoshared:{device_hash}"
```

Example: `mg:v1:vel:demo_merchant:device:5m:hmac-sha256:ab12...`. Keys therefore contain only the namespace, a signal word, a window label and an HMAC hash.

### 4.2 Store interface

```python
class FeatureStoreUnavailable(RuntimeError):
    """Raised when Redis cannot be reached or a command fails. Never carries connection details."""

class RedisFeatureStore:
    def __init__(self, client: Redis, key_prefix: str = "mg:v1") -> None: ...
    @classmethod
    def from_settings(cls) -> "RedisFeatureStore":
        """redis.asyncio.Redis.from_url(get_settings().redis_url); lazy; no connection at import."""

    async def record_and_count(self, *, key: str, member: str, window_seconds: int, now: datetime) -> int: ...
    async def check_then_add(self, *, key: str, member: str, ttl_seconds: int, now: datetime) -> bool: ...
    async def record_demo_merchant(self, *, device_hash: str, merchant_namespace: str, now: datetime) -> None: ...
    async def count_demo_merchants(self, *, device_hash: str, now: datetime) -> int: ...
    async def aclose(self) -> None: ...
```

### 4.3 Sliding-window algorithm (`record_and_count`)

One `pipeline(transaction=True)` per call, with epoch-millisecond scores:

```python
score_ms = int(now.timestamp() * 1000)          # now must be timezone-aware; naive -> ValueError
cutoff_ms = score_ms - window_seconds * 1000
ZREMRANGEBYSCORE key "-inf" cutoff_ms           # inclusive removal: expires exactly at the window edge
ZADD      key score_ms member
EXPIRE    key (window_seconds + 60)             # cleanup buffer, set on every call
ZCARD     key
```

Return the final `ZCARD` value. Rules:

- `member` is a caller-supplied unique string (`str(mandate_event_id)` or `str(risk_session_id)`). Unique members guarantee that distinct events in the same second never overwrite each other, and re-recording the same member is a no-op count.
- A member recorded at `T` is counted for every `now` in `(T, T + window]` at count time and removed once `now >= T + window`, because `ZREMRANGEBYSCORE` removes `score <= now - window`. This is the exact-expiry behaviour the architecture demands.
- Recording happens **before** counting (record-then-count), so the returned velocity includes the current event. Document this in the module docstring; A5 must know that a first-ever event yields velocity `1`, not `0`.
- Wrap every `redis.RedisError` (and any other `Exception` from the round trip) into `FeatureStoreUnavailable`. Never include the URL, host or raw error text beyond the exception class name.

### 4.4 Known devices (`check_then_add`)

```python
exists = SISMEMBER key member
if not exists: SADD key member; EXPIRE key ttl_seconds
return not exists        # True means "device is new for this key"
```

Used with `known_devices_key(customer_hash)` and `member = device_fingerprint_hash`, TTL 24h, so `is_new_device_for_customer` means "device hash not seen for this customer hash in 24h". The read-then-write is deliberately not atomic: two simultaneous first-seen events may both report "new", which is the conservative direction for a risk system. Document this in the module docstring.

### 4.5 Demo shared-merchant sets

- `record_demo_merchant`: `ZADD {prefix}:demoshared:{device_hash} score=epoch_ms member=merchant_namespace`, then `EXPIRE 3660`.
- `count_demo_merchants`: `ZREMRANGEBYSCORE key "-inf" (now_ms - 3_600_000)` then `ZCARD` — the number of **distinct merchant namespaces** that recorded the same device hash within the last hour (including the current one).
- These methods are called only for demo-simulated traffic (Section 6). Production traffic must never create or read `demoshared` keys.

### 4.6 Hygiene rules

- Every written key gets a TTL on every write; no key may exist without one (a test asserts this).
- No key/value ever contains a raw identifier; only HMAC hashes, namespaces, signal words and UUID members.
- `aclose()` closes the injected client; the store never creates background work.

## 5. Feature extraction (`extractor.py`)

### 5.1 Interface

```python
CORRELATION_FALLBACK_NAMESPACE = "uncorrelated"   # only for webhook events with no correlated session

class FeatureExtractor:
    def __init__(
        self,
        store: RedisFeatureStore,
        reputation: NetworkReputationProvider | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None: ...

    async def extract_for_event(
        self, event: MandateWebhookEvent, risk_session: RiskSession | None, now: datetime
    ) -> FeatureSnapshot: ...

    async def extract_for_session(
        self, risk_session: RiskSession, now: datetime
    ) -> FeatureSnapshot: ...
```

Rules common to both methods:

- `now` must be timezone-aware UTC (naive input raises `ValueError`); it is the single frozen clock for scores, `calculated_at` and every `FeatureSource.captured_at`.
- `feature_snapshot_id = uuid_factory()`; `feature_version = "rules-v1"`; `calculated_at = now`.
- The extractor **returns** the contract; it does not persist. The caller (A5) persists it via A1 inside its own transaction.
- Recording rule (velocity): record-then-count, so counts include the current event/session.
- The `sources` dict contains an entry for **every feature field the extractor attempted**, including `MISSING`/`NOT_APPLICABLE` entries for fields left `None`; it contains no extra keys. Source labels are fixed (Section 5.4); `captured_at` is `now` for Redis-derived entries, `risk_session.updated_at` for session-derived entries and `event.received_at` for event-derived entries.
- Every `FeatureSource.source` value is at most 80 characters and contains no identifier values.

### 5.2 Velocity features

Velocity keys use `merchant_namespace` from the correlated session; an uncorrelated event uses `CORRELATION_FALLBACK_NAMESPACE` (a valid A0 namespace string kept in its own bucket, never mixed with a real merchant). If a required hash is absent, the feature is `None` and its source entry has `availability=MISSING` — never `0`.

| Feature | Required input | Computation |
|---|---|---|
| `ip_velocity_5m` | `session.ip_hash` | `record_and_count(velocity_key(ns,"ip","5m",ip_hash), member, 300, now)` |
| `ip_velocity_1h` | `session.ip_hash` | same key family with `"1h"` / 3600 |
| `device_velocity_5m` | `session.device_fingerprint_hash` | `velocity_key(ns,"device","5m",...)`, 300 |
| `device_velocity_1h` | `session.device_fingerprint_hash` | `"1h"` / 3600 |
| `customer_velocity_1h` | `session.customer_reference_hash` | `velocity_key(ns,"customer","1h",...)`, 3600 |
| `vpa_velocity_1h` | `event.vpa_hash` (event path only; always `None` on the session path) | `velocity_key(ns,"vpa","1h",...)`, 3600 |

`member` is `str(event.mandate_event_id)` on the event path and `str(risk_session.risk_session_id)` on the session path.

### 5.3 Behaviour, network and mandate features

| Feature | Computation | Unavailable |
|---|---|---|
| `is_new_device_for_customer` | requires both `session.customer_reference_hash` and `session.device_fingerprint_hash`; `check_then_add(known_devices_key(customer_hash), device_hash, 86_400, now)` | `None` + `MISSING` |
| `flow_duration_seconds` | `int((session.flow_completed_at - session.flow_started_at).total_seconds())`; defensively `None` if negative | `None` + `MISSING` if either time is absent |
| `user_agent_bot_suspected` | always `None` in v1 — only a UA **hash** is stored, so no classification is possible post-hoc; `NOT_APPLICABLE` source entry. Documented deferral (interface change to A2 required to classify at capture time) | always |
| `npci_risk_flag` | event path only, requires `event.failure_reason`: `True` when `"npci"` occurs in `failure_reason.lower()`, else `False` | `None` when `failure_reason` is absent |
| `payer_bank_or_compliance_flag` | event path only, requires `event.failure_reason`: `True` when any token of `{"payer bank", "payer_bank", "compliance", "frozen", "blocked", "fraud", "aml"}` occurs in `failure_reason.lower()`, else `False` | `None` when `failure_reason` is absent |
| `network_vpn_or_proxy` | from the reputation port (Section 7) when configured and it returns a result | `None` + `NOT_APPLICABLE` source entry |
| `network_reputation_score` | from the reputation port when configured | `None` + `NOT_APPLICABLE` source entry |
| `max_amount_paise` | `session.mandate_intent.max_amount_paise` | `None` + `MISSING` when no session |
| `mandate_frequency` | `session.mandate_intent.frequency` | `None` + `MISSING` when no session |
| `expiry_days_from_registration` | `(session.mandate_intent.expire_at - session.flow_started_at).days` when both exist and the difference is >= 0 | `None` + `MISSING` otherwise |

The keyword detection above is a *feature flag*, not a decision: A5 owns whatever scoring consequence the NPCI/payer-bank wording carries. A4 only reports what the stored text literally contains.

### 5.4 Fixed source labels

```text
redis_velocity:{signal}:{window_label}   e.g. redis_velocity:device:5m   (AVAILABLE)
redis_known_devices                                                      (AVAILABLE)
redis_demo_shared_merchants                                              (DEMO_SIMULATED)
risk_session                                                             (AVAILABLE)
mandate_event                                                            (AVAILABLE)
network_reputation_provider                                              (AVAILABLE, only when it returned data)
```

`availability=MISSING` entries use `source="not_available:{field_name}"` and `captured_at=None`. `NOT_APPLICABLE` is used when a signal cannot logically apply on a path (for example `vpa_velocity_1h` on the session path).

## 6. Demo simulation rules

A snapshot is **demo-simulated** when either applies:

- Event path: `event.is_demo_event` is `True`; or
- Either path: `risk_session.merchant_namespace` starts with `"demo"` (A9 names its simulated namespaces `demo_*`).

Exact behaviour:

- When demo-simulated **and** a device hash is available: call `record_demo_merchant(device_hash, merchant_namespace, now)`, then `shared_demo_merchant_count_1h = count_demo_merchants(device_hash, now)`; the source entry uses `availability=DEMO_SIMULATED` with source `redis_demo_shared_merchants` and `captured_at=now`; set `is_demo_simulation=True`.
- When demo-simulated but no device hash exists: `shared_demo_merchant_count_1h=None`, `is_demo_simulation=True`, `MISSING` source entry.
- When not demo-simulated: `shared_demo_merchant_count_1h=None`, `is_demo_simulation=False`, and the demo key methods are **never called** — production traffic must not create or read `demoshared` keys.

This matches the A0 contract rule that a non-null `shared_demo_merchant_count_1h` requires `is_demo_simulation=True` plus a `DEMO_SIMULATED` source entry, and the architecture requirement that simulated data is always visibly labelled.

## 7. Reputation port and integration seam (`reputation.py`)

```python
@dataclass(frozen=True)
class ReputationResult:
    vpn_or_proxy: bool
    reputation_score: int          # 0..100, clamped by the caller before use

class NetworkReputationProvider(Protocol):
    async def inspect(self, ip_hash: str, now: datetime) -> ReputationResult | None: ...

class UnavailableNetworkReputationProvider:
    async def inspect(self, ip_hash: str, now: datetime) -> ReputationResult | None:
        return None
```

- A4 ships only the protocol and the unavailable default. No external IP-reputation HTTP call exists in v1.
- The port deliberately receives only the **hash**. A real provider needs the raw IP, which would require a separately approved interface change (raw values must not start flowing through A4); until then the honest v1 behaviour is "no network reputation data".
- The provider is optional and asynchronous; it must never block the Redis window work and its failure is treated as "no data" (`None`), not an error.

### 7.1 Application wiring is A5's job, not A4's

A4 makes **no edits** to `main.py` and sets no `app.state`. Reserved `app.state` keys, documented for A5:

```text
app.state.feature_extractor            # FeatureExtractor(store=RedisFeatureStore.from_settings(), reputation=...)
app.state.network_reputation_provider  # optional; default UnavailableNetworkReputationProvider()
```

A5 composes the A3 `MandateEventEvaluator` and A2 `PrecheckEvaluator` implementations around `FeatureExtractor` and persists the returned snapshots. The same `RedisFeatureStore` instance may be shared by both paths; it is stateless apart from the Redis data itself.

## 8. Tests and acceptance criteria

Unit tests run against `fakeredis` with a frozen clock and fixed UUID factory — no PostgreSQL, no network. Integration tests use real Redis (database 15) and real PostgreSQL via A1's setup. Only synthetic demo values and `hmac-sha256:`-format hash strings appear in tests; never commit a real identifier.

### 8.1 Test helper (`tests/helpers/redis_test_client.py`)

```python
TEST_REDIS_URL = "redis://localhost:6379/15"

def make_test_redis() -> Redis:
    """Return an async Redis client for TEST_REDIS_URL and FLUSHDB it.
    Fail with a helpful message when TEST_REDIS_URL is unreachable/missing."""
```

Integration tests import this helper; do not modify A1's `tests/integration/conftest.py`.

### 8.2 Unit tests — `tests/unit/test_feature_windows.py`

Build `RedisFeatureStore(FakeAsyncRedis(), key_prefix="mg:test")`. Use fixed datetimes such as `T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc)` and advance the frozen clock explicitly.

```text
test_member_recorded_at_t0_is_counted_inside_window
test_member_still_counted_one_millisecond_before_window_edge
test_member_excluded_exactly_at_window_edge
test_second_window_member_counts_after_first_expires
test_same_second_distinct_members_both_count
test_same_member_recorded_twice_counts_once
test_different_merchant_namespaces_do_not_share_counters
test_different_signals_do_not_share_counters
test_every_written_key_has_a_ttl
test_naive_now_is_rejected_with_value_error
test_invalid_namespace_is_rejected_with_safe_message
test_invalid_hash_is_rejected_with_safe_message
test_redis_error_is_wrapped_as_feature_store_unavailable
test_check_then_add_reports_true_then_false
test_known_devices_ttl_is_24_hours
test_demo_shared_merchants_count_distinct_namespaces_in_1h
test_demo_shared_merchants_excludes_expired_namespace
```

The window-edge tests are the core acceptance proof demanded by the architecture plan: they must assert inclusion at `T0 + window - 1ms` and exclusion at `T0 + window` for both the 5-minute and 1-hour windows.

### 8.3 Unit tests — `tests/unit/test_feature_extractor.py`

Use `fakeredis`, a fixed clock and a fixed UUID factory. Build contract objects with obviously fake values (hashes like `hmac-sha256:` + `"ab"*32`, namespaces `demo_merchant_one`).

```text
test_correlated_confirmed_event_builds_full_valid_snapshot
test_uncorrelated_event_uses_uncorrelated_namespace_for_vpa_velocity
test_session_extraction_builds_precheck_snapshot_without_vpa
test_missing_hashes_yield_missing_sources_not_zero_counts
test_npci_risk_flag_true_for_fixture_failure_reason
test_payer_bank_flag_matches_bounded_tokens_only
test_flags_are_none_without_failure_reason
test_demo_event_records_and_counts_shared_merchant
test_demo_session_via_namespace_prefix_counts_shared_merchant
test_non_demo_traffic_never_touches_demo_keys
test_new_device_true_then_false_for_same_customer
test_device_new_for_one_customer_not_for_another
test_flow_duration_computed_from_session_times
test_mandate_intent_fields_flow_into_snapshot
test_snapshot_validates_against_a0_contract
test_sources_cover_every_attempted_field
test_redis_failure_raises_feature_store_unavailable
```

For `test_npci_risk_flag_true_for_fixture_failure_reason`, reuse the phrase `NPCI risk flag` from the A0 rejected-event fixture. For `test_snapshot_validates_against_a0_contract`, run `FeatureSnapshot.model_validate(snapshot.model_dump(mode="json"))` on every emitted snapshot.

### 8.4 Integration tests — `tests/integration/test_feature_store_redis.py`

Use the `make_test_redis()` helper against real Redis. These prove fakeredis did not hide real behaviour.

```text
test_real_redis_matches_window_semantics
test_all_written_keys_carry_ttl_on_real_redis
test_two_store_instances_share_counts_via_real_redis
test_concurrent_recordings_are_all_counted
```

For `test_concurrent_recordings_are_all_counted`, gather 25 `record_and_count` calls with distinct members via `asyncio.gather` on the same key and assert the final count is 25 with no lost updates.

### 8.5 Integration tests — `tests/integration/test_feature_extractor_persistence.py`

Use A1's `TEST_POSTGRES_URL` fixture setup for PostgreSQL plus the Redis test helper. This is the only place A4 touches repositories.

```text
test_extracted_event_snapshot_persists_and_reloads
test_extracted_session_snapshot_persists_with_risk_session_link
test_demo_snapshot_satisfies_demo_source_contract_rule
```

Flow: inside `async with transaction() as session`, create a `RiskSession` via `RiskSessionRepository.create`, extract for an event/session with the real store, persist with `FeatureSnapshotRepository.create`, then reload with `get`/`get_latest_for_event` and assert the reloaded contract equals the extracted one (same `feature_version`, feature values and source availabilities). `test_demo_snapshot_satisfies_demo_source_contract_rule` asserts `is_demo_simulation=True`, non-null counter and `sources["shared_demo_merchant_count_1h"].availability == DEMO_SIMULATED`.

### 8.6 Manual acceptance (before A5 exists)

1. `docker compose up -d postgres redis`, then run the extractor through the integration tests.
2. During a simulated burst, inspect `redis-cli --scan --pattern 'mg:*'` from another shell: every key matches the Section 4.1 catalogue, every key has a TTL (`redis-cli ttl <key>` > 0), and no key contains anything but a namespace, signal word, window label and `hmac-sha256:` hash.
3. Stop the Redis container and confirm extraction raises `FeatureStoreUnavailable` rather than silently returning zeros.

### 8.7 Verification commands

```powershell
docker compose up -d postgres redis
$env:TEST_POSTGRES_URL = "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
$env:TEST_REDIS_URL = "redis://localhost:6379/15"
uv --directory apps/api run pytest tests/unit/test_feature_windows.py tests/unit/test_feature_extractor.py -q
uv --directory apps/api run pytest tests/integration/test_feature_store_redis.py tests/integration/test_feature_extractor_persistence.py -q
uv --directory apps/api run ruff check app/services/features tests/helpers/redis_test_client.py tests/unit/test_feature_windows.py tests/unit/test_feature_extractor.py
uv --directory apps/api run ruff format --check app/services/features tests/helpers/redis_test_client.py
uv --directory apps/api run mypy app/services/features
docker compose down
```

Do not mark the package done until all commands pass. Performance context: feature extraction is inside the 200 ms webhook-to-assessment budget; one pipeline per signal (at most six) plus one known-device check must stay well inside it — never add a per-feature network round trip.

## 9. Merge and downstream handoff

### Inputs consumed

| Producer | Input | A4 use |
|---|---|---|
| A0 | `FeatureSnapshot`, `FeatureSource`, `Availability`, `RiskSession`, `MandateWebhookEvent`, settings | Exact public shape; import only, never edit. `Settings.redis_url` builds the store lazily. |
| A1 | `transaction()`, `RiskSessionRepository`, `FeatureSnapshotRepository` | Persistence round-trip proof in integration tests only; no application-code persistence. |
| A2 | Session hash fields and trust conventions | A4 reads only `*_hash` fields; it never sees or requests raw values. |
| A3 | `MandateWebhookEvent` and the `MandateEventEvaluator` seam | A4 supplies `extract_for_event` for A5's evaluator; A3's code is untouched. |

### Outputs produced

| Consumer | Receives from A4 | Integration rule |
|---|---|---|
| A5 rule engine | `FeatureExtractor` + `FeatureSnapshot` contracts | A5 composes both evaluator ports, persists snapshots/assessments via A1 and sets `app.state.feature_extractor`; scoring/points stay in A5. |
| A2 pre-check path | `extract_for_session` | A5's `PrecheckEvaluator` calls it inside the A2-provided transaction. |
| A11 ML shadow model | persisted snapshots via A1 | Reads `FeatureSnapshot` rows only; A4 is unaware of it. |
| A9 simulator | demo namespace convention (`demo_*`) and key catalogue | Simulated merchants automatically exercise the demo shared-merchant path. |
| A10 CI/security | window tests + Redis hygiene tests | CI asserts TTLs and no-raw-identifier invariants. |

### Merge order

1. A0 (`contracts-v1`) and A1 must be merged first; A4's integration tests also need A2's conventions (merged or stubbed).
2. A4 merges in parallel with A2/A3 — it adds no routes and edits no shared runtime files.
3. A5 merges after A4 and wires `app.state.feature_extractor` plus the composite evaluators; that is the only integration change.
4. A9/A10/A11 consume A4 outputs without modification.

### Pull-request handoff checklist

State in the A4 PR: the key catalogue and TTL table; the exact window-edge proof (test names and results); the record-then-count and check-then-add semantics; the demo labelling proof; the `fakeredis` dev-dependency rationale; results for all commands in Section 8.7; confirmation that only the owned paths plus the single `pyproject.toml` dev-group line changed; and known deferred work (real reputation provider, UA capture-time classification, A5 composition, raw-IP provider interface change).

## 10. Failure conditions

The A4 work is unsafe or incomplete if any of these occur:

- A raw VPA, IP, device fingerprint, customer identifier or secret appears in any Redis key, value, log line, metric label or test fixture.
- Any written key lacks a TTL, or state can outlive its window.
- A member recorded at `T` is still counted at `T + window`, or same-second events overwrite each other.
- Unavailable inputs are reported as `0`, or missing data is silently invented.
- Redis failure is swallowed into permissive behaviour instead of raising `FeatureStoreUnavailable`.
- A non-null `shared_demo_merchant_count_1h` exists without `is_demo_simulation=True` and a `DEMO_SIMULATED` source, or production traffic touches demo keys.
- A4 computes scores, points, decisions or reason codes, persists snapshots from application code, edits `main.py`/`app.state`, changes frozen contracts, or implements another agent's owned area.
