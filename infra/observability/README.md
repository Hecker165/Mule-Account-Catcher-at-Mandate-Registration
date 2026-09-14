# Observability

Mandate Guardian exposes a minimal, zero-dependency Prometheus surface at
`GET http://localhost:8000/metrics`. There are no dashboards, no client
libraries and no per-identifier series.

## Exposed metrics

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `http_requests_total` | counter | `method`, `route`, `status` | Requests served since process start |
| `http_request_duration_seconds_count` | counter | `method`, `route`, `status` | Duration observations |
| `http_request_duration_seconds_sum` | gauge | `method`, `route`, `status` | Cumulative request seconds |

`route` is the **route template** (`/v1/risk-sessions/{risk_session_id}/precheck`)
or `unmatched` — never the concrete path.

## Cardinality rules

- Label keys are restricted to `{method, route, status}`; anything else raises
  `ValueError` in `apps/api/app/core/metrics.py` and is skipped by the middleware.
- `method` is limited to the fixed HTTP method set; `status` must be an int
  status string; `route` must be a path template — a route segment that looks
  like a UUID, long hex blob or long number is rejected. Worst-case series
  count is bounded by routes × methods × statuses actually served.

## Why identifiers are excluded

Series with request IDs, client addresses or concrete paths are unbounded and
leak customer context into a scraping pipeline. The metrics surface carries
application behaviour only; per-request forensics belongs to the audit chain,
which is pseudonymised and verified (A1), not to metrics.

## Run Prometheus locally

```powershell
docker run --rm -p 9090:9090 `
  -v D:\path\to\repo\infra\observability\prometheus.yml:/etc/prometheus/prometheus.yml `
  prom/prometheus
```

Then open <http://localhost:9090> and query e.g.
`rate(http_requests_total[5m])`. No Grafana dashboards are required; a JSON
may be added later without changing the exposition.

