"""Integration tests for A2 session capture and precheck orchestrator services with PostgreSQL."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.contracts.common import Decision, DecisionStage, RiskSessionStatus
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import MandateIntent, RiskSession
from app.repositories import (
    AuditRepository,
    FeatureSnapshotRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)
from app.services.precheck_orchestrator import (
    InvalidPrecheckAssessment,
    PrecheckOrchestrator,
    PrecheckUnavailable,
    RiskSessionNotFound,
    RiskSessionNotReady,
)
from app.services.precheck_provider import UnavailablePrecheckEvaluator
from app.services.privacy_hashing import HmacPseudonymizer
from app.services.session_capture import SessionCaptureService


@pytest.fixture
def session_factory(test_session_factory):
    """Return the test session factory."""
    return test_session_factory


@pytest.fixture
def pseudonymizer() -> HmacPseudonymizer:
    """Create a pseudonymizer with test pepper."""
    return HmacPseudonymizer("test-pepper-1234567890")


@pytest.fixture
def clock():
    """Fixed clock for deterministic tests."""
    fixed = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    return lambda: fixed


@pytest.fixture
def uuid_factory():
    """UUID factory for tests."""
    return uuid4


class TestSessionCaptureService:
    """Integration tests for SessionCaptureService with real A1 repositories."""

    async def test_create_session_persists_and_returns_hashes(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Create session via service and verify HMAC hashes in persisted contract."""
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_001",
            customer_reference="cust-secret-123",
            device_fingerprint="fp-secret-456",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )

        assert result.is_replay is False
        session = result.session
        assert session.status == RiskSessionStatus.CREATED
        assert session.customer_reference_hash.startswith("hmac-sha256:")
        assert session.ip_hash.startswith("hmac-sha256:")
        assert session.device_fingerprint_hash.startswith("hmac-sha256:")
        assert session.user_agent_hash.startswith("hmac-sha256:")

    async def test_create_same_order_ref_is_idempotent_replay(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Repeated create with same order_ref returns original session."""
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_002",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        # First create
        result1 = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        assert result1.is_replay is False

        # Second create with same order_ref
        result2 = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        assert result2.is_replay is True
        assert result2.session.risk_session_id == result1.session.risk_session_id

    async def test_record_telemetry_marks_ready_and_audits(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Record telemetry transitions session to READY and appends audit event."""
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_003",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        from app.contracts.risk_session import RiskSessionTelemetryUpdateRequest

        telemetry = RiskSessionTelemetryUpdateRequest(
            device_fingerprint="fp-updated",
            flow_completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        )

        updated = await service.record_telemetry(
            session_id, telemetry, peer_ip="192.168.1.1", request_user_agent="real-browser/2.0"
        )

        assert updated.status == RiskSessionStatus.READY
        assert updated.device_fingerprint_hash != result.session.device_fingerprint_hash
        assert updated.flow_completed_at == telemetry.flow_completed_at

    async def test_record_telemetry_rejects_consumed_session(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Telemetry on CONSUMED session raises ValueError."""
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_004",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        # Mark session CONSUMED via repository
        async with session_factory() as session:
            session_repo = RiskSessionRepository(session)
            await session_repo.update_status(session_id, RiskSessionStatus.CONSUMED, clock())
            await session.commit()

        from app.contracts.risk_session import RiskSessionTelemetryUpdateRequest

        telemetry = RiskSessionTelemetryUpdateRequest(
            flow_completed_at=datetime(2026, 1, 15, 10, 6, 0, tzinfo=UTC),
        )

        with pytest.raises(ValueError, match="already consumed"):
            await service.record_telemetry(
                session_id, telemetry, peer_ip="192.168.1.1", request_user_agent="real-browser/2.0"
            )


class TestPrecheckOrchestrator:
    """Integration tests for PrecheckOrchestrator with real A1 repositories."""

    async def test_missing_session_raises_not_found(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Assessing non-existent session raises RiskSessionNotFound."""
        orchestrator = PrecheckOrchestrator(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            risk_assessment_repo=RiskAssessmentRepository,
            audit_repo=AuditRepository,
            evaluator=UnavailablePrecheckEvaluator(),
            clock=clock,
        )

        with pytest.raises(RiskSessionNotFound):
            await orchestrator.assess(uuid4())

    async def test_created_session_raises_not_ready(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Precheck on CREATED session raises RiskSessionNotReady."""
        # Create a session
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_005",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        # Precheck on CREATED session (no telemetry)
        orchestrator = PrecheckOrchestrator(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            risk_assessment_repo=RiskAssessmentRepository,
            audit_repo=AuditRepository,
            evaluator=UnavailablePrecheckEvaluator(),
            clock=clock,
        )

        with pytest.raises(RiskSessionNotReady):
            await orchestrator.assess(session_id)

    async def test_unavailable_evaluator_leaves_session_ready(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Unavailable evaluator leaves session READY, no audit event."""
        # Create and ready a session
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_006",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        # Record telemetry -> READY
        from app.contracts.risk_session import RiskSessionTelemetryUpdateRequest

        telemetry = RiskSessionTelemetryUpdateRequest(
            flow_completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        await service.record_telemetry(
            session_id, telemetry, peer_ip="192.168.1.1", request_user_agent="real-browser/2.0"
        )

        # Precheck with unavailable evaluator
        orchestrator = PrecheckOrchestrator(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            risk_assessment_repo=RiskAssessmentRepository,
            audit_repo=AuditRepository,
            evaluator=UnavailablePrecheckEvaluator(),
            clock=clock,
        )

        with pytest.raises(PrecheckUnavailable):
            await orchestrator.assess(session_id)

        # Session should remain READY
        async with session_factory() as session:
            session_repo = RiskSessionRepository(session)
            session_obj = await session_repo.get(session_id)
            assert session_obj.status == RiskSessionStatus.READY

        # No precheck_completed audit event
        async with session_factory() as session:
            audit_repo = AuditRepository(session)
            events = await audit_repo.list_for_aggregate("risk_session", session_id)
            precheck_events = [
                e for e in events if e.event_type == "risk_session.precheck_completed"
            ]
            assert len(precheck_events) == 0

    async def test_valid_precheck_consumes_session_and_audits(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Valid precheck consumes session, persists assessment, and audits."""
        # Create and ready a session
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_007",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        # Record telemetry -> READY
        from app.contracts.risk_session import RiskSessionTelemetryUpdateRequest

        telemetry = RiskSessionTelemetryUpdateRequest(
            flow_completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        await service.record_telemetry(
            session_id, telemetry, peer_ip="192.168.1.1", request_user_agent="real-browser/2.0"
        )

        # Precheck with fake evaluator (persists snapshot + assessment like A5)
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

        orchestrator = PrecheckOrchestrator(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            risk_assessment_repo=RiskAssessmentRepository,
            audit_repo=AuditRepository,
            evaluator=FakeEvaluator(),
            clock=clock,
        )

        precheck_result = await orchestrator.assess(session_id)

        assert precheck_result.is_replay is False
        assert precheck_result.assessment.stage == DecisionStage.PRECHECK
        assert precheck_result.assessment.risk_session_id == session_id
        assert precheck_result.assessment.mandate_event_id is None

        # Session should be CONSUMED
        async with session_factory() as session:
            session_repo = RiskSessionRepository(session)
            session_obj = await session_repo.get(session_id)
            assert session_obj.status == RiskSessionStatus.CONSUMED

        # Assessment should be persisted
        async with session_factory() as session:
            assessment_repo = RiskAssessmentRepository(session)
            assessment = await assessment_repo.get_precheck_for_session(session_id)
            assert assessment is not None
            assert assessment.assessment_id == precheck_result.assessment.assessment_id

        # Audit event should be created
        async with session_factory() as session:
            audit_repo = AuditRepository(session)
            events = await audit_repo.list_for_aggregate("risk_session", session_id)
            precheck_events = [
                e for e in events if e.event_type == "risk_session.precheck_completed"
            ]
            assert len(precheck_events) == 1
            payload = precheck_events[0].redacted_payload
            assert "assessment_id" in payload
            assert "decision" in payload
            assert "score" in payload
            assert "engine_version" in payload
            assert "evaluation_latency_ms" in payload

    async def test_consumed_session_returns_existing_assessment(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Second precheck on CONSUMED session replays existing assessment."""
        # Create, ready, and consume a session
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_008",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        from app.contracts.risk_session import RiskSessionTelemetryUpdateRequest

        telemetry = RiskSessionTelemetryUpdateRequest(
            flow_completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        await service.record_telemetry(
            session_id, telemetry, peer_ip="192.168.1.1", request_user_agent="real-browser/2.0"
        )

        # First precheck
        class FakeEvaluator:
            """Persists snapshot + assessment like A5's PrecheckEvaluationService."""

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

        evaluator = FakeEvaluator()
        orchestrator = PrecheckOrchestrator(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            risk_assessment_repo=RiskAssessmentRepository,
            audit_repo=AuditRepository,
            evaluator=evaluator,
            clock=clock,
        )

        result1 = await orchestrator.assess(session_id)
        assert result1.is_replay is False
        assert evaluator.call_count == 1

        # Second precheck -> replay
        result2 = await orchestrator.assess(session_id)
        assert result2.is_replay is True
        assert result2.assessment.assessment_id == result1.assessment.assessment_id
        assert evaluator.call_count == 1  # Not called again

    async def test_wrong_stage_raises_invalid(
        self, session_factory, pseudonymizer, clock, uuid_factory
    ):
        """Evaluator returning POST_CONFIRMATION stage raises InvalidPrecheckAssessment."""
        # Create and ready a session
        service = SessionCaptureService(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            audit_repo=AuditRepository,
            pseudonymizer=pseudonymizer,
            clock=clock,
            uuid_factory=uuid_factory,
        )

        from app.contracts.risk_session import RiskSessionCreateRequest

        request = RiskSessionCreateRequest(
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_svc_009",
            customer_reference="cust-001",
            device_fingerprint="fp-secret-001",
            flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
        )

        result = await service.create(
            request, peer_ip="192.168.1.1", request_user_agent="test-agent/1.0"
        )
        session_id = result.session.risk_session_id

        from app.contracts.risk_session import RiskSessionTelemetryUpdateRequest

        telemetry = RiskSessionTelemetryUpdateRequest(
            flow_completed_at=datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        )
        await service.record_telemetry(
            session_id, telemetry, peer_ip="192.168.1.1", request_user_agent="real-browser/2.0"
        )

        # Evaluator returning wrong stage
        class BadEvaluator:
            async def assess(self, db_session, risk_session: RiskSession) -> RiskAssessment:
                return RiskAssessment(
                    schema_version="1.0",
                    assessment_id=uuid4(),
                    risk_session_id=risk_session.risk_session_id,
                    mandate_event_id=uuid4(),
                    feature_snapshot_id=uuid4(),
                    stage=DecisionStage.POST_CONFIRMATION,
                    score=50,
                    decision=Decision.ALLOW,
                    engine_version="rules-v1",
                    rule_evaluations=[
                        RuleEvaluation(
                            rule_id="test_rule",
                            triggered=True,
                            points=50,
                            reason_code="TEST",
                            reason_text="Test rule",
                        )
                    ],
                    evaluation_latency_ms=5,
                    assessed_at=datetime.now(UTC),
                )

        orchestrator = PrecheckOrchestrator(
            session_factory=session_factory,
            risk_session_repo=RiskSessionRepository,
            risk_assessment_repo=RiskAssessmentRepository,
            audit_repo=AuditRepository,
            evaluator=BadEvaluator(),
            clock=clock,
        )

        with pytest.raises(InvalidPrecheckAssessment, match="stage must be PRECHECK"):
            await orchestrator.assess(session_id)
