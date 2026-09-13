"""A4 feature package: Redis feature store and deterministic feature extraction."""

from app.services.features.extractor import CORRELATION_FALLBACK_NAMESPACE, FeatureExtractor
from app.services.features.keys import (
    DEMO_SHARED_TTL_SECONDS,
    KNOWN_DEVICES_TTL_SECONDS,
    SIGNALS,
    WINDOWS,
    demo_shared_merchants_key,
    known_devices_key,
    velocity_key,
)
from app.services.features.redis_store import FeatureStoreUnavailable, RedisFeatureStore
from app.services.features.reputation import (
    NetworkReputationProvider,
    ReputationResult,
    UnavailableNetworkReputationProvider,
)

__all__ = [
    "CORRELATION_FALLBACK_NAMESPACE",
    "DEMO_SHARED_TTL_SECONDS",
    "KNOWN_DEVICES_TTL_SECONDS",
    "SIGNALS",
    "WINDOWS",
    "FeatureExtractor",
    "FeatureStoreUnavailable",
    "NetworkReputationProvider",
    "RedisFeatureStore",
    "ReputationResult",
    "UnavailableNetworkReputationProvider",
    "demo_shared_merchants_key",
    "known_devices_key",
    "velocity_key",
]
