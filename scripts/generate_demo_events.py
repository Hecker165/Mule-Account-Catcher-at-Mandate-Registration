"""Generate reproducible local-demo replay files from a scenario definition.

Writes one directory per scenario containing sessions.jsonl, telemetry.jsonl,
webhooks.jsonl and manifest.json. Webhook bodies are built exactly as the
demo runner builds them (fixture bytes, optional notes injection, signed
after modification). Never calls the API and never touches Redis/Postgres.

WARNING: outputs are for local demo replay only; they carry test signatures
made with a non-production secret and must never be sent to production.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "data" / "scenarios"
FIXTURES_DIR = ROOT / "data" / "fixtures" / "webhooks"

WARNING = (
    "WARNING: generated files are for local demo replay only "
    "(test signatures, fake customers/devices); never replay against production."
)


def _rand8() -> str:
    return uuid4().hex[:8]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_base_time(raw: str) -> datetime:
    moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("--base-time must be a timezone-aware ISO timestamp")
    return moment.astimezone(UTC)


def _session_body(
    namespace: str, customer: str, device: str, flow_seconds: int, base: datetime
) -> dict[str, object]:
    return {
        "merchant_namespace": namespace,
        "checkout_order_ref": f"demo-{_rand8()}",
        "customer_reference": customer,
        "device_fingerprint": device,
        "flow_started_at": _iso(base - timedelta(seconds=flow_seconds)),
        "mandate_intent": {},
    }


def _session_body_with_terms(
    namespace: str, customer: str, device: str, base: datetime
) -> dict[str, object]:
    body = _session_body(namespace, customer, device, 2, base)
    body["mandate_intent"] = {"max_amount_paise": 5_000_000}
    return body


def _telemetry_body(base: datetime) -> dict[str, object]:
    return {"flow_completed_at": _iso(base)}


def _webhook_entry(
    fixture: str,
    event_id: str,
    secret: str,
    correlate_session_id: str | None,
    demo_header: bool,
) -> dict[str, object]:
    raw = (FIXTURES_DIR / fixture).read_bytes()
    body = json.loads(raw)
    if correlate_session_id is not None:
        body["payload"]["token"]["entity"]["notes"] = {"risk_session_id": correlate_session_id}
    canonical = json.dumps(body, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    headers: dict[str, str] = {"x-razorpay-event-id": event_id}
    if demo_header:
        headers["X-Demo-Event"] = "true"
    return {
        "body": canonical.decode("utf-8"),
        "signature": signature,
        "headers": headers,
    }


def _build(
    name: str, params: dict[str, object], base: datetime, secret: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Return (sessions, telemetry, webhooks) mirroring the runner's order."""
    tag = _rand8()
    sessions: list[dict[str, object]] = []
    telemetry: list[dict[str, object]] = []
    webhooks: list[dict[str, object]] = []

    def add_session(namespace: str, customer: str, device: str, flow_seconds: int) -> None:
        sessions.append(_session_body(namespace, customer, device, flow_seconds, base))
        telemetry.append(_telemetry_body(base))

    if name == "legitimate_flow":
        prefix = str(params.get("namespace_prefix", "demo_legit_"))
        total = int(params.get("sessions", 20))
        flow = int(params.get("flow_seconds", 30))
        for index in range(1, total + 1):
            add_session(
                f"{prefix}{index:02d}",
                f"demo-customer-legit-{tag}-{index:02d}",
                f"demo-device-legit-{tag}-{index:02d}",
                flow,
            )
    elif name == "bot_burst":
        namespace = str(params.get("namespace", "demo_bot_burst"))
        total = int(params.get("sessions", 24))
        flow = int(params.get("flow_seconds", 2))
        for _ in range(total):
            add_session(namespace, f"demo-customer-{tag}", f"demo-device-{tag}", flow)
    elif name in ("confirmed_high_risk_block", "worker_failure_recovery"):
        seed = params.get("seed", {})
        assert isinstance(seed, dict)
        namespace = str(seed.get("namespace", "demo_high_risk"))
        total = int(seed.get("sessions", 21))
        flow = int(seed.get("flow_seconds", 30))
        for _ in range(total):
            add_session(namespace, f"demo-customer-{tag}", f"demo-device-{tag}", flow)
        correlated_id = str(uuid4())
        sessions.append(
            _session_body_with_terms(
                namespace, f"demo-customer-correlated-{tag}", f"demo-device-{tag}", base
            )
        )
        telemetry.append(_telemetry_body(base))
        webhooks.append(
            _webhook_entry(
                str(params.get("fixture", "token_confirmed_body.json")),
                f"{params.get('event_id_prefix', 'evt_demo_')}{_rand8()}",
                secret,
                correlated_id,
                bool(params.get("demo_header", True)),
            )
        )
    elif name == "npci_risk_rejection":
        webhooks.append(
            _webhook_entry(
                str(params.get("fixture", "token_rejected_npci_risk_body.json")),
                f"{params.get('event_id_prefix', 'evt_demo_rejected_')}{_rand8()}",
                secret,
                None,
                bool(params.get("demo_header", True)),
            )
        )
    elif name == "duplicate_webhook":
        entry = _webhook_entry(
            str(params.get("fixture", "token_confirmed_body.json")),
            f"{params.get('event_id_prefix', 'evt_demo_dup_')}{_rand8()}",
            secret,
            None,
            bool(params.get("demo_header", True)),
        )
        webhooks.extend([entry, entry])
    elif name == "shared_demo_merchant_velocity":
        namespaces = params.get("namespaces", [])
        assert isinstance(namespaces, list)
        flow = int(params.get("flow_seconds", 30))
        for index, namespace in enumerate(namespaces[:3]):
            add_session(
                str(namespace),
                f"demo-customer-shared-{tag}-{index}",
                f"demo-device-{tag}",
                flow,
            )
    else:
        raise ValueError(f"unknown scenario: {name}")
    return sessions, telemetry, webhooks


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def generate(name: str, out_dir: Path, base: datetime, secret: str) -> Path:
    """Generate replay files for one scenario; return the scenario directory."""
    definition = json.loads((SCENARIOS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    params = definition.get("params", {})
    assert isinstance(params, dict)
    sessions, telemetry, webhooks = _build(name, params, base, secret)
    target = out_dir / name
    target.mkdir(parents=True, exist_ok=True)
    _write_jsonl(target / "sessions.jsonl", sessions)
    _write_jsonl(target / "telemetry.jsonl", telemetry)
    _write_jsonl(target / "webhooks.jsonl", webhooks)
    manifest = {
        "scenario": name,
        "generated_at": _iso(datetime.now(UTC)),
        "sessions": len(sessions),
        "telemetry": len(telemetry),
        "webhooks": len(webhooks),
        "secret_fingerprint": secret[:4],
        "note": "random UUID/checkout-ref/event-id suffixes differ per run",
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate local demo replay files.")
    parser.add_argument("--scenario", default="all", help="{name|all}")
    parser.add_argument("--out-dir", default="data/scenarios/generated")
    parser.add_argument("--secret", default="local-test-secret")
    parser.add_argument("--base-time", default="2026-01-15T10:00:00Z")
    args = parser.parse_args()

    if args.secret.startswith("rzp_live"):
        print("refusing a live Razorpay secret for demo file generation", file=sys.stderr)
        sys.exit(2)
    try:
        base = _parse_base_time(args.base_time)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)

    if args.scenario == "all":
        names = sorted(path.stem for path in SCENARIOS_DIR.glob("*.json"))
    else:
        names = [args.scenario]
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    for name in names:
        target = generate(name, out_dir, base, args.secret)
        print(f"wrote {target}")
    print(WARNING)


if __name__ == "__main__":
    main()
