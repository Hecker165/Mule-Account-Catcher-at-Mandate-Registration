"""Canonical Redis key builders for the A4 feature store.

`keys.py` builds every key; nothing else formats keys. Keys contain only the
key prefix, merchant namespace, a signal word, a window label and HMAC hashes —
never raw identifiers.
"""

from __future__ import annotations

import re

SIGNALS = frozenset({"ip", "device", "customer", "vpa"})
WINDOWS = {"5m": 300, "1h": 3600}  # label -> window seconds
KNOWN_DEVICES_TTL_SECONDS = 86_400  # 24h
DEMO_SHARED_TTL_SECONDS = 3_660  # 1h window + 60s buffer

_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")
_HASH_PATTERN = re.compile(r"^hmac-sha256:[a-f0-9]{64}$")


def velocity_key(
    key_prefix: str,
    merchant_namespace: str,
    signal: str,
    window_label: str,
    hash_value: str,
) -> str:
    """Build a sliding-window velocity key for one merchant/signal/window/hash."""
    _check_namespace(merchant_namespace)
    if signal not in SIGNALS:
        raise ValueError("invalid velocity signal")
    if window_label not in WINDOWS:
        raise ValueError("invalid velocity window")
    _check_hash(hash_value)
    return f"{key_prefix}:vel:{merchant_namespace}:{signal}:{window_label}:{hash_value}"


def known_devices_key(key_prefix: str, customer_hash: str) -> str:
    """Build the known-devices set key for one customer hash."""
    _check_hash(customer_hash)
    return f"{key_prefix}:dev:{customer_hash}"


def demo_shared_merchants_key(key_prefix: str, device_hash: str) -> str:
    """Build the demo shared-merchant set key for one device hash."""
    _check_hash(device_hash)
    return f"{key_prefix}:demoshared:{device_hash}"


def _check_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or _NAMESPACE_PATTERN.match(namespace) is None:
        raise ValueError("invalid merchant namespace")


def _check_hash(hash_value: str) -> None:
    if not isinstance(hash_value, str) or _HASH_PATTERN.match(hash_value) is None:
        raise ValueError("invalid hash value")
