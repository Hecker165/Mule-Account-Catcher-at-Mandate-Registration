"""Redis windowed-counter feature store (velocity only, never the audit store).

Semantics the rest of the system relies on:

- ``record_and_count`` records **before** counting, so the returned velocity
  includes the current event. A first-ever event yields velocity ``1``, not ``0``.
- ``check_then_add`` is deliberately a read-then-write, not atomic: two
  simultaneous first-seen events may both report "new", which is the
  conservative direction for a risk system.
- Every written key gets a TTL on every write; no key may exist without one.
- Keys and values contain only HMAC hashes, merchant namespaces, signal words,
  window labels and UUID members — never raw identifiers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import redis.asyncio as redis

from app.core.settings import get_settings
from app.services.features.keys import DEMO_SHARED_TTL_SECONDS, demo_shared_merchants_key


class FeatureStoreUnavailable(RuntimeError):
    """Raised when Redis cannot be reached or a command fails.

    Never carries connection details.
    """

    def __init__(self) -> None:
        super().__init__("feature store is unavailable")


class RedisFeatureStore:
    """Sliding-window counters and short-lived sets backed by Redis."""

    def __init__(self, client: redis.Redis[Any], key_prefix: str = "mg:v1") -> None:
        self._client = client
        self._key_prefix = key_prefix

    @classmethod
    def from_settings(cls) -> RedisFeatureStore:
        """Build a store from settings; lazy, no connection at import."""
        return cls(redis.Redis.from_url(get_settings().redis_url))

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    async def record_and_count(
        self, *, key: str, member: str, window_seconds: int, now: datetime
    ) -> int:
        """Record one member in a sliding window and return the live count."""
        score_ms = _epoch_ms(now)
        cutoff_ms = score_ms - window_seconds * 1000
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, "-inf", cutoff_ms)
                pipe.zadd(key, {member: score_ms})
                pipe.expire(key, window_seconds + 60)
                pipe.zcard(key)
                results = await pipe.execute()
        except Exception as exc:
            raise FeatureStoreUnavailable() from exc
        return int(results[-1])

    async def check_then_add(
        self, *, key: str, member: str, ttl_seconds: int, now: datetime
    ) -> bool:
        """Return True when the member is new for the key (then remember it)."""
        _require_aware(now)
        try:
            exists = await self._client.sismember(key, member)
            if exists:
                return False
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.sadd(key, member)
                pipe.expire(key, ttl_seconds)
                await pipe.execute()
        except Exception as exc:
            raise FeatureStoreUnavailable() from exc
        return True

    async def record_demo_merchant(
        self, *, device_hash: str, merchant_namespace: str, now: datetime
    ) -> None:
        """Remember that a device hash was seen at a merchant namespace."""
        score_ms = _epoch_ms(now)
        key = demo_shared_merchants_key(self._key_prefix, device_hash)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.zadd(key, {merchant_namespace: score_ms})
                pipe.expire(key, DEMO_SHARED_TTL_SECONDS)
                await pipe.execute()
        except Exception as exc:
            raise FeatureStoreUnavailable() from exc

    async def count_demo_merchants(self, *, device_hash: str, now: datetime) -> int:
        """Count distinct merchant namespaces for a device hash in the last hour."""
        score_ms = _epoch_ms(now)
        key = demo_shared_merchants_key(self._key_prefix, device_hash)
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, "-inf", score_ms - 3_600_000)
                pipe.zcard(key)
                results = await pipe.execute()
        except Exception as exc:
            raise FeatureStoreUnavailable() from exc
        return int(results[-1])

    async def aclose(self) -> None:
        """Close the injected client; the store never creates background work."""
        await self._client.close()


def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")


def _epoch_ms(now: datetime) -> int:
    _require_aware(now)
    return int(now.timestamp() * 1000)
