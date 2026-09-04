#!/usr/bin/env python3
"""CLI for verifying audit chain integrity.

Usage:
    python scripts/verify_audit_chain.py --aggregate-type risk_session \\
        --aggregate-id UUID [--database-url URL]

Exits:
    0 - valid chain (or empty)
    1 - invalid chain
    2 - invalid CLI arguments
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

# Make the API package importable regardless of the caller's working directory
API_DIR = Path(__file__).resolve().parent.parent / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.core.settings import get_settings  # noqa: E402
from app.persistence.audit_chain import AuditVerificationResult  # noqa: E402
from app.persistence.session import dispose_engine, get_session_factory  # noqa: E402
from app.repositories.audit import AuditRepository  # noqa: E402


async def verify_aggregate(
    aggregate_type: str, aggregate_id: UUID, database_url: str | None = None
) -> AuditVerificationResult:
    """Verify an aggregate's audit chain."""
    if database_url is not None:
        import os

        await dispose_engine()
        get_settings.cache_clear()
        os.environ["POSTGRES_URL"] = database_url

    session_factory = get_session_factory()
    async with session_factory() as session:
        repo = AuditRepository(session)
        result = await repo.verify_aggregate(aggregate_type, aggregate_id)

    if database_url is not None:
        await dispose_engine()

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify tamper-evident audit chain for an aggregate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--aggregate-type",
        required=True,
        choices=["risk_session", "mandate_event", "risk_assessment", "action_request"],
        help="Type of aggregate to verify",
    )
    parser.add_argument(
        "--aggregate-id",
        required=True,
        help="UUID of the aggregate",
    )
    parser.add_argument(
        "--database-url",
        help="PostgreSQL connection URL (defaults to Settings.postgres_url)",
    )

    try:
        args = parser.parse_args()
    except SystemExit:
        return 2

    try:
        aggregate_id = UUID(args.aggregate_id)
    except ValueError:
        print("Error: --aggregate-id must be a valid UUID", file=sys.stderr)
        return 2

    result = asyncio.run(verify_aggregate(args.aggregate_type, aggregate_id, args.database_url))

    # Print JSON result
    print(json.dumps(asdict(result)))

    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
