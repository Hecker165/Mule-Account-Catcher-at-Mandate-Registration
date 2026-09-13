"""A8 integration tests: redacted projections, audit chains and verification."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.contracts.action import ActionRequest
from app.contracts.common import (
    ActionStatus,
    ActionType,
    AuditActorType,
    Decision,
    DecisionStage,
    MandateEventType,
)
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_assessment import RiskAssessment, RuleEvaluation
from app.contracts.risk_session import MandateIntent, RiskSession
from app.main import create_app
from app.repositories import (
    ActionRepository,
    AuditRepository,
    FeatureSnapshotRepository,
    MandateEventRepository,
    RiskAssessmentRepository,
    RiskSessionRepository,
)

NOW = datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)
RAW_MARKER = "zz-raw-marker-never-shown"


@pytest.fixture
def app(test_session_factory):
    """Create app wired to the NullPool test engine."""
    application = create_app()
    application.state.session_factory = test_session_factory
    return application


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


async def _seed_assessment(
    session_factory,
    decision: Decision = Decision.ALLOW,
    score: int = 10,
    stage: DecisionStage = DecisionStage.POST_CONFIRMATION,
    with_action: bool = False,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Seed session/event/snapshot/assessment (+ optional action) via A1 repos."""
    async with session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(
            RiskSession(
                schema_version="1.0",
                risk_session_id=uuid4(),
                merchant_namespace="demo_merchant_one",
                checkout_order_ref=f"order_{uuid4().hex[:8]}",
                flow_started_at=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
                mandate_intent=MandateIntent(max_amount_paise=500000, frequency="monthly"),
                status="READY",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        event = MandateWebhookEvent(
            schema_version="1.0",
            mandate_event_id=uuid4(),
            provider="razorpay",
            provider_event_id=f"evt_dash_{uuid4().hex[:8]}",
            event_type=MandateEventType.TOKEN_CONFIRMED,
            token_id="token_demo123",
            risk_session_id=stored_session.risk_session_id,
            vpa_hash="hmac-sha256:" + "12" * 32,
            vpa_handle="upi",
            failure_reason=failure_reason,
            provider_created_at=NOW,
            received_at=NOW,
            raw_payload_sha256="sha256:" + "b" * 64,
            is_demo_event=False,
        )
        await MandateEventRepository(session).create_or_get(event)
        snapshot = await FeatureSnapshotRepository(session).create(
            FeatureSnapshot(
                schema_version="1.0",
                feature_snapshot_id=uuid4(),
                mandate_event_id=event.mandate_event_id,
                risk_session_id=stored_session.risk_session_id,
                feature_version="rules-v1",
                calculated_at=NOW,
                sources={},
                is_demo_simulation=False,
            )
        )
        triggered = decision == Decision.BLOCK
        assessment = await RiskAssessmentRepository(session).create(
            RiskAssessment(
                schema_version="1.0",
                assessment_id=uuid4(),
                risk_session_id=stored_session.risk_session_id,
                mandate_event_id=event.mandate_event_id,
                feature_snapshot_id=snapshot.feature_snapshot_id,
                stage=stage,
                score=score,
                decision=decision,
                engine_version="rules-v1",
                rule_evaluations=[
                    RuleEvaluation(
                        rule_id="velocity_device_burst",
                        triggered=triggered,
                        points=25 if triggered else 0,
                        reason_code="DEVICE_VELOCITY_BURST" if triggered else None,
                        reason_text="Device made 7 registrations in 5 minutes."
                        if triggered
                        else None,
                    )
                ],
                evaluation_latency_ms=12,
                assessed_at=NOW,
            )
        )
        await AuditRepository(session).append(
            aggregate_type="risk_assessment",
            aggregate_id=assessment.assessment_id,
            event_type="risk_assessment.completed",
            actor_type=AuditActorType.SYSTEM,
            actor_id="risk_engine",
            occurred_at=NOW,
            redacted_payload={"assessment_id": str(assessment.assessment_id)},
        )
        action_id = None
        if with_action:
            request = ActionRequest(
                schema_version="1.0",
                action_request_id=uuid4(),
                assessment_id=assessment.assessment_id,
                mandate_event_id=event.mandate_event_id,
                action_type=ActionType.TOKEN_REVOKE,
                token_id=event.token_id,
                idempotency_key=f"revoke-{event.mandate_event_id}",
                status=ActionStatus.SUCCEEDED,
                requested_at=NOW,
            )
            await ActionRepository(session).create_request(request, {"token_id": event.token_id})
            action_id = request.action_request_id
        await session.commit()
        return {"assessment": assessment, "event": event, "action_id": action_id}


def test_dashboard_assessments_route_returns_redacted_projection(
    client: TestClient, test_session_factory
) -> None:
    seeded = asyncio.run(_seed_assessment(test_session_factory, Decision.ALLOW, 10))
    response = client.get("/v1/dashboard/assessments?limit=100")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 100
    assert data["offset"] == 0
    items = [
        item
        for item in data["items"]
        if item["assessment_id"] == str(seeded["assessment"].assessment_id)
    ]
    assert len(items) == 1
    item = items[0]
    assert item["assessment_id"] == str(seeded["assessment"].assessment_id)
    assert item["decision"] == "ALLOW"
    assert item["score"] == 10
    assert item["stage"] == "POST_CONFIRMATION"
    assert item["token_id"] == "token_demo123"
    assert item["vpa_handle"] == "upi"
    assert item["is_demo_event"] is False
    assert item["action_status"] is None
    assert item["evaluation_latency_ms"] == 12
    assert item["reasons"] == []
    assert response.headers.get("Cache-Control") == "no-store"


def test_dashboard_route_maps_only_listed_fields(client: TestClient, test_session_factory) -> None:
    asyncio.run(_seed_assessment(test_session_factory, Decision.BLOCK, 80, with_action=True))
    item = client.get("/v1/dashboard/assessments").json()["items"][0]
    assert set(item) == {
        "assessment_id",
        "decision",
        "score",
        "stage",
        "assessed_at",
        "evaluation_latency_ms",
        "token_id",
        "vpa_handle",
        "is_demo_event",
        "action_status",
        "reasons",
    }
    assert item["action_status"] == "SUCCEEDED"
    assert item["reasons"] == [
        {
            "rule_id": "velocity_device_burst",
            "reason_code": "DEVICE_VELOCITY_BURST",
            "reason_text": "Device made 7 registrations in 5 minutes.",
            "points": 25,
        }
    ]


def test_dashboard_route_includes_latency_from_assessment_contract(
    client: TestClient, test_session_factory
) -> None:
    seeded = asyncio.run(_seed_assessment(test_session_factory))
    wanted = str(seeded["assessment"].assessment_id)
    items = client.get("/v1/dashboard/assessments?limit=100").json()["items"]
    item = next(item for item in items if item["assessment_id"] == wanted)
    assert item["evaluation_latency_ms"] == 12


def test_dashboard_limit_is_validated_1_to_100(client: TestClient) -> None:
    assert client.get("/v1/dashboard/assessments?limit=0").status_code == 422
    assert client.get("/v1/dashboard/assessments?limit=101").status_code == 422
    assert client.get("/v1/dashboard/assessments?limit=1").status_code == 200


def test_dashboard_decision_filter_filters(client: TestClient, test_session_factory) -> None:
    allow = asyncio.run(_seed_assessment(test_session_factory, Decision.ALLOW, 10))
    block = asyncio.run(_seed_assessment(test_session_factory, Decision.BLOCK, 80))
    allow_id = str(allow["assessment"].assessment_id)
    block_id = str(block["assessment"].assessment_id)
    blocked = client.get("/v1/dashboard/assessments?decision=BLOCK&limit=100").json()["items"]
    blocked_ids = {item["assessment_id"] for item in blocked}
    assert block_id in blocked_ids
    assert allow_id not in blocked_ids
    assert all(item["decision"] == "BLOCK" for item in blocked)
    allowed = client.get("/v1/dashboard/assessments?decision=ALLOW&limit=100").json()["items"]
    allowed_ids = {item["assessment_id"] for item in allowed}
    assert allow_id in allowed_ids
    assert block_id not in allowed_ids


def test_dashboard_offset_paginates(client: TestClient, test_session_factory) -> None:
    for _ in range(3):
        asyncio.run(_seed_assessment(test_session_factory))
    full = client.get("/v1/dashboard/assessments?limit=100").json()["items"]
    first_page = client.get("/v1/dashboard/assessments?limit=2&offset=0").json()
    second_page = client.get("/v1/dashboard/assessments?limit=2&offset=2").json()
    assert [item["assessment_id"] for item in first_page["items"]] == [
        item["assessment_id"] for item in full[:2]
    ]
    assert [item["assessment_id"] for item in second_page["items"]] == [
        item["assessment_id"] for item in full[2:4]
    ]
    assert second_page["offset"] == 2
    first_ids = {item["assessment_id"] for item in first_page["items"]}
    assert all(item["assessment_id"] not in first_ids for item in second_page["items"])


def test_dashboard_route_never_contains_hashes_or_payload_json(
    client: TestClient, test_session_factory
) -> None:
    asyncio.run(
        _seed_assessment(test_session_factory, failure_reason=f"{RAW_MARKER} failure detail")
    )
    body = client.get("/v1/dashboard/assessments?limit=100").text
    assert "hmac-sha256:" not in body
    assert "sha256:" not in body
    assert RAW_MARKER not in body


def test_audit_events_route_lists_chain_newest_first(
    client: TestClient, test_session_factory
) -> None:
    seeded = asyncio.run(_seed_assessment(test_session_factory))
    assessment_id = str(seeded["assessment"].assessment_id)
    response = client.get(
        f"/v1/dashboard/audit-events?aggregate_type=risk_assessment&aggregate_id={assessment_id}"
    )
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1
    assert events[0]["event_type"] == "risk_assessment.completed"
    assert events[0]["sequence_number"] == 1
    assert response.headers.get("Cache-Control") == "no-store"


def test_audit_verify_route_reports_valid_chain(client: TestClient, test_session_factory) -> None:
    seeded = asyncio.run(_seed_assessment(test_session_factory))
    assessment_id = str(seeded["assessment"].assessment_id)
    response = client.get(
        "/v1/dashboard/audit-events/verify?aggregate_type=risk_assessment"
        f"&aggregate_id={assessment_id}"
    )
    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "checked_events": 1,
        "first_invalid_sequence": None,
        "reason": None,
    }


def test_audit_verify_route_reports_tampered_chain(
    client: TestClient, test_session_factory
) -> None:
    seeded = asyncio.run(_seed_assessment(test_session_factory))
    assessment_id = seeded["assessment"].assessment_id

    async def _tamper() -> None:
        async with test_session_factory() as session:
            await session.execute(
                text("UPDATE audit_events SET event_hash = '00' WHERE aggregate_id = :id"),
                {"id": assessment_id},
            )
            await session.commit()

    asyncio.run(_tamper())
    response = client.get(
        "/v1/dashboard/audit-events/verify?aggregate_type=risk_assessment"
        f"&aggregate_id={assessment_id}"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is False
    assert data["first_invalid_sequence"] == 1


def test_unknown_aggregate_type_is_rejected_422(client: TestClient) -> None:
    response = client.get(f"/v1/dashboard/audit-events?aggregate_type=bogus&aggregate_id={uuid4()}")
    assert response.status_code == 422


def test_missing_aggregate_verifies_as_valid_zero(client: TestClient) -> None:
    response = client.get(
        f"/v1/dashboard/audit-events/verify?aggregate_type=risk_assessment&aggregate_id={uuid4()}"
    )
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["checked_events"] == 0


def test_dashboard_routes_are_get_only_and_set_no_store(client: TestClient) -> None:
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/v1/dashboard/assessments")
        assert response.status_code == 405
    for path in (
        "/v1/dashboard/assessments",
        f"/v1/dashboard/audit-events?aggregate_type=risk_assessment&aggregate_id={uuid4()}",
        f"/v1/dashboard/audit-events/verify?aggregate_type=risk_assessment&aggregate_id={uuid4()}",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("X-Request-ID") is not None
