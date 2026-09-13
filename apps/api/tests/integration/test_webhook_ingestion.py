"""A3 integration tests: ingestion transactional guarantees (PostgreSQL)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.contracts.common import MandateEventType
from app.contracts.mandate_event import MandateWebhookEvent
from app.persistence.models import MandateWebhookEvent as MandateWebhookEventORM
from app.repositories import AuditRepository, MandateEventRepository, RiskSessionRepository
from app.services.mandate_evaluator_port import UnavailableMandateEvaluator
from app.services.webhook_event_parser import RazorpayWebhookParser
from app.services.webhook_ingestion import WebhookIngestionService
from app.services.webhook_pseudonymisation import VpaPseudonymizer
from tests.helpers.webhook_signing import load_webhook_fixture


def _event(provider_event_id: str = "evt_ingest_001") -> MandateWebhookEvent:
    return MandateWebhookEvent(
        schema_version="1.0",
        mandate_event_id=uuid4(),
        provider="razorpay",
        provider_event_id=provider_event_id,
        event_type=MandateEventType.TOKEN_CONFIRMED,
        token_id="token_demo123",
        risk_session_id=None,
        vpa_hash=None,
        vpa_handle="upi",
        provider_created_at=datetime(2024, 1, 15, 10, 2, tzinfo=UTC),
        received_at=datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        raw_payload_sha256="sha256:" + "c" * 64,
        is_demo_event=False,
    )


async def test_create_or_get_is_idempotent_against_real_database(test_session_factory) -> None:
    async with test_session_factory() as session:
        repo = MandateEventRepository(session)
        stored, created = await repo.create_or_get(_event("evt_idem_001"))
        assert created is True
        await session.commit()

    async with test_session_factory() as session:
        repo = MandateEventRepository(session)
        same, created = await repo.create_or_get(_event("evt_idem_001"))
        assert created is False
        assert same.mandate_event_id == stored.mandate_event_id
        await session.commit()

    async with test_session_factory() as session:
        count = await session.execute(
            select(func.count())
            .select_from(MandateWebhookEventORM)
            .where(MandateWebhookEventORM.provider_event_id == "evt_idem_001")
        )
        assert count.scalar_one() == 1


async def test_audit_event_chain_is_built_for_mandate_event_aggregate(
    test_session_factory,
) -> None:
    async with test_session_factory() as session:
        service = WebhookIngestionService(
            mandate_events=MandateEventRepository(session),
            audit=AuditRepository(session),
            risk_sessions=RiskSessionRepository(session),
            evaluator=UnavailableMandateEvaluator(),
            clock=lambda: datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        )
        result = await service.ingest(_event("evt_chain_001"), None, None, None)
        await session.commit()

    async with test_session_factory() as session:
        events = await AuditRepository(session).list_for_aggregate(
            "mandate_event", result.mandate_event_id
        )
        assert len(events) == 1
        assert events[0].event_type == "mandate_event.received"
        verification = await AuditRepository(session).verify_aggregate(
            "mandate_event", result.mandate_event_id
        )
        assert verification.valid is True


async def test_rollback_on_evaluator_error_leaves_no_event_and_no_audit_rows(
    test_session_factory,
) -> None:
    class ExplodingEvaluator:
        async def evaluate(self, db_session: Any, event: MandateWebhookEvent) -> None:
            raise RuntimeError("evaluator exploded")

    async with test_session_factory() as session:
        service = WebhookIngestionService(
            mandate_events=MandateEventRepository(session),
            audit=AuditRepository(session),
            risk_sessions=RiskSessionRepository(session),
            evaluator=ExplodingEvaluator(),  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        )
        with pytest.raises(RuntimeError, match="evaluator exploded"):
            await service.ingest(_event("evt_rollback_001"), None, None, None)
        await session.rollback()

    async with test_session_factory() as session:
        event_count = await session.execute(
            select(func.count())
            .select_from(MandateWebhookEventORM)
            .where(MandateWebhookEventORM.provider_event_id == "evt_rollback_001")
        )
        assert event_count.scalar_one() == 0
        audit_count = await session.execute(
            text("SELECT COUNT(*) FROM audit_events WHERE event_type = 'mandate_event.received'")
        )
        assert audit_count.scalar_one() == 0


async def test_raw_payload_sha256_matches_body_and_body_is_never_persisted(
    test_session_factory,
) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    parser = RazorpayWebhookParser(VpaPseudonymizer("test-pepper-1234567890"))
    parsed = parser.classify_and_parse(raw, "evt_rawsha_001", False)
    assert parsed.event is not None

    async with test_session_factory() as session:
        service = WebhookIngestionService(
            mandate_events=MandateEventRepository(session),
            audit=AuditRepository(session),
            risk_sessions=RiskSessionRepository(session),
            evaluator=UnavailableMandateEvaluator(),
            clock=lambda: datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        )
        result = await service.ingest(
            parsed.event,
            parsed.notes_risk_session_id,
            parsed.notes_merchant_namespace,
            parsed.notes_checkout_order_ref,
        )
        await session.commit()

    async with test_session_factory() as session:
        stored = await MandateEventRepository(session).get(result.mandate_event_id)
        assert stored is not None
        assert stored.raw_payload_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert "demo.user" not in stored.model_dump_json()
        row = await session.execute(
            select(MandateWebhookEventORM).where(
                MandateWebhookEventORM.provider_event_id == "evt_rawsha_001"
            )
        )
        orm_obj = row.scalar_one()
        assert raw.decode("utf-8") not in str(orm_obj.vpa_hash)
