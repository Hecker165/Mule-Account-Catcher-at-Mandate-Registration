"""Pure deterministic rule engine: snapshot + stage -> score and decision.

The engine is synchronous, pure and free of I/O, clock access and
randomness. Same input + same config always gives a byte-identical result.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.contracts.common import Decision, DecisionStage
from app.contracts.feature_snapshot import FeatureSnapshot
from app.contracts.risk_assessment import RuleEvaluation
from app.domain.rules.config import RuleSetConfig
from app.domain.rules.renderer import render_reason


@dataclass(frozen=True)
class RuleEngineResult:
    score: int
    decision: Decision
    rule_evaluations: tuple[RuleEvaluation, ...]


class RuleEngine:
    """Evaluates the versioned rule catalogue against a feature snapshot."""

    def __init__(self, config: RuleSetConfig | None = None) -> None:
        self._config = config or RuleSetConfig.default()

    @property
    def engine_version(self) -> str:
        return self._config.engine_version

    def evaluate(self, snapshot: FeatureSnapshot, stage: DecisionStage) -> RuleEngineResult:
        """Score a snapshot at a lifecycle stage with full ordered evaluations."""
        evaluations: list[RuleEvaluation] = []
        for rule in self._config.rules:
            if stage not in rule.stages:
                evaluations.append(
                    RuleEvaluation(
                        rule_id=rule.rule_id,
                        triggered=False,
                        points=0,
                        reason_code=None,
                        reason_text=None,
                    )
                )
                continue
            if rule.predicate(snapshot):
                evaluations.append(
                    RuleEvaluation(
                        rule_id=rule.rule_id,
                        triggered=True,
                        points=rule.points,
                        reason_code=rule.reason_code,
                        reason_text=render_reason(rule, snapshot),
                    )
                )
            else:
                evaluations.append(
                    RuleEvaluation(
                        rule_id=rule.rule_id,
                        triggered=False,
                        points=0,
                        reason_code=None,
                        reason_text=None,
                    )
                )
        score = max(0, min(100, sum(evaluation.points for evaluation in evaluations)))
        if stage == DecisionStage.PRECHECK:
            decision = (
                Decision.CHALLENGE
                if score >= self._config.thresholds.challenge_min
                else Decision.ALLOW
            )
        else:
            decision = (
                Decision.BLOCK if score >= self._config.thresholds.block_min else Decision.ALLOW
            )
        return RuleEngineResult(score=score, decision=decision, rule_evaluations=tuple(evaluations))
