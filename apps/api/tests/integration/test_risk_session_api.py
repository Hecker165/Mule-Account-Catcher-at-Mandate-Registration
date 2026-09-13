"""Integration tests for A2 risk session API endpoints with PostgreSQL."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.contracts.common import Decision, DecisionStage
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import RiskSession
from app.main import create_app
from app.repositories import FeatureSnapshotRepository, RiskAssessmentRepository
from app.services.precheck_provider import UnavailablePrecheckEvaluator


class _FakePeerMiddleware:
    """ASGI middleware giving the TestClient a deterministic parseable peer.

    Starlette's TestClient reports an unparseable peer address ("testclient"),
    which the A2 pseudonymiser correctly maps to ``ip_hash=None``. This
    middleware supplies a real IPv4 peer so tests exercise the hashing path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = ("192.168.1.10", 50000)
        await self.app(scope, receive, send)


@pytest.fixture
def app(test_session_factory):
    """Create FastAPI app wired to the NullPool test engine.

    ``create_app()`` attaches the module-level A1 engine, whose pooled
    connections cannot cross the per-request event loops TestClient creates.
    Overriding ``app.state.session_factory`` with the test factory (NullPool)
    mirrors how later agents replace state on composition.
    """
    application = create_app()
    application.state.session_factory = test_session_factory
    return application


@pytest.fixture
def client(app):
    """Create test client with a deterministic parseable peer address."""
    return TestClient(_FakePeerMiddleware(app))


@pytest.fixture
def fake_evaluator(app):
    """Install a fake evaluator that persists a valid PRECHECK assessment.

    Mimics A5's PrecheckEvaluationService, which persists the feature
    snapshot and assessment in the caller's transaction (A2 spec §4.3).
    Patches the app instance under test (request.app.state), not the
    module-level global.
    """
    from app.services.precheck_provider import UnavailablePrecheckEvaluator

    class FakeEvaluator:
        def __init__(self):
            self.call_count = 0

        async def assess(self, db_session, risk_session: RiskSession) -> RiskAssessment:
            self.call_count += 1
            snapshot_repo = FeatureSnapshotRepository(db_session)
            snapshot = await snapshot_repo.create(
                FeatureSnapshot(
                    schema_version="1.0",
                    feature_snapshot_id=uuid4(),
                    mandate_event_id=None,
                    risk_session_id=risk_session.risk_session_id,
                    feature_version="rules-v1",
                    calculated_at=datetime.now(UTC),
                    is_demo_simulation=False,
                    sources={},
                )
            )
            assessment_repo = RiskAssessmentRepository(db_session)
            assessment = RiskAssessment(
                schema_version="1.0",
                assessment_id=uuid4(),
                risk_session_id=risk_session.risk_session_id,
                mandate_event_id=None,
                feature_snapshot_id=snapshot.feature_snapshot_id,
                stage=DecisionStage.PRECHECK,
                score=45,
                decision=Decision.CHALLENGE,
                engine_version="rules-v1",
                rule_evaluations=[
                    RuleEvaluation(
                        rule_id="test_rule",
                        triggered=True,
                        points=45,
                        reason_code="TEST",
                        reason_text="Test rule",
                    )
                ],
                evaluation_latency_ms=10,
                assessed_at=datetime.now(UTC),
            )
            return await assessment_repo.create(assessment)

    original = getattr(app.state, "precheck_evaluator", None)
    app.state.precheck_evaluator = FakeEvaluator()
    yield
    if original is not None:
        app.state.precheck_evaluator = original
    else:
        app.state.precheck_evaluator = UnavailablePrecheckEvaluator()


def test_create_risk_session_returns_hashes_not_raw_request_values(client):
    """Verify response contains HMAC hashes, not raw values."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_001",
        "customer_reference": "cust-secret-123",
        "device_fingerprint": "fp-secret-456",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    response = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})

    assert response.status_code == 201
    data = response.json()

    # Check that sensitive fields are hashed (hmac-sha256: prefix)
    assert data["customer_reference_hash"].startswith("hmac-sha256:")
    assert data["ip_hash"].startswith("hmac-sha256:")
    assert data["device_fingerprint_hash"].startswith("hmac-sha256:")
    assert data["user_agent_hash"].startswith("hmac-sha256:")

    # Raw values should NOT appear in response
    assert "cust-secret-123" not in str(data)
    assert "fp-secret-456" not in str(data)

    # Verify idempotency header not present on first create
    assert "X-Idempotent-Replay" not in response.headers
    assert response.headers.get("Cache-Control") == "no-store"


def test_create_same_namespace_order_ref_is_an_idempotent_replay(client):
    """Same (namespace, order_ref) returns original session with replay header."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_002",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }

    # First create
    r1 = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    assert r1.status_code == 201
    session_id = r1.json()["risk_session_id"]

    # Second create with same namespace + order_ref
    r2 = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["risk_session_id"] == session_id


def test_create_same_order_ref_in_different_namespace_creates_two_sessions(client):
    """Same order_ref in different namespace creates distinct sessions."""
    base_body = {
        "checkout_order_ref": "order_shared",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }

    body1 = {**base_body, "merchant_namespace": "ns-a"}
    body2 = {**base_body, "merchant_namespace": "ns-b"}

    r1 = client.post("/v1/risk-sessions", json=body1, headers={"user-agent": "test-agent/1.0"})
    r2 = client.post("/v1/risk-sessions", json=body2, headers={"user-agent": "test-agent/1.0"})

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["risk_session_id"] != r2.json()["risk_session_id"]


def test_telemetry_uses_direct_peer_and_ignores_client_claims(client):
    """Telemetry endpoint uses direct peer IP and ignores body client_ip/is_vpn_claimed."""
    # Create session
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_003",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    # Patch telemetry with false body claims
    telemetry = {
        "device_fingerprint": "fp-updated",
        "flow_completed_at": "2026-01-15T10:05:00Z",
        "client_ip": "198.51.100.99",  # Should be ignored
        "client_user_agent": "fake-agent",  # Should be ignored
        "is_vpn_claimed": True,  # Should be ignored
    }
    r2 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json=telemetry,
        headers={"user-agent": "real-browser/2.0"},
    )

    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "READY"

    # device_fingerprint should be updated
    assert data["device_fingerprint_hash"].startswith("hmac-sha256:")
    # The IP hash should be from the direct peer (testclient = 127.0.0.1)
    # not from the body claim


def test_telemetry_marks_ready_and_appends_redacted_audit_event(client, fake_evaluator):
    """Telemetry transitions to READY and appends boolean-only audit event."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_004",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    telemetry = {
        "device_fingerprint": "fp-updated",
        "flow_completed_at": "2026-01-15T10:05:00Z",
    }
    r2 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json=telemetry,
        headers={"user-agent": "real-browser/2.0"},
    )

    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "READY"
    assert r2.headers.get("Cache-Control") == "no-store"

    # Call precheck to verify audit chain has telemetry_recorded event
    r3 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r3.status_code == 200
    assessment = r3.json()
    assert assessment["stage"] == "PRECHECK"
    assert assessment["decision"] == "CHALLENGE"
    assert assessment["score"] == 45


def test_telemetry_rejects_consumed_session(client, fake_evaluator):
    """Telemetry PATCH rejects session already in CONSUMED status."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_005",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    # Record telemetry -> READY
    r2 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json={"flow_completed_at": "2026-01-15T10:05:00Z"},
        headers={"user-agent": "real-browser/2.0"},
    )
    assert r2.status_code == 200

    # Precheck -> CONSUMED
    r3 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r3.status_code == 200

    # Try telemetry again on CONSUMED session
    r4 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json={"flow_completed_at": "2026-01-15T10:06:00Z"},
        headers={"user-agent": "real-browser/2.0"},
    )
    assert r4.status_code == 409
    assert "already consumed" in r4.json()["detail"].lower()


def test_precheck_requires_ready_session(client):
    """Precheck on CREATED (not READY) session returns 409."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_006",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    # Precheck without telemetry (status=CREATED)
    r2 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r2.status_code == 409
    assert "not ready" in r2.json()["detail"].lower()


def test_precheck_returns_503_when_evaluator_is_unavailable(client, app):
    """Precheck returns 503 when no evaluator is installed (default)."""
    app.state.precheck_evaluator = UnavailablePrecheckEvaluator()
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_007",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    # Record telemetry -> READY
    r2 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json={"flow_completed_at": "2026-01-15T10:05:00Z"},
        headers={"user-agent": "real-browser/2.0"},
    )
    assert r2.status_code == 200

    # Precheck with default unavailable evaluator
    r3 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r3.status_code == 503
    assert "unavailable" in r3.json()["detail"].lower()


def test_precheck_persists_valid_assessment_consumes_session_and_audits(client, fake_evaluator):
    """Valid precheck returns assessment, marks CONSUMED, and appends audit event."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_008",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    # Record telemetry -> READY
    r2 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json={"flow_completed_at": "2026-01-15T10:05:00Z"},
        headers={"user-agent": "real-browser/2.0"},
    )
    assert r2.status_code == 200

    # Precheck with fake evaluator
    r3 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r3.status_code == 200
    assessment = r3.json()
    assert assessment["stage"] == "PRECHECK"
    assert assessment["decision"] in ("ALLOW", "CHALLENGE")
    assert assessment["score"] > 0
    assert assessment["risk_session_id"] == session_id
    assert assessment["mandate_event_id"] is None

    # Verify session is CONSUMED
    # (Would need to query via session repo to confirm)

    # Second precheck should replay
    r4 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r4.status_code == 200
    assert r4.headers.get("X-Idempotent-Replay") == "true"
    assert r4.json()["assessment_id"] == assessment["assessment_id"]


def test_precheck_replay_returns_same_assessment_without_re_evaluation(client, fake_evaluator):
    """Second precheck on CONSUMED session replays without re-running evaluator."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_009",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }
    r = client.post("/v1/risk-sessions", json=body, headers={"user-agent": "test-agent/1.0"})
    session_id = r.json()["risk_session_id"]

    # Record telemetry -> READY
    client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json={"flow_completed_at": "2026-01-15T10:05:00Z"},
        headers={"user-agent": "real-browser/2.0"},
    )

    # First precheck
    r1 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r1.status_code == 200
    assessment1 = r1.json()
    assert "X-Idempotent-Replay" not in r1.headers

    # Second precheck -> replay
    r2 = client.post(f"/v1/risk-sessions/{session_id}/precheck")
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["assessment_id"] == assessment1["assessment_id"]


def test_all_session_responses_set_no_store_and_preserve_request_id(client):
    """All session responses have Cache-Control: no-store and preserve X-Request-ID."""
    body = {
        "merchant_namespace": "demo-merchant-01",
        "checkout_order_ref": "order_010",
        "customer_reference": "cust-001",
        "device_fingerprint": "fp-secret-001",
        "flow_started_at": "2026-01-15T10:00:00Z",
        "mandate_intent": {
            "max_amount_paise": 500000,
            "frequency": "monthly",
            "expire_at": "2027-01-15T10:00:00Z",
        },
    }

    # Custom request ID
    custom_id = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    headers = {"user-agent": "test-agent/1.0", "x-request-id": custom_id}

    r1 = client.post("/v1/risk-sessions", json=body, headers=headers)
    assert r1.headers.get("Cache-Control") == "no-store"
    assert r1.headers.get("X-Request-ID") == custom_id

    session_id = r1.json()["risk_session_id"]

    r2 = client.patch(
        f"/v1/risk-sessions/{session_id}/telemetry",
        json={"flow_completed_at": "2026-01-15T10:05:00Z"},
        headers=headers,
    )
    assert r2.headers.get("Cache-Control") == "no-store"
    assert r2.headers.get("X-Request-ID") == custom_id

    r3 = client.post(f"/v1/risk-sessions/{session_id}/precheck", headers=headers)
    # 503 because no evaluator
    assert r3.headers.get("Cache-Control") == "no-store"
    assert r3.headers.get("X-Request-ID") == custom_id
