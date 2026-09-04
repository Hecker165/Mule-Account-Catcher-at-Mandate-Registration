"""Risk session repository implementation.

Provides CRUD operations for risk sessions with proper contract mapping.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import HashValue
from app.contracts.common import RiskSessionStatus
from app.contracts.risk_session import RiskSession
from app.persistence.models import RiskSession as RiskSessionORM
from app.persistence.types import (
    risk_session_from_row,
    risk_session_to_row,
)


class RiskSessionRepository:
    """Repository for risk session persistence."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, session: RiskSession) -> RiskSession:
        """Create a new risk session from a full contract."""
        row_data = risk_session_to_row(session)
        orm_obj = RiskSessionORM(**row_data)
        self.session.add(orm_obj)
        await self.session.flush()
        return risk_session_from_row(orm_obj)

    async def get(self, risk_session_id: UUID) -> RiskSession | None:
        """Get a risk session by ID."""
        stmt = select(RiskSessionORM).where(RiskSessionORM.risk_session_id == risk_session_id)
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None
        return risk_session_from_row(orm_obj)

    async def get_by_order_ref(
        self, merchant_namespace: str, checkout_order_ref: str
    ) -> RiskSession | None:
        """Get a risk session by merchant namespace and checkout order reference."""
        stmt = select(RiskSessionORM).where(
            RiskSessionORM.merchant_namespace == merchant_namespace,
            RiskSessionORM.checkout_order_ref == checkout_order_ref,
        )
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            return None
        return risk_session_from_row(orm_obj)

    async def update_telemetry(
        self,
        risk_session_id: UUID,
        customer_reference_hash: HashValue | None,
        ip_hash: HashValue | None,
        device_fingerprint_hash: HashValue | None,
        user_agent_hash: HashValue | None,
        flow_completed_at: datetime | None,
        status: RiskSessionStatus,
        updated_at: datetime,
    ) -> RiskSession:
        """Update telemetry fields on an existing risk session.

        Only updates non-None hash/timestamp arguments; retains existing values otherwise.
        Always applies the supplied status and updated_at.
        """
        stmt = select(RiskSessionORM).where(RiskSessionORM.risk_session_id == risk_session_id)
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            raise ValueError(f"Risk session {risk_session_id} not found")

        if customer_reference_hash is not None:
            orm_obj.customer_reference_hash = customer_reference_hash
        if ip_hash is not None:
            orm_obj.ip_hash = ip_hash
        if device_fingerprint_hash is not None:
            orm_obj.device_fingerprint_hash = device_fingerprint_hash
        if user_agent_hash is not None:
            orm_obj.user_agent_hash = user_agent_hash
        if flow_completed_at is not None:
            orm_obj.flow_completed_at = flow_completed_at

        orm_obj.status = status.value if isinstance(status, RiskSessionStatus) else status
        orm_obj.updated_at = updated_at

        await self.session.flush()
        return risk_session_from_row(orm_obj)

    async def update_status(
        self,
        risk_session_id: UUID,
        status: RiskSessionStatus,
        updated_at: datetime,
    ) -> RiskSession:
        """Update only the status and updated_at of a risk session."""
        stmt = select(RiskSessionORM).where(RiskSessionORM.risk_session_id == risk_session_id)
        result = await self.session.execute(stmt)
        orm_obj = result.scalar_one_or_none()
        if orm_obj is None:
            raise ValueError(f"Risk session {risk_session_id} not found")

        orm_obj.status = status.value if isinstance(status, RiskSessionStatus) else status
        orm_obj.updated_at = updated_at

        await self.session.flush()
        return risk_session_from_row(orm_obj)
