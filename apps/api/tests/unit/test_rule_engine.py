"""A5 unit tests: pure rule-engine behaviour — triggers, clamping and stages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.contracts.common import Availability, Decision, DecisionStage, FeatureSource
from app.contracts.feature_snapshot import FeatureSnapshot
from app.domain.rules.catalogue import DEFAULT_RULES
from app.domain.rules.config import DecisionThresholds, RuleDefinition, RuleSetConfig
from app.domain.rules.engine import RuleEngine

NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
ALL_STAGES = frozenset(
    {DecisionStage.PRECHECK, DecisionStage.POST_CONFIRMATION, DecisionStage.REJECTION_AUDIT}
)


def _snapshot(**overrides: Any) -> FeatureSnapshot:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "feature_snapshot_id": uuid4(),
        "mandate_event_id": uuid4(),
        "risk_session_id": uuid4(),
        "feature_version": "rules-v1",
        "calculated_at": NOW,
        "ip_velocity_5m": 1,
        "ip_velocity_1h": 2,
        "device_velocity_5m": 1,
        "device_velocity_1h": 2,
        "customer_velocity_1h": 1,
        "vpa_velocity_1h": 1,
        "shared_demo_merchant_count_1h": None,
        "is_new_device_for_customer": False,
        "flow_duration_seconds": 300,
        "user_agent_bot_suspected": None,
        "network_vpn_or_proxy": False,
        "network_reputation_score": 80,
        "npci_risk_flag": None,
        "payer_bank_or_compliance_flag": None,
        "max_amount_paise": 500000,
        "mandate_frequency": "monthly",
        "expiry_days_from_registration": 365,
        "sources": {},
        "is_demo_simulation": False,
    }
    base.update(overrides)
    if (
        base.get("shared_demo_merchant_count_1h") is not None
        and "is_demo_simulation" not in overrides
    ):
        base["is_demo_simulation"] = True
        base["sources"] = {
            **base["sources"],
            "shared_demo_merchant_count_1h": FeatureSource(
                availability=Availability.DEMO_SIMULATED,
                source="redis_demo_shared_merchants",
                captured_at=NOW,
            ),
        }
    return FeatureSnapshot(**base)


def _synthetic_config(points: int) -> RuleSetConfig:
    return RuleSetConfig(
        thresholds=DecisionThresholds(),
        rules=(
            RuleDefinition(
                rule_id="synthetic_threshold",
                points=points,
                stages=ALL_STAGES,
                predicate=lambda snapshot: True,
                reason_code="SYNTHETIC",
                reason_template="Synthetic rule fired.",
            ),
        ),
    )


@pytest.fixture
def engine() -> RuleEngine:
    return RuleEngine()


def test_clean_snapshot_scores_zero_and_allows_everywhere(engine: RuleEngine) -> None:
    snapshot = _snapshot()
    for stage in ALL_STAGES:
        result = engine.evaluate(snapshot, stage)
        assert result.score == 0
        assert result.decision == Decision.ALLOW
        assert [
            evaluation.rule_id for evaluation in result.rule_evaluations if evaluation.triggered
        ] == []


def test_every_catalogue_rule_triggers_on_its_documented_condition(engine: RuleEngine) -> None:
    cases: list[tuple[str, dict[str, Any]]] = [
        ("velocity_ip_burst", {"ip_velocity_5m": 5}),
        ("velocity_ip_burst", {"ip_velocity_1h": 20}),
        ("velocity_device_burst", {"device_velocity_5m": 5}),
        ("velocity_device_burst", {"device_velocity_1h": 20}),
        ("velocity_customer_burst", {"customer_velocity_1h": 10}),
        ("velocity_vpa_burst", {"vpa_velocity_1h": 10}),
        ("new_device_for_established_customer", {"is_new_device_for_customer": True}),
        ("implausible_flow_duration", {"flow_duration_seconds": 3}),
        ("network_anonymity", {"network_vpn_or_proxy": True}),
        ("network_anonymity", {"network_reputation_score": 30}),
        ("npci_risk_rejection", {"npci_risk_flag": True}),
        ("payer_bank_compliance_rejection", {"payer_bank_or_compliance_flag": True}),
        ("demo_shared_merchant_velocity", {"shared_demo_merchant_count_1h": 3}),
        ("unusual_mandate_terms", {"max_amount_paise": 5_000_000}),
        ("unusual_mandate_terms", {"expiry_days_from_registration": 1095}),
    ]
    for rule_id, fields in cases:
        rule = next(rule for rule in DEFAULT_RULES if rule.rule_id == rule_id)
        stage = (
            DecisionStage.REJECTION_AUDIT
            if rule.stages == frozenset({DecisionStage.REJECTION_AUDIT})
            else DecisionStage.POST_CONFIRMATION
        )
        result = engine.evaluate(_snapshot(**fields), stage)
        triggered = [e.rule_id for e in result.rule_evaluations if e.triggered]
        assert rule_id in triggered, (rule_id, fields)


def test_every_rule_is_none_safe_and_never_raises(engine: RuleEngine) -> None:
    empty = _snapshot(
        ip_velocity_5m=None,
        ip_velocity_1h=None,
        device_velocity_5m=None,
        device_velocity_1h=None,
        customer_velocity_1h=None,
        vpa_velocity_1h=None,
        is_new_device_for_customer=None,
        flow_duration_seconds=None,
        network_vpn_or_proxy=None,
        network_reputation_score=None,
        npci_risk_flag=None,
        payer_bank_or_compliance_flag=None,
        shared_demo_merchant_count_1h=None,
        max_amount_paise=None,
        expiry_days_from_registration=None,
    )
    for stage in ALL_STAGES:
        result = engine.evaluate(empty, stage)
        assert result.score == 0
        assert result.decision == Decision.ALLOW


def test_missing_features_never_trigger_rules(engine: RuleEngine) -> None:
    snapshot = _snapshot(ip_velocity_5m=None, device_velocity_5m=None)
    result = engine.evaluate(snapshot, DecisionStage.POST_CONFIRMATION)
    by_id = {evaluation.rule_id: evaluation for evaluation in result.rule_evaluations}
    assert by_id["velocity_ip_burst"].triggered is False
    assert by_id["velocity_device_burst"].triggered is False


def test_score_is_clamped_to_100(engine: RuleEngine) -> None:
    snapshot = _snapshot(
        ip_velocity_5m=50,
        ip_velocity_1h=200,
        device_velocity_5m=50,
        device_velocity_1h=200,
        customer_velocity_1h=50,
        vpa_velocity_1h=50,
        is_new_device_for_customer=True,
        flow_duration_seconds=1,
        network_vpn_or_proxy=True,
        shared_demo_merchant_count_1h=9,
        max_amount_paise=9_000_000,
    )
    result = engine.evaluate(snapshot, DecisionStage.POST_CONFIRMATION)
    raw = sum(evaluation.points for evaluation in result.rule_evaluations)
    assert raw > 100
    assert result.score == 100
    assert result.decision == Decision.BLOCK


def test_rule_evaluations_preserve_catalogue_order(engine: RuleEngine) -> None:
    result = engine.evaluate(_snapshot(), DecisionStage.PRECHECK)
    assert [e.rule_id for e in result.rule_evaluations] == [r.rule_id for r in DEFAULT_RULES]
    assert len(result.rule_evaluations) == len(DEFAULT_RULES) >= 1


def test_untriggered_rules_report_zero_points_and_no_reason(engine: RuleEngine) -> None:
    result = engine.evaluate(_snapshot(), DecisionStage.POST_CONFIRMATION)
    for evaluation in result.rule_evaluations:
        if not evaluation.triggered:
            assert evaluation.points == 0
            assert evaluation.reason_code is None
            assert evaluation.reason_text is None


def test_triggered_rules_report_points_reason_code_and_text(engine: RuleEngine) -> None:
    result = engine.evaluate(_snapshot(device_velocity_5m=7), DecisionStage.POST_CONFIRMATION)
    evaluation = next(e for e in result.rule_evaluations if e.rule_id == "velocity_device_burst")
    assert evaluation.triggered is True
    assert evaluation.points == 25
    assert evaluation.reason_code == "DEVICE_VELOCITY_BURST"
    assert evaluation.reason_text is not None and "7" in evaluation.reason_text


@pytest.mark.parametrize("stage", list(ALL_STAGES))
def test_threshold_29_allows_in_every_stage(stage: DecisionStage) -> None:
    result = RuleEngine(_synthetic_config(29)).evaluate(_snapshot(), stage)
    assert result.score == 29
    assert result.decision == Decision.ALLOW


def test_threshold_30_challenges_in_precheck_only() -> None:
    engine = RuleEngine(_synthetic_config(30))
    assert engine.evaluate(_snapshot(), DecisionStage.PRECHECK).decision == Decision.CHALLENGE
    assert engine.evaluate(_snapshot(), DecisionStage.POST_CONFIRMATION).decision == Decision.ALLOW
    assert engine.evaluate(_snapshot(), DecisionStage.REJECTION_AUDIT).decision == Decision.ALLOW


def test_threshold_69_challenges_in_precheck_and_allows_post_confirmation() -> None:
    engine = RuleEngine(_synthetic_config(69))
    assert engine.evaluate(_snapshot(), DecisionStage.PRECHECK).decision == Decision.CHALLENGE
    assert engine.evaluate(_snapshot(), DecisionStage.POST_CONFIRMATION).decision == Decision.ALLOW
    assert engine.evaluate(_snapshot(), DecisionStage.REJECTION_AUDIT).decision == Decision.ALLOW


def test_threshold_70_blocks_post_confirmation_and_rejection_audit() -> None:
    engine = RuleEngine(_synthetic_config(70))
    assert engine.evaluate(_snapshot(), DecisionStage.PRECHECK).decision == Decision.CHALLENGE
    assert engine.evaluate(_snapshot(), DecisionStage.POST_CONFIRMATION).decision == Decision.BLOCK
    assert engine.evaluate(_snapshot(), DecisionStage.REJECTION_AUDIT).decision == Decision.BLOCK


def test_challenge_never_appears_outside_precheck(engine: RuleEngine) -> None:
    hot = _snapshot(
        device_velocity_5m=50,
        is_new_device_for_customer=True,
        flow_duration_seconds=1,
        shared_demo_merchant_count_1h=9,
    )
    for stage in (DecisionStage.POST_CONFIRMATION, DecisionStage.REJECTION_AUDIT):
        assert engine.evaluate(hot, stage).decision in (Decision.ALLOW, Decision.BLOCK)


def test_npci_rule_ignored_outside_rejection_audit(engine: RuleEngine) -> None:
    snapshot = _snapshot(npci_risk_flag=True)
    for stage in (DecisionStage.PRECHECK, DecisionStage.POST_CONFIRMATION):
        result = engine.evaluate(snapshot, stage)
        evaluation = next(e for e in result.rule_evaluations if e.rule_id == "npci_risk_rejection")
        assert evaluation.triggered is False
        assert evaluation.points == 0
    result = engine.evaluate(snapshot, DecisionStage.REJECTION_AUDIT)
    assert (
        next(e for e in result.rule_evaluations if e.rule_id == "npci_risk_rejection").triggered
        is True
    )


def test_payer_bank_rule_ignored_outside_rejection_audit(engine: RuleEngine) -> None:
    snapshot = _snapshot(payer_bank_or_compliance_flag=True)
    for stage in (DecisionStage.PRECHECK, DecisionStage.POST_CONFIRMATION):
        result = engine.evaluate(snapshot, stage)
        evaluation = next(
            e for e in result.rule_evaluations if e.rule_id == "payer_bank_compliance_rejection"
        )
        assert evaluation.triggered is False
    assert engine.evaluate(snapshot, DecisionStage.REJECTION_AUDIT).score == 60


def test_reason_text_never_exceeds_240_chars(engine: RuleEngine) -> None:
    hot = _snapshot(
        ip_velocity_5m=99,
        ip_velocity_1h=999,
        device_velocity_5m=99,
        device_velocity_1h=999,
        customer_velocity_1h=999,
        vpa_velocity_1h=999,
        shared_demo_merchant_count_1h=999,
        flow_duration_seconds=0,
        max_amount_paise=9_999_999,
    )
    for stage in ALL_STAGES:
        for evaluation in engine.evaluate(hot, stage).rule_evaluations:
            if evaluation.triggered:
                assert evaluation.reason_text is not None
                assert len(evaluation.reason_text) <= 240


def test_reason_text_never_contains_hashes(engine: RuleEngine) -> None:
    snapshot = _snapshot(device_velocity_5m=7)
    for stage in ALL_STAGES:
        for evaluation in engine.evaluate(snapshot, stage).rule_evaluations:
            if evaluation.triggered and evaluation.reason_text is not None:
                assert "hmac-sha256:" not in evaluation.reason_text
                assert "sha256:" not in evaluation.reason_text


def test_engine_version_is_rules_v1(engine: RuleEngine) -> None:
    assert engine.engine_version == "rules-v1"


def test_same_inputs_give_identical_results(engine: RuleEngine) -> None:
    snapshot = _snapshot(device_velocity_5m=7, is_new_device_for_customer=True)
    first = engine.evaluate(snapshot, DecisionStage.PRECHECK)
    second = engine.evaluate(snapshot, DecisionStage.PRECHECK)
    assert first == second
    assert first.rule_evaluations == second.rule_evaluations
