"""A6 integration tests: worker flows against real PostgreSQL."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.action import ActionRequest
from app.contracts.common import (
    ActionStatus,
    ActionType,
    Decision,
    DecisionStage,
    MandateEventType,
)
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import MandateIntent, RiskSession
from app.integrations.razorpay.mock_client import MockTokenRevokeClient
from app.integrations.razorpay.types import RevokeOutcome
from app.persistence.models import ActionAttempt as ActionAttemptORM
from app.persistence.models import ActionRequest as ActionRequestORM
from app.repositories import (
    ActionRepository,
    AuditRepository,
    FeatureSnapshotRepository,
    MandateEventRepository,
    OutboxRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)
from app.repositories.action_attempt_read import ActionAttemptReadRepository
from app.workers.config import WorkerConfig
from app.workers.outbox_worker import OutboxWorker

T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


@asynccontextmanager
async def _tx(test_session_factory):  # type: ignore[no-untyped-def]
    """Transaction wrapper over the test session factory (mirrors A1 semantics)."""
    async with test_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _session() -> RiskSession:
    return RiskSession(
        schema_version="1.0",
        risk_session_id=uuid4(),
        merchant_namespace="demo_merchant_one",
        checkout_order_ref=f"order_{uuid4().hex[:8]}",
        flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        mandate_intent=MandateIntent(max_amount_paise=500000, frequency="monthly"),
        status="READY",
        created_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
    )


def _event(session_id: UUID) -> MandateWebhookEvent:
    return MandateWebhookEvent(
        schema_version="1.0",
        mandate_event_id=uuid4(),
        provider="razorpay",
        provider_event_id=f"evt_a6_{uuid4().hex[:8]}",
        event_type=MandateEventType.TOKEN_CONFIRMED,
        token_id="token_demo123",
        risk_session_id=session_id,
        provider_created_at=datetime(2026, 1, 15, 10, 2, 0, tzinfo=UTC),
        received_at=datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        raw_payload_sha256="sha256:" + "b" * 64,
        is_demo_event=False,
    )


async def _seed_request(
    session: AsyncSession, now: datetime, token_id: str = "token_demo123"
) -> tuple[UUID, UUID]:
    """Seed session/event/snapshot/assessment/action+outbox in A5's exact shape."""
    stored_session = await RiskSessionRepository(session).create(_session())
    event = _event(stored_session.risk_session_id)
    await MandateEventRepository(session).create_or_get(event)
    snapshot = await FeatureSnapshotRepository(session).create(
        FeatureSnapshot(
            schema_version="1.0",
            feature_snapshot_id=uuid4(),
            mandate_event_id=event.mandate_event_id,
            risk_session_id=stored_session.risk_session_id,
            feature_version="rules-v1",
            calculated_at=now,
            sources={},
            is_demo_simulation=False,
        )
    )
    assessment = await RiskAssessmentRepository(session).create(
        RiskAssessment(
            schema_version="1.0",
            assessment_id=uuid4(),
            risk_session_id=stored_session.risk_session_id,
            mandate_event_id=event.mandate_event_id,
            feature_snapshot_id=snapshot.feature_snapshot_id,
            stage=DecisionStage.POST_CONFIRMATION,
            score=80,
            decision=Decision.BLOCK,
            engine_version="rules-v1",
            rule_evaluations=[
                RuleEvaluation(
                    rule_id="velocity_device_burst",
                    triggered=True,
                    points=25,
                    reason_code="DEVICE_VELOCITY_BURST",
                    reason_text="Device made 7 registrations in 5 minutes (9 in 1 hour).",
                )
            ],
            evaluation_latency_ms=5,
            assessed_at=now,
        )
    )
    request = ActionRequest(
        schema_version="1.0",
        action_request_id=uuid4(),
        assessment_id=assessment.assessment_id,
        mandate_event_id=event.mandate_event_id,
        action_type=ActionType.TOKEN_REVOKE,
        token_id=token_id,
        idempotency_key=f"revoke-{event.mandate_event_id}",
        status=ActionStatus.QUEUED,
        requested_at=now,
    )
    await ActionRepository(session).create_request(
        request,
        {
            "action_request_id": str(request.action_request_id),
            "mandate_event_id": str(event.mandate_event_id),
            "token_id": token_id,
            "idempotency_key": request.idempotency_key,
            "action_type": "TOKEN_REVOKE",
            "requested_at": now.isoformat().replace("+00:00", "Z"),
        },
    )
    return request.action_request_id, event.mandate_event_id


def _worker(
    test_session_factory,
    mock: MockTokenRevokeClient,
    now_box: list,
    worker_id: str = "test-worker-1",
    **overrides: Any,
) -> OutboxWorker:
    params: dict[str, Any] = {
        "revoke_client": mock,
        "clock": lambda: now_box[0],
        "session_factory": lambda: _tx(test_session_factory),
    }
    if worker_id != "test-worker-1":
        params["config"] = WorkerConfig(worker_id=worker_id)
    params.update(overrides)
    return OutboxWorker(**params)  # type: ignore[arg-type]


async def test_end_to_end_revoke_success_flow(test_session_factory) -> None:
    mock = MockTokenRevokeClient()
    now_box = [T0]
    async with test_session_factory() as session:
        request_id, _ = await _seed_request(session, T0)
        await session.commit()

    worker = _worker(test_session_factory, mock, now_box)
    assert await worker.process_pending_once() == 1

    async with test_session_factory() as session:
        request = await ActionRepository(session).get_request(request_id)
        assert request is not None
        assert request.status == ActionStatus.SUCCEEDED
        assert await OutboxRepository(session).get_pending_count() == 0
        attempts = await ActionAttemptReadRepository(session).count_attempts(request_id)
        assert attempts == 1
        events = await AuditRepository(session).list_for_aggregate("action_request", request_id)
        assert [event.event_type for event in events] == ["action_attempt.succeeded"]
    assert len(mock.recorded_calls) == 1


async def test_retry_then_success_flow_across_cycles(test_session_factory) -> None:
    mock = MockTokenRevokeClient()
    mock.schedule_failures(1, RevokeOutcome.RETRYABLE, 503)
    now_box = [T0]
    async with test_session_factory() as session:
        request_id, _ = await _seed_request(session, T0)
        await session.commit()

    worker = _worker(test_session_factory, mock, now_box)
    assert await worker.process_pending_once() == 1
    async with test_session_factory() as session:
        request = await ActionRepository(session).get_request(request_id)
        assert request is not None
        assert request.status == ActionStatus.RETRYING

    now_box[0] += timedelta(seconds=6)
    assert await worker.process_pending_once() == 1
    async with test_session_factory() as session:
        request = await ActionRepository(session).get_request(request_id)
        assert request is not None
        assert request.status == ActionStatus.SUCCEEDED
    calls = mock.recorded_calls
    assert len(calls) == 2
    assert calls[0].idempotency_key == calls[1].idempotency_key


async def test_two_workers_never_process_the_same_message(test_session_factory) -> None:
    async with test_session_factory() as session:
        for _ in range(10):
            await _seed_request(session, T0)
        await session.commit()

    now_box = [T0]
    mock = MockTokenRevokeClient()
    first = _worker(test_session_factory, mock, now_box, worker_id="worker-a")
    second = _worker(test_session_factory, mock, now_box, worker_id="worker-b")
    processed = await asyncio.gather(
        first.process_pending_once(10), second.process_pending_once(10)
    )
    assert sum(processed) == 10

    async with test_session_factory() as session:
        total = await session.execute(select(func.count()).select_from(ActionAttemptORM))
        assert total.scalar_one() == 10
        rows = (await session.execute(select(ActionRequestORM))).scalars().all()
        assert len(rows) == 10
        for row in rows:
            count = await ActionAttemptReadRepository(session).count_attempts(row.action_request_id)
            assert count == 1


async def test_dead_letter_after_permanent_failure(test_session_factory) -> None:
    mock = MockTokenRevokeClient()
    mock.schedule_failures(3, RevokeOutcome.PERMANENT, 400)
    now_box = [T0]
    async with test_session_factory() as session:
        request_id, _ = await _seed_request(session, T0)
        await session.commit()

    worker = _worker(test_session_factory, mock, now_box)
    assert await worker.process_pending_once() == 1
    async with test_session_factory() as session:
        request = await ActionRepository(session).get_request(request_id)
        assert request is not None
        assert request.status == ActionStatus.FAILED
        assert await OutboxRepository(session).get_dead_letter_count() == 1
    assert len(mock.recorded_calls) == 1
    assert await worker.process_pending_once() == 0
    assert len(mock.recorded_calls) == 1


async def test_max_attempts_dead_letters_after_eight_attempts(test_session_factory) -> None:
    mock = MockTokenRevokeClient()
    mock.schedule_failures(20, RevokeOutcome.RETRYABLE, 503)
    now_box = [T0]
    async with test_session_factory() as session:
        request_id, _ = await _seed_request(session, T0)
        await session.commit()

    worker = _worker(test_session_factory, mock, now_box)
    for _ in range(9):
        await worker.process_pending_once()
        now_box[0] += timedelta(seconds=400)

    async with test_session_factory() as session:
        request = await ActionRepository(session).get_request(request_id)
        assert request is not None
        assert request.status == ActionStatus.FAILED
        assert await ActionAttemptReadRepository(session).count_attempts(request_id) == 8
        assert await OutboxRepository(session).get_dead_letter_count() == 1
    assert len(mock.recorded_calls) == 8


async def test_audit_chain_for_action_aggregate_verifies(test_session_factory) -> None:
    mock = MockTokenRevokeClient()
    now_box = [T0]
    async with test_session_factory() as session:
        request_id, _ = await _seed_request(session, T0)
        await session.commit()

    await _worker(test_session_factory, mock, now_box).process_pending_once()
    async with test_session_factory() as session:
        verification = await AuditRepository(session).verify_aggregate("action_request", request_id)
        assert verification.valid is True
        assert verification.checked_events == 1


async def test_worker_cycle_after_success_is_a_noop(test_session_factory) -> None:
    mock = MockTokenRevokeClient()
    now_box = [T0]
    async with test_session_factory() as session:
        await _seed_request(session, T0)
        await session.commit()

    worker = _worker(test_session_factory, mock, now_box)
    assert await worker.process_pending_once() == 1
    assert await worker.process_pending_once() == 0
    assert len(mock.recorded_calls) == 1
