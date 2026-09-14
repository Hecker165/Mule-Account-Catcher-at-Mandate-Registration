"""A10 dynamic redaction tests: no raw VPA, signature or secret reaches a log record.

Every test drives the real ``create_app()`` over ``TestClient`` (no lifespan,
no database) through fail-closed paths — invalid signature (401), unconfigured
verifier (503), missing session (404) — with ``caplog`` at DEBUG. A fabricated
raw-VPA marker is sent in request bodies and must never appear in any captured
log record. The request bodies are shaped like webhook deliveries but never
reach ingestion: these paths reject before parsing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.metrics import REGISTRY
from app.core.settings import Settings
from app.main import create_app
from app.services.webhook_signature import WebhookSignatureVerifier

_RAW_VPA_MARKER = "raw-vpa-marker@upi"
_DUMMY_KEY_SECRET = "dummy-key-secret-9f83bd1c"
_DUMMY_WEBHOOK_SECRET = "dummy-webhook-secret-2c71ea94"
_MISSING_SESSION_ID = "123e4567-e89b-42d3-a456-426614174000"
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _logs_text(caplog: pytest.LogCaptureFixture) -> str:
    """Render captured records the way the configured handler would."""
    return "\n".join(record.getMessage() for record in caplog.records)


def _app_logs_text(caplog: pytest.LogCaptureFixture) -> str:
    """Render only application log records (module ``app.*``).

    The TestClient's own httpx logger logs the request line (method and URL
    path) at INFO — that is the test harness's access log, not an application
    record. The redaction guarantee covers what the application's own code
    emits.
    """
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app")
    )


def _webhook_body_with_marker() -> bytes:
    """A token.confirmed-shaped body whose VPA carries the raw marker."""
    body = {
        "event": "token.confirmed",
        "payload": {
            "token": {
                "entity": {
                    "id": "token_redaction_probe",
                    "vpa": _RAW_VPA_MARKER,
                    "notes": {},
                }
            }
        },
    }
    return json.dumps(body).encode("utf-8")


class _NoRowResult:
    """SQLAlchemy result stub: every query comes back empty."""

    def scalar_one_or_none(self) -> None:
        return None


class _NoRowSession:
    """Minimal async-session stub: no rows anywhere, safe rollback/commit."""

    async def execute(self, *_args: Any, **_kwargs: Any) -> _NoRowResult:
        return _NoRowResult()

    async def rollback(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> _NoRowSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


def test_settings_repr_and_str_contain_no_secret_values() -> None:
    settings = Settings(
        razorpay_key_id=SecretStr("rzp_test_dummy_key_id_01"),
        razorpay_key_secret=SecretStr(_DUMMY_KEY_SECRET),
        razorpay_webhook_secret=SecretStr(_DUMMY_WEBHOOK_SECRET),
    )

    rendered = f"{settings!s}\n{settings!r}"

    assert _DUMMY_KEY_SECRET not in rendered
    assert _DUMMY_WEBHOOK_SECRET not in rendered
    assert "rzp_test_dummy_key_id_01" not in rendered


def test_webhook_invalid_signature_logs_no_body_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    application = create_app()
    application.state.webhook_signature_verifier = WebhookSignatureVerifier(_DUMMY_WEBHOOK_SECRET)
    client = TestClient(application)
    body = _webhook_body_with_marker()

    response = client.post(
        "/v1/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": "0" * 64, "x-razorpay-event-id": "evt_redaction_1"},
    )

    assert response.status_code == 401
    logs = _logs_text(caplog)
    assert _RAW_VPA_MARKER not in logs
    assert _DUMMY_WEBHOOK_SECRET not in logs
    assert "0" * 64 not in logs


def test_webhook_unconfigured_secret_logs_no_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    application = create_app()
    application.state.webhook_signature_verifier = WebhookSignatureVerifier(None)
    client = TestClient(application)
    body = _webhook_body_with_marker()

    response = client.post(
        "/v1/webhooks/razorpay",
        content=body,
        headers={"x-razorpay-signature": "f" * 64, "x-razorpay-event-id": "evt_redaction_2"},
    )

    assert response.status_code == 503
    logs = _logs_text(caplog)
    assert _RAW_VPA_MARKER not in logs
    assert "f" * 64 not in logs
    assert "evt_redaction_2" not in logs


def test_precheck_unavailable_logs_no_session_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    application = create_app()
    application.state.session_factory = lambda: _NoRowSession()
    client = TestClient(application)

    response = client.post(f"/v1/risk-sessions/{_MISSING_SESSION_ID}/precheck")

    assert response.status_code == 404
    assert _MISSING_SESSION_ID not in _app_logs_text(caplog)
    assert _RAW_VPA_MARKER not in _logs_text(caplog)


def test_metrics_labels_contain_no_identifiers() -> None:
    REGISTRY.reset()
    application = create_app()
    client = TestClient(application)
    client.get("/healthz")
    # A request that would embed an identifier: only the route template may appear.
    client.get(f"/v1/risk-sessions/{_MISSING_SESSION_ID}/precheck")
    client.post("/v1/webhooks/razorpay", content=b"{}")

    rendered = client.get("/metrics")

    assert rendered.status_code == 200
    assert rendered.headers["content-type"].startswith("text/plain")
    assert rendered.headers["cache-control"] == "no-store"
    text = rendered.text
    assert "# TYPE http_requests_total counter" in text
    assert not _UUID_RE.search(text)
    assert "token_" not in text
    assert "hmac-sha256" not in text
    assert _RAW_VPA_MARKER not in text


def test_metrics_cardinality_guard_rejects_bad_labels() -> None:
    REGISTRY.reset()
    with pytest.raises(ValueError):
        REGISTRY.inc(
            "http_requests_total",
            {
                "method": "GET",
                "route": f"/v1/risk-sessions/{_MISSING_SESSION_ID}/precheck",
                "status": "200",
            },
        )
    with pytest.raises(ValueError):
        REGISTRY.inc("http_requests_total", {"method": "GET", "route": "/healthz", "status": "ok"})
    with pytest.raises(ValueError):
        REGISTRY.inc(
            "http_requests_total", {"method": "TRACE", "route": "/healthz", "status": "200"}
        )
    with pytest.raises(ValueError):
        REGISTRY.inc(
            "http_requests_total",
            {"method": "GET", "route": "/healthz", "status": "200", "client": "10.0.0.1"},
        )
