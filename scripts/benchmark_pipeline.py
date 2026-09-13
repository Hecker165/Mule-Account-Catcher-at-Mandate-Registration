"""Benchmark webhook-to-assessment and pre-check latency against arch targets.

Sends signed webhooks and pre-checks to a running stack, reports n/p50/p95
against the 200 ms (webhook) and 150 ms (pre-check) targets, and always exits
0: slower machines show real measured numbers instead of failing. Never
includes secrets or full request bodies in the output (JSON on stdout, the
human-readable summary on stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import platform
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONFIRMED_FIXTURE = ROOT / "data" / "fixtures" / "webhooks" / "token_confirmed_body.json"

WEBHOOK_TARGET_MS = 200
PRECHECK_TARGET_MS = 150


def _refuse_live_secret(secret: str) -> None:
    if secret.startswith("rzp_live"):
        print("refusing a live Razorpay secret for benchmarking", file=sys.stderr)
        sys.exit(2)


def _sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _percentile(sorted_ms: list[float], pct: float) -> float:
    if not sorted_ms:
        return 0.0
    import math

    index = min(len(sorted_ms) - 1, max(0, math.ceil(pct / 100 * len(sorted_ms)) - 1))
    return sorted_ms[index]


def _summary(times_ms: list[float], target_ms: int) -> dict[str, object]:
    ordered = sorted(times_ms)
    p50 = statistics.median(ordered) if ordered else 0.0
    p95 = _percentile(ordered, 95)
    return {
        "n": len(ordered),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "target_ms": target_ms,
        "within_target": bool(ordered) and p95 <= target_ms,
    }


def _webhook_times(client: httpx.Client, secret: str, samples: int) -> list[float]:
    raw = CONFIRMED_FIXTURE.read_bytes()
    times: list[float] = []
    for _ in range(samples):
        event_id = f"evt_bench_{uuid4().hex[:8]}"
        start = time.perf_counter()
        response = client.post(
            "/v1/webhooks/razorpay",
            content=raw,
            headers={
                "x-razorpay-signature": _sign(raw, secret),
                "x-razorpay-event-id": event_id,
                "X-Demo-Event": "true",
            },
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.raise_for_status()
        times.append(elapsed_ms)
    return times


def _precheck_times(client: httpx.Client, samples: int) -> list[float]:
    now = datetime.now(UTC)
    times: list[float] = []
    for index in range(samples):
        tag = uuid4().hex[:8]
        created = client.post(
            "/v1/risk-sessions",
            json={
                "merchant_namespace": "demo_benchmark",
                "checkout_order_ref": f"demo-bench-{tag}-{index}",
                "customer_reference": f"demo-customer-bench-{tag}-{index}",
                "device_fingerprint": f"demo-device-bench-{tag}-{index}",
                "flow_started_at": (now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
                "mandate_intent": {},
            },
        )
        created.raise_for_status()
        session_id = created.json()["risk_session_id"]
        telemetry = client.patch(
            f"/v1/risk-sessions/{session_id}/telemetry",
            json={"flow_completed_at": now.isoformat().replace("+00:00", "Z")},
        )
        telemetry.raise_for_status()
        start = time.perf_counter()
        precheck = client.post(f"/v1/risk-sessions/{session_id}/precheck")
        elapsed_ms = (time.perf_counter() - start) * 1000
        precheck.raise_for_status()
        times.append(elapsed_ms)
    return times


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the risk pipeline.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--secret", default="local-test-secret")
    args = parser.parse_args()
    _refuse_live_secret(args.secret)
    if args.samples < 1:
        print("--samples must be >= 1", file=sys.stderr)
        sys.exit(2)

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        webhook = _summary(_webhook_times(client, args.secret, args.samples), WEBHOOK_TARGET_MS)
        precheck = _summary(_precheck_times(client, args.samples), PRECHECK_TARGET_MS)

    print(json.dumps({"webhook": webhook, "precheck": precheck}))
    context = f"{platform.system()}/{platform.release()} {platform.machine()} ({platform.node()})"
    print(
        f"machine: {context} | base_url: {args.base_url} | "
        f"webhook n={webhook['n']} p50={webhook['p50_ms']}ms "
        f"p95={webhook['p95_ms']}ms target={WEBHOOK_TARGET_MS}ms "
        f"within={webhook['within_target']} | "
        f"precheck n={precheck['n']} p50={precheck['p50_ms']}ms "
        f"p95={precheck['p95_ms']}ms target={PRECHECK_TARGET_MS}ms "
        f"within={precheck['within_target']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
