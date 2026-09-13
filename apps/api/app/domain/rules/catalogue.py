"""Versioned rules-v1 rule catalogue: id, points, stage scope and predicates.

Predicates are None-safe: if any referenced feature is ``None``, the rule
does not trigger. Velocity conditions read record-then-count values, so a
first-ever event has velocity 1.
"""

from __future__ import annotations

from app.contracts.common import DecisionStage
from app.contracts.feature_snapshot import FeatureSnapshot
from app.domain.rules.config import RuleDefinition

_ALL_STAGES = frozenset(
    {
        DecisionStage.PRECHECK,
        DecisionStage.POST_CONFIRMATION,
        DecisionStage.REJECTION_AUDIT,
    }
)
_REJECTION_ONLY = frozenset({DecisionStage.REJECTION_AUDIT})


def _ip_burst(snapshot: FeatureSnapshot) -> bool:
    five = snapshot.ip_velocity_5m
    hour = snapshot.ip_velocity_1h
    return (five is not None and five >= 5) or (hour is not None and hour >= 20)


def _device_burst(snapshot: FeatureSnapshot) -> bool:
    five = snapshot.device_velocity_5m
    hour = snapshot.device_velocity_1h
    return (five is not None and five >= 5) or (hour is not None and hour >= 20)


def _customer_burst(snapshot: FeatureSnapshot) -> bool:
    value = snapshot.customer_velocity_1h
    return value is not None and value >= 10


def _vpa_burst(snapshot: FeatureSnapshot) -> bool:
    value = snapshot.vpa_velocity_1h
    return value is not None and value >= 10


def _new_device(snapshot: FeatureSnapshot) -> bool:
    return snapshot.is_new_device_for_customer is True


def _implausible_flow(snapshot: FeatureSnapshot) -> bool:
    value = snapshot.flow_duration_seconds
    return value is not None and value <= 3


def _network_anonymity(snapshot: FeatureSnapshot) -> bool:
    if snapshot.network_vpn_or_proxy is True:
        return True
    score = snapshot.network_reputation_score
    return score is not None and score <= 30


def _npci_rejection(snapshot: FeatureSnapshot) -> bool:
    return snapshot.npci_risk_flag is True


def _payer_bank_rejection(snapshot: FeatureSnapshot) -> bool:
    return snapshot.payer_bank_or_compliance_flag is True


def _demo_shared_velocity(snapshot: FeatureSnapshot) -> bool:
    value = snapshot.shared_demo_merchant_count_1h
    return value is not None and value >= 3


def _unusual_terms(snapshot: FeatureSnapshot) -> bool:
    amount = snapshot.max_amount_paise
    expiry = snapshot.expiry_days_from_registration
    return (amount is not None and amount >= 5_000_000) or (expiry is not None and expiry >= 1095)


DEFAULT_RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        rule_id="velocity_ip_burst",
        points=25,
        stages=_ALL_STAGES,
        predicate=_ip_burst,
        reason_code="IP_VELOCITY_BURST",
        reason_template="IP made {ip_velocity_5m} registrations in 5 minutes "
        "({ip_velocity_1h} in 1 hour).",
    ),
    RuleDefinition(
        rule_id="velocity_device_burst",
        points=25,
        stages=_ALL_STAGES,
        predicate=_device_burst,
        reason_code="DEVICE_VELOCITY_BURST",
        reason_template="Device made {device_velocity_5m} registrations in 5 minutes "
        "({device_velocity_1h} in 1 hour).",
    ),
    RuleDefinition(
        rule_id="velocity_customer_burst",
        points=15,
        stages=_ALL_STAGES,
        predicate=_customer_burst,
        reason_code="CUSTOMER_VELOCITY_BURST",
        reason_template="Customer made {customer_velocity_1h} registrations in 1 hour.",
    ),
    RuleDefinition(
        rule_id="velocity_vpa_burst",
        points=15,
        stages=_ALL_STAGES,
        predicate=_vpa_burst,
        reason_code="VPA_VELOCITY_BURST",
        reason_template="Destination VPA handle received {vpa_velocity_1h} registrations "
        "in 1 hour.",
    ),
    RuleDefinition(
        rule_id="new_device_for_established_customer",
        points=20,
        stages=_ALL_STAGES,
        predicate=_new_device,
        reason_code="NEW_DEVICE_FOR_CUSTOMER",
        reason_template="Registration came from a device not seen for this customer in 24 hours.",
    ),
    RuleDefinition(
        rule_id="implausible_flow_duration",
        points=20,
        stages=_ALL_STAGES,
        predicate=_implausible_flow,
        reason_code="IMPLAUSIBLE_FLOW_DURATION",
        reason_template="Checkout completed in only {flow_duration_seconds} seconds.",
    ),
    RuleDefinition(
        rule_id="network_anonymity",
        points=20,
        stages=_ALL_STAGES,
        predicate=_network_anonymity,
        reason_code="NETWORK_ANONYMITY",
        reason_template="Network signals indicated VPN/proxy or a poor reputation score.",
    ),
    RuleDefinition(
        rule_id="npci_risk_rejection",
        points=100,
        stages=_REJECTION_ONLY,
        predicate=_npci_rejection,
        reason_code="NPCI_RISK_REJECTION",
        reason_template="Mandate was rejected with NPCI risk wording: {npci_flag_present}.",
    ),
    RuleDefinition(
        rule_id="payer_bank_compliance_rejection",
        points=60,
        stages=_REJECTION_ONLY,
        predicate=_payer_bank_rejection,
        reason_code="PAYER_BANK_COMPLIANCE_REJECTION",
        reason_template="Mandate was rejected with payer-bank or compliance wording.",
    ),
    RuleDefinition(
        rule_id="demo_shared_merchant_velocity",
        points=30,
        stages=_ALL_STAGES,
        predicate=_demo_shared_velocity,
        reason_code="DEMO_SHARED_MERCHANT_VELOCITY",
        reason_template="Device registered at {shared_demo_merchant_count_1h} simulated shared "
        "test merchants in 1 hour (demo data).",
    ),
    RuleDefinition(
        rule_id="unusual_mandate_terms",
        points=10,
        stages=_ALL_STAGES,
        predicate=_unusual_terms,
        reason_code="UNUSUAL_MANDATE_TERMS",
        reason_template="Mandate amount or expiry is unusual for registration risk.",
    ),
)
