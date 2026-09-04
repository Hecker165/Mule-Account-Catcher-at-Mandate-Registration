from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.api.routes import risk_sessions
from app.persistence.session import get_session_factory
from app.services.precheck_provider import UnavailablePrecheckEvaluator


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title="Mandate Guardian API",
        version="0.1.0",
        description="Merchant-layer, explainable UPI mandate-risk controls.",
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # A1: attach session factory to app state
    application.state.session_factory = get_session_factory()

    # A2: install unavailable precheck evaluator; A5 will replace during composition
    application.state.precheck_evaluator = UnavailablePrecheckEvaluator()

    # A2: register risk session routes
    application.include_router(risk_sessions.router, prefix="/v1")

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
