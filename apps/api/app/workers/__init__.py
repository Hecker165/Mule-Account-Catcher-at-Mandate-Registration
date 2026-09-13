"""A6 worker package: outbox consumer and revoke execution."""

from app.workers.config import WorkerConfig
from app.workers.outbox_worker import OutboxWorker

__all__ = ["OutboxWorker", "WorkerConfig"]
