"""A9 route tests: listing, guards, conflicts and response shape.

Uses httpx ASGITransport over the real create_app() (no running server).
"""

from __future__ import annotations

import json
from datetime import datetime
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

EXPECTED_NAMES = {
    "legitimate_flow",
    "bot_burst",
    "npci_risk_rejection",
    "confirmed_high_risk_block",
    "duplicate_webhook",
    "worker_failure_recovery",
    "shared_demo_merchant_velocity",
}


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
async def demo_runner(app: Any) -> Any:
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    runner = DemoScenarioRunner(
        http=http,
        webhook_secret=TEST_WEBHOOK_SECRET,
        revoke_client=app.state.token_revoke_client,
    )
    app.state.demo_runner = runner
    yield runner
    await http.aclose()


@pytest.fixture
async def client(app: Any, demo_runner: DemoScenarioRunner) -> Any:
    _flush_demo_redis()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def test_demo_scenarios_route_lists_seven_scenarios(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/demo/scenarios")

    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store"
    data = response.json()
    assert set(scenario["name"] for scenario in data["scenarios"]) == EXPECTED_NAMES
    assert len(data["scenarios"]) == 7
    for scenario in data["scenarios"]:
        assert scenario["title"]
        assert scenario["description"]
        assert scenario["expected_outcome"]


async def test_demo_scenario_list_matches_definition_files(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/demo/scenarios")

    assert response.status_code == 200
    data = response.json()
    by_name = {scenario["name"]: scenario for scenario in data["scenarios"]}
    root = Path(__file__).resolve().parents[4]
    files = sorted((root / "data" / "scenarios").glob("*.json"))
    assert len(files) == 7
    for path in files:
        definition = json.loads(path.read_text(encoding="utf-8"))
        listed = by_name[definition["name"]]
        assert listed["title"] == definition["title"]
        assert listed["description"] == definition["description"]
        assert listed["expected_outcome"] == definition["expected_outcome"]


async def test_demo_route_unknown_scenario_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/demo/scenarios/no_such_scenario/run")

    assert response.status_code == 404
    assert response.json() == {"detail": "unknown demo scenario"}
    assert response.headers.get("Cache-Control") == "no-store"


async def test_demo_route_is_hidden_when_app_env_is_production(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        listed = await client.get("/v1/demo/scenarios")
        assert listed.status_code == 404
        assert listed.headers.get("Cache-Control") == "no-store"

        run = await client.post("/v1/demo/scenarios/legitimate_flow/run")
        assert run.status_code == 404
        assert run.headers.get("Cache-Control") == "no-store"
    finally:
        get_settings.cache_clear()


async def test_demo_route_concurrent_run_returns_409(
    client: httpx.AsyncClient, demo_runner: DemoScenarioRunner
) -> None:
    lock = demo_runner.locks["legitimate_flow"]
    await lock.acquire()
    try:
        response = await client.post("/v1/demo/scenarios/legitimate_flow/run")
    finally:
        lock.release()

    assert response.status_code == 409
    assert response.json() == {"detail": "scenario run already in progress"}
    assert response.headers.get("Cache-Control") == "no-store"


async def test_demo_route_sets_no_store(client: httpx.AsyncClient) -> None:
    listed = await client.get("/v1/demo/scenarios")
    assert listed.headers.get("Cache-Control") == "no-store"

    unknown = await client.post("/v1/demo/scenarios/no_such_scenario/run")
    assert unknown.status_code == 404
    assert unknown.headers.get("Cache-Control") == "no-store"


async def test_scenario_result_shape_has_checks_and_timings(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/demo/scenarios/legitimate_flow/run")

    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store"
    data = response.json()
    assert data["name"] == "legitimate_flow"
    assert data["status"] == "SUCCEEDED"
    started = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
    finished = datetime.fromisoformat(data["finished_at"].replace("Z", "+00:00"))
    assert finished >= started
    assert data["duration_ms"] >= 0
    assert len(data["checks"]) == 2
    for check in data["checks"]:
        assert check["check"]
        assert check["passed"] is True
        assert check["detail"]
