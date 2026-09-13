"""Demo control API: scenario listing and synchronous runs.

All responses set ``Cache-Control: no-store``. The whole router is hidden
(404) when ``app_env == "production"``. Run failures are data (HTTP 200 with
per-check verdicts), not HTTP errors; only unknown names (404), concurrent
runs (409) and missing definitions (500) are HTTP errors.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.settings import get_settings
from app.services.demo_runner import (
    DemoDefinitionsUnavailableError,
    DemoScenarioRunner,
    ScenarioAlreadyRunningError,
    UnknownDemoScenarioError,
)

router = APIRouter(prefix="/demo", tags=["demo"])

_NO_STORE = "no-store"


class ScenarioSummaryItem(BaseModel):
    name: str
    title: str
    description: str
    expected_outcome: str


class ScenarioListResponse(BaseModel):
    scenarios: list[ScenarioSummaryItem]


class ScenarioCheckItem(BaseModel):
    check: str
    passed: bool
    detail: str


class ScenarioRunResponse(BaseModel):
    name: str
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    checks: list[ScenarioCheckItem] = Field(default_factory=list)


def _is_production() -> bool:
    return get_settings().app_env == "production"


def _webhook_secret() -> str | None:
    settings = get_settings()
    secret = settings.razorpay_webhook_secret
    if secret is None:
        return None
    value = secret.get_secret_value()
    return value or None


def get_demo_runner(app: FastAPI) -> DemoScenarioRunner:
    """Return the cached runner, building the production one on first use.

    Tests override ``app.state.demo_runner`` directly with an ASGITransport
    runner; the cached instance keeps ``runner.locks`` stable for the 409 path.
    """
    runner = getattr(app.state, "demo_runner", None)
    if runner is None:
        settings = get_settings()
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{settings.api_port}", timeout=30.0)
        runner = DemoScenarioRunner(
            http=http,
            webhook_secret=_webhook_secret(),
            revoke_client=getattr(app.state, "token_revoke_client", None),
        )
        app.state.demo_runner = runner
    return runner


def _iso_z(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@router.get("/scenarios", response_model=ScenarioListResponse)
async def list_demo_scenarios(
    request: Request, response: Response
) -> JSONResponse | ScenarioListResponse:
    """List exactly the seven demo scenarios."""
    if _is_production():
        return JSONResponse(
            status_code=404, content={"detail": "not found"}, headers={"Cache-Control": _NO_STORE}
        )
    try:
        summaries = get_demo_runner(request.app).list_scenarios()
    except DemoDefinitionsUnavailableError:
        return JSONResponse(
            status_code=500,
            content={"detail": "demo scenario definitions are unavailable"},
            headers={"Cache-Control": _NO_STORE},
        )
    response.headers["Cache-Control"] = _NO_STORE
    return ScenarioListResponse(
        scenarios=[
            ScenarioSummaryItem(
                name=summary.name,
                title=summary.title,
                description=summary.description,
                expected_outcome=summary.expected_outcome,
            )
            for summary in summaries
        ]
    )


@router.post("/scenarios/{name}/run", response_model=ScenarioRunResponse)
async def run_demo_scenario(
    request: Request, response: Response, name: str
) -> JSONResponse | ScenarioRunResponse:
    """Execute one scenario synchronously and return its per-check verdict."""
    if _is_production():
        return JSONResponse(
            status_code=404, content={"detail": "not found"}, headers={"Cache-Control": _NO_STORE}
        )
    runner = get_demo_runner(request.app)
    try:
        result = await runner.run(name)
    except UnknownDemoScenarioError:
        return JSONResponse(
            status_code=404,
            content={"detail": "unknown demo scenario"},
            headers={"Cache-Control": _NO_STORE},
        )
    except ScenarioAlreadyRunningError:
        return JSONResponse(
            status_code=409,
            content={"detail": "scenario run already in progress"},
            headers={"Cache-Control": _NO_STORE},
        )
    response.headers["Cache-Control"] = _NO_STORE
    return ScenarioRunResponse(
        name=result.name,
        status=result.status,
        started_at=_iso_z(result.started_at),
        finished_at=_iso_z(result.finished_at),
        duration_ms=result.duration_ms,
        checks=[
            ScenarioCheckItem(check=check.check, passed=check.passed, detail=check.detail)
            for check in result.checks
        ],
    )
