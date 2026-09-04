"""Unit tests for the health check routes. A0 owns this module."""

import uuid

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_route() -> None:
    """Check status code, exact JSON body, and generated X-Request-ID."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "mandate-guardian-api",
        "version": "0.1.0",
    }

    # Must have a parseable UUID4 request ID
    request_id = response.headers.get("X-Request-ID")
    assert request_id is not None
    assert uuid.UUID(request_id, version=4)


def test_readyz_route() -> None:
    """Check readiness probe response."""
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "service": "mandate-guardian-api",
        "version": "0.1.0",
    }


def test_request_id_preservation() -> None:
    """Check that a valid incoming X-Request-ID is preserved."""
    test_id = str(uuid.uuid4())
    response = client.get("/healthz", headers={"X-Request-ID": test_id})
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == test_id
