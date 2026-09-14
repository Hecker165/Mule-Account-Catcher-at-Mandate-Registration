"""Prometheus metrics endpoint.

``GET /metrics`` exposes the process-wide ``REGISTRY`` in Prometheus text
format: counters only, bounded cardinality, no identifiers. No auth and
``Cache-Control: no-store`` — the endpoint carries no application data
beyond the counters, so exposing it unauthenticated is documented in the
threat model.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.core.metrics import REGISTRY

router = APIRouter(prefix="/metrics", tags=["metrics"])

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("")
async def metrics() -> Response:
    """Render the current counters in Prometheus text format."""
    return Response(
        content=REGISTRY.render_prometheus(),
        media_type=_PROMETHEUS_CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )
