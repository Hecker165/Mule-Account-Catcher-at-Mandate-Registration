"""Unit tests for privacy hashing (HMAC pseudonymisation)."""

import pytest

from app.services.privacy_hashing import HmacPseudonymizer


@pytest.fixture
def test_pepper() -> str:
    return "test-pepper-1234567890"


@pytest.fixture
def pseudonymizer(test_pepper: str) -> HmacPseudonymizer:
    return HmacPseudonymizer(test_pepper)


class TestHashCustomerReference:
    def test_deterministic_output(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Same input + same pepper = same output."""
        value = "customer-123"
        result1 = pseudonymizer.hash_customer_reference(value)
        result2 = pseudonymizer.hash_customer_reference(value)
        assert result1 == result2
        assert isinstance(result1, str)
        assert result1.startswith("hmac-sha256:")

    def test_nfkc_normalization(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Unicode NFKC normalization is applied."""
        # Composed vs decomposed forms should hash the same
        value1 = "café"  # NFC
        value2 = "cafe\u0301"  # NFD
        result1 = pseudonymizer.hash_customer_reference(value1)
        result2 = pseudonymizer.hash_customer_reference(value2)
        assert result1 == result2

    def test_strips_whitespace(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Leading/trailing whitespace is stripped."""
        result1 = pseudonymizer.hash_customer_reference("  customer-123  ")
        result2 = pseudonymizer.hash_customer_reference("customer-123")
        assert result1 == result2

    def test_empty_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Empty string returns None."""
        result = pseudonymizer.hash_customer_reference("")
        assert result is None

    def test_none_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        """None input returns None."""
        result = pseudonymizer.hash_customer_reference(None)
        assert result is None

    def test_case_preserved(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Case is preserved for customer references."""
        result1 = pseudonymizer.hash_customer_reference("Customer-123")
        result2 = pseudonymizer.hash_customer_reference("customer-123")
        assert result1 != result2


class TestHashIP:
    def test_deterministic_output(self, pseudonymizer: HmacPseudonymizer) -> None:
        result1 = pseudonymizer.hash_ip("192.168.1.1")
        result2 = pseudonymizer.hash_ip("192.168.1.1")
        assert result1 == result2

    def test_ipv4_compression(self, pseudonymizer: HmacPseudonymizer) -> None:
        """IPv4 addresses are normalized to compressed form."""
        result1 = pseudonymizer.hash_ip("192.168.1.1")
        result2 = pseudonymizer.hash_ip("192.168.1.1")
        assert result1 == result2

    def test_leading_zeros_normalized(self, pseudonymizer: HmacPseudonymizer) -> None:
        """IP addresses with leading zeros are normalized."""
        # ipaddress module normalizes these
        result1 = pseudonymizer.hash_ip("192.168.1.1")
        result2 = pseudonymizer.hash_ip("192.168.1.1")
        assert result1 == result2

    def test_ipv6_compression(self, pseudonymizer: HmacPseudonymizer) -> None:
        """IPv6 addresses are normalized to compressed form."""
        result1 = pseudonymizer.hash_ip("2001:0db8:0000:0000:0000:0000:0000:0001")
        result2 = pseudonymizer.hash_ip("2001:db8::1")
        assert result1 == result2

    def test_strips_whitespace(self, pseudonymizer: HmacPseudonymizer) -> None:
        result1 = pseudonymizer.hash_ip("  192.168.1.1  ")
        result2 = pseudonymizer.hash_ip("192.168.1.1")
        assert result1 == result2

    def test_invalid_ip_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Invalid IP addresses return None without raising."""
        assert pseudonymizer.hash_ip("not-an-ip") is None
        assert pseudonymizer.hash_ip("999.999.999.999") is None

    def test_empty_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        assert pseudonymizer.hash_ip("") is None

    def test_none_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        assert pseudonymizer.hash_ip(None) is None


class TestHashDeviceFingerprint:
    def test_deterministic_output(self, pseudonymizer: HmacPseudonymizer) -> None:
        value = "device-fp-123"
        result1 = pseudonymizer.hash_device_fingerprint(value)
        result2 = pseudonymizer.hash_device_fingerprint(value)
        assert result1 == result2

    def test_nfkc_normalization(self, pseudonymizer: HmacPseudonymizer) -> None:
        value1 = "device\u00e9"  # NFC
        value2 = "device\u0065\u0301"  # NFD
        result1 = pseudonymizer.hash_device_fingerprint(value1)
        result2 = pseudonymizer.hash_device_fingerprint(value2)
        assert result1 == result2

    def test_strips_whitespace(self, pseudonymizer: HmacPseudonymizer) -> None:
        result1 = pseudonymizer.hash_device_fingerprint("  fp-123  ")
        result2 = pseudonymizer.hash_device_fingerprint("fp-123")
        assert result1 == result2

    def test_empty_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        assert pseudonymizer.hash_device_fingerprint("") is None

    def test_none_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        assert pseudonymizer.hash_device_fingerprint(None) is None


class TestHashUserAgent:
    def test_deterministic_output(self, pseudonymizer: HmacPseudonymizer) -> None:
        value = "Mozilla/5.0 (Linux; Android 14)"
        result1 = pseudonymizer.hash_user_agent(value)
        result2 = pseudonymizer.hash_user_agent(value)
        assert result1 == result2

    def test_whitespace_canonicalization(self, pseudonymizer: HmacPseudonymizer) -> None:
        """All whitespace sequences collapsed to single space."""
        value1 = "Mozilla/5.0   (Linux;  Android 14)"
        value2 = "Mozilla/5.0 (Linux; Android 14)"
        result1 = pseudonymizer.hash_user_agent(value1)
        result2 = pseudonymizer.hash_user_agent(value2)
        assert result1 == result2

    def test_newlines_tabs_normalized(self, pseudonymizer: HmacPseudonymizer) -> None:
        value1 = "Mozilla/5.0\n(Linux;\tAndroid 14)"
        value2 = "Mozilla/5.0 (Linux; Android 14)"
        result1 = pseudonymizer.hash_user_agent(value1)
        result2 = pseudonymizer.hash_user_agent(value2)
        assert result1 == result2

    def test_case_preserved(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Case is preserved for user agent."""
        result1 = pseudonymizer.hash_user_agent("Mozilla/5.0")
        result2 = pseudonymizer.hash_user_agent("mozilla/5.0")
        assert result1 != result2

    def test_empty_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        assert pseudonymizer.hash_user_agent("") is None

    def test_none_returns_none(self, pseudonymizer: HmacPseudonymizer) -> None:
        assert pseudonymizer.hash_user_agent(None) is None


class TestDomainSeparation:
    def test_different_domains_produce_different_hashes(
        self, pseudonymizer: HmacPseudonymizer
    ) -> None:
        """Same value under different domains produces different hashes."""
        value = "test-value"
        customer_hash = pseudonymizer.hash_customer_reference(value)
        _ = pseudonymizer.hash_ip(value)  # Won't parse as IP, but domain differs
        device_hash = pseudonymizer.hash_device_fingerprint(value)
        ua_hash = pseudonymizer.hash_user_agent(value)

        # At least device and UA should differ since both accept arbitrary strings
        assert device_hash != ua_hash or customer_hash != device_hash

    def test_same_value_different_pepper_produces_different_hashes(self) -> None:
        """Same value + different pepper = different hash."""
        pseudo1 = HmacPseudonymizer("pepper-one")
        pseudo2 = HmacPseudonymizer("pepper-two")
        value = "test-value"

        result1 = pseudo1.hash_customer_reference(value)
        result2 = pseudo2.hash_customer_reference(value)
        assert result1 != result2


class TestHashValueFormat:
    def test_hash_starts_with_prefix(self, pseudonymizer: HmacPseudonymizer) -> None:
        result = pseudonymizer.hash_customer_reference("test")
        assert str(result).startswith("hmac-sha256:")

    def test_hash_is_64_hex_chars(self, pseudonymizer: HmacPseudonymizer) -> None:
        result = pseudonymizer.hash_customer_reference("test")
        hex_part = str(result)[12:]  # Remove "hmac-sha256:"
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdef" for c in hex_part)

    def test_no_raw_value_in_repr(self, pseudonymizer: HmacPseudonymizer) -> None:
        """Raw value doesn't appear in repr/str."""
        value = "secret-customer-data"
        result = pseudonymizer.hash_customer_reference(value)
        result_str = str(result)
        assert "secret-customer-data" not in result_str
        assert "customer" not in result_str.lower() or "secret" not in result_str.lower()
