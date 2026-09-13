"""Mandate event evaluator port; A4/A5 install the real implementation via app.state."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.mandate_event import MandateWebhookEvent


class MandateEventEvaluator(Protocol):
    """Extension interface implemented by A4/A5 inside A3's transaction."""

    async def evaluate(self, db_session: AsyncSession, event: MandateWebhookEvent) -> None: ...


class MandateEvaluatorUnavailable(RuntimeError):
    """Raised when the real A4/A5 evaluator has not been installed."""

    def __init__(self) -> None:
        super().__init__("mandate event evaluator is unavailable")


class UnavailableMandateEvaluator:
    """Fallback that always raises; the event and audit stay committed."""

    async def evaluate(self, db_session: AsyncSession, event: MandateWebhookEvent) -> None:
        raise MandateEvaluatorUnavailable()
