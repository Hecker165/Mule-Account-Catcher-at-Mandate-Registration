"""A5 unit tests: evaluation services with fakes — persistence and revoke rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.contracts.common import (
    Availability,
    Decision,
    DecisionStage,
    FeatureSource,
    MandateEventType,
)
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment
from app.contracts.risk_session import MandateIntent, RiskSession
from app.domain.rules.engine import RuleEngine, RuleEngineResult
from app.services.risk_evaluation import (
    EvaluationInvariantError,
    MandateEvaluationService,
    PrecheckEvaluationService,
)

NOW = datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)


def _session(session_id: UUID | None = None) -> RiskSession:
    return RiskSession(
        schema_version="1.0",
        risk_session_id=session_id or uuid4(),
        merchant_namespace="demo_merchant_one",
        mandate_intent=MandateIntent(max_amount_paise=500000, frequency="monthly"),
        status="READY",
        created_at=NOW,
        updated_at=NOW,
    )


def _event(
    event_type: MandateEventType = MandateEventType.TOKEN_CONFIRMED,
    session_id: UUID | None = None,
) -> MandateWebhookEvent:
    return MandateWebhookEvent(
        schema_version="1.0",
        mandate_event_id=uuid4(),
        provider="razorpay",
        provider_event_id=f"evt_{uuid4().hex[:8]}",
        event_type=event_type,
        token_id="token_demo123",
        risk_session_id=session_id,
        provider_created_at=NOW,
        received_at=NOW,
        raw_payload_sha256="sha256:" + "b" * 64,
        is_demo_event=False,
    )


def _snapshot(**overrides: Any) -> FeatureSnapshot:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "feature_snapshot_id": uuid4(),
        "mandate_event_id": uuid4(),
        "risk_session_id": uuid4(),
        "feature_version": "rules-v1",
        "calculated_at": NOW,
        "sources": {},
        "is_demo_simulation": False,
    }
    base.update(overrides)
    return FeatureSnapshot(**base)


def _hot_snapshot() -> FeatureSnapshot:
    return _snapshot(
        device_velocity_5m=7,
        device_velocity_1h=9,
        is_new_device_for_customer=True,
        flow_duration_seconds=2,
        shared_demo_merchant_count_1h=3,
        is_demo_simulation=True,
        sources={
            "shared_demo_merchant_count_1h": FeatureSource(
                availability=Availability.DEMO_SIMULATED,
                source="redis_demo_shared_merchants",
                captured_at=NOW,
            )
        },
    )


class FakeSnapshotRepository:
    def __init__(self, fail: bool = False) -> None:
        self.created: list[FeatureSnapshot] = []
        self.fail = fail

    async def create(self, snapshot: FeatureSnapshot) -> FeatureSnapshot:
        if self.fail:
            raise IntegrityError("INSERT", {}, Exception("duplicate snapshot"))
        self.created.append(snapshot)
        return snapshot


class FakeAssessmentRepository:
    def __init__(self) -> None:
        self.created: list[RiskAssessment] = []

    async def create(self, assessment: RiskAssessment) -> RiskAssessment:
        self.created.append(assessment)
        return assessment


class FakeAuditRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class FakeActionRepository:
    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.outbox_payloads: list[dict[str, Any] | None] = []

    async def create_request(
        self, request: Any, outbox_payload: dict[str, Any] | None = None
    ) -> Any:
        self.requests.append(request)
        self.outbox_payloads.append(outbox_payload)
        return request


class FakeSessionRepository:
    def __init__(self, sessions: dict[UUID, RiskSession] | None = None) -> None:
        self.sessions = sessions or {}
        self.get_calls = 0

    async def get(self, risk_session_id: UUID) -> RiskSession | None:
        self.get_calls += 1
        return self.sessions.get(risk_session_id)


class FakeExtractor:
    def __init__(self, snapshot: FeatureSnapshot) -> None:
        self.snapshot = snapshot
        self.event_calls = 0
        self.session_calls = 0

    async def extract_for_event(
        self, event: MandateWebhookEvent, risk_session: RiskSession | None, now: datetime
    ) -> FeatureSnapshot:
        self.event_calls += 1
        return self.snapshot

    async def extract_for_session(
        self, risk_session: RiskSession, now: datetime
    ) -> FeatureSnapshot:
        self.session_calls += 1
        return self.snapshot


class _Fakes:
    def __init__(self, snapshot: FeatureSnapshot) -> None:
        self.snapshots = FakeSnapshotRepository()
        self.assessments = FakeAssessmentRepository()
        self.audits = FakeAuditRepository()
        self.actions = FakeActionRepository()
        self.sessions = FakeSessionRepository()
        self.extractor = FakeExtractor(snapshot)
        self.db_session = AsyncMock()


def _mandate_service(fakes: _Fakes, engine: Any = None) -> MandateEvaluationService:
    return MandateEvaluationService(
        extractor=fakes.extractor,  # type: ignore[arg-type]
        engine=engine or RuleEngine(),
        clock=lambda: NOW,
        uuid_factory=uuid4,
        snapshot_repository=lambda session: fakes.snapshots,  # type: ignore[return-value]
        assessment_repository=lambda session: fakes.assessments,  # type: ignore[return-value]
        action_repository=lambda session: fakes.actions,  # type: ignore[return-value]
        audit_repository=lambda session: fakes.audits,  # type: ignore[return-value]
        session_repository=lambda session: fakes.sessions,  # type: ignore[return-value]
    )


def _precheck_service(fakes: _Fakes, engine: Any = None) -> PrecheckEvaluationService:
    return PrecheckEvaluationService(
        extractor=fakes.extractor,  # type: ignore[arg-type]
        engine=engine or RuleEngine(),
        clock=lambda: NOW,
        uuid_factory=uuid4,
        snapshot_repository=lambda session: fakes.snapshots,  # type: ignore[return-value]
        assessment_repository=lambda session: fakes.assessments,  # type: ignore[return-value]
        audit_repository=lambda session: fakes.audits,  # type: ignore[return-value]
    )


async def test_mandate_evaluation_persists_snapshot_assessment_and_one_audit_event() -> None:
    fakes = _Fakes(_snapshot())
    event = _event()
    await _mandate_service(fakes).evaluate(fakes.db_session, event)

    assert len(fakes.snapshots.created) == 1
    assert len(fakes.assessments.created) == 1
    assessment = fakes.assessments.created[0]
    assert assessment.stage == DecisionStage.POST_CONFIRMATION
    assert assessment.mandate_event_id == event.mandate_event_id
    assert assessment.decision == Decision.ALLOW
    assert len(fakes.audits.events) == 1
    assert fakes.audits.events[0]["event_type"] == "risk_assessment.completed"
    assert len(fakes.actions.requests) == 0


async def test_blocked_confirmed_event_queues_exactly_one_revoke_request_and_outbox_message() -> (
    None
):
    fakes = _Fakes(_hot_snapshot())
    event = _event()
    await _mandate_service(fakes).evaluate(fakes.db_session, event)

    assessment = fakes.assessments.created[0]
    assert assessment.decision == Decision.BLOCK
    assert assessment.score == 95
    assert len(fakes.actions.requests) == 1
    request = fakes.actions.requests[0]
    assert request.mandate_event_id == event.mandate_event_id
    assert request.assessment_id == assessment.assessment_id
    assert request.token_id == event.token_id
    assert fakes.actions.outbox_payloads[0] is not None
    assert fakes.actions.outbox_payloads[0]["token_id"] == event.token_id
    audit_types = [event_["event_type"] for event_ in fakes.audits.events]
    assert audit_types == ["risk_assessment.completed", "action_request.created"]


async def test_allowed_confirmed_event_queues_no_revoke() -> None:
    fakes = _Fakes(_snapshot())
    await _mandate_service(fakes).evaluate(fakes.db_session, _event())
    assert fakes.assessments.created[0].decision == Decision.ALLOW
    assert len(fakes.actions.requests) == 0
    assert len(fakes.audits.events) == 1


async def test_rejected_block_event_records_assessment_but_never_queues_a_revoke() -> None:
    fakes = _Fakes(_snapshot(npci_risk_flag=True))
    event = _event(MandateEventType.TOKEN_REJECTED)
    await _mandate_service(fakes).evaluate(fakes.db_session, event)

    assessment = fakes.assessments.created[0]
    assert assessment.stage == DecisionStage.REJECTION_AUDIT
    assert assessment.decision == Decision.BLOCK
    assert assessment.score == 100
    assert len(fakes.actions.requests) == 0
    assert len(fakes.audits.events) == 1


async def test_cancelled_event_is_treated_as_rejection_audit() -> None:
    fakes = _Fakes(_snapshot())
    event = _event(MandateEventType.TOKEN_CANCELLED)
    await _mandate_service(fakes).evaluate(fakes.db_session, event)
    assert fakes.assessments.created[0].stage == DecisionStage.REJECTION_AUDIT


async def test_uncorrelated_event_produces_assessment_with_null_session_link() -> None:
    fakes = _Fakes(_snapshot())
    await _mandate_service(fakes).evaluate(fakes.db_session, _event(session_id=None))
    assert fakes.assessments.created[0].risk_session_id is None
    assert fakes.sessions.get_calls == 0

    unknown = _event(session_id=uuid4())
    fakes2 = _Fakes(_snapshot())
    await _mandate_service(fakes2).evaluate(fakes2.db_session, unknown)
    assert fakes2.assessments.created[0].risk_session_id == unknown.risk_session_id


async def test_precheck_assessment_has_stage_precheck_and_session_link() -> None:
    fakes = _Fakes(_snapshot())
    session = _session()
    assessment = await _precheck_service(fakes).assess(fakes.db_session, session)

    assert assessment.stage == DecisionStage.PRECHECK
    assert assessment.risk_session_id == session.risk_session_id
    assert assessment.mandate_event_id is None
    assert assessment.decision in (Decision.ALLOW, Decision.CHALLENGE)
    assert len(fakes.snapshots.created) == 1
    assert len(fakes.assessments.created) == 1


async def test_precheck_service_never_touches_the_session_row() -> None:
    fakes = _Fakes(_snapshot())
    session = _session()
    before = session.model_dump()
    await _precheck_service(fakes).assess(fakes.db_session, session)
    assert session.model_dump() == before
    assert not hasattr(_precheck_service(fakes), "_sessions")


async def test_audit_payloads_contain_fixed_keys_only() -> None:
    fakes = _Fakes(_hot_snapshot())
    await _mandate_service(fakes).evaluate(fakes.db_session, _event())

    assessment_payload = fakes.audits.events[0]["redacted_payload"]
    assert set(assessment_payload) == {
        "assessment_id",
        "stage",
        "decision",
        "score",
        "engine_version",
        "evaluation_latency_ms",
        "triggered_rule_count",
    }
    assert assessment_payload["triggered_rule_count"] == 4
    action_payload = fakes.audits.events[1]["redacted_payload"]
    assert set(action_payload) == {
        "action_type",
        "token_id",
        "idempotency_key",
        "assessment_id",
    }
    assert "hmac-sha256" not in str(assessment_payload)


async def test_evaluation_latency_ms_is_present_and_bounded() -> None:
    fakes = _Fakes(_snapshot())
    await _mandate_service(fakes).evaluate(fakes.db_session, _event())
    latency = fakes.assessments.created[0].evaluation_latency_ms
    assert isinstance(latency, int)
    assert 0 <= latency <= 60_000


async def test_invariant_violation_raises_and_persists_nothing() -> None:
    class BadEngine:
        engine_version = "rules-v1"

        def evaluate(self, snapshot: FeatureSnapshot, stage: DecisionStage) -> RuleEngineResult:
            from app.contracts.risk_assessment import RuleEvaluation

            return RuleEngineResult(
                score=50,
                decision=Decision.CHALLENGE,
                rule_evaluations=(RuleEvaluation(rule_id="bad_rule", triggered=True, points=50),),
            )

    fakes = _Fakes(_snapshot())
    with pytest.raises(EvaluationInvariantError):
        await _mandate_service(fakes, engine=BadEngine()).evaluate(fakes.db_session, _event())
    assert len(fakes.snapshots.created) == 0
    assert len(fakes.assessments.created) == 0
    assert len(fakes.audits.events) == 0


async def test_duplicate_snapshot_integrity_error_propagates() -> None:
    fakes = _Fakes(_snapshot())
    fakes.snapshots = FakeSnapshotRepository(fail=True)
    with pytest.raises(IntegrityError):
        await _mandate_service(fakes).evaluate(fakes.db_session, _event())
    assert len(fakes.assessments.created) == 0
    assert len(fakes.audits.events) == 0
