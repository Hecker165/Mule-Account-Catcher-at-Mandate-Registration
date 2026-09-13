from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app.api.routes import dashboard, demo, risk_sessions, webhooks
from app.core.settings import get_settings
from app.domain.rules.engine import RuleEngine
from app.integrations.razorpay.client import RazorpayTokenRevokeClient, TokenRevokeClient
from app.integrations.razorpay.mock_client import MockTokenRevokeClient
from app.persistence.session import get_session_factory
from app.services.features.extractor import FeatureExtractor
from app.services.features.redis_store import RedisFeatureStore
from app.services.features.reputation import UnavailableNetworkReputationProvider
from app.services.risk_evaluation import MandateEvaluationService, PrecheckEvaluationService
from app.services.webhook_event_parser import RazorpayWebhookParser
from app.services.webhook_pseudonymisation import VpaPseudonymizer
from app.services.webhook_signature import WebhookSignatureVerifier
from app.workers.outbox_worker import OutboxWorker


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    key_id = settings.razorpay_key_id.get_secret_value() if settings.razorpay_key_id else ""
    key_secret = (
        settings.razorpay_key_secret.get_secret_value() if settings.razorpay_key_secret else ""
    )
    if key_id and key_secret:
        revoke_client: TokenRevokeClient = RazorpayTokenRevokeClient(
            key_id=settings.razorpay_key_id,  # type: ignore[arg-type]
            key_secret=settings.razorpay_key_secret,  # type: ignore[arg-type]
        )
    else:
        revoke_client = MockTokenRevokeClient()  # credential-free local/demo mode

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        worker = OutboxWorker(revoke_client=revoke_client)
        stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run_forever(stop_event))
        yield
        stop_event.set()
        await task

    application = FastAPI(
        title="Mandate Guardian API",
        version="0.1.0",
        description="Merchant-layer, explainable UPI mandate-risk controls.",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    application.state.token_revoke_client = revoke_client

    # A1: attach session factory to app state
    application.state.session_factory = get_session_factory()

    # A2: register risk session routes
    application.include_router(risk_sessions.router, prefix="/v1")

    # A3: webhook gateway state
    application.state.webhook_signature_verifier = WebhookSignatureVerifier(
        settings.razorpay_webhook_secret
    )
    application.state.webhook_vpa_pseudonymizer = VpaPseudonymizer(settings.hmac_pepper)
    application.state.webhook_parser = RazorpayWebhookParser(
        application.state.webhook_vpa_pseudonymizer
    )
    application.include_router(webhooks.router, prefix="/v1")

    # A8: read-only dashboard routes (no auth: documented buildathon limitation)
    application.include_router(dashboard.router, prefix="/v1")

    # A9: demo control API (hidden in production via the route's own guard)
    application.include_router(demo.router, prefix="/v1")

    # A5: rule engine composition; replaces the A2/A3 Unavailable* placeholders
    store = RedisFeatureStore.from_settings()
    reputation = UnavailableNetworkReputationProvider()
    extractor = FeatureExtractor(store=store, reputation=reputation)
    engine = RuleEngine()  # rules-v1 defaults

    application.state.feature_extractor = extractor  # A4 seam
    application.state.network_reputation_provider = reputation  # A4 seam
    application.state.precheck_evaluator = PrecheckEvaluationService(
        extractor=extractor,
        engine=engine,
    )
    application.state.mandate_event_evaluator = MandateEvaluationService(
        extractor=extractor,
        engine=engine,
    )

    @application.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Attach X-Request-ID header to every response.

        Reuses a valid UUID4 from the incoming request header if present;
        otherwise generates a new one.
        """
        request_id: str | None = request.headers.get("x-request-id")
        if request_id:
            try:
                # Reuse a valid incoming UUID request ID (any version);
                # normalise to its canonical lowercase form.
                request_id = str(uuid.UUID(request_id))
            except ValueError:
                request_id = None

        if not request_id:
            request_id = str(uuid.uuid4())

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe — always returns OK if the process is running."""
        return {
            "status": "ok",
            "service": "mandate-guardian-api",
            "version": "0.1.0",
        }

    @application.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness probe — configuration-only in A0.

        A1/A4 may later add actual Postgres/Redis checks without changing
        the response shape.
        """
        return {
            "status": "ready",
            "service": "mandate-guardian-api",
            "version": "0.1.0",
        }

    return application


app = create_app()
