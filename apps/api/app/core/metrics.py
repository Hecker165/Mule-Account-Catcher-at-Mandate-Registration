"""Zero-dependency metrics registry and Prometheus text-format rendering.

Hand-rolled in-process counters (no client libraries): ``inc`` maintains
``*_total`` counters and ``observe_seconds`` maintains ``<name>_count`` /
``<name>_sum`` families. Thread-safe via a single lock; the process-wide
``REGISTRY`` singleton backs ``GET /metrics``.

Cardinality guard: label keys are restricted to ``{"method", "route",
"status"}`` and label values are validated (fixed method set, route must be
a path template without embedded values, status an int string). A violation
raises ``ValueError`` — the guard that keeps ``/metrics`` bounded and
identifier-free. The rendered exposition contains no application data
beyond the counters: never payloads, never request IDs, never client
addresses. Enforced by ``tests/security/test_log_redaction.py``.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

_ALLOWED_LABEL_KEYS = frozenset({"method", "route", "status"})
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

# Value-like shapes that must never appear inside a route label: UUIDs, long
# hex blobs and long numbers are all real cardinality bombs. A route segment
# may only be a plain template word or a ``{placeholder}``.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_LONG_NUMERIC_RE = re.compile(r"\b\d{5,}\b")
_TEMPLATE_SEGMENT_RE = re.compile(r"^[a-z0-9_.-]+$")
_PLACEHOLDER_RE = re.compile(r"^\{[a-zA-Z_][a-zA-Z0-9_]*\}$")


class MetricsRegistry:
    """Thread-safe in-process counter registry with bounded label space."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._types: dict[str, str] = {}

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        """Increment a ``*_total`` counter; raises ValueError on bad labels."""
        metric = name if name.endswith("_total") else f"{name}_total"
        key_labels = self._validate_labels(labels)
        with self._lock:
            key = (metric, key_labels)
            self._series[key] = self._series.get(key, 0.0) + value
            self._types[metric] = "counter"

    def observe_seconds(self, name: str, labels: dict[str, str] | None, seconds: float) -> None:
        """Record one duration observation as ``<name>_count``/``<name>_sum``."""
        key_labels = self._validate_labels(labels)
        count_key = (f"{name}_count", key_labels)
        sum_key = (f"{name}_sum", key_labels)
        with self._lock:
            self._series[count_key] = self._series.get(count_key, 0.0) + 1.0
            self._series[sum_key] = self._series.get(sum_key, 0.0) + float(seconds)
            self._types[f"{name}_count"] = "counter"
            self._types[f"{name}_sum"] = "gauge"

    def render_prometheus(self) -> str:
        """Render the exposition (``text/plain; version=0.0.4``)."""
        with self._lock:
            snapshot = dict(self._series)
            types = dict(self._types)
        lines: list[str] = []
        current_name: str | None = None
        for (name, labels), value in sorted(
            snapshot.items(), key=lambda item: (item[0][0], item[0][1])
        ):
            if name != current_name:
                lines.append(f"# TYPE {name} {types.get(name, 'untyped')}")
                current_name = name
            lines.append(f"{name}{_render_labels(labels)} {_render_value(value)}")
        return "\n".join(lines) + "\n" if lines else ""

    def reset(self) -> None:
        """Clear all series (tests only)."""
        with self._lock:
            self._series.clear()
            self._types.clear()

    def _validate_labels(self, labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        """Normalise and validate one label set; the cardinality guard."""
        if not labels:
            return ()
        unknown = set(labels) - _ALLOWED_LABEL_KEYS
        if unknown:
            raise ValueError(f"metric label keys outside the allowed set: {sorted(unknown)}")
        method = labels.get("method")
        if method is not None and method not in _ALLOWED_METHODS:
            raise ValueError(f"metric method label is not a known HTTP method: {method!r}")
        route = labels.get("route")
        if route is not None:
            _validate_route(route)
        status = labels.get("status")
        if status is not None and not (status.isdigit() and 100 <= int(status) <= 599):
            raise ValueError(f"metric status label is not an int status code: {status!r}")
        return tuple(sorted(labels.items()))


def _validate_route(route: str) -> None:
    """Reject route labels that are not path templates without embedded values."""
    if route == "unmatched":
        return
    if not route.startswith("/"):
        raise ValueError(f"route label must be a path template: {route!r}")
    for segment in route.strip("/").split("/"):
        if _PLACEHOLDER_RE.match(segment):
            continue
        if not _TEMPLATE_SEGMENT_RE.match(segment):
            raise ValueError(f"route label segment is not a template segment: {segment!r}")
        if (
            _UUID_RE.search(segment)
            or _LONG_HEX_RE.search(segment)
            or _LONG_NUMERIC_RE.search(segment)
        ):
            raise ValueError(f"route label appears to embed a value: {route!r}")


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    escaped = ",".join(f'{key}="{_escape_label_value(value)}"' for key, value in labels)
    return "{" + escaped + "}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_value(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.9f}"


REGISTRY = MetricsRegistry()


async def request_metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Request metrics middleware: counts and observes duration per response.

    Labels: ``method`` = HTTP method, ``route`` = the matched route template
    (else ``"unmatched"``), ``status`` = response status code. Never logs and
    never raises into the request path: recording is wrapped in try/except
    and the response is always returned.
    """
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _record_request(request, "500", time.perf_counter() - started)
        raise
    _record_request(request, str(response.status_code), time.perf_counter() - started)
    return response


def _record_request(request: Request, status: str, seconds: float) -> None:
    route = getattr(request.scope.get("route"), "path", "unmatched")
    labels = {"method": request.method, "route": route, "status": status}
    try:
        REGISTRY.inc("http_requests_total", labels)
        REGISTRY.observe_seconds("http_request_duration_seconds", labels, seconds)
    except (ValueError, TypeError, AttributeError):
        # Any label outside the bounded space is skipped, never raised into
        # the request path and never logged (which would risk payload content).
        return
