"""A6 worker configuration: poll interval, batch size, attempts and backoff."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str = "outbox-worker-1"
    poll_interval_seconds: float = 2.0
    batch_size: int = 5
    max_attempts: int = 8
    backoff_base_seconds: float = 5.0
    backoff_cap_seconds: float = 300.0

    def backoff_for(self, attempt_number: int) -> float:
        """Exponential backoff: min(base * 2**(attempt-1), cap)."""
        backoff: float = min(
            self.backoff_base_seconds * (2 ** (attempt_number - 1)),
            self.backoff_cap_seconds,
        )
        return backoff
