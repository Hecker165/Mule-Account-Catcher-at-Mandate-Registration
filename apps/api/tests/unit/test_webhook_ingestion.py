"""A3 unit tests: idempotent ingestion, correlation and evaluator hand-off."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from app.contracts.common import AuditActorType, MandateEventType
from app.contracts.mandate_event import MandateWebhookEvent
from app.services.mandate_evaluator_port import (
    UnavailableMandateEvaluator,
)
from app.services.webhook_ingestion import IngestResult, WebhookIngestionService

FIXED_NOW = datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC)


class FakeMandateEventRepository:
    def __init__(self) -> None:
        self.events: dict[tuple[str, str], MandateWebhookEvent] = {}

    @property
    def session(self) -> Any:
        return AsyncMock()

    async def create_or_get(self, event: MandateWebhookEvent) -> tuple[MandateWebhookEvent, bool]:
        key = (event.provider, event.provider_event_id)
        if key in self.events:
            return self.events[key], False
        self.events[key] = event
        return event, True


class FakeAuditRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class FakeRiskSessionRepository:
    def __init__(self) -> None:
        self.by_id: dict[UUID, Any] = {}
        self.by_order: dict[tuple[str, str], Any] = {}

    async def get(self, risk_session_id: UUID) -> Any | None:
        return self.by_id.get(risk_session_id)

    async def get_by_order_ref(
        self, merchant_namespace: str, checkout_order_ref: str
    ) -> Any | None:
        return self.by_order.get((merchant_namespace, checkout_order_ref))


class FakeSession:
    def __init__(self, risk_session_id: UUID) -> None:
        self.risk_session_id = risk_session_id


class FakeEvaluator:
    def __init__(self) -> None:
        self.call_count = 0

    async def evaluate(self, db_session: Any, event: MandateWebhookEvent) -> None:
        self.call_count += 1


def _event(provider_event_id: str = "evt_unit_001") -> MandateWebhookEvent:
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
        received_at=FIXED_NOW,
        raw_payload_sha256="sha256:" + "b" * 64,
        is_demo_event=False,
    )


@pytest.fixture
def repos() -> tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository]:
    return FakeMandateEventRepository(), FakeAuditRepository(), FakeRiskSessionRepository()


def _service(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
    evaluator: Any,
) -> WebhookIngestionService:
    mandate_events, audit, risk_sessions = repos
    return WebhookIngestionService(
        mandate_events=mandate_events,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        risk_sessions=risk_sessions,  # type: ignore[arg-type]
        evaluator=evaluator,
        clock=lambda: FIXED_NOW,
    )


async def test_first_delivery_persists_event_and_appends_exactly_one_audit_event(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    service = _service(repos, FakeEvaluator())
    result = await service.ingest(_event(), None, None, None)

    assert isinstance(result, IngestResult)
    assert result.is_duplicate is False
    assert result.correlation_status == "unavailable"
    assert result.evaluation_status == "completed"
    assert len(repos[1].events) == 1
    audit_event = repos[1].events[0]
    assert audit_event["event_type"] == "mandate_event.received"
    assert audit_event["aggregate_type"] == "mandate_event"
    assert audit_event["aggregate_id"] == result.mandate_event_id
    assert audit_event["actor_type"] == AuditActorType.WEBHOOK
    assert audit_event["actor_id"] == "razorpay_webhook"


async def test_duplicate_delivery_returns_original_without_audit_or_evaluation(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    evaluator = FakeEvaluator()
    service = _service(repos, evaluator)
    first = await service.ingest(_event("evt_dup_001"), None, None, None)
    second = await service.ingest(_event("evt_dup_001"), None, None, None)

    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.mandate_event_id == first.mandate_event_id
    assert second.evaluation_status == "skipped_duplicate"
    assert evaluator.call_count == 1
    assert len(repos[1].events) == 1


async def test_unavailable_evaluator_still_persists_event_with_not_configured_status(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    service = _service(repos, UnavailableMandateEvaluator())
    result = await service.ingest(_event(), None, None, None)

    assert result.is_duplicate is False
    assert result.evaluation_status == "not_configured"
    assert len(repos[1].events) == 1
    assert repos[1].events[0]["redacted_payload"]["evaluation_status"] == "not_configured"


async def test_unexpected_evaluator_error_marks_transaction_for_rollback(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    class ExplodingEvaluator:
        async def evaluate(self, db_session: Any, event: MandateWebhookEvent) -> None:
            raise RuntimeError("boom")

    service = _service(repos, ExplodingEvaluator())
    with pytest.raises(RuntimeError, match="boom"):
        await service.ingest(_event(), None, None, None)
    assert len(repos[1].events) == 0


async def test_notes_uuid_correlation_links_existing_session(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    session_id = uuid4()
    repos[2].by_id[session_id] = FakeSession(session_id)
    service = _service(repos, FakeEvaluator())

    result = await service.ingest(_event(), session_id, None, None)

    assert result.correlation_status == "correlated"
    stored = repos[0].events[("razorpay", "evt_unit_001")]
    assert stored.risk_session_id == session_id


async def test_unknown_notes_uuid_yields_unresolved_and_null_link(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    service = _service(repos, FakeEvaluator())
    result = await service.ingest(_event(), uuid4(), None, None)

    assert result.correlation_status == "unresolved"
    stored = repos[0].events[("razorpay", "evt_unit_001")]
    assert stored.risk_session_id is None


async def test_order_mapping_correlation_links_session(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    session_id = uuid4()
    repos[2].by_order[("demo-merchant-01", "order_demo_001")] = FakeSession(session_id)
    service = _service(repos, FakeEvaluator())

    result = await service.ingest(_event(), None, "demo-merchant-01", "order_demo_001")

    assert result.correlation_status == "correlated"
    assert repos[0].events[("razorpay", "evt_unit_001")].risk_session_id == session_id


async def test_absent_notes_yields_unavailable_correlation(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    service = _service(repos, FakeEvaluator())
    result = await service.ingest(_event(), None, None, None)
    assert result.correlation_status == "unavailable"


async def test_audit_payload_contains_fixed_keys_only_and_no_raw_values(
    repos: tuple[FakeMandateEventRepository, FakeAuditRepository, FakeRiskSessionRepository],
) -> None:
    service = _service(repos, FakeEvaluator())
    await service.ingest(_event(), None, None, None)

    payload = repos[1].events[0]["redacted_payload"]
    assert set(payload) == {
        "event_type",
        "provider",
        "token_id",
        "correlation_status",
        "risk_session_id",
        "is_demo_event",
        "vpa_handle",
        "failure_reason_present",
        "evaluation_status",
    }
    assert payload["event_type"] == "token.confirmed"
    assert payload["provider"] == "razorpay"
    assert payload["failure_reason_present"] is False
    assert "demo.user" not in str(payload)
