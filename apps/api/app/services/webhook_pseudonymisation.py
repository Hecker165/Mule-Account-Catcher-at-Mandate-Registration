"""VPA pseudonymisation for A3 webhook ingestion.

A2 owns ``privacy_hashing.py`` and its four domains. A3 owns the ``vpa``
domain here so the two packages never edit the same file. The construction
matches A2 exactly and uses the single ``HMAC_PEPPER`` from A0 settings.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata

from pydantic import SecretStr

from app.contracts.common import HashValue


class VpaPseudonymizer:
    """Domain-separated HMAC-SHA256 pseudonymizer for VPA identifiers."""

    DOMAIN_VPA = b"vpa"

    def __init__(self, pepper: SecretStr | str) -> None:
        if isinstance(pepper, SecretStr):
            self._pepper = pepper.get_secret_value().encode("utf-8")
        else:
            self._pepper = pepper.encode("utf-8")

    def hash_vpa(
        self, username: str | None, handle: str | None
    ) -> tuple[HashValue | None, str | None]:
        """Hash a VPA, returning ``(vpa_hash, vpa_handle)``.

        Returns ``(None, None)`` when no usable username is present.
        The handle is lowercased and non-secret; it is stored as a plain column.
        """
        normalised_username = self._normalise_part(username)
        if not normalised_username:
            return None, None
        normalised_handle = self._normalise_part(handle)
        if normalised_handle:
            canonical = f"{normalised_username}@{normalised_handle}"
        else:
            canonical = normalised_username
        mac = hmac.new(
            self._pepper,
            self.DOMAIN_VPA + b"\x00" + canonical.encode("utf-8"),
            hashlib.sha256,
        )
        vpa_hash = HashValue(f"hmac-sha256:{mac.hexdigest()}")
        return vpa_hash, normalised_handle

    @staticmethod
    def _normalise_part(value: str | None) -> str | None:
        if value is None:
            return None
        normalised = unicodedata.normalize("NFKC", value).strip().lower()
        return normalised or None
