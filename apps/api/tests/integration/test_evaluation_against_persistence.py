"""A5 integration tests: evaluation against real A1 repos and Redis extractor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import redis.asyncio as async_redis
from sqlalchemy import func, select

from app.contracts.common import Decision, DecisionStage, MandateEventType
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_session import MandateIntent, RiskSession
from app.domain.rules.catalogue import DEFAULT_RULES
from app.domain.rules.engine import RuleEngine
from app.persistence.models import ActionRequest as ActionRequestORM
from app.persistence.models import OutboxMessage as OutboxMessageORM
from app.repositories import (
    AuditRepository,
    FeatureSnapshotRepository,
    MandateEventRepository,
    OutboxRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)
from app.services.features.extractor import FeatureExtractor
from app.services.features.redis_store import RedisFeatureStore
from app.services.risk_evaluation import MandateEvaluationService, PrecheckEvaluationService
from tests.helpers.redis_test_client import make_test_redis

NOW = datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)
IP_HASH = "hmac-sha256:" + "ab" * 32
DEVICE_HASH = "hmac-sha256:" + "cd" * 32
CUSTOMER_HASH = "hmac-sha256:" + "ef" * 32
VPA_HASH = "hmac-sha256:" + "12" * 32


@pytest.fixture
async def redis_client() -> Any:
    client = make_test_redis()
    yield client
    await client.aclose()


@pytest.fixture
def services(redis_client: async_redis.Redis) -> tuple[Any, Any]:
    extractor = FeatureExtractor(RedisFeatureStore(redis_client, key_prefix="mg:int"))
    engine = RuleEngine()
    clock = lambda: NOW  # noqa: E731
    return (
        MandateEvaluationService(extractor=extractor, engine=engine, clock=clock),
        PrecheckEvaluationService(extractor=extractor, engine=engine, clock=clock),
    )


def _session(**overrides: Any) -> RiskSession:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "risk_session_id": uuid4(),
        "merchant_namespace": "demo_merchant_one",
        "checkout_order_ref": f"order_{uuid4().hex[:8]}",
        "customer_reference_hash": CUSTOMER_HASH,
        "ip_hash": IP_HASH,
        "device_fingerprint_hash": DEVICE_HASH,
        "user_agent_hash": "hmac-sha256:" + "99" * 32,
        "flow_started_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "flow_completed_at": datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
        "mandate_intent": MandateIntent(
            max_amount_paise=500000,
            frequency="monthly",
            expire_at=datetime(2027, 1, 15, 10, 0, 0, tzinfo=UTC),
        ),
        "status": "READY",
        "created_at": datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return RiskSession(**base)


def _event(
    event_type: MandateEventType = MandateEventType.TOKEN_CONFIRMED,
    session_id: Any = None,
    failure_reason: str | None = None,
    index: int = 0,
) -> MandateWebhookEvent:
    return MandateWebhookEvent(
        schema_version="1.0",
        mandate_event_id=uuid4(),
        provider="razorpay",
        provider_event_id=f"evt_eval_{index}_{uuid4().hex[:8]}",
        event_type=event_type,
        token_id="token_demo123",
        risk_session_id=session_id,
        vpa_hash=VPA_HASH,
        vpa_handle="upi",
        failure_reason=failure_reason,
        provider_created_at=datetime(2026, 1, 15, 10, 2, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        raw_payload_sha256="sha256:" + "b" * 64,
        is_demo_event=False,
    )


async def test_full_webhook_evaluation_persists_and_reloads_assessment_with_rule_rows(
    test_session_factory, services: tuple[Any, Any]
) -> None:
    mandate_service, _ = services
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        event = _event(session_id=stored_session.risk_session_id)
        await MandateEventRepository(session).create_or_get(event)
        await mandate_service.evaluate(session, event)
        await session.commit()
        event_id = event.mandate_event_id

    async with test_session_factory() as session:
        reloaded = await RiskAssessmentRepository(session).get_for_event(
            event_id, DecisionStage.POST_CONFIRMATION, "rules-v1"
        )
        assert reloaded is not None
        assert reloaded.decision == Decision.ALLOW
        assert [e.rule_id for e in reloaded.rule_evaluations] == [r.rule_id for r in DEFAULT_RULES]
        assert all(e.points >= 0 for e in reloaded.rule_evaluations)
        verification = await AuditRepository(session).verify_aggregate(
            "risk_assessment", reloaded.assessment_id
        )
        assert verification.valid is True


async def test_revoke_request_and_outbox_commit_atomically(
    test_session_factory, services: tuple[Any, Any]
) -> None:
    mandate_service, _ = services
    last_event_id = None
    for index in range(10):
        async with test_session_factory() as session:
            # Fresh session per event (one snapshot per session per version),
            # but identical hashes so velocities accumulate on shared keys.
            stored_session = await RiskSessionRepository(session).create(_session())
            event = _event(session_id=stored_session.risk_session_id, index=index)
            await MandateEventRepository(session).create_or_get(event)
            await mandate_service.evaluate(session, event)
            await session.commit()
            last_event_id = event.mandate_event_id

    async with test_session_factory() as session:
        assert last_event_id is not None
        assessment = await RiskAssessmentRepository(session).get_for_event(
            last_event_id, DecisionStage.POST_CONFIRMATION, "rules-v1"
        )
        assert assessment is not None
        assert assessment.decision == Decision.BLOCK
        assert assessment.score >= 70
        action_count = await session.execute(select(func.count()).select_from(ActionRequestORM))
        assert action_count.scalar_one() == 1
        assert await OutboxRepository(session).get_pending_count() == 1
        action_row = (await session.execute(select(ActionRequestORM).limit(1))).scalar_one()
        assert action_row.token_id == "token_demo123"
        verification = await AuditRepository(session).verify_aggregate(
            "action_request", action_row.action_request_id
        )
        assert verification.valid is True


async def test_rejected_event_creates_no_action_rows(
    test_session_factory, services: tuple[Any, Any]
) -> None:
    mandate_service, _ = services
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        event = _event(
            MandateEventType.TOKEN_REJECTED,
            session_id=stored_session.risk_session_id,
            failure_reason="Mandate rejected: NPCI risk flag raised by payer bank",
        )
        await MandateEventRepository(session).create_or_get(event)
        await mandate_service.evaluate(session, event)
        await session.commit()
        event_id = event.mandate_event_id

    async with test_session_factory() as session:
        assessment = await RiskAssessmentRepository(session).get_for_event(
            event_id, DecisionStage.REJECTION_AUDIT, "rules-v1"
        )
        assert assessment is not None
        assert assessment.decision == Decision.BLOCK
        assert assessment.score == 100
        action_count = await session.execute(select(func.count()).select_from(ActionRequestORM))
        assert action_count.scalar_one() == 0
        outbox_count = await session.execute(select(func.count()).select_from(OutboxMessageORM))
        assert outbox_count.scalar_one() == 0


async def test_precheck_assessment_links_session_and_snapshot(
    test_session_factory, services: tuple[Any, Any]
) -> None:
    _, precheck_service = services
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        assessment = await precheck_service.assess(session, stored_session)
        await session.commit()
        assessment_id = assessment.assessment_id
        session_id = stored_session.risk_session_id

    async with test_session_factory() as session:
        reloaded = await RiskAssessmentRepository(session).get(assessment_id)
        assert reloaded is not None
        assert reloaded.stage == DecisionStage.PRECHECK
        assert reloaded.mandate_event_id is None
        assert reloaded.risk_session_id == session_id
        snapshot = await FeatureSnapshotRepository(session).get(reloaded.feature_snapshot_id)
        assert snapshot is not None
        assert snapshot.risk_session_id == session_id
        untouched = await RiskSessionRepository(session).get(session_id)
        assert untouched is not None
        assert untouched.status == "READY"


async def test_rule_evaluation_rows_preserve_position_and_reasons(
    test_session_factory, services: tuple[Any, Any]
) -> None:
    mandate_service, _ = services
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        event = _event(
            MandateEventType.TOKEN_REJECTED,
            session_id=stored_session.risk_session_id,
            failure_reason="Mandate rejected: NPCI risk flag",
        )
        await MandateEventRepository(session).create_or_get(event)
        await mandate_service.evaluate(session, event)
        await session.commit()
        event_id = event.mandate_event_id

    async with test_session_factory() as session:
        assessment = await RiskAssessmentRepository(session).get_for_event(
            event_id, DecisionStage.REJECTION_AUDIT, "rules-v1"
        )
        assert assessment is not None
        assert [e.rule_id for e in assessment.rule_evaluations] == [
            r.rule_id for r in DEFAULT_RULES
        ]
        triggered = [e for e in assessment.rule_evaluations if e.triggered]
        assert [e.rule_id for e in triggered] == [
            "new_device_for_established_customer",
            "npci_risk_rejection",
        ]
        assert triggered[1].reason_code == "NPCI_RISK_REJECTION"
        assert triggered[1].reason_text is not None
        assert len(triggered[1].reason_text) <= 240
        for evaluation in assessment.rule_evaluations:
            if not evaluation.triggered:
                assert evaluation.points == 0
                assert evaluation.reason_code is None
