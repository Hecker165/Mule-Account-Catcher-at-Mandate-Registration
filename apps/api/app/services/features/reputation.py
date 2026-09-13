"""Optional network-reputation port; v1 ships only an unavailable default.

The port deliberately receives only the IP **hash**. A real provider needs the
raw IP, which would require a separately approved interface change (raw values
must not start flowing through A4); until then the honest v1 behaviour is "no
network reputation data".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ReputationResult:
    vpn_or_proxy: bool
    reputation_score: int  # 0..100, clamped by the caller before use


class NetworkReputationProvider(Protocol):
    async def inspect(self, ip_hash: str, now: datetime) -> ReputationResult | None: ...


class UnavailableNetworkReputationProvider:
    async def inspect(self, ip_hash: str, now: datetime) -> ReputationResult | None:
        return None
