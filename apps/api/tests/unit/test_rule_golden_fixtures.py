"""A5 unit tests: golden rule fixtures yield exact score, decision and rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts.common import Decision, DecisionStage
from app.contracts.feature_snapshot import FeatureSnapshot
from app.domain.rules.catalogue import DEFAULT_RULES
from app.domain.rules.config import DecisionThresholds, RuleDefinition, RuleSetConfig
from app.domain.rules.engine import RuleEngine

_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "data" / "fixtures" / "rules"

_ALL_STAGES = frozenset(
    {DecisionStage.PRECHECK, DecisionStage.POST_CONFIRMATION, DecisionStage.REJECTION_AUDIT}
)


def _load_manifest() -> dict[str, dict[str, bool]]:
    return json.loads((_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _threshold_config(points: int) -> RuleSetConfig:
    return RuleSetConfig(
        thresholds=DecisionThresholds(),
        rules=(
            RuleDefinition(
                rule_id="synthetic_threshold",
                points=points,
                stages=_ALL_STAGES,
                predicate=lambda snapshot: True,
                reason_code="SYNTHETIC",
                reason_template="Synthetic rule fired.",
            ),
        ),
    )


def test_manifest_lists_every_fixture() -> None:
    manifest = _load_manifest()
    on_disk = sorted(p.name for p in _FIXTURE_DIR.glob("*.json") if p.name != "manifest.json")
    assert sorted(manifest) == on_disk
    assert all(entry.get("valid") is True for entry in manifest.values())
    assert len(on_disk) == 10


@pytest.mark.parametrize("name", sorted(_load_manifest()))
def test_every_golden_fixture_matches_expected_score_decision_and_rules(name: str) -> None:
    bundle = _load_fixture(name)
    snapshot = FeatureSnapshot.model_validate(bundle["snapshot"])
    expected = bundle["expected"]
    if name.startswith("threshold_"):
        config = _threshold_config(expected["score"])
        result = RuleEngine(config).evaluate(snapshot, DecisionStage(bundle["stage"]))
        assert result.score == expected["score"]
        assert result.decision == Decision(expected["decision"])
        by_stage = expected["expected_by_stage"]
        for stage_name, decision in by_stage.items():
            stage_result = RuleEngine(config).evaluate(snapshot, DecisionStage(stage_name))
            assert stage_result.decision == Decision(decision), stage_name
    else:
        result = RuleEngine().evaluate(snapshot, DecisionStage(bundle["stage"]))
        assert result.score == expected["score"]
        assert result.decision == Decision(expected["decision"])
    triggered = [e.rule_id for e in result.rule_evaluations if e.triggered]
    assert triggered == expected["triggered_rule_ids"]
    if not name.startswith("threshold_"):
        assert [e.rule_id for e in result.rule_evaluations] == [r.rule_id for r in DEFAULT_RULES]


@pytest.mark.parametrize("name", sorted(_load_manifest()))
def test_every_golden_snapshot_round_trips_through_a0_contract(name: str) -> None:
    bundle = _load_fixture(name)
    snapshot = FeatureSnapshot.model_validate(bundle["snapshot"])
    reloaded = FeatureSnapshot.model_validate(snapshot.model_dump(mode="json"))
    assert reloaded == snapshot
