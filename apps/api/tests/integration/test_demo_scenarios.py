"""A9 scenario tests: all seven scenarios run green against the real stack.

Each test drives the DemoScenarioRunner exactly as the demo route does: real
HTTP over ASGI, real PostgreSQL, real Redis, real rule engine and the mock
revoke client. Webhook scenarios exercise real server-side signing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis as sync_redis

from app.core.settings import get_settings
from app.main import create_app
from app.persistence.session import dispose_engine
from app.services.demo_runner import DemoScenarioRunner
from app.services.webhook_signature import WebhookSignatureVerifier
from tests.helpers.webhook_signing import TEST_WEBHOOK_SECRET

ROOT = Path(__file__).resolve().parents[4]


def _flush_demo_redis() -> None:
    """Delete the app's velocity keys so each test starts with cold windows."""
    client = sync_redis.Redis.from_url(get_settings().redis_url)
    try:
        for key in client.scan_iter("mg:v1*"):
            client.delete(key)
    finally:
        client.close()


@pytest.fixture(autouse=True)
async def _fresh_engine() -> Any:
    """Rebind the global transaction() engine to the current test loop."""
    await dispose_engine()
    yield
    await dispose_engine()


@pytest.fixture
def app(test_session_factory: Any) -> Any:
    application = create_app()
    application.state.session_factory = test_session_factory
    application.state.webhook_signature_verifier = WebhookSignatureVerifier(TEST_WEBHOOK_SECRET)
    return application


@pytest.fixture
async def runner(app: Any) -> Any:
    _flush_demo_redis()
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    demo_runner = DemoScenarioRunner(
        http=http,
        webhook_secret=TEST_WEBHOOK_SECRET,
        revoke_client=app.state.token_revoke_client,
    )
    yield demo_runner
    await http.aclose()


def _checks(result: Any) -> dict[str, Any]:
    return {check.check: check for check in result.checks}


async def test_legitimate_flow_all_allow_zero_false_positives(runner: DemoScenarioRunner) -> None:
    result = await runner.run("legitimate_flow")

    assert result.status == "SUCCEEDED"
    checks = _checks(result)
    assert checks["all_sessions_allowed"].passed is True
    assert checks["zero_false_positives"].passed is True


async def test_bot_burst_challenges_at_least_18_of_24(runner: DemoScenarioRunner) -> None:
    result = await runner.run("bot_burst")

    assert result.status == "SUCCEEDED"
    assert _checks(result)["at_least_18_challenged"].passed is True


async def test_bot_burst_borderline_allows_are_explained(runner: DemoScenarioRunner) -> None:
    result = await runner.run("bot_burst")

    checks = _checks(result)
    assert checks["borderline_allows_explained"].passed is True
    assert "scored 20" in checks["borderline_allows_explained"].detail


async def test_npc_i_risk_rejection_scores_100_without_revoke(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("npci_risk_rejection")

    assert result.status == "SUCCEEDED"
    checks = _checks(result)
    assert checks["webhook_processed"].passed is True
    assert checks["score_100_block"].passed is True
    assert checks["npci_reason_present"].passed is True
    assert checks["no_revoke_queued"].passed is True


async def test_confirmed_high_risk_block_queues_exactly_one_revoke(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("confirmed_high_risk_block")

    assert result.status == "SUCCEEDED"
    checks = _checks(result)
    assert checks["score_100_block"].passed is True
    assert checks["exactly_one_revoke"].passed is True
    assert checks["revoke_lifecycle_started"].passed is True


async def test_confirmed_high_risk_block_audit_chain_verifies(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("confirmed_high_risk_block")

    assert _checks(result)["audit_chain_valid"].passed is True


async def test_duplicate_webhook_yields_single_event_and_assessment(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("duplicate_webhook")

    assert result.status == "SUCCEEDED"
    checks = _checks(result)
    assert checks["first_delivery_processed"].passed is True
    assert checks["second_delivery_duplicate"].passed is True
    assert checks["single_event_and_assessment"].passed is True


async def test_worker_failure_recovery_retries_then_succeeds(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("worker_failure_recovery")

    assert result.status == "SUCCEEDED"
    checks = _checks(result)
    assert checks["first_attempt_retried"].passed is True
    assert checks["final_status_succeeded"].passed is True


async def test_worker_failure_recovery_has_exactly_two_attempts(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("worker_failure_recovery")

    checks = _checks(result)
    assert checks["exactly_two_attempts"].passed is True
    assert checks["no_duplicate_revocation"].passed is True


async def test_shared_demo_merchant_velocity_challenges_third_session(
    runner: DemoScenarioRunner,
) -> None:
    result = await runner.run("shared_demo_merchant_velocity")

    assert result.status == "SUCCEEDED"
    checks = _checks(result)
    assert checks["third_session_challenged"].passed is True
    assert checks["demo_counter_is_three"].passed is True
    assert checks["demo_source_labelled"].passed is True


async def test_webhook_scenarios_fail_honestly_without_secret(app: Any) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        secretless = DemoScenarioRunner(
            http=http,
            webhook_secret=None,
            revoke_client=app.state.token_revoke_client,
        )
        rejected = await secretless.run("npci_risk_rejection")
        assert rejected.status == "FAILED"
        assert len(rejected.checks) == 1
        assert rejected.checks[0].check == "webhook_secret_configured"
        assert rejected.checks[0].passed is False
        assert rejected.checks[0].detail == "webhook signature verification is not configured"

        duplicated = await secretless.run("duplicate_webhook")
        assert duplicated.status == "FAILED"
        assert duplicated.checks[0].check == "webhook_secret_configured"


async def test_scenario_results_carry_checks_and_timings(runner: DemoScenarioRunner) -> None:
    result = await runner.run("shared_demo_merchant_velocity")

    assert result.finished_at >= result.started_at
    assert result.duration_ms >= 0
    assert len(result.checks) == 3
    for check in result.checks:
        assert check.check
        assert isinstance(check.passed, bool)
        assert check.detail


def _run_script(*args: str, cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "--directory", "apps/api", "run", "python", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_generate_demo_events_writes_signed_files_for_a_scenario(tmp_path: Path) -> None:
    completed = _run_script(
        "../../scripts/generate_demo_events.py",
        "--scenario",
        "duplicate_webhook",
        "--out-dir",
        str(tmp_path / "generated"),
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr

    target = tmp_path / "generated" / "duplicate_webhook"
    sessions = (target / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
    webhooks = (target / "webhooks.jsonl").read_text(encoding="utf-8").splitlines()
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert sessions == []
    assert len(webhooks) == 2
    for line in webhooks:
        entry = json.loads(line)
        recomputed = hmac.new(
            TEST_WEBHOOK_SECRET.encode("utf-8"),
            entry["body"].encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert recomputed == entry["signature"]
        assert entry["headers"]["x-razorpay-event-id"]
    assert manifest["scenario"] == "duplicate_webhook"
    assert manifest["webhooks"] == 2
    assert manifest["secret_fingerprint"] == TEST_WEBHOOK_SECRET[:4]


def test_generate_demo_events_refuses_live_secrets(tmp_path: Path) -> None:
    completed = _run_script(
        "../../scripts/generate_demo_events.py",
        "--scenario",
        "bot_burst",
        "--out-dir",
        str(tmp_path / "generated"),
        "--secret",
        "rzp_live_abc123",
        cwd=ROOT,
    )
    assert completed.returncode != 0


def test_benchmark_pipeline_reports_percentiles_json(
    test_session_factory: Any, tmp_path: Path
) -> None:
    holder: dict[str, Any] = {}

    def _serve() -> None:
        holder["app"] = create_app()
        holder["app"].state.session_factory = test_session_factory
        holder["app"].state.webhook_signature_verifier = WebhookSignatureVerifier(
            TEST_WEBHOOK_SECRET
        )
        import uvicorn

        holder["server"] = uvicorn.Server(
            uvicorn.Config(holder["app"], host="127.0.0.1", port=8119, log_level="error")
        )
        holder["server"].run()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                response = httpx.get("http://127.0.0.1:8119/healthz", timeout=2.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("benchmark app did not start on port 8119")
        _flush_demo_redis()
        completed = _run_script(
            "../../scripts/benchmark_pipeline.py",
            "--base-url",
            "http://127.0.0.1:8119",
            "--samples",
            "5",
            "--secret",
            TEST_WEBHOOK_SECRET,
            cwd=ROOT,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        for section, target_ms in (("webhook", 200), ("precheck", 150)):
            assert payload[section]["n"] == 5
            assert payload[section]["p50_ms"] >= 0
            assert payload[section]["p95_ms"] >= payload[section]["p50_ms"]
            assert payload[section]["target_ms"] == target_ms
            assert isinstance(payload[section]["within_target"], bool)
        assert TEST_WEBHOOK_SECRET not in completed.stdout
    finally:
        server = holder.get("server")
        if server is not None:
            server.should_exit = True
        thread.join(timeout=15)


if __name__ == "__main__":
    print("run via pytest: pytest tests/integration/test_demo_scenarios.py -q")
