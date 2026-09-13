"""A4 integration tests: window semantics, TTLs and concurrency (real Redis)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as async_redis

from app.services.features.keys import velocity_key
from app.services.features.redis_store import RedisFeatureStore
from tests.helpers.redis_test_client import make_test_redis

T0 = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
HASH_A = "hmac-sha256:" + "ab" * 32


@pytest.fixture
async def client() -> Any:
    client = make_test_redis()
    yield client
    await client.aclose()


@pytest.fixture
def store(client: async_redis.Redis) -> RedisFeatureStore:
    return RedisFeatureStore(client, key_prefix="mg:int")


async def test_real_redis_matches_window_semantics(store: RedisFeatureStore) -> None:
    key = velocity_key("mg:int", "demo_merchant_one", "ip", "5m", HASH_A)
    await store.record_and_count(key=key, member="m1", window_seconds=300, now=T0)
    inside = await store.record_and_count(
        key=key,
        member="m2",
        window_seconds=300,
        now=T0 + timedelta(minutes=5) - timedelta(milliseconds=1),
    )
    assert inside == 2
    at_edge = await store.record_and_count(
        key=key, member="m3", window_seconds=300, now=T0 + timedelta(minutes=5)
    )
    assert at_edge == 2  # m2 and m3 remain; only m1 expired


async def test_all_written_keys_carry_ttl_on_real_redis(
    store: RedisFeatureStore, client: async_redis.Redis
) -> None:
    await store.record_and_count(
        key=velocity_key("mg:int", "demo_merchant_one", "device", "1h", HASH_A),
        member="m1",
        window_seconds=3600,
        now=T0,
    )
    await store.check_then_add(key="mg:int:dev:known", member="d1", ttl_seconds=86_400, now=T0)
    await store.record_demo_merchant(device_hash=HASH_A, merchant_namespace="demo_one", now=T0)

    keys = [k async for k in client.scan_iter("mg:int:*")]
    assert len(keys) == 3
    for key in keys:
        assert await client.ttl(key) > 0


async def test_two_store_instances_share_counts_via_real_redis(
    client: async_redis.Redis,
) -> None:
    first = RedisFeatureStore(client, key_prefix="mg:int")
    second = RedisFeatureStore(client, key_prefix="mg:int")
    key = velocity_key("mg:int", "demo_merchant_one", "ip", "5m", HASH_A)
    await first.record_and_count(key=key, member="m1", window_seconds=300, now=T0)
    count = await second.record_and_count(key=key, member="m2", window_seconds=300, now=T0)
    assert count == 2


async def test_concurrent_recordings_are_all_counted(store: RedisFeatureStore) -> None:
    key = velocity_key("mg:int", "demo_merchant_one", "customer", "1h", HASH_A)
    results = await asyncio.gather(
        *(
            store.record_and_count(key=key, member=f"m{i:02d}", window_seconds=3600, now=T0)
            for i in range(25)
        )
    )
    assert sorted(results) == list(range(1, 26))
