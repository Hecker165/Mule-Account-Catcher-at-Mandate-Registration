"""Razorpay webhook gateway: signature verification and idempotent ingestion."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.settings import get_settings
from app.repositories.audit import AuditRepository
from app.repositories.mandate_events import MandateEventRepository
from app.repositories.risk_sessions import RiskSessionRepository
from app.services.mandate_evaluator_port import UnavailableMandateEvaluator
from app.services.webhook_event_parser import WebhookParseError
from app.services.webhook_ingestion import WebhookIngestionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["razorpay-webhooks"])

_NO_STORE = {"Cache-Control": "no-store"}


@router.post("/razorpay")
async def razorpay_webhook(request: Request) -> JSONResponse:
    """Verify, normalise and ingest one Razorpay webhook delivery."""
    verifier = getattr(request.app.state, "webhook_signature_verifier", None)
    if verifier is None or not verifier.is_configured:
        return JSONResponse(
            status_code=503,
            content={"detail": "webhook signature verification is not configured"},
            headers=_NO_STORE,
        )

    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature")
    if not verifier.verify(raw_body, signature):
        incoming_request_id = request.headers.get("x-request-id")
        logger.warning("webhook rejected request_id=%s", incoming_request_id)
        return JSONResponse(
            status_code=401,
            content={"detail": "invalid webhook signature"},
            headers=_NO_STORE,
        )

    parser = getattr(request.app.state, "webhook_parser", None)
    if parser is None:
        return JSONResponse(
            status_code=500,
            content={"detail": "webhook parser is not configured"},
            headers=_NO_STORE,
        )
    classification = None
    try:
        classification = parser.classify_and_parse(
            raw_body,
            provider_event_id=request.headers.get("x-razorpay-event-id"),
            is_demo_event=_is_demo_event(request),
        )
    except WebhookParseError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)}, headers=_NO_STORE)

    assert classification is not None
    if classification.kind == "ignored":
        logger.info(
            "webhook ignored event_type=%s reason=%s",
            classification.raw_event_type,
            classification.ignore_reason,
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": "ignored",
                "event_type": classification.raw_event_type or "unknown",
                "reason": classification.ignore_reason or "ignored",
            },
            headers=_NO_STORE,
        )

    assert classification.event is not None
    evaluator = getattr(request.app.state, "mandate_event_evaluator", None)
    if evaluator is None:
        evaluator = UnavailableMandateEvaluator()

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        service = WebhookIngestionService(
            mandate_events=MandateEventRepository(session),
            audit=AuditRepository(session),
            risk_sessions=RiskSessionRepository(session),
            evaluator=evaluator,
            clock=lambda: datetime.now(UTC),
        )
        try:
            result = await service.ingest(
                classification.event,
                classification.notes_risk_session_id,
                classification.notes_merchant_namespace,
                classification.notes_checkout_order_ref,
            )
        except Exception:
            await session.rollback()
            logger.exception("webhook ingestion failed")
            return JSONResponse(
                status_code=500, content={"detail": "webhook ingestion failed"}, headers=_NO_STORE
            )
        await session.commit()

    if result.is_duplicate:
        return JSONResponse(
            status_code=200,
            content={
                "status": "duplicate",
                "mandate_event_id": str(result.mandate_event_id),
                "duplicate": True,
            },
            headers={**_NO_STORE, "X-Idempotent-Replay": "true"},
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "processed",
            "mandate_event_id": str(result.mandate_event_id),
            "duplicate": False,
            "correlation": result.correlation_status,
            "evaluation": result.evaluation_status,
        },
        headers=_NO_STORE,
    )


def _is_demo_event(request: Request) -> bool:
    flag = request.headers.get("x-demo-event", "").strip().lower() == "true"
    return bool(flag and get_settings().app_env != "production")
