"""HMAC pseudonymisation for A2 session capture.

Implements deterministic HMAC-SHA256 hashing with domain separation
for customer_reference, IP address, device_fingerprint, and user_agent.
"""

import hashlib
import hmac
import ipaddress
import unicodedata

from pydantic import SecretStr

from app.contracts.common import HashValue


class HmacPseudonymizer:
    """Domain-separated HMAC-SHA256 pseudonymizer.

    Uses a shared pepper and per-field domain labels to ensure
    identical values under different domains produce different hashes.
    """

    DOMAIN_CUSTOMER_REFERENCE = b"customer_reference"
    DOMAIN_IP_ADDRESS = b"ip_address"
    DOMAIN_DEVICE_FINGERPRINT = b"device_fingerprint"
    DOMAIN_USER_AGENT = b"user_agent"

    def __init__(self, pepper: SecretStr | str) -> None:
        if isinstance(pepper, SecretStr):
            self._pepper = pepper.get_secret_value().encode("utf-8")
        else:
            self._pepper = pepper.encode("utf-8")

    def _normalize_customer_reference(self, value: str | None) -> bytes | None:
        if value is None:
            return None
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            return None
        return normalized.encode("utf-8")

    def _normalize_ip(self, value: str | None) -> bytes | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        try:
            ip = ipaddress.ip_address(normalized)
            return ip.compressed.encode("utf-8")
        except ValueError:
            return None

    def _normalize_device_fingerprint(self, value: str | None) -> bytes | None:
        if value is None:
            return None
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            return None
        return normalized.encode("utf-8")

    def _normalize_user_agent(self, value: str | None) -> bytes | None:
        if value is None:
            return None
        normalized = unicodedata.normalize("NFKC", value)
        # Split on all whitespace and join with single space
        parts = normalized.split()
        if not parts:
            return None
        return " ".join(parts).encode("utf-8")

    def _hash(self, domain: bytes, value_bytes: bytes | None) -> HashValue | None:
        if value_bytes is None:
            return None
        mac = hmac.new(self._pepper, domain + b"\x00" + value_bytes, hashlib.sha256)
        return HashValue(f"hmac-sha256:{mac.hexdigest()}")

    def hash_customer_reference(self, value: str | None) -> HashValue | None:
        return self._hash(self.DOMAIN_CUSTOMER_REFERENCE, self._normalize_customer_reference(value))

    def hash_ip(self, value: str | None) -> HashValue | None:
        return self._hash(self.DOMAIN_IP_ADDRESS, self._normalize_ip(value))

    def hash_device_fingerprint(self, value: str | None) -> HashValue | None:
        return self._hash(self.DOMAIN_DEVICE_FINGERPRINT, self._normalize_device_fingerprint(value))

    def hash_user_agent(self, value: str | None) -> HashValue | None:
        return self._hash(self.DOMAIN_USER_AGENT, self._normalize_user_agent(value))
