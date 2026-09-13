"""A4 test helper: build a Redis test client from TEST_REDIS_URL (database 15)."""

from __future__ import annotations

import os

import pytest
import redis
import redis.asyncio as async_redis

TEST_REDIS_URL = "redis://localhost:6379/15"


def make_test_redis() -> async_redis.Redis:
    """Return an async Redis client for TEST_REDIS_URL and FLUSHDB it.

    Fail with a helpful message when TEST_REDIS_URL is unreachable/missing.
    The flush uses a short-lived synchronous client so the helper works from
    both sync and async test contexts.
    """
    url = os.environ.get("TEST_REDIS_URL", TEST_REDIS_URL)
    try:
        sync_client = redis.Redis.from_url(url, socket_connect_timeout=2)
        sync_client.ping()
        sync_client.flushdb()
        sync_client.close()
    except redis.exceptions.RedisError as exc:
        pytest.fail(
            "TEST_REDIS_URL is unreachable: "
            f"{type(exc).__name__}. Start Redis with `docker compose up -d redis` "
            f"and set TEST_REDIS_URL={TEST_REDIS_URL!r}."
        )
    return async_redis.Redis.from_url(url)
