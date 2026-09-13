"""A3 integration tests: webhook route security and idempotency (PostgreSQL)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.contracts.action import ActionRequest
from app.contracts.common import ActionStatus, ActionType, Decision, DecisionStage
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.main import create_app
from app.persistence.models import ActionRequest as ActionRequestORM
from app.persistence.models import MandateWebhookEvent as MandateWebhookEventORM
from app.persistence.models import OutboxMessage
from app.persistence.models import RiskAssessment as RiskAssessmentORM
from app.repositories import (
    ActionRepository,
    FeatureSnapshotRepository,
    RiskAssessmentRepository,
)
from app.services.mandate_evaluator_port import UnavailableMandateEvaluator
from app.services.webhook_event_parser import RazorpayWebhookParser
from app.services.webhook_pseudonymisation import VpaPseudonymizer
from app.services.webhook_signature import WebhookSignatureVerifier
from tests.helpers.webhook_signing import (
    TEST_WEBHOOK_SECRET,
    load_webhook_fixture,
    sign_body,
)


@pytest.fixture
def app(test_session_factory):
    """Create app wired to the NullPool test engine with a configured verifier."""
    application = create_app()
    application.state.session_factory = test_session_factory
    application.state.webhook_signature_verifier = WebhookSignatureVerifier(TEST_WEBHOOK_SECRET)
    application.state.webhook_vpa_pseudonymizer = VpaPseudonymizer("test-pepper-1234567890")
    application.state.webhook_parser = RazorpayWebhookParser(
        application.state.webhook_vpa_pseudonymizer
    )
    application.state.mandate_event_evaluator = UnavailableMandateEvaluator()
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _post_webhook(
    client: TestClient,
    raw_body: bytes,
    signature: str | None,
    event_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers: dict[str, str] = {"content-type": "application/json"}
    if signature is not None:
        headers["x-razorpay-signature"] = signature
    if event_id is not None:
        headers["x-razorpay-event-id"] = event_id
    if extra_headers:
        headers.update(extra_headers)
    return client.post("/v1/webhooks/razorpay", content=raw_body, headers=headers)


def _count_rows(test_session_factory, model: Any) -> int:
    async def _query() -> int:
        async with test_session_factory() as session:
            result = await session.execute(select(func.count()).select_from(model))
            return int(result.scalar_one())

    return asyncio.run(_query())


def _confirmed_body(**overrides: Any) -> bytes:
    body: dict[str, Any] = {
        "entity": "event",
        "account_id": "acc_demo_123",
        "event": "token.confirmed",
        "contains": ["token"],
        "payload": {
            "token": {
                "entity": {
                    "id": "token_demo123",
                    "entity": "token",
                    "method": "upi",
                    "recurring_status": "confirmed",
                    "notes": [],
                    "vpa": {"username": "demo.user", "handle": "upi"},
                }
            }
        },
        "created_at": 1705312920,
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def test_valid_signed_fixture_is_accepted_exactly_once(client, test_session_factory) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    response = _post_webhook(client, raw, sign_body(raw), "evt_demo_confirmed_001")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "processed"
    assert data["duplicate"] is False
    assert data["correlation"] == "unavailable"
    assert data["evaluation"] == "not_configured"
    assert response.headers.get("Cache-Control") == "no-store"
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 1


def test_replay_of_same_signed_payload_is_idempotent_with_replay_header(
    client, test_session_factory
) -> None:
    raw = _confirmed_body()
    first = _post_webhook(client, raw, sign_body(raw), "evt_replay_001")
    second = _post_webhook(client, raw, sign_body(raw), "evt_replay_001")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["duplicate"] is True
    assert second.json()["mandate_event_id"] == first.json()["mandate_event_id"]
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 1


def test_replay_creates_no_second_assessment_or_revoke_job(
    client, test_session_factory, app
) -> None:
    from datetime import UTC, datetime

    class RecordingEvaluator:
        def __init__(self) -> None:
            self.call_count = 0

        async def assess_snapshot(self, db_session, event: MandateWebhookEvent) -> None:
            self.call_count += 1
            snapshot = await FeatureSnapshotRepository(db_session).create(
                FeatureSnapshot(
                    schema_version="1.0",
                    feature_snapshot_id=uuid4(),
                    mandate_event_id=event.mandate_event_id,
                    risk_session_id=event.risk_session_id,
                    feature_version="rules-v1",
                    calculated_at=datetime.now(UTC),
                    is_demo_simulation=False,
                    sources={},
                )
            )
            assessment = await RiskAssessmentRepository(db_session).create(
                RiskAssessment(
                    schema_version="1.0",
                    assessment_id=uuid4(),
                    risk_session_id=event.risk_session_id,
                    mandate_event_id=event.mandate_event_id,
                    feature_snapshot_id=snapshot.feature_snapshot_id,
                    stage=DecisionStage.POST_CONFIRMATION,
                    score=85,
                    decision=Decision.BLOCK,
                    engine_version="rules-v1",
                    rule_evaluations=[
                        RuleEvaluation(
                            rule_id="test_rule",
                            triggered=True,
                            points=85,
                            reason_code="TEST",
                            reason_text="Test rule",
                        )
                    ],
                    evaluation_latency_ms=10,
                    assessed_at=datetime.now(UTC),
                )
            )
            await ActionRepository(db_session).create_request(
                ActionRequest(
                    schema_version="1.0",
                    action_request_id=uuid4(),
                    assessment_id=assessment.assessment_id,
                    mandate_event_id=event.mandate_event_id,
                    action_type=ActionType.TOKEN_REVOKE,
                    token_id=event.token_id,
                    idempotency_key=f"revoke-{event.token_id}-{assessment.assessment_id.hex[:8]}",
                    status=ActionStatus.QUEUED,
                    requested_at=datetime.now(UTC),
                ),
                outbox_payload={"token_id": event.token_id},
            )

        async def evaluate(self, db_session, event: MandateWebhookEvent) -> None:
            await self.assess_snapshot(db_session, event)

    evaluator = RecordingEvaluator()
    app.state.mandate_event_evaluator = evaluator

    raw = _confirmed_body()
    first = _post_webhook(client, raw, sign_body(raw), "evt_revoke_001")
    second = _post_webhook(client, raw, sign_body(raw), "evt_revoke_001")

    assert first.status_code == 200
    assert first.json()["evaluation"] == "completed"
    assert second.json()["status"] == "duplicate"
    assert evaluator.call_count == 1
    assert _count_rows(test_session_factory, RiskAssessmentORM) == 1
    assert _count_rows(test_session_factory, ActionRequestORM) == 1
    assert _count_rows(test_session_factory, OutboxMessage) == 1


def test_invalid_signature_is_rejected_401_before_persistence(client, test_session_factory) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    response = _post_webhook(client, raw, "0" * 64, "evt_bad_sig_001")

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid webhook signature"}
    assert response.headers.get("Cache-Control") == "no-store"
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_missing_signature_is_rejected_401(client, test_session_factory) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    response = _post_webhook(client, raw, None, "evt_no_sig_001")

    assert response.status_code == 401
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_tampered_body_after_signing_is_rejected_401(client, test_session_factory) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    signature = sign_body(raw)
    tampered = raw.replace(b"token_demo123", b"token_demo999")
    response = _post_webhook(client, tampered, signature, "evt_tamper_001")

    assert response.status_code == 401
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_unconfigured_secret_fails_closed_503(client, test_session_factory, app) -> None:
    app.state.webhook_signature_verifier = WebhookSignatureVerifier(None)
    raw = load_webhook_fixture("token_confirmed_body.json")
    response = _post_webhook(client, raw, sign_body(raw), "evt_nosecret_001")

    assert response.status_code == 503
    assert response.json() == {"detail": "webhook signature verification is not configured"}
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_malformed_json_with_valid_signature_is_400(client, test_session_factory) -> None:
    raw = b'{"event": "token.confirmed", '
    response = _post_webhook(client, raw, sign_body(raw), "evt_malformed_001")

    assert response.status_code == 400
    assert response.json() == {"detail": "webhook body is not valid JSON"}
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_unrelated_event_type_is_acked_ignored_with_no_rows(client, test_session_factory) -> None:
    raw = _confirmed_body(event="payment.captured")
    response = _post_webhook(client, raw, sign_body(raw), "evt_unrelated_001")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["event_type"] == "payment.captured"
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_non_upi_method_is_acked_ignored_with_no_rows(client, test_session_factory) -> None:
    parsed = json.loads(_confirmed_body())
    parsed["payload"]["token"]["entity"]["method"] = "card"
    raw = json.dumps(parsed).encode("utf-8")
    response = _post_webhook(client, raw, sign_body(raw), "evt_card_001")

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "unsupported method"
    assert _count_rows(test_session_factory, MandateWebhookEventORM) == 0


def test_notes_risk_session_id_correlates_to_risk_session(client, test_session_factory) -> None:
    created = client.post(
        "/v1/risk-sessions",
        json={
            "merchant_namespace": "demo-merchant-01",
            "checkout_order_ref": "order_webhook_001",
            "customer_reference": "cust-001",
            "device_fingerprint": "fp-secret-001",
            "flow_started_at": "2026-01-15T10:00:00Z",
            "mandate_intent": {
                "max_amount_paise": 500000,
                "frequency": "monthly",
                "expire_at": "2027-01-15T10:00:00Z",
            },
        },
        headers={"user-agent": "test-agent/1.0"},
    )
    assert created.status_code == 201
    session_id = created.json()["risk_session_id"]

    parsed = json.loads(_confirmed_body())
    parsed["payload"]["token"]["entity"]["notes"] = {"risk_session_id": session_id}
    raw = json.dumps(parsed).encode("utf-8")
    response = _post_webhook(client, raw, sign_body(raw), "evt_correlated_001")

    assert response.status_code == 200
    assert response.json()["correlation"] == "correlated"

    async def _stored_session_id() -> str | None:
        async with test_session_factory() as session:
            result = await session.execute(
                select(MandateWebhookEventORM.risk_session_id).where(
                    MandateWebhookEventORM.provider_event_id == "evt_correlated_001"
                )
            )
            row_id = result.scalar_one_or_none()
            return str(row_id) if row_id is not None else None

    assert asyncio.run(_stored_session_id()) == session_id


def test_absent_correlation_is_persisted_with_null_session(client, test_session_factory) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    response = _post_webhook(client, raw, sign_body(raw), "evt_uncorrelated_001")

    assert response.status_code == 200
    assert response.json()["correlation"] == "unavailable"

    async def _stored_session_id() -> str | None:
        async with test_session_factory() as session:
            result = await session.execute(
                select(MandateWebhookEventORM.risk_session_id).where(
                    MandateWebhookEventORM.provider_event_id == "evt_uncorrelated_001"
                )
            )
            return result.scalar_one_or_none()

    assert asyncio.run(_stored_session_id()) is None


def test_unknown_notes_uuid_is_persisted_as_unresolved(client) -> None:
    parsed = json.loads(_confirmed_body())
    parsed["payload"]["token"]["entity"]["notes"] = {"risk_session_id": str(uuid4())}
    raw = json.dumps(parsed).encode("utf-8")
    response = _post_webhook(client, raw, sign_body(raw), "evt_unresolved_001")

    assert response.status_code == 200
    assert response.json()["correlation"] == "unresolved"


def test_responses_set_no_store_and_preserve_request_id(client) -> None:
    custom_id = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    headers = {"x-request-id": custom_id}
    raw = _confirmed_body()

    processed = _post_webhook(client, raw, sign_body(raw), "evt_hdr_001", headers)
    assert processed.headers.get("Cache-Control") == "no-store"
    assert processed.headers.get("X-Request-ID") == custom_id

    rejected = _post_webhook(client, raw, "0" * 64, "evt_hdr_002", headers)
    assert rejected.status_code == 401
    assert rejected.headers.get("Cache-Control") == "no-store"
    assert rejected.headers.get("X-Request-ID") == custom_id


def test_no_raw_vpa_signature_or_body_in_any_response_or_log(
    client, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "zz-unique-vpa-user-99"
    signature_marker = sign_body(_confirmed_body())
    parsed = json.loads(_confirmed_body())
    parsed["payload"]["token"]["entity"]["vpa"] = {"username": marker, "handle": "upi"}
    raw = json.dumps(parsed).encode("utf-8")

    with caplog.at_level("INFO"):
        processed = _post_webhook(client, raw, sign_body(raw), "evt_redact_001")
        duplicate = _post_webhook(client, raw, sign_body(raw), "evt_redact_001")

    assert processed.status_code == 200
    assert marker not in processed.text
    assert marker not in duplicate.text
    assert signature_marker not in processed.text
    assert raw.decode("utf-8") not in processed.text
    log_text = caplog.text
    assert marker not in log_text
    assert signature_marker not in log_text


def test_raw_payload_sha256_matches_signed_bytes(client, test_session_factory) -> None:
    raw = load_webhook_fixture("token_confirmed_body.json")
    _post_webhook(client, raw, sign_body(raw), "evt_sha_001")

    async def _stored_sha() -> str | None:
        async with test_session_factory() as session:
            result = await session.execute(
                select(MandateWebhookEventORM.raw_payload_sha256).where(
                    MandateWebhookEventORM.provider_event_id == "evt_sha_001"
                )
            )
            return result.scalar_one_or_none()

    assert asyncio.run(_stored_sha()) == "sha256:" + hashlib.sha256(raw).hexdigest()
