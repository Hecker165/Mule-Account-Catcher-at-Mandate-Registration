"""A4 unit tests: frozen-clock window expiry, isolation and hygiene (fakeredis)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import redis.exceptions
from fakeredis.aioredis import FakeRedis as FakeAsyncRedis

from app.services.features.keys import velocity_key
from app.services.features.redis_store import FeatureStoreUnavailable, RedisFeatureStore

T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
HASH_A = "hmac-sha256:" + "ab" * 32
HASH_B = "hmac-sha256:" + "cd" * 32
NS = "demo_merchant_one"


@pytest.fixture
def store() -> RedisFeatureStore:
    return RedisFeatureStore(FakeAsyncRedis(), key_prefix="mg:test")


def _key(signal: str = "ip", window: str = "5m", hash_value: str = HASH_A) -> str:
    return velocity_key("mg:test", NS, signal, window, hash_value)


async def test_member_recorded_at_t0_is_counted_inside_window(store: RedisFeatureStore) -> None:
    count = await store.record_and_count(key=_key(), member="m1", window_seconds=300, now=T0)
    assert count == 1
    count = await store.record_and_count(
        key=_key(), member="m2", window_seconds=300, now=T0 + timedelta(minutes=4)
    )
    assert count == 2


@pytest.mark.parametrize("window_label,window_seconds", [("5m", 300), ("1h", 3600)])
async def test_member_still_counted_one_millisecond_before_window_edge(
    store: RedisFeatureStore, window_label: str, window_seconds: int
) -> None:
    key = _key(window=window_label)
    await store.record_and_count(key=key, member="m1", window_seconds=window_seconds, now=T0)
    count = await store.record_and_count(
        key=key,
        member="m2",
        window_seconds=window_seconds,
        now=T0 + timedelta(seconds=window_seconds) - timedelta(milliseconds=1),
    )
    assert count == 2


@pytest.mark.parametrize("window_label,window_seconds", [("5m", 300), ("1h", 3600)])
async def test_member_excluded_exactly_at_window_edge(
    store: RedisFeatureStore, window_label: str, window_seconds: int
) -> None:
    key = _key(window=window_label)
    await store.record_and_count(key=key, member="m1", window_seconds=window_seconds, now=T0)
    count = await store.record_and_count(
        key=key,
        member="m2",
        window_seconds=window_seconds,
        now=T0 + timedelta(seconds=window_seconds),
    )
    assert count == 1


async def test_second_window_member_counts_after_first_expires(store: RedisFeatureStore) -> None:
    key = _key()
    first = await store.record_and_count(key=key, member="m1", window_seconds=300, now=T0)
    assert first == 1
    second = await store.record_and_count(
        key=key, member="m2", window_seconds=300, now=T0 + timedelta(minutes=5, seconds=1)
    )
    assert second == 1


async def test_same_second_distinct_members_both_count(store: RedisFeatureStore) -> None:
    key = _key()
    await store.record_and_count(key=key, member=str(uuid4()), window_seconds=300, now=T0)
    count = await store.record_and_count(key=key, member=str(uuid4()), window_seconds=300, now=T0)
    assert count == 2


async def test_same_member_recorded_twice_counts_once(store: RedisFeatureStore) -> None:
    key = _key()
    await store.record_and_count(key=key, member="same", window_seconds=300, now=T0)
    count = await store.record_and_count(key=key, member="same", window_seconds=300, now=T0)
    assert count == 1


async def test_different_merchant_namespaces_do_not_share_counters(
    store: RedisFeatureStore,
) -> None:
    key_one = velocity_key("mg:test", "demo_merchant_one", "ip", "5m", HASH_A)
    key_two = velocity_key("mg:test", "demo_merchant_two", "ip", "5m", HASH_A)
    await store.record_and_count(key=key_one, member="m1", window_seconds=300, now=T0)
    count = await store.record_and_count(key=key_two, member="m2", window_seconds=300, now=T0)
    assert count == 1


async def test_different_signals_do_not_share_counters(store: RedisFeatureStore) -> None:
    await store.record_and_count(key=_key(signal="ip"), member="m1", window_seconds=300, now=T0)
    count = await store.record_and_count(
        key=_key(signal="device"), member="m1", window_seconds=300, now=T0
    )
    assert count == 1


async def test_every_written_key_has_a_ttl(store: RedisFeatureStore) -> None:
    client = store._client  # noqa: SLF001 - white-box TTL assertion is the point
    await store.record_and_count(key=_key(), member="m1", window_seconds=300, now=T0)
    await store.check_then_add(key="mg:test:dev:known", member="d1", ttl_seconds=86_400, now=T0)
    await store.record_demo_merchant(device_hash=HASH_A, merchant_namespace=NS, now=T0)
    for key in ("mg:test:dev:known", _key()):
        assert await client.ttl(key) > 0
    demo_keys = [k async for k in client.scan_iter("mg:test:demoshared:*")]
    assert len(demo_keys) == 1
    assert await client.ttl(demo_keys[0]) > 0


async def test_naive_now_is_rejected_with_value_error(store: RedisFeatureStore) -> None:
    naive = datetime(2026, 1, 15, 10, 0, 0)
    with pytest.raises(ValueError):
        await store.record_and_count(key=_key(), member="m1", window_seconds=300, now=naive)
    with pytest.raises(ValueError):
        await store.check_then_add(key="k", member="m", ttl_seconds=60, now=naive)


@pytest.mark.parametrize("namespace", ["Demo_Merchant", "ab", "x" * 65, "", "has space"])
def test_invalid_namespace_is_rejected_with_safe_message(namespace: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        velocity_key("mg:test", namespace, "ip", "5m", HASH_A)
    if namespace:
        assert namespace not in str(exc_info.value)


@pytest.mark.parametrize("hash_value", ["raw-ip-1.2.3.4", "hmac-sha256:ZZZ", "", "hmac-sha256:ab"])
def test_invalid_hash_is_rejected_with_safe_message(hash_value: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        velocity_key("mg:test", NS, "ip", "5m", hash_value)
    if hash_value:
        assert hash_value not in str(exc_info.value)


def _broken_client() -> MagicMock:
    client = MagicMock()
    client.pipeline.side_effect = redis.exceptions.RedisError("boom")
    client.sismember.side_effect = redis.exceptions.RedisError("boom")
    return client


async def test_redis_error_is_wrapped_as_feature_store_unavailable() -> None:
    store = RedisFeatureStore(_broken_client())
    with pytest.raises(FeatureStoreUnavailable):
        await store.record_and_count(key=_key(), member="m1", window_seconds=300, now=T0)
    with pytest.raises(FeatureStoreUnavailable):
        await store.check_then_add(key="k", member="m", ttl_seconds=60, now=T0)
    with pytest.raises(FeatureStoreUnavailable):
        await store.count_demo_merchants(device_hash=HASH_A, now=T0)


async def test_check_then_add_reports_true_then_false(store: RedisFeatureStore) -> None:
    first = await store.check_then_add(
        key="mg:test:dev:c1", member="device-1", ttl_seconds=86_400, now=T0
    )
    second = await store.check_then_add(
        key="mg:test:dev:c1", member="device-1", ttl_seconds=86_400, now=T0
    )
    assert first is True
    assert second is False


async def test_known_devices_ttl_is_24_hours(store: RedisFeatureStore) -> None:
    await store.check_then_add(key="mg:test:dev:c2", member="device-1", ttl_seconds=86_400, now=T0)
    assert await store._client.ttl("mg:test:dev:c2") == 86_400  # noqa: SLF001


async def test_demo_shared_merchants_count_distinct_namespaces_in_1h(
    store: RedisFeatureStore,
) -> None:
    await store.record_demo_merchant(
        device_hash=HASH_A, merchant_namespace="demo_merchant_one", now=T0
    )
    await store.record_demo_merchant(
        device_hash=HASH_A, merchant_namespace="demo_merchant_two", now=T0
    )
    await store.record_demo_merchant(
        device_hash=HASH_A, merchant_namespace="demo_merchant_one", now=T0
    )
    assert await store.count_demo_merchants(device_hash=HASH_A, now=T0) == 2


async def test_demo_shared_merchants_excludes_expired_namespace(store: RedisFeatureStore) -> None:
    await store.record_demo_merchant(device_hash=HASH_A, merchant_namespace=NS, now=T0)
    assert await store.count_demo_merchants(device_hash=HASH_A, now=T0) == 1
    assert (
        await store.count_demo_merchants(
            device_hash=HASH_A, now=T0 + timedelta(hours=1) - timedelta(milliseconds=1)
        )
        == 1
    )
    assert await store.count_demo_merchants(device_hash=HASH_A, now=T0 + timedelta(hours=1)) == 0
