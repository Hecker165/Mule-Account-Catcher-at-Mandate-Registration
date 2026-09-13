"""A2 pre-check evaluator port and availability.

Defines the extension interface for A5's risk evaluator and the
unavailable fallback that returns 503 when A5 is not installed.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.risk_assessment import RiskAssessment
from app.contracts.risk_session import RiskSession


class PrecheckEvaluator(Protocol):
    """Extension interface implemented by A5's rules engine.

    A2 calls `assess` inside an A1 transaction. A5 is responsible for:
    - extracting a FeatureSnapshot from the risk session
    - computing a RiskAssessment with stage=PRECHECK
    - persisting both via A1 repositories in the supplied transaction
    - returning the persisted RiskAssessment
    """

    async def assess(
        self, db_session: AsyncSession, risk_session: RiskSession
    ) -> RiskAssessment: ...


class PrecheckEvaluatorUnavailable(RuntimeError):
    """Raised when the real A5 evaluator has not been installed."""

    def __init__(self) -> None:
        super().__init__("precheck evaluator is unavailable")


class UnavailablePrecheckEvaluator:
    """Fallback that always raises PrecheckEvaluatorUnavailable.

    A2 routes map this to a safe 503. Do not create a permissive
    fallback that silently ALLOWs a customer.
    """

    async def assess(self, db_session: AsyncSession, risk_session: RiskSession) -> RiskAssessment:
        raise PrecheckEvaluatorUnavailable()


def get_precheck_evaluator(request: Request) -> PrecheckEvaluator:
    """FastAPI dependency to retrieve the evaluator from app state.

    Reads ``request.app.state.precheck_evaluator``. If missing, returns the
    unavailable fallback; never a permissive default. A5 replaces the state
    assignment during application composition.
    """
    evaluator = getattr(request.app.state, "precheck_evaluator", None)
    return evaluator if evaluator is not None else UnavailablePrecheckEvaluator()
