"""Central logging configuration.

Configures the root logger once per process with a fixed, minimal format.
The module never touches message content and adds no request hooks: callers
must never pass request bodies, signatures, credentials, raw VPAs or raw
identifiers as log arguments — log only IDs, statuses and fixed strings.
This discipline is enforced by ``tests/security/test_log_redaction.py`` and
``tests/security/test_static_secret_scan.py``.
"""

from __future__ import annotations

import logging

from app.core.settings import get_settings

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# Module-level idempotency guard: configure the root logger once per process.
_configured = False


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger once per process.

    Adds a single ``StreamHandler`` with the fixed format
    ``"%(asctime)s %(levelname)s %(name)s %(message)s"`` and sets the root
    level from the argument or, when omitted, ``settings.log_level``.
    Idempotent: subsequent calls return without adding duplicate handlers
    or overwriting an explicitly requested level.
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(_resolve_level(level) or _resolve_level(get_settings().log_level))
    _configured = True


def _resolve_level(level: str | None) -> int:
    """Map a level name to its numeric value; fall back to INFO."""
    if not level:
        return logging.INFO
    resolved = getattr(logging, level.upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO
