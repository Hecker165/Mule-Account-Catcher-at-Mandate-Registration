"""Immutable rules-v1 configuration: points, thresholds and decision boundaries.

Thresholds live only here; no frontend or route code may contain a score
boundary. Predicates are code, not data, so these are frozen dataclasses
rather than Pydantic models.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.contracts.common import DecisionStage
from app.contracts.feature_snapshot import FeatureSnapshot

_RULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_REASON_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")


@dataclass(frozen=True)
class DecisionThresholds:
    challenge_min: int = 30  # PRECHECK: score >= 30 -> CHALLENGE, else ALLOW
    block_min: int = 70  # POST_CONFIRMATION / REJECTION_AUDIT: score >= 70 -> BLOCK


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str  # ^[a-z][a-z0-9_]{2,63}$ (A0 RuleEvaluation pattern)
    points: int  # 0..100
    stages: frozenset[DecisionStage]  # stages where the rule may fire
    predicate: Callable[[FeatureSnapshot], bool]  # must be None-safe
    reason_code: str  # UPPER_SNAKE, 1..80 chars
    reason_template: str  # str.format template, <= 240 chars when rendered

    def __post_init__(self) -> None:
        if _RULE_ID_PATTERN.match(self.rule_id) is None:
            raise ValueError(f"invalid rule_id: {self.rule_id}")
        if not 0 <= self.points <= 100:
            raise ValueError(f"rule points out of range: {self.rule_id}")
        if not self.stages:
            raise ValueError(f"rule stages must not be empty: {self.rule_id}")
        if _REASON_CODE_PATTERN.match(self.reason_code) is None:
            raise ValueError(f"invalid reason_code: {self.rule_id}")
        if not self.reason_template:
            raise ValueError(f"rule reason_template must not be empty: {self.rule_id}")


@dataclass(frozen=True)
class RuleSetConfig:
    thresholds: DecisionThresholds
    rules: tuple[RuleDefinition, ...]
    engine_version: str = "rules-v1"

    @classmethod
    def default(cls) -> RuleSetConfig:
        """Default rules-v1 catalogue with default thresholds."""
        from app.domain.rules.catalogue import DEFAULT_RULES

        return cls(thresholds=DecisionThresholds(), rules=DEFAULT_RULES)
