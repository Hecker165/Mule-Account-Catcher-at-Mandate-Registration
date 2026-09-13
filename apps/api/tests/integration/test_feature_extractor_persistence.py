"""A4 integration tests: extracted snapshots persist via real A1 repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import redis.asyncio as async_redis

from app.contracts.common import Availability, MandateEventType
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_session import MandateIntent, RiskSession
from app.repositories import (
    FeatureSnapshotRepository,
    MandateEventRepository,
    RiskSessionRepository,
)
from app.services.features.extractor import FeatureExtractor
from app.services.features.redis_store import RedisFeatureStore
from tests.helpers.redis_test_client import make_test_redis

NOW = datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)
IP_HASH = "hmac-sha256:" + "ab" * 32
DEVICE_HASH = "hmac-sha256:" + "cd" * 32
CUSTOMER_HASH = "hmac-sha256:" + "ef" * 32


@pytest.fixture
async def redis_client() -> Any:
    client = make_test_redis()
    yield client
    await client.aclose()


def _session(**overrides: Any) -> RiskSession:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "risk_session_id": uuid4(),
        "merchant_namespace": "demo_merchant_one",
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


def _event(**overrides: Any) -> MandateWebhookEvent:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "mandate_event_id": uuid4(),
        "provider": "razorpay",
        "provider_event_id": f"evt_persist_{uuid4().hex[:8]}",
        "event_type": MandateEventType.TOKEN_CONFIRMED,
        "token_id": "token_demo123",
        "risk_session_id": None,
        "vpa_hash": "hmac-sha256:" + "12" * 32,
        "vpa_handle": "upi",
        "provider_created_at": datetime(2026, 1, 15, 10, 2, 0, tzinfo=UTC),
        "received_at": datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        "raw_payload_sha256": "sha256:" + "b" * 64,
        "is_demo_event": False,
    }
    base.update(overrides)
    return MandateWebhookEvent(**base)


async def test_extracted_event_snapshot_persists_and_reloads(
    test_session_factory, redis_client: async_redis.Redis
) -> None:
    extractor = FeatureExtractor(RedisFeatureStore(redis_client, key_prefix="mg:int"))
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        event = _event()
        await MandateEventRepository(session).create_or_get(event)
        snapshot = await extractor.extract_for_event(event, stored_session, NOW)
        persisted = await FeatureSnapshotRepository(session).create(snapshot)
        await session.commit()
        snapshot_id = persisted.feature_snapshot_id
        event_id = event.mandate_event_id

    async with test_session_factory() as session:
        reloaded = await FeatureSnapshotRepository(session).get(snapshot_id)
        latest = await FeatureSnapshotRepository(session).get_latest_for_event(event_id, "rules-v1")
        assert reloaded is not None and latest is not None
        assert reloaded.feature_snapshot_id == latest.feature_snapshot_id
        assert reloaded.feature_version == snapshot.feature_version == "rules-v1"
        assert reloaded.ip_velocity_5m == snapshot.ip_velocity_5m == 1
        assert reloaded.mandate_event_id == event_id
        assert reloaded.risk_session_id == stored_session.risk_session_id
        for field, source in snapshot.sources.items():
            assert reloaded.sources[field].availability == source.availability


async def test_extracted_session_snapshot_persists_with_risk_session_link(
    test_session_factory, redis_client: async_redis.Redis
) -> None:
    extractor = FeatureExtractor(RedisFeatureStore(redis_client, key_prefix="mg:int"))
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        snapshot = await extractor.extract_for_session(stored_session, NOW)
        persisted = await FeatureSnapshotRepository(session).create(snapshot)
        await session.commit()
        snapshot_id = persisted.feature_snapshot_id

    async with test_session_factory() as session:
        reloaded = await FeatureSnapshotRepository(session).get(snapshot_id)
        assert reloaded is not None
        assert reloaded.mandate_event_id is None
        assert reloaded.risk_session_id == stored_session.risk_session_id
        assert reloaded.device_velocity_1h == 1
        assert reloaded.is_new_device_for_customer is True


async def test_demo_snapshot_satisfies_demo_source_contract_rule(
    test_session_factory, redis_client: async_redis.Redis
) -> None:
    extractor = FeatureExtractor(RedisFeatureStore(redis_client, key_prefix="mg:int"))
    async with test_session_factory() as session:
        stored_session = await RiskSessionRepository(session).create(_session())
        event = _event(is_demo_event=True)
        await MandateEventRepository(session).create_or_get(event)
        snapshot = await extractor.extract_for_event(event, stored_session, NOW)
        await FeatureSnapshotRepository(session).create(snapshot)
        await session.commit()

    assert snapshot.is_demo_simulation is True
    assert snapshot.shared_demo_merchant_count_1h is not None
    assert (
        snapshot.sources["shared_demo_merchant_count_1h"].availability
        == Availability.DEMO_SIMULATED
    )
