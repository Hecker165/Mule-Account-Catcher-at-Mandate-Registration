"""Unit tests for precheck orchestrator with fake repositories and evaluator."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.contracts.common import Decision, DecisionStage, RiskSessionStatus
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import MandateIntent, RiskSession
from app.services.precheck_orchestrator import (
    InvalidPrecheckAssessment,
    PrecheckEvaluatorUnavailable,
    PrecheckOrchestrator,
    PrecheckResult,
    PrecheckUnavailable,
    RiskSessionInconsistent,
    RiskSessionNotFound,
    RiskSessionNotReady,
)


class FakeRiskSessionRepository:
    def __init__(self):
        self.sessions = {}

    async def get(self, risk_session_id: UUID):
        return self.sessions.get(risk_session_id)

    async def update_status(
        self, risk_session_id: UUID, status: RiskSessionStatus, updated_at: datetime
    ):
        session = self.sessions[risk_session_id]
        session.status = status
        session.updated_at = updated_at
        return session


class FakeRiskAssessmentRepository:
    def __init__(self):
        self.assessments = {}

    async def get_precheck_for_session(self, risk_session_id: UUID):
        return self.assessments.get(risk_session_id)

    async def create(self, assessment: RiskAssessment):
        self.assessments[assessment.risk_session_id] = assessment
        return assessment


class FakeAuditRepository:
    def __init__(self):
        self.events = []

    async def append(self, **kwargs):
        self.events.append(kwargs)


class FakeEvaluator:
    """Fake evaluator that persists a valid assessment (mimics A5's service)."""

    def __init__(self, assessment_repo=None):
        self.call_count = 0
        self.should_fail = False
        self.fail_reason = None
        self.assessment_repo = assessment_repo

    async def assess(self, db_session, risk_session: RiskSession) -> "RiskAssessment":
        self.call_count += 1
        if self.should_fail:
            raise self.fail_reason

        assessment = RiskAssessment(
            schema_version="1.0",
            assessment_id=uuid4(),
            risk_session_id=risk_session.risk_session_id,
            mandate_event_id=None,
            feature_snapshot_id=uuid4(),
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
        # A5's evaluator is responsible for persisting the assessment (A2 §4.3).
        if self.assessment_repo is not None:
            await self.assessment_repo.create(assessment)
        return assessment


class TestPrecheckOrchestrator:
    @pytest.fixture
    def session_factory(self):
        """Factory that returns a mock session context manager."""
        mock_session = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = mock_session
        cm.__aexit__.return_value = None
        return MagicMock(return_value=cm)

    @pytest.fixture
    def orchestrator(self, session_factory):
        repo = FakeRiskSessionRepository()
        assessment_repo = FakeRiskAssessmentRepository()
        audit_repo = FakeAuditRepository()
        evaluator = FakeEvaluator(assessment_repo)
        clock = MagicMock(return_value=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC))

        return (
            PrecheckOrchestrator(
                session_factory=session_factory,
                risk_session_repo=type("Repo", (), {"__call__": lambda s, session: repo})(),
                risk_assessment_repo=type(
                    "Repo", (), {"__call__": lambda s, session: assessment_repo}
                )(),
                audit_repo=type("Repo", (), {"__call__": lambda s, session: audit_repo})(),
                evaluator=evaluator,
                clock=clock,
            ),
            repo,
            assessment_repo,
            audit_repo,
            evaluator,
            clock,
        )

    def _make_session(self, risk_session_id: UUID, status: RiskSessionStatus):
        return RiskSession(
            schema_version="1.0",
            risk_session_id=risk_session_id,
            merchant_namespace="demo-merchant-01",
            checkout_order_ref="order_001",
            mandate_intent=MandateIntent(
                max_amount_paise=500000,
                frequency="monthly",
                expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
            status=status,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def test_missing_session_raises_not_found(self, orchestrator):
        orch, repo, _, _, _, _ = orchestrator
        repo.sessions.clear()

        with pytest.raises(RiskSessionNotFound):
            await orch.assess(uuid4())

    async def test_created_session_raises_not_ready(self, orchestrator):
        orch, repo, _, _, _, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.CREATED)

        with pytest.raises(RiskSessionNotReady):
            await orch.assess(session_id)

    async def test_unavailable_evaluator_leaves_session_ready(self, orchestrator):
        orch, repo, _, audit_repo, evaluator, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.READY)
        evaluator.should_fail = True
        evaluator.fail_reason = PrecheckEvaluatorUnavailable()

        with pytest.raises(PrecheckUnavailable):
            await orch.assess(session_id)

        # Session should remain READY
        assert repo.sessions[session_id].status == RiskSessionStatus.READY
        # No audit event should be written
        event_type = "risk_session.precheck_completed"
        precheck_events = [e for e in audit_repo.events if e.get("event_type") == event_type]
        assert len(precheck_events) == 0

    async def test_valid_precheck_consumes_session_and_audits(self, orchestrator):
        orch, repo, assessment_repo, audit_repo, evaluator, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.READY)

        result = await orch.assess(session_id)

        assert isinstance(result, PrecheckResult)
        assert result.is_replay is False
        assert result.assessment.risk_session_id == session_id
        assert result.assessment.stage == DecisionStage.PRECHECK
        assert result.assessment.mandate_event_id is None

        # Session should be CONSUMED
        assert repo.sessions[session_id].status == RiskSessionStatus.CONSUMED

        # Assessment should be persisted
        assert session_id in assessment_repo.assessments

        # Audit event should be created
        event_type = "risk_session.precheck_completed"
        precheck_events = [e for e in audit_repo.events if e.get("event_type") == event_type]
        assert len(precheck_events) == 1
        payload = precheck_events[0]["redacted_payload"]
        assert "assessment_id" in payload
        assert "decision" in payload
        assert "score" in payload
        assert "engine_version" in payload
        assert "evaluation_latency_ms" in payload

    async def test_consumed_session_returns_existing_assessment(self, orchestrator):
        orch, repo, assessment_repo, audit_repo, evaluator, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.CONSUMED)

        # Create existing assessment
        existing_assessment = RiskAssessment(
            schema_version="1.0",
            assessment_id=uuid4(),
            risk_session_id=session_id,
            mandate_event_id=None,
            feature_snapshot_id=uuid4(),
            stage=DecisionStage.PRECHECK,
            score=30,
            decision=Decision.ALLOW,
            engine_version="rules-v1",
            rule_evaluations=[
                RuleEvaluation(
                    rule_id="test_rule",
                    triggered=True,
                    points=30,
                    reason_code="TEST",
                    reason_text="Test rule",
                )
            ],
            evaluation_latency_ms=5,
            assessed_at=datetime.now(UTC),
        )
        assessment_repo.assessments[session_id] = existing_assessment

        result = await orch.assess(session_id)

        assert result.is_replay is True
        assert result.assessment.assessment_id == existing_assessment.assessment_id
        # Evaluator should not be called again
        assert evaluator.call_count == 0
        # No new audit event
        event_type = "risk_session.precheck_completed"
        precheck_events = [e for e in audit_repo.events if e.get("event_type") == event_type]
        assert len(precheck_events) == 0

    async def test_consumed_without_assessment_raises_inconsistent(self, orchestrator):
        orch, repo, assessment_repo, _, _, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.CONSUMED)
        # No assessment in repo

        with pytest.raises(RiskSessionInconsistent):
            await orch.assess(session_id)

    async def test_wrong_stage_raises_invalid(self, orchestrator):
        orch, repo, _, _, evaluator, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.READY)

        # Make evaluator return POST_CONFIRMATION assessment
        async def bad_assess(db_session, risk_session):
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

        evaluator.assess = bad_assess

        with pytest.raises(InvalidPrecheckAssessment):
            await orch.assess(session_id)

    async def test_wrong_session_id_raises_invalid(self, orchestrator):
        orch, repo, _, _, evaluator, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.READY)

        # Make evaluator return mismatched session ID
        async def bad_assess(db_session, risk_session):
            return RiskAssessment(
                schema_version="1.0",
                assessment_id=uuid4(),
                risk_session_id=uuid4(),  # Different!
                mandate_event_id=None,
                feature_snapshot_id=uuid4(),
                stage=DecisionStage.PRECHECK,
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

        evaluator.assess = bad_assess

        with pytest.raises(InvalidPrecheckAssessment):
            await orch.assess(session_id)

    async def test_non_null_mandate_event_raises_invalid(self, orchestrator):
        orch, repo, _, _, evaluator, _ = orchestrator
        session_id = uuid4()
        repo.sessions[session_id] = self._make_session(session_id, RiskSessionStatus.READY)

        async def bad_assess(db_session, risk_session):
            return RiskAssessment(
                schema_version="1.0",
                assessment_id=uuid4(),
                risk_session_id=risk_session.risk_session_id,
                mandate_event_id=uuid4(),  # Should be None!
                feature_snapshot_id=uuid4(),
                stage=DecisionStage.PRECHECK,
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

        evaluator.assess = bad_assess

        with pytest.raises(InvalidPrecheckAssessment):
            await orch.assess(session_id)
