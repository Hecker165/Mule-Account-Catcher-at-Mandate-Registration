"""Session capture service for A2 session creation and telemetry recording.

Implements the business logic for creating risk sessions and recording
telemetry with proper HMAC pseudonymisation and audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.contracts.common import AuditActorType, RiskSessionStatus
from app.contracts.risk_session import (
    RiskSession,
    RiskSessionCreateRequest,
    RiskSessionTelemetryUpdateRequest,
)
from app.repositories import AuditRepository, RiskSessionRepository
from app.services.privacy_hashing import HmacPseudonymizer


@dataclass(frozen=True)
class CreateSessionResult:
    """Result of a session creation attempt."""

    session: RiskSession
    is_replay: bool


class SessionCaptureService:
    """Service for risk session creation and telemetry recording."""

    def __init__(
        self,
        session_factory: Callable[..., Any],
        risk_session_repo: type[RiskSessionRepository],
        audit_repo: type[AuditRepository],
        pseudonymizer: HmacPseudonymizer,
        clock: Callable[[], datetime],
        uuid_factory: Callable[[], UUID],
    ) -> None:
        self._session_factory = session_factory
        self._risk_session_repo = risk_session_repo
        self._audit_repo = audit_repo
        self._pseudonymizer = pseudonymizer
        self._clock = clock
        self._uuid_factory = uuid_factory

    async def create(
        self,
        request: RiskSessionCreateRequest,
        peer_ip: str | None,
        request_user_agent: str | None,
    ) -> CreateSessionResult:
        """Create a new risk session or return existing one on idempotent replay."""
        async with self._session_factory() as session:
            session_repo = self._risk_session_repo(session)
            audit_repo = self._audit_repo(session)

            # Pseudonymise sensitive values from request body
            customer_reference_hash = self._pseudonymizer.hash_customer_reference(
                request.customer_reference
            )
            device_fingerprint_hash = self._pseudonymizer.hash_device_fingerprint(
                request.device_fingerprint
            )

            # Pseudonymise peer context from request (trusted sources only)
            ip_hash = self._pseudonymizer.hash_ip(peer_ip)
            user_agent_hash = self._pseudonymizer.hash_user_agent(request_user_agent)

            now = self._clock()

            # Build the full RiskSession contract
            risk_session = RiskSession(
                schema_version="1.0",
                risk_session_id=self._uuid_factory(),
                merchant_namespace=request.merchant_namespace,
                checkout_order_ref=request.checkout_order_ref,
                customer_reference_hash=customer_reference_hash,
                ip_hash=ip_hash,
                device_fingerprint_hash=device_fingerprint_hash,
                user_agent_hash=user_agent_hash,
                flow_started_at=request.flow_started_at,
                flow_completed_at=None,
                mandate_intent=request.mandate_intent,
                status=RiskSessionStatus.CREATED,
                created_at=now,
                updated_at=now,
            )

            # Idempotency: check for existing session by order_ref
            if request.checkout_order_ref is not None:
                existing = await session_repo.get_by_order_ref(
                    request.merchant_namespace, request.checkout_order_ref
                )
                if existing is not None:
                    return CreateSessionResult(session=existing, is_replay=True)

            # Try to create new session
            try:
                created = await session_repo.create(risk_session)
                await session.flush()

                # Audit: risk_session.created
                await audit_repo.append(
                    aggregate_type="risk_session",
                    aggregate_id=created.risk_session_id,
                    event_type="risk_session.created",
                    actor_type=AuditActorType.SYSTEM,
                    actor_id="checkout_api",
                    occurred_at=now,
                    redacted_payload={
                        "merchant_namespace": request.merchant_namespace,
                        "checkout_order_ref_present": request.checkout_order_ref is not None,
                        "customer_reference_present": request.customer_reference is not None,
                        "device_fingerprint_present": request.device_fingerprint is not None,
                        "network_peer_ip_present": peer_ip is not None,
                        "request_user_agent_present": request_user_agent is not None,
                        "flow_started_at_present": request.flow_started_at is not None,
                        "max_amount_paise_present": request.mandate_intent.max_amount_paise
                        is not None,
                        "frequency_present": request.mandate_intent.frequency is not None,
                    },
                )

                await session.commit()

                return CreateSessionResult(session=created, is_replay=False)

            except IntegrityError:
                # Concurrent create won the race - re-query and return replay
                await session.rollback()
                if request.checkout_order_ref is not None:
                    existing = await session_repo.get_by_order_ref(
                        request.merchant_namespace, request.checkout_order_ref
                    )
                    if existing is not None:
                        return CreateSessionResult(session=existing, is_replay=True)
                # Should not happen, but if it does, re-raise
                raise

    async def record_telemetry(
        self,
        risk_session_id: UUID,
        request: RiskSessionTelemetryUpdateRequest,
        peer_ip: str | None,
        request_user_agent: str | None,
    ) -> RiskSession:
        """Record telemetry and transition session to READY."""
        async with self._session_factory() as session:
            session_repo = self._risk_session_repo(session)
            audit_repo = self._audit_repo(session)

            # Load existing session
            existing = await session_repo.get(risk_session_id)
            if existing is None:
                raise ValueError(f"risk session {risk_session_id} not found")

            # Reject if already consumed or expired
            if existing.status in (RiskSessionStatus.CONSUMED, RiskSessionStatus.EXPIRED):
                raise ValueError(f"risk session is already {existing.status.value.lower()}")

            # Pseudonymise new values from trusted sources
            ip_hash = self._pseudonymizer.hash_ip(peer_ip)
            user_agent_hash = self._pseudonymizer.hash_user_agent(request_user_agent)

            # Handle device fingerprint from body if provided
            device_fingerprint_hash = None
            if request.device_fingerprint is not None:
                device_fingerprint_hash = self._pseudonymizer.hash_device_fingerprint(
                    request.device_fingerprint
                )

            now = self._clock()

            # Update telemetry - only update non-None values
            updated = await session_repo.update_telemetry(
                risk_session_id=risk_session_id,
                customer_reference_hash=None,  # retain existing
                ip_hash=ip_hash,
                device_fingerprint_hash=device_fingerprint_hash,
                user_agent_hash=user_agent_hash,
                flow_completed_at=request.flow_completed_at,
                status=RiskSessionStatus.READY,
                updated_at=now,
            )

            # Audit: risk_session.telemetry_recorded
            await audit_repo.append(
                aggregate_type="risk_session",
                aggregate_id=risk_session_id,
                event_type="risk_session.telemetry_recorded",
                actor_type=AuditActorType.SYSTEM,
                actor_id="checkout_api",
                occurred_at=now,
                redacted_payload={
                    "network_peer_ip_present": peer_ip is not None,
                    "request_user_agent_present": request_user_agent is not None,
                    "device_fingerprint_updated": request.device_fingerprint is not None,
                    "flow_completed_at_present": request.flow_completed_at is not None,
                    "untrusted_client_ip_ignored": True,
                    "untrusted_vpn_claim_ignored": True,
                },
            )

            await session.commit()

            return updated
