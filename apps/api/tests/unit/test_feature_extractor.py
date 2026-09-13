"""A4 unit tests: deterministic FeatureSnapshot extraction (fakeredis)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import redis.exceptions
from fakeredis.aioredis import FakeRedis as FakeAsyncRedis

from app.contracts.common import Availability, MandateEventType
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_session import MandateIntent, RiskSession
from app.services.features.extractor import FeatureExtractor
from app.services.features.redis_store import FeatureStoreUnavailable, RedisFeatureStore
from app.services.features.reputation import ReputationResult

NOW = datetime(2026, 1, 15, 10, 5, 0, tzinfo=UTC)
IP_HASH = "hmac-sha256:" + "ab" * 32
DEVICE_HASH = "hmac-sha256:" + "cd" * 32
CUSTOMER_HASH = "hmac-sha256:" + "ef" * 32
VPA_HASH = "hmac-sha256:" + "12" * 32

EXPECTED_FIELDS = {
    "ip_velocity_5m",
    "ip_velocity_1h",
    "device_velocity_5m",
    "device_velocity_1h",
    "customer_velocity_1h",
    "vpa_velocity_1h",
    "shared_demo_merchant_count_1h",
    "is_new_device_for_customer",
    "flow_duration_seconds",
    "user_agent_bot_suspected",
    "network_vpn_or_proxy",
    "network_reputation_score",
    "npci_risk_flag",
    "payer_bank_or_compliance_flag",
    "max_amount_paise",
    "mandate_frequency",
    "expiry_days_from_registration",
}


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
        "provider_event_id": "evt_test_001",
        "event_type": MandateEventType.TOKEN_CONFIRMED,
        "token_id": "token_demo123",
        "risk_session_id": None,
        "vpa_hash": VPA_HASH,
        "vpa_handle": "upi",
        "provider_created_at": datetime(2026, 1, 15, 10, 2, 0, tzinfo=UTC),
        "received_at": datetime(2026, 1, 15, 10, 2, 1, tzinfo=UTC),
        "raw_payload_sha256": "sha256:" + "b" * 64,
        "is_demo_event": False,
    }
    base.update(overrides)
    return MandateWebhookEvent(**base)


@pytest.fixture
def extractor() -> FeatureExtractor:
    store = RedisFeatureStore(FakeAsyncRedis(), key_prefix="mg:test")
    return FeatureExtractor(
        store, uuid_factory=lambda: UUID("c3d4e5f6-a7b8-4c9d-0e1f-2a3b4c5d6e7f")
    )


async def test_correlated_confirmed_event_builds_full_valid_snapshot(
    extractor: FeatureExtractor,
) -> None:
    session = _session(merchant_namespace="prod_merchant")
    snapshot = await extractor.extract_for_event(_event(), session, NOW)

    assert snapshot.mandate_event_id is not None
    assert snapshot.risk_session_id == session.risk_session_id
    assert snapshot.feature_version == "rules-v1"
    assert snapshot.calculated_at == NOW
    assert snapshot.ip_velocity_5m == 1
    assert snapshot.ip_velocity_1h == 1
    assert snapshot.device_velocity_5m == 1
    assert snapshot.device_velocity_1h == 1
    assert snapshot.customer_velocity_1h == 1
    assert snapshot.vpa_velocity_1h == 1
    assert snapshot.is_new_device_for_customer is True
    assert snapshot.flow_duration_seconds == 300
    assert snapshot.is_demo_simulation is False
    assert snapshot.shared_demo_merchant_count_1h is None
    for field in EXPECTED_FIELDS:
        assert snapshot.sources[field].availability == Availability.AVAILABLE or field in {
            "user_agent_bot_suspected",
            "network_vpn_or_proxy",
            "network_reputation_score",
            "npci_risk_flag",
            "payer_bank_or_compliance_flag",
            "shared_demo_merchant_count_1h",
        }


async def test_uncorrelated_event_uses_uncorrelated_namespace_for_vpa_velocity(
    extractor: FeatureExtractor,
) -> None:
    snapshot = await extractor.extract_for_event(_event(), None, NOW)

    assert snapshot.risk_session_id is None
    assert snapshot.vpa_velocity_1h == 1
    assert snapshot.ip_velocity_5m is None
    assert snapshot.sources["ip_velocity_5m"].availability == Availability.MISSING
    assert snapshot.max_amount_paise is None
    assert snapshot.is_new_device_for_customer is None


async def test_session_extraction_builds_precheck_snapshot_without_vpa(
    extractor: FeatureExtractor,
) -> None:
    session = _session()
    snapshot = await extractor.extract_for_session(session, NOW)

    assert snapshot.mandate_event_id is None
    assert snapshot.risk_session_id == session.risk_session_id
    assert snapshot.vpa_velocity_1h is None
    assert snapshot.sources["vpa_velocity_1h"].availability == Availability.NOT_APPLICABLE
    assert snapshot.ip_velocity_5m == 1
    assert snapshot.npci_risk_flag is None


async def test_missing_hashes_yield_missing_sources_not_zero_counts(
    extractor: FeatureExtractor,
) -> None:
    session = _session(
        customer_reference_hash=None,
        ip_hash=None,
        device_fingerprint_hash=None,
        user_agent_hash=None,
    )
    snapshot = await extractor.extract_for_event(
        _event(vpa_hash=None, failure_reason=None), session, NOW
    )

    for field in (
        "ip_velocity_5m",
        "ip_velocity_1h",
        "device_velocity_5m",
        "device_velocity_1h",
        "customer_velocity_1h",
        "vpa_velocity_1h",
    ):
        assert getattr(snapshot, field) is None
        assert snapshot.sources[field].availability == Availability.MISSING
    assert snapshot.is_new_device_for_customer is None
    assert snapshot.npci_risk_flag is None


async def test_npci_risk_flag_true_for_fixture_failure_reason(
    extractor: FeatureExtractor,
) -> None:
    event = _event(failure_reason="Mandate rejected: NPCI risk flag raised by payer bank")
    snapshot = await extractor.extract_for_event(event, _session(), NOW)

    assert snapshot.npci_risk_flag is True
    assert snapshot.payer_bank_or_compliance_flag is True
    assert snapshot.sources["npci_risk_flag"].availability == Availability.AVAILABLE


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("compliance hold by bank", True),
        ("account frozen for review", True),
        ("blocked by issuer", True),
        ("suspected fraud pattern", True),
        ("aml screening required", True),
        ("payer_bank timeout", True),
        ("all clear, no issues", False),
        ("payerbank timeout", False),
        ("NPCI settlement done", False),
    ],
)
async def test_payer_bank_flag_matches_bounded_tokens_only(
    extractor: FeatureExtractor, reason: str, expected: bool
) -> None:
    snapshot = await extractor.extract_for_event(_event(failure_reason=reason), _session(), NOW)
    assert snapshot.payer_bank_or_compliance_flag is expected


async def test_flags_are_none_without_failure_reason(extractor: FeatureExtractor) -> None:
    snapshot = await extractor.extract_for_event(_event(failure_reason=None), _session(), NOW)
    assert snapshot.npci_risk_flag is None
    assert snapshot.payer_bank_or_compliance_flag is None
    assert snapshot.sources["npci_risk_flag"].availability == Availability.MISSING


async def test_demo_event_records_and_counts_shared_merchant(
    extractor: FeatureExtractor,
) -> None:
    first = await extractor.extract_for_event(_event(is_demo_event=True), _session(), NOW)
    assert first.is_demo_simulation is True
    assert first.shared_demo_merchant_count_1h == 1
    assert (
        first.sources["shared_demo_merchant_count_1h"].availability == Availability.DEMO_SIMULATED
    )

    other_session = _session(merchant_namespace="demo_merchant_two")
    second = await extractor.extract_for_event(_event(is_demo_event=True), other_session, NOW)
    assert second.shared_demo_merchant_count_1h == 2


async def test_demo_session_via_namespace_prefix_counts_shared_merchant(
    extractor: FeatureExtractor,
) -> None:
    snapshot = await extractor.extract_for_session(_session(), NOW)
    assert snapshot.is_demo_simulation is True
    assert snapshot.shared_demo_merchant_count_1h == 1


async def test_non_demo_traffic_never_touches_demo_keys(extractor: FeatureExtractor) -> None:
    session = _session(merchant_namespace="prod_merchant")
    snapshot = await extractor.extract_for_event(_event(), session, NOW)
    session_snapshot = await extractor.extract_for_session(session, NOW)

    assert snapshot.is_demo_simulation is False
    assert snapshot.shared_demo_merchant_count_1h is None
    assert session_snapshot.is_demo_simulation is False
    store_client = extractor._store._client  # noqa: SLF001
    assert [k async for k in store_client.scan_iter("mg:test:demoshared:*")] == []


async def test_new_device_true_then_false_for_same_customer(extractor: FeatureExtractor) -> None:
    first = await extractor.extract_for_session(_session(), NOW)
    second = await extractor.extract_for_session(_session(), NOW)
    assert first.is_new_device_for_customer is True
    assert second.is_new_device_for_customer is False


async def test_device_new_for_one_customer_not_for_another(extractor: FeatureExtractor) -> None:
    other_customer = "hmac-sha256:" + "00" * 32
    first = await extractor.extract_for_session(_session(), NOW)
    second = await extractor.extract_for_session(
        _session(customer_reference_hash=other_customer), NOW
    )
    assert first.is_new_device_for_customer is True
    assert second.is_new_device_for_customer is True


async def test_flow_duration_computed_from_session_times(extractor: FeatureExtractor) -> None:
    snapshot = await extractor.extract_for_session(_session(), NOW)
    assert snapshot.flow_duration_seconds == 300
    assert snapshot.sources["flow_duration_seconds"].availability == Availability.AVAILABLE
    assert snapshot.sources["flow_duration_seconds"].source == "risk_session"


async def test_flow_duration_missing_without_completed_at(extractor: FeatureExtractor) -> None:
    snapshot = await extractor.extract_for_session(_session(flow_completed_at=None), NOW)
    assert snapshot.flow_duration_seconds is None
    assert snapshot.sources["flow_duration_seconds"].availability == Availability.MISSING


async def test_mandate_intent_fields_flow_into_snapshot(extractor: FeatureExtractor) -> None:
    snapshot = await extractor.extract_for_session(_session(), NOW)
    assert snapshot.max_amount_paise == 500000
    assert snapshot.mandate_frequency == "monthly"
    assert snapshot.expiry_days_from_registration == 365


async def test_snapshot_validates_against_a0_contract(extractor: FeatureExtractor) -> None:
    session = _session()
    snapshots = [
        await extractor.extract_for_event(_event(), session, NOW),
        await extractor.extract_for_event(_event(), None, NOW),
        await extractor.extract_for_session(session, NOW),
        await extractor.extract_for_event(_event(is_demo_event=True), session, NOW),
    ]
    for snapshot in snapshots:
        reloaded = FeatureSnapshot.model_validate(snapshot.model_dump(mode="json"))
        assert reloaded.feature_snapshot_id == snapshot.feature_snapshot_id


async def test_sources_cover_every_attempted_field(extractor: FeatureExtractor) -> None:
    snapshot = await extractor.extract_for_event(_event(), _session(), NOW)
    assert set(snapshot.sources) == EXPECTED_FIELDS
    for field, source in snapshot.sources.items():
        assert len(source.source) <= 80, field
        assert "hmac-sha256" not in source.source, field
        assert "demo.user" not in source.source, field


async def test_reputation_provider_feeds_network_features() -> None:
    class FakeReputation:
        async def inspect(self, ip_hash: str, now: datetime) -> ReputationResult:
            assert ip_hash == IP_HASH
            return ReputationResult(vpn_or_proxy=True, reputation_score=250)

    store = RedisFeatureStore(FakeAsyncRedis(), key_prefix="mg:test")
    extractor = FeatureExtractor(store, reputation=FakeReputation())
    snapshot = await extractor.extract_for_session(_session(), NOW)
    assert snapshot.network_vpn_or_proxy is True
    assert snapshot.network_reputation_score == 100  # clamped from 250


async def test_reputation_failure_means_no_data() -> None:
    class ExplodingReputation:
        async def inspect(self, ip_hash: str, now: datetime) -> ReputationResult | None:
            raise RuntimeError("provider down")

    store = RedisFeatureStore(FakeAsyncRedis(), key_prefix="mg:test")
    extractor = FeatureExtractor(store, reputation=ExplodingReputation())
    snapshot = await extractor.extract_for_session(_session(), NOW)
    assert snapshot.network_vpn_or_proxy is None
    assert snapshot.network_reputation_score is None


async def test_redis_failure_raises_feature_store_unavailable() -> None:
    client = MagicMock()
    client.pipeline.side_effect = redis.exceptions.RedisError("boom")
    extractor = FeatureExtractor(RedisFeatureStore(client))
    with pytest.raises(FeatureStoreUnavailable):
        await extractor.extract_for_session(_session(), NOW)


async def test_naive_now_is_rejected(extractor: FeatureExtractor) -> None:
    naive = datetime(2026, 1, 15, 10, 5, 0)
    with pytest.raises(ValueError):
        await extractor.extract_for_session(_session(), naive)
    with pytest.raises(ValueError):
        await extractor.extract_for_event(_event(), _session(), naive)
