"""PostgreSQL integration test configuration.

Requires TEST_POSTGRES_URL environment variable.
Never silently falls back to SQLite.
"""

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.persistence import models  # noqa: F401

# Import all models to register with Base.metadata
from app.persistence.base import Base  # noqa: F401


def _get_test_db_url() -> str:
    """Get test database URL from environment.

    Fails with helpful message if not set.
    """
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.fail(
            "TEST_POSTGRES_URL environment variable is required for integration tests.\n"
            "For local Docker Compose:\n"
            '  export TEST_POSTGRES_URL="postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"\n'
            "Then run: docker compose up -d postgres redis"
        )
    return url


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Provide the test database URL."""
    return _get_test_db_url()


@pytest.fixture(scope="session")
def test_engine(test_db_url: str):
    """Create test engine and run migrations once per session.

    Uses ``NullPool`` so no pooled connection is ever carried across the
    per-test event loops that pytest-asyncio creates.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(test_db_url, pool_pre_ping=True, future=True, poolclass=NullPool)

    # Run alembic upgrade head (paths resolved relative to this file so the
    # fixture works regardless of the pytest working directory).
    base_dir = Path(__file__).resolve().parents[2]
    alembic_cfg = Config(str(base_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(base_dir / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.upgrade(alembic_cfg, "head")

    yield engine

    import asyncio

    asyncio.run(engine.dispose())


@pytest.fixture(scope="session")
def test_session_factory(test_engine) -> async_sessionmaker[AsyncSession]:
    """Create async session factory for tests."""
    return async_sessionmaker(test_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def session(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Provide an AsyncSession that rolls back after each test."""
    async with test_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture
async def clean_session(
    test_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Provide a clean AsyncSession for schema-inspection tests."""
    async with test_session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


@pytest.fixture(autouse=True)
async def truncate_tables(session: AsyncSession) -> AsyncGenerator[None, None]:
    """Truncate all A1 tables between tests in FK-safe order."""
    yield
    # Discard any failed/leftover transaction before truncating
    await session.rollback()
    # Truncate in reverse FK order
    tables = [
        "audit_events",
        "outbox_messages",
        "action_attempts",
        "action_requests",
        "rule_evaluations",
        "risk_assessments",
        "feature_snapshots",
        "mandate_webhook_events",
        "risk_sessions",
    ]
    for table in tables:
        await session.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
    await session.commit()
