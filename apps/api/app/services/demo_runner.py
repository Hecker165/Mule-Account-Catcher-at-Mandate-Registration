"""Demo scenario runner.

Executes the seven ``data/scenarios`` definitions reproducibly against the
real running stack (the same public HTTP endpoints a real integration would
use, plus server-side webhook signing) and reports honest per-check verdicts.

A check that cannot pass is reported as failed with a clear detail, never
papered over. No raw VPA/IP/secret ever appears in results: details carry
counts, scores and decisions only.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx

from app.contracts.common import ActionStatus, Availability, DecisionStage
from app.integrations.razorpay.client import RazorpayTokenRevokeClient
from app.integrations.razorpay.mock_client import MockTokenRevokeClient
from app.persistence.session import transaction
from app.repositories import ActionRepository, AuditRepository, RiskAssessmentRepository
from app.repositories.action_attempt_read import ActionAttemptReadRepository
from app.repositories.demo_read import DemoReadRepository
from app.workers.outbox_worker import OutboxWorker

_SIGNING_GUARD_DETAIL = "webhook signature verification is not configured"
_RERUN_NOTE = (
    "Re-running within 5 minutes may raise velocity counts "
    "(bot burst stays green; legitimate flow stays green up to four runs "
    "per 5-minute window)."
)


@dataclass(frozen=True)
class ScenarioCheck:
    check: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: str  # "SUCCEEDED" | "FAILED"
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    checks: tuple[ScenarioCheck, ...]


@dataclass(frozen=True)
class ScenarioSummary:
    name: str
    title: str
    description: str
    expected_outcome: str


class UnknownDemoScenarioError(ValueError):
    """Raised when no scenario definition matches the requested name."""


class ScenarioAlreadyRunningError(RuntimeError):
    """Raised when the same scenario is already running."""


class DemoDefinitionsUnavailableError(RuntimeError):
    """Raised when the scenario definitions directory cannot be read."""


def _default_scenarios_dir() -> Path:
    # services/demo_runner.py -> app -> api -> apps -> repo root
    return Path(__file__).resolve().parents[4] / "data" / "scenarios"


def _default_fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "fixtures" / "webhooks"


class DemoScenarioRunner:
    """Runs declarative demo scenarios against the real stack over HTTP."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        webhook_secret: str | None,
        revoke_client: RazorpayTokenRevokeClient | MockTokenRevokeClient | None,
        scenarios_dir: Path | None = None,
        fixtures_dir: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._http = http
        self._webhook_secret = webhook_secret
        self._revoke_client = revoke_client
        self._scenarios_dir = scenarios_dir or _default_scenarios_dir()
        self._fixtures_dir = fixtures_dir or _default_fixtures_dir()
        self._clock = clock
        self._uuid_factory = uuid_factory
        self.locks: dict[str, asyncio.Lock] = {}
        for summary in self._try_list():
            self.locks.setdefault(summary.name, asyncio.Lock())

    def list_scenarios(self) -> list[ScenarioSummary]:
        """List the seven scenario definitions; 500-worthy errors raise."""
        try:
            entries = sorted(self._scenarios_dir.glob("*.json"))
            if not entries:
                raise DemoDefinitionsUnavailableError("no scenario definitions found")
            summaries = [self._read_summary(path) for path in entries]
        except DemoDefinitionsUnavailableError:
            raise
        except Exception as exc:
            raise DemoDefinitionsUnavailableError(
                "demo scenario definitions are unavailable"
            ) from exc
        for summary in summaries:
            self.locks.setdefault(summary.name, asyncio.Lock())
        return summaries

    async def run(self, name: str) -> ScenarioResult:
        """Execute one scenario synchronously; unknown names and conflicts raise."""
        definition = self._load_definition(name)
        scenario_name = str(definition.get("name", name))
        lock = self.locks.setdefault(scenario_name, asyncio.Lock())
        if lock.locked():
            raise ScenarioAlreadyRunningError(f"scenario run already in progress: {name}")
        async with lock:
            return await self._execute(scenario_name, definition)

    async def _execute(self, name: str, definition: dict[str, Any]) -> ScenarioResult:
        started_at = self._clock()
        try:
            params = definition.get("params", {})
            if not isinstance(params, dict):
                params = {}
            checks = await self._dispatch(name, params)
        except Exception as exc:  # failure is data, not an HTTP error
            checks = (
                ScenarioCheck(
                    check="unexpected_error",
                    passed=False,
                    detail=f"unexpected {type(exc).__name__} during scenario execution",
                ),
            )
        finished_at = self._clock()
        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        status = "SUCCEEDED" if all(check.passed for check in checks) else "FAILED"
        return ScenarioResult(
            name=name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            checks=checks,
        )

    async def _dispatch(self, name: str, params: dict[str, Any]) -> tuple[ScenarioCheck, ...]:
        if name == "legitimate_flow":
            return await self._run_legitimate_flow(params)
        if name == "bot_burst":
            return await self._run_bot_burst(params)
        if name == "npci_risk_rejection":
            return await self._run_npci_risk_rejection(params)
        if name == "confirmed_high_risk_block":
            return await self._run_confirmed_high_risk_block(params)
        if name == "duplicate_webhook":
            return await self._run_duplicate_webhook(params)
        if name == "worker_failure_recovery":
            return await self._run_worker_failure_recovery(params)
        if name == "shared_demo_merchant_velocity":
            return await self._run_shared_demo_merchant_velocity(params)
        raise UnknownDemoScenarioError(f"unknown demo scenario: {name}")

    # ── Building blocks ────────────────────────────────────────────────

    def _uuid8(self) -> str:
        return self._uuid_factory().hex[:8]

    @staticmethod
    def _iso(moment: datetime) -> str:
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def _create_ready_session(
        self,
        *,
        namespace: str,
        customer_ref: str,
        device_fp: str,
        flow_seconds: int,
        max_amount_paise: int | None = None,
    ) -> str:
        """Create a session and record telemetry (READY, no assessment).

        No pre-check runs here: the caller either pre-checks (session-based
        scenarios) or lets a later webhook evaluation own the session's single
        feature snapshot (A1 allows one snapshot per session per version).
        """
        now = self._clock()
        intent: dict[str, Any] = {}
        if max_amount_paise is not None:
            intent["max_amount_paise"] = max_amount_paise
        create_resp = await self._http.post(
            "/v1/risk-sessions",
            json={
                "merchant_namespace": namespace,
                "checkout_order_ref": f"demo-{self._uuid8()}",
                "customer_reference": customer_ref,
                "device_fingerprint": device_fp,
                "flow_started_at": self._iso(now - timedelta(seconds=flow_seconds)),
                "mandate_intent": intent,
            },
        )
        create_resp.raise_for_status()
        session_id = str(create_resp.json()["risk_session_id"])
        telemetry_resp = await self._http.patch(
            f"/v1/risk-sessions/{session_id}/telemetry",
            json={"flow_completed_at": self._iso(now)},
        )
        telemetry_resp.raise_for_status()
        return session_id

    async def _drive_session(
        self,
        *,
        namespace: str,
        customer_ref: str,
        device_fp: str,
        flow_seconds: int,
        max_amount_paise: int | None = None,
    ) -> dict[str, Any]:
        """Create a session, record telemetry and run the pre-check over HTTP."""
        session_id = await self._create_ready_session(
            namespace=namespace,
            customer_ref=customer_ref,
            device_fp=device_fp,
            flow_seconds=flow_seconds,
            max_amount_paise=max_amount_paise,
        )
        precheck_resp = await self._http.post(f"/v1/risk-sessions/{session_id}/precheck")
        precheck_resp.raise_for_status()
        return {"risk_session_id": session_id, "assessment": precheck_resp.json()}

    def _signed_webhook_body(self, fixture: str, correlate_session_id: str | None) -> bytes:
        raw = (self._fixtures_dir / fixture).read_bytes()
        body = json.loads(raw)
        if correlate_session_id is not None:
            entity = body["payload"]["token"]["entity"]
            entity["notes"] = {"risk_session_id": correlate_session_id}
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    async def _post_webhook(
        self, raw_body: bytes, event_id: str, demo_header: bool
    ) -> httpx.Response:
        assert self._webhook_secret is not None
        signature = hmac.new(
            self._webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        headers = {
            "x-razorpay-signature": signature,
            "x-razorpay-event-id": event_id,
        }
        if demo_header:
            headers["X-Demo-Event"] = "true"
        return await self._http.post("/v1/webhooks/razorpay", content=raw_body, headers=headers)

    def _secret_guard(self) -> ScenarioCheck | None:
        if self._webhook_secret is None:
            return ScenarioCheck(
                check="webhook_secret_configured",
                passed=False,
                detail=_SIGNING_GUARD_DETAIL,
            )
        return None

    # ── Scenarios ──────────────────────────────────────────────────────

    async def _run_legitimate_flow(self, params: dict[str, Any]) -> tuple[ScenarioCheck, ...]:
        prefix = str(params.get("namespace_prefix", "demo_legit_"))
        total = int(params.get("sessions", 20))
        flow_seconds = int(params.get("flow_seconds", 30))
        run_tag = self._uuid8()
        decisions: list[str] = []
        for index in range(1, total + 1):
            namespace = f"{prefix}{index:02d}"
            driven = await self._drive_session(
                namespace=namespace,
                customer_ref=f"demo-customer-legit-{run_tag}-{index:02d}",
                device_fp=f"demo-device-legit-{run_tag}-{index:02d}",
                flow_seconds=flow_seconds,
            )
            decisions.append(str(driven["assessment"]["decision"]))
        allowed = sum(1 for decision in decisions if decision == "ALLOW")
        others = total - allowed
        return (
            ScenarioCheck(
                check="all_sessions_allowed",
                passed=allowed == total,
                detail=f"{allowed}/{total} pre-checks returned ALLOW.",
            ),
            ScenarioCheck(
                check="zero_false_positives",
                passed=others == 0,
                detail=f"{others} sessions challenged or blocked. {_RERUN_NOTE}",
            ),
        )

    async def _run_bot_burst(self, params: dict[str, Any]) -> tuple[ScenarioCheck, ...]:
        namespace = str(params.get("namespace", "demo_bot_burst"))
        total = int(params.get("sessions", 24))
        flow_seconds = int(params.get("flow_seconds", 2))
        run_tag = self._uuid8()
        shared_device = f"demo-device-{run_tag}"
        shared_customer = f"demo-customer-{run_tag}"
        outcomes: list[tuple[int, str, int]] = []
        for index in range(1, total + 1):
            driven = await self._drive_session(
                namespace=namespace,
                customer_ref=shared_customer,
                device_fp=shared_device,
                flow_seconds=flow_seconds,
            )
            assessment = driven["assessment"]
            outcomes.append((index, str(assessment["decision"]), int(assessment["score"])))
        challenged = sum(1 for _, decision, _ in outcomes if decision == "CHALLENGE")
        allows = [(index, score) for index, decision, score in outcomes if decision == "ALLOW"]
        borderline_ok = all(score < 30 and index <= 5 for index, score in allows)
        if allows:
            listed = ", ".join(f"session {index} scored {score}" for index, score in allows)
            borderline_detail = (
                f"{listed} (flow duration only) and "
                "intentionally below the challenge threshold. "
                f"{_RERUN_NOTE}"
            )
        else:
            borderline_detail = (
                "No borderline ALLOWs on this run; velocity windows were already "
                f"warm from a recent run. {_RERUN_NOTE}"
            )
        return (
            ScenarioCheck(
                check="at_least_18_challenged",
                passed=challenged >= 18,
                detail=f"{challenged}/{total} sessions challenged.",
            ),
            ScenarioCheck(
                check="borderline_allows_explained",
                passed=borderline_ok,
                detail=borderline_detail,
            ),
        )

    async def _run_npci_risk_rejection(self, params: dict[str, Any]) -> tuple[ScenarioCheck, ...]:
        guard = self._secret_guard()
        if guard is not None:
            return (guard,)
        fixture = str(params.get("fixture", "token_rejected_npci_risk_body.json"))
        prefix = str(params.get("event_id_prefix", "evt_demo_rejected_"))
        demo_header = bool(params.get("demo_header", True))
        event_id = f"{prefix}{self._uuid8()}"
        raw_body = self._signed_webhook_body(fixture, None)
        response = await self._post_webhook(raw_body, event_id, demo_header)
        data = response.json()
        processed = data.get("status") == "processed"
        async with transaction() as session:
            demo = DemoReadRepository(session)
            stored = await demo.get_event_for_provider_event("razorpay", event_id)
            assessment = None
            revoke_count = -1
            if stored is not None:
                assessment = await RiskAssessmentRepository(session).get_for_event(
                    stored.mandate_event_id, DecisionStage.REJECTION_AUDIT, "rules-v1"
                )
                revoke_count = await demo.count_action_requests_for_mandate_event(
                    stored.mandate_event_id
                )
        score_ok = (
            assessment is not None
            and assessment.score == 100
            and assessment.decision.value == "BLOCK"
            and assessment.stage.value == "REJECTION_AUDIT"
        )
        score_detail = (
            f"score {assessment.score}, decision {assessment.decision.value}."
            if assessment is not None
            else "no REJECTION_AUDIT assessment found for the event."
        )
        npci_present = assessment is not None and any(
            evaluation.rule_id == "npci_risk_rejection"
            for evaluation in assessment.rule_evaluations
        )
        return (
            ScenarioCheck(
                check="webhook_processed",
                passed=processed,
                detail="webhook acknowledged as processed."
                if processed
                else f"webhook status was {data.get('status')!r}.",
            ),
            ScenarioCheck(check="score_100_block", passed=score_ok, detail=score_detail),
            ScenarioCheck(
                check="npci_reason_present",
                passed=npci_present,
                detail="rule npci_risk_rejection triggered."
                if npci_present
                else "rule npci_risk_rejection not found in evaluations.",
            ),
            ScenarioCheck(
                check="no_revoke_queued",
                passed=revoke_count == 0,
                detail=f"{revoke_count} revoke requests queued (rejected mandates need none).",
            ),
        )

    async def _seed_high_risk_namespace(
        self, seed: dict[str, Any], run_tag: str
    ) -> tuple[str, str]:
        """Run full pre-check sessions to warm velocity; return shared ids."""
        namespace = str(seed.get("namespace", "demo_high_risk"))
        total = int(seed.get("sessions", 21))
        flow_seconds = int(seed.get("flow_seconds", 30))
        shared_device = f"demo-device-{run_tag}"
        shared_customer = f"demo-customer-{run_tag}"
        for _ in range(total):
            await self._drive_session(
                namespace=namespace,
                customer_ref=shared_customer,
                device_fp=shared_device,
                flow_seconds=flow_seconds,
            )
        return namespace, shared_device

    async def _drive_high_risk_webhook(
        self,
        *,
        namespace: str,
        device_fp: str,
        fixture: str,
        prefix: str,
        demo_header: bool,
    ) -> tuple[str, dict[str, Any]]:
        """Correlated READY session engineered to score 100, then the webhook.

        The correlated session is deliberately *not* pre-checked: the webhook
        evaluation owns its single feature snapshot, sees a new device for a
        fresh customer (+20), burst velocities (+50), a 2-second flow (+20)
        and unusual mandate terms (+10) for exactly 100 -> BLOCK.
        """
        session_id = await self._create_ready_session(
            namespace=namespace,
            customer_ref=f"demo-customer-correlated-{self._uuid8()}",
            device_fp=device_fp,
            flow_seconds=2,
            max_amount_paise=5_000_000,
        )
        event_id = f"{prefix}{self._uuid8()}"
        raw_body = self._signed_webhook_body(fixture, session_id)
        response = await self._post_webhook(raw_body, event_id, demo_header)
        return event_id, response.json()

    async def _run_confirmed_high_risk_block(
        self, params: dict[str, Any]
    ) -> tuple[ScenarioCheck, ...]:
        guard = self._secret_guard()
        if guard is not None:
            return (guard,)
        fixture = str(params.get("fixture", "token_confirmed_body.json"))
        prefix = str(params.get("event_id_prefix", "evt_demo_block_"))
        demo_header = bool(params.get("demo_header", True))
        seed = params.get("seed", {})
        if not isinstance(seed, dict):
            seed = {}
        run_tag = self._uuid8()
        namespace, shared_device = await self._seed_high_risk_namespace(seed, run_tag)
        event_id, data = await self._drive_high_risk_webhook(
            namespace=namespace,
            device_fp=shared_device,
            fixture=fixture,
            prefix=prefix,
            demo_header=demo_header,
        )
        processed = data.get("status") == "processed"
        async with transaction() as session:
            demo = DemoReadRepository(session)
            stored = await demo.get_event_for_provider_event("razorpay", event_id)
            assessment = None
            request = None
            revoke_count = -1
            chain_valid = False
            if stored is not None:
                assessment = await RiskAssessmentRepository(session).get_for_event(
                    stored.mandate_event_id, DecisionStage.POST_CONFIRMATION, "rules-v1"
                )
                request = await demo.get_action_request_for_mandate_event(stored.mandate_event_id)
                revoke_count = await demo.count_action_requests_for_mandate_event(
                    stored.mandate_event_id
                )
            if assessment is not None:
                verification = await AuditRepository(session).verify_aggregate(
                    "risk_assessment", assessment.assessment_id
                )
                chain_valid = verification.valid
        score_ok = (
            assessment is not None
            and assessment.score == 100
            and assessment.decision.value == "BLOCK"
        )
        lifecycle_ok = request is not None and request.status in (
            ActionStatus.QUEUED,
            ActionStatus.IN_PROGRESS,
            ActionStatus.RETRYING,
            ActionStatus.SUCCEEDED,
        )
        return (
            ScenarioCheck(
                check="webhook_processed",
                passed=processed,
                detail="webhook acknowledged as processed."
                if processed
                else f"webhook status was {data.get('status')!r}.",
            ),
            ScenarioCheck(
                check="score_100_block",
                passed=score_ok,
                detail=(
                    f"score {assessment.score}, decision {assessment.decision.value}."
                    if assessment is not None
                    else "no POST_CONFIRMATION assessment found for the event."
                ),
            ),
            ScenarioCheck(
                check="exactly_one_revoke",
                passed=revoke_count == 1,
                detail=f"{revoke_count} revoke requests queued for the event.",
            ),
            ScenarioCheck(
                check="revoke_lifecycle_started",
                passed=lifecycle_ok,
                detail=(
                    f"revoke request status is {request.status.value}."
                    if request is not None
                    else "no revoke request found for the event."
                ),
            ),
            ScenarioCheck(
                check="audit_chain_valid",
                passed=chain_valid,
                detail="audit chain verified for the assessment aggregate."
                if chain_valid
                else "audit chain verification failed for the assessment aggregate.",
            ),
        )

    async def _run_duplicate_webhook(self, params: dict[str, Any]) -> tuple[ScenarioCheck, ...]:
        guard = self._secret_guard()
        if guard is not None:
            return (guard,)
        fixture = str(params.get("fixture", "token_confirmed_body.json"))
        prefix = str(params.get("event_id_prefix", "evt_demo_dup_"))
        demo_header = bool(params.get("demo_header", True))
        event_id = f"{prefix}{self._uuid8()}"
        raw_body = self._signed_webhook_body(fixture, None)
        first = await self._post_webhook(raw_body, event_id, demo_header)
        second = await self._post_webhook(raw_body, event_id, demo_header)
        first_data = first.json()
        second_data = second.json()
        first_ok = first_data.get("status") == "processed"
        replay_header = second.headers.get("X-Idempotent-Replay") == "true"
        second_ok = second_data.get("status") == "duplicate" and replay_header
        async with transaction() as session:
            demo = DemoReadRepository(session)
            event_count = await demo.count_events_for_provider_event("razorpay", event_id)
            stored = await demo.get_event_for_provider_event("razorpay", event_id)
            assessment = None
            if stored is not None:
                assessment = await RiskAssessmentRepository(session).get_for_event(
                    stored.mandate_event_id, DecisionStage.POST_CONFIRMATION, "rules-v1"
                )
        single_ok = event_count == 1 and assessment is not None
        return (
            ScenarioCheck(
                check="first_delivery_processed",
                passed=first_ok,
                detail="first delivery acknowledged as processed."
                if first_ok
                else f"first delivery status was {first_data.get('status')!r}.",
            ),
            ScenarioCheck(
                check="second_delivery_duplicate",
                passed=second_ok,
                detail="replay acknowledged as duplicate with X-Idempotent-Replay: true."
                if second_ok
                else f"replay status was {second_data.get('status')!r}.",
            ),
            ScenarioCheck(
                check="single_event_and_assessment",
                passed=single_ok,
                detail=f"{event_count} stored event(s), "
                f"{'one' if assessment is not None else 'no'} assessment.",
            ),
        )

    async def _run_worker_failure_recovery(
        self, params: dict[str, Any]
    ) -> tuple[ScenarioCheck, ...]:
        guard = self._secret_guard()
        if guard is not None:
            return (guard,)
        if not isinstance(self._revoke_client, MockTokenRevokeClient):
            return (
                ScenarioCheck(
                    check="failure_injection_available",
                    passed=False,
                    detail="real Razorpay client configured",
                ),
            )
        fixture = str(params.get("fixture", "token_confirmed_body.json"))
        prefix = str(params.get("event_id_prefix", "evt_demo_recovery_"))
        demo_header = bool(params.get("demo_header", True))
        seed = params.get("seed", {})
        if not isinstance(seed, dict):
            seed = {}
        timeout_seconds = int(params.get("poll_timeout_seconds", 30))
        scheduled = int(params.get("scheduled_failures", 1))
        run_tag = self._uuid8()
        namespace, shared_device = await self._seed_high_risk_namespace(seed, run_tag)
        self._revoke_client.schedule_failures(scheduled)
        event_id, data = await self._drive_high_risk_webhook(
            namespace=namespace,
            device_fp=shared_device,
            fixture=fixture,
            prefix=prefix,
            demo_header=demo_header,
        )
        if data.get("status") != "processed":
            return (
                ScenarioCheck(
                    check="webhook_processed",
                    passed=False,
                    detail=f"webhook status was {data.get('status')!r}.",
                ),
            )
        request_id, mandate_event_id = await self._request_ids_for_event(event_id)
        if request_id is None or mandate_event_id is None:
            return (
                ScenarioCheck(
                    check="revoke_queued",
                    passed=False,
                    detail="no revoke request was queued for the recovery webhook.",
                ),
            )
        worker = OutboxWorker(revoke_client=self._revoke_client)
        deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
        final_status: ActionStatus | None = None
        while datetime.now(UTC) < deadline:
            await worker.process_pending_once()
            async with transaction() as session:
                current = await ActionRepository(session).get_request(request_id)
                final_status = current.status if current is not None else None
            if final_status == ActionStatus.SUCCEEDED:
                break
            await asyncio.sleep(0.5)
        async with transaction() as session:
            demo = DemoReadRepository(session)
            attempts = await demo.list_attempts_for_request(request_id)
            revoke_count = await demo.count_action_requests_for_mandate_event(mandate_event_id)
            counted = await ActionAttemptReadRepository(session).count_attempts(request_id)
        first = attempts[0] if attempts else None
        first_retried = (
            first is not None
            and first.status == ActionStatus.RETRYING
            and first.safe_error_code == "UPSTREAM_SERVER_ERROR"
        )
        succeeded = [attempt for attempt in attempts if attempt.status == ActionStatus.SUCCEEDED]
        return (
            ScenarioCheck(
                check="failure_injection_available",
                passed=True,
                detail=f"mock revoke client accepted {scheduled} scheduled failure(s).",
            ),
            ScenarioCheck(
                check="first_attempt_retried",
                passed=first_retried,
                detail=(
                    f"attempt 1 {first.status.value} with {first.safe_error_code}."
                    if first is not None
                    else "no attempts recorded for the request."
                ),
            ),
            ScenarioCheck(
                check="final_status_succeeded",
                passed=final_status == ActionStatus.SUCCEEDED,
                detail=f"final revoke request status is {final_status.value}."
                if final_status is not None
                else "revoke request is missing.",
            ),
            ScenarioCheck(
                check="exactly_two_attempts",
                passed=counted == 2,
                detail=f"{counted} attempts recorded for the request.",
            ),
            ScenarioCheck(
                check="no_duplicate_revocation",
                passed=revoke_count == 1 and len(succeeded) == 1,
                detail=f"{revoke_count} request(s), {len(succeeded)} successful attempt(s).",
            ),
        )

    async def _request_ids_for_event(
        self, provider_event_id: str
    ) -> tuple[UUID | None, UUID | None]:
        """Find the action request queued for one delivered provider event."""
        async with transaction() as session:
            demo = DemoReadRepository(session)
            stored = await demo.get_event_for_provider_event("razorpay", provider_event_id)
            if stored is None:
                return None, None
            request = await demo.get_action_request_for_mandate_event(stored.mandate_event_id)
            if request is None:
                return None, stored.mandate_event_id
            return request.action_request_id, stored.mandate_event_id

    async def _run_shared_demo_merchant_velocity(
        self, params: dict[str, Any]
    ) -> tuple[ScenarioCheck, ...]:
        namespaces = params.get("namespaces", [])
        if not isinstance(namespaces, list) or len(namespaces) < 3:
            namespaces = ["demo_shared_one", "demo_shared_two", "demo_shared_three"]
        flow_seconds = int(params.get("flow_seconds", 30))
        run_tag = self._uuid8()
        shared_device = f"demo-device-{run_tag}"
        driven: list[dict[str, Any]] = []
        for index, namespace in enumerate(namespaces[:3]):
            driven.append(
                await self._drive_session(
                    namespace=str(namespace),
                    customer_ref=f"demo-customer-shared-{run_tag}-{index}",
                    device_fp=shared_device,
                    flow_seconds=flow_seconds,
                )
            )
        third = driven[2]["assessment"]
        third_decision = str(third["decision"])
        third_score = int(third["score"])
        async with transaction() as session:
            snapshot = await DemoReadRepository(session).get_latest_snapshot_for_session(
                UUID(str(driven[2]["risk_session_id"]))
            )
        counter = snapshot.shared_demo_merchant_count_1h if snapshot is not None else None
        source = snapshot.sources.get("shared_demo_merchant_count_1h") if snapshot else None
        labelled = source is not None and source.availability == Availability.DEMO_SIMULATED
        return (
            ScenarioCheck(
                check="third_session_challenged",
                passed=third_decision == "CHALLENGE",
                detail=f"session 3 of 3 decided {third_decision} with score {third_score}.",
            ),
            ScenarioCheck(
                check="demo_counter_is_three",
                passed=counter == 3,
                detail=f"shared_demo_merchant_count_1h is {counter}.",
            ),
            ScenarioCheck(
                check="demo_source_labelled",
                passed=labelled,
                detail="shared counter source is labelled DEMO_SIMULATED."
                if labelled
                else "shared counter source is not labelled DEMO_SIMULATED.",
            ),
        )

    # ── Definitions ────────────────────────────────────────────────────

    def _try_list(self) -> list[ScenarioSummary]:
        try:
            return self._read_all()
        except Exception:
            return []

    def _read_all(self) -> list[ScenarioSummary]:
        entries = sorted(self._scenarios_dir.glob("*.json"))
        if not entries:
            raise DemoDefinitionsUnavailableError("no scenario definitions found")
        return [self._read_summary(path) for path in entries]

    @staticmethod
    def _read_summary(path: Path) -> ScenarioSummary:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("name", "title", "description", "expected_outcome", "params"):
            if key not in data:
                raise DemoDefinitionsUnavailableError("demo scenario definitions are unavailable")
        return ScenarioSummary(
            name=str(data["name"]),
            title=str(data["title"]),
            description=str(data["description"]),
            expected_outcome=str(data["expected_outcome"]),
        )

    def _load_definition(self, name: str) -> dict[str, Any]:
        path = self._scenarios_dir / f"{name}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UnknownDemoScenarioError(f"unknown demo scenario: {name}") from exc
        if not isinstance(data, dict) or data.get("name") != name:
            raise UnknownDemoScenarioError(f"unknown demo scenario: {name}")
        return data
