"""A10 performance: 1,000-event audit chain verifies in under 5 seconds.

Appends 1,000 fixed synthetic audit events to one aggregate through
``AuditRepository.append`` (real advisory locking, real hash chain), then
measures ``verify_aggregate`` and asserts the architecture's target exactly:
under 5 seconds, ``valid=True``, ``checked_events=1000``. Only verification
is time-asserted — the append loop's speed is acceptable to be slow (batched
insertion would require an A1 interface change and is out of scope).

Requires a migrated ``TEST_POSTGRES_URL`` (run the integration suite first
or ``alembic upgrade head``); the test cleans up only its own aggregate rows.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.contracts.common import AuditActorType
from app.persistence.models import AuditEvent as AuditEventORM
from app.repositories.audit import AuditRepository

EVENT_COUNT = 1_000
VERIFY_BUDGET_SECONDS = 5.0
# aggregate_type is a contract Literal; a fresh synthetic UUID per run keeps
# the synthetic chain disjoint from every real risk_session aggregate.
_AGGREGATE_TYPE = "risk_session"


def _test_db_url() -> str:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.fail(
            "TEST_POSTGRES_URL environment variable is required for the audit "
            "scale test (see tests/integration/conftest.py)."
        )
    return url


async def test_thousand_event_audit_chain_verifies_under_five_seconds() -> None:
    engine = create_async_engine(
        _test_db_url(), pool_pre_ping=True, future=True, poolclass=NullPool
    )
    aggregate_id = uuid4()
    occurred_at = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            # Fail fast with a clear message when the schema is missing.
            await session.execute(text("SELECT 1 FROM audit_events LIMIT 1"))
            repo = AuditRepository(session)
            for index in range(EVENT_COUNT):
                await repo.append(
                    aggregate_type=_AGGREGATE_TYPE,
                    aggregate_id=aggregate_id,
                    event_type="audit_scale_test.event",
                    actor_type=AuditActorType.SYSTEM,
                    actor_id="a10-scale-test",
                    occurred_at=occurred_at,
                    redacted_payload={"index": index, "note": "synthetic"},
                )
            await session.commit()

            started = time.perf_counter()
            result = await repo.verify_aggregate(_AGGREGATE_TYPE, aggregate_id)
            elapsed = time.perf_counter() - started

        assert result.valid is True
        assert result.checked_events == EVENT_COUNT
        print(
            f"audit verification of {EVENT_COUNT} events "
            f"took {elapsed * 1000:.1f} ms (budget {VERIFY_BUDGET_SECONDS * 1000:.0f} ms)"
        )
        assert elapsed < VERIFY_BUDGET_SECONDS, (
            f"verify_aggregate took {elapsed:.3f}s, budget is {VERIFY_BUDGET_SECONDS:.0f}s"
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(AuditEventORM).where(
                    AuditEventORM.aggregate_type == _AGGREGATE_TYPE,
                    AuditEventORM.aggregate_id == aggregate_id,
                )
            )
        await engine.dispose()
