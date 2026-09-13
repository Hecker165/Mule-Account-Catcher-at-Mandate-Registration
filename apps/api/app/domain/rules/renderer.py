"""Explanation renderer: bounded, PII-free reason text from rule templates."""

from __future__ import annotations

from typing import Any

from app.contracts.feature_snapshot import FeatureSnapshot
from app.domain.rules.config import RuleDefinition

_MAX_REASON_CHARS = 240


class _SafeMapping(dict[str, Any]):
    """Format mapping that renders missing fields as ``"unavailable"``."""

    def __missing__(self, key: str) -> str:
        return "unavailable"


def render_reason(rule: RuleDefinition, snapshot: FeatureSnapshot) -> str:
    """Fill ``rule.reason_template`` from snapshot fields; never raise."""
    mapping = _SafeMapping({"npci_flag_present": "flagged"})
    mapping.update(snapshot.model_dump())
    try:
        text = rule.reason_template.format_map(mapping)
    except Exception:
        text = rule.reason_template
    text = text[:_MAX_REASON_CHARS]
    if "hmac-sha256:" in text or "sha256:" in text:
        text = text.replace("hmac-sha256:", "[redacted]").replace("sha256:", "[redacted]")
    return text
