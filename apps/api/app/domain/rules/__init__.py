"""A5 rule engine package: deterministic rules-v1 evaluation."""

from app.domain.rules.catalogue import DEFAULT_RULES
from app.domain.rules.config import DecisionThresholds, RuleDefinition, RuleSetConfig
from app.domain.rules.engine import RuleEngine, RuleEngineResult
from app.domain.rules.renderer import render_reason

__all__ = [
    "DEFAULT_RULES",
    "DecisionThresholds",
    "RuleDefinition",
    "RuleEngine",
    "RuleEngineResult",
    "RuleSetConfig",
    "render_reason",
]
