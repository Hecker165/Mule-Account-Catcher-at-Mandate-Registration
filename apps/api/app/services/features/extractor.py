"""Deterministic FeatureSnapshot extraction from mandate events and risk sessions.

The extractor returns an unpersisted ``FeatureSnapshot``; the caller (A5)
persists it via A1 inside its own transaction. Every output is a pure function
of (stored contract inputs, Redis state, injected clock, injected UUID factory).

The ``npci_risk_flag`` / ``payer_bank_or_compliance_flag`` keyword detection is
a *feature flag*, not a decision: A5 owns whatever scoring consequence the
NPCI/payer-bank wording carries. A4 only reports what the stored text contains.

``user_agent_bot_suspected`` is always ``None`` in v1: only a UA **hash** is
stored, so no classification is possible post-hoc. Classifying at capture time
would require an interface change to A2.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from app.contracts.common import Availability, FeatureSource
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.mandate_event import MandateWebhookEvent
from app.contracts.risk_session import RiskSession
from app.services.features.keys import (
    KNOWN_DEVICES_TTL_SECONDS,
    WINDOWS,
    known_devices_key,
    velocity_key,
)
from app.services.features.redis_store import RedisFeatureStore
from app.services.features.reputation import NetworkReputationProvider

CORRELATION_FALLBACK_NAMESPACE = "uncorrelated"

_PAYER_BANK_TOKENS = frozenset(
    {"payer bank", "payer_bank", "compliance", "frozen", "blocked", "fraud", "aml"}
)

_VELOCITY_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("ip_velocity_5m", "ip", "5m"),
    ("ip_velocity_1h", "ip", "1h"),
    ("device_velocity_5m", "device", "5m"),
    ("device_velocity_1h", "device", "1h"),
    ("customer_velocity_1h", "customer", "1h"),
    ("vpa_velocity_1h", "vpa", "1h"),
)


class FeatureExtractor:
    """Builds FeatureSnapshot contracts from stored events and sessions."""

    def __init__(
        self,
        store: RedisFeatureStore,
        reputation: NetworkReputationProvider | None = None,
        uuid_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._reputation = reputation
        self._uuid_factory = uuid_factory

    async def extract_for_event(
        self, event: MandateWebhookEvent, risk_session: RiskSession | None, now: datetime
    ) -> FeatureSnapshot:
        """Extract features for a stored webhook event (A3 -> A5 path)."""
        _require_aware(now)
        namespace = (
            risk_session.merchant_namespace
            if risk_session is not None
            else CORRELATION_FALLBACK_NAMESPACE
        )
        builder = _Builder(now, risk_session, event.received_at)
        await self._velocities(
            builder, namespace, str(event.mandate_event_id), now, event.vpa_hash, True
        )
        await self._device_and_session(builder, now)
        self._event_flags(builder, event)
        await self._network(builder, risk_session, now)
        self._mandate(builder, risk_session)
        await self._demo(
            builder,
            namespace,
            now,
            is_demo=event.is_demo_event or _is_demo_namespace(namespace),
        )
        return FeatureSnapshot(
            schema_version="1.0",
            feature_snapshot_id=self._uuid_factory(),
            mandate_event_id=event.mandate_event_id,
            risk_session_id=risk_session.risk_session_id if risk_session is not None else None,
            feature_version="rules-v1",
            calculated_at=now,
            **builder.features,
            sources=builder.sources,
            is_demo_simulation=builder.is_demo_simulation,
        )

    async def extract_for_session(
        self, risk_session: RiskSession, now: datetime
    ) -> FeatureSnapshot:
        """Extract features for a READY risk session (A2 -> A5 pre-check path)."""
        _require_aware(now)
        builder = _Builder(now, risk_session, None)
        await self._velocities(
            builder,
            risk_session.merchant_namespace,
            str(risk_session.risk_session_id),
            now,
            None,
            False,
        )
        await self._device_and_session(builder, now)
        builder.set("npci_risk_flag", None, _missing("npci_risk_flag"))
        builder.set(
            "payer_bank_or_compliance_flag", None, _missing("payer_bank_or_compliance_flag")
        )
        await self._network(builder, risk_session, now)
        self._mandate(builder, risk_session)
        await self._demo(
            builder,
            risk_session.merchant_namespace,
            now,
            is_demo=_is_demo_namespace(risk_session.merchant_namespace),
        )
        return FeatureSnapshot(
            schema_version="1.0",
            feature_snapshot_id=self._uuid_factory(),
            mandate_event_id=None,
            risk_session_id=risk_session.risk_session_id,
            feature_version="rules-v1",
            calculated_at=now,
            **builder.features,
            sources=builder.sources,
            is_demo_simulation=builder.is_demo_simulation,
        )

    async def _velocities(
        self,
        builder: _Builder,
        namespace: str,
        member: str,
        now: datetime,
        vpa_hash: str | None,
        vpa_applies: bool,
    ) -> None:
        hashes: dict[str, str | None] = {
            "ip": builder.session_ip_hash,
            "device": builder.session_device_hash,
            "customer": builder.session_customer_hash,
            "vpa": vpa_hash,
        }
        for field, signal, window in _VELOCITY_FIELDS:
            if signal == "vpa" and not vpa_applies:
                builder.set(field, None, _not_applicable(field))
                continue
            hash_value = hashes[signal]
            if hash_value is None:
                builder.set(field, None, _missing(field))
                continue
            key = velocity_key(self._store.key_prefix, namespace, signal, window, hash_value)
            count = await self._store.record_and_count(
                key=key, member=member, window_seconds=WINDOWS[window], now=now
            )
            builder.set(
                field,
                count,
                FeatureSource(
                    availability=Availability.AVAILABLE,
                    source=f"redis_velocity:{signal}:{window}",
                    captured_at=now,
                ),
            )

    async def _device_and_session(self, builder: _Builder, now: datetime) -> None:
        customer_hash = builder.session_customer_hash
        device_hash = builder.session_device_hash
        if customer_hash is None or device_hash is None:
            builder.set("is_new_device_for_customer", None, _missing("is_new_device_for_customer"))
        else:
            is_new = await self._store.check_then_add(
                key=known_devices_key(self._store.key_prefix, customer_hash),
                member=device_hash,
                ttl_seconds=KNOWN_DEVICES_TTL_SECONDS,
                now=now,
            )
            builder.set(
                "is_new_device_for_customer",
                is_new,
                FeatureSource(
                    availability=Availability.AVAILABLE,
                    source="redis_known_devices",
                    captured_at=now,
                ),
            )
        if builder.flow_duration_seconds is None:
            builder.set("flow_duration_seconds", None, _missing("flow_duration_seconds"))
        else:
            builder.set(
                "flow_duration_seconds",
                builder.flow_duration_seconds,
                builder.session_source(),
            )
        builder.set("user_agent_bot_suspected", None, _not_applicable("user_agent_bot_suspected"))

    def _event_flags(self, builder: _Builder, event: MandateWebhookEvent) -> None:
        if event.failure_reason is None:
            builder.set("npci_risk_flag", None, _missing("npci_risk_flag"))
            builder.set(
                "payer_bank_or_compliance_flag", None, _missing("payer_bank_or_compliance_flag")
            )
            return
        lowered = event.failure_reason.lower()
        builder.set("npci_risk_flag", "npci" in lowered, builder.event_source())
        builder.set(
            "payer_bank_or_compliance_flag",
            any(token in lowered for token in _PAYER_BANK_TOKENS),
            builder.event_source(),
        )

    async def _network(
        self, builder: _Builder, risk_session: RiskSession | None, now: datetime
    ) -> None:
        result = None
        if (
            self._reputation is not None
            and risk_session is not None
            and risk_session.ip_hash is not None
        ):
            try:
                result = await self._reputation.inspect(risk_session.ip_hash, now)
            except Exception:
                result = None
        if result is None:
            builder.set("network_vpn_or_proxy", None, _not_applicable("network_vpn_or_proxy"))
            builder.set(
                "network_reputation_score", None, _not_applicable("network_reputation_score")
            )
            return
        builder.set(
            "network_vpn_or_proxy",
            result.vpn_or_proxy,
            FeatureSource(
                availability=Availability.AVAILABLE,
                source="network_reputation_provider",
                captured_at=now,
            ),
        )
        builder.set(
            "network_reputation_score",
            max(0, min(100, result.reputation_score)),
            FeatureSource(
                availability=Availability.AVAILABLE,
                source="network_reputation_provider",
                captured_at=now,
            ),
        )

    def _mandate(self, builder: _Builder, risk_session: RiskSession | None) -> None:
        if risk_session is None:
            builder.set("max_amount_paise", None, _missing("max_amount_paise"))
            builder.set("mandate_frequency", None, _missing("mandate_frequency"))
            builder.set(
                "expiry_days_from_registration", None, _missing("expiry_days_from_registration")
            )
            return
        intent = risk_session.mandate_intent
        if intent.max_amount_paise is None:
            builder.set("max_amount_paise", None, _missing("max_amount_paise"))
        else:
            builder.set("max_amount_paise", intent.max_amount_paise, builder.session_source())
        if intent.frequency is None:
            builder.set("mandate_frequency", None, _missing("mandate_frequency"))
        else:
            builder.set("mandate_frequency", intent.frequency, builder.session_source())
        if builder.expiry_days is None:
            builder.set(
                "expiry_days_from_registration", None, _missing("expiry_days_from_registration")
            )
        else:
            builder.set(
                "expiry_days_from_registration", builder.expiry_days, builder.session_source()
            )

    async def _demo(self, builder: _Builder, namespace: str, now: datetime, is_demo: bool) -> None:
        device_hash = builder.session_device_hash
        if is_demo and device_hash is not None:
            await self._store.record_demo_merchant(
                device_hash=device_hash, merchant_namespace=namespace, now=now
            )
            count = await self._store.count_demo_merchants(device_hash=device_hash, now=now)
            builder.set(
                "shared_demo_merchant_count_1h",
                count,
                FeatureSource(
                    availability=Availability.DEMO_SIMULATED,
                    source="redis_demo_shared_merchants",
                    captured_at=now,
                ),
            )
            builder.is_demo_simulation = True
            return
        builder.set(
            "shared_demo_merchant_count_1h", None, _missing("shared_demo_merchant_count_1h")
        )
        builder.is_demo_simulation = is_demo


class _Builder:
    """Collects feature values, sources and session-derived helpers."""

    def __init__(
        self, now: datetime, session: RiskSession | None, event_received_at: datetime | None
    ) -> None:
        self.now = now
        self.session = session
        self.event_received_at = event_received_at
        self.features: dict[str, object] = {}
        self.sources: dict[str, FeatureSource] = {}
        self.is_demo_simulation = False

    def set(self, field: str, value: object, source: FeatureSource) -> None:
        self.features[field] = value
        self.sources[field] = source

    def session_source(self) -> FeatureSource:
        assert self.session is not None
        return FeatureSource(
            availability=Availability.AVAILABLE,
            source="risk_session",
            captured_at=self.session.updated_at,
        )

    def event_source(self) -> FeatureSource:
        assert self.event_received_at is not None
        return FeatureSource(
            availability=Availability.AVAILABLE,
            source="mandate_event",
            captured_at=self.event_received_at,
        )

    @property
    def session_ip_hash(self) -> str | None:
        return self.session.ip_hash if self.session is not None else None

    @property
    def session_device_hash(self) -> str | None:
        return self.session.device_fingerprint_hash if self.session is not None else None

    @property
    def session_customer_hash(self) -> str | None:
        return self.session.customer_reference_hash if self.session is not None else None

    @property
    def flow_duration_seconds(self) -> int | None:
        if self.session is None:
            return None
        started = self.session.flow_started_at
        completed = self.session.flow_completed_at
        if started is None or completed is None:
            return None
        seconds = int((completed - started).total_seconds())
        return seconds if seconds >= 0 else None

    @property
    def expiry_days(self) -> int | None:
        if self.session is None:
            return None
        started = self.session.flow_started_at
        expire_at = self.session.mandate_intent.expire_at
        if started is None or expire_at is None:
            return None
        days = (expire_at - started).days
        return days if days >= 0 else None


def _missing(field: str) -> FeatureSource:
    return FeatureSource(
        availability=Availability.MISSING, source=f"not_available:{field}", captured_at=None
    )


def _not_applicable(field: str) -> FeatureSource:
    return FeatureSource(
        availability=Availability.NOT_APPLICABLE,
        source=f"not_applicable:{field}",
        captured_at=None,
    )


def _is_demo_namespace(namespace: str) -> bool:
    return namespace.startswith("demo")


def _require_aware(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
