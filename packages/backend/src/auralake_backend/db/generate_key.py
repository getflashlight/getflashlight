"""Generate an API key from the command line.

Usage (inside the backend container):

    docker compose exec backend auralake-generate-key
    docker compose exec backend auralake-generate-key --name "CI pipeline"
"""

from __future__ import annotations

import argparse
import os
import sys

from auralake_backend.db.engine import get_engine, init_engine
from auralake_backend.server.auth import create_api_key


def run() -> None:
    parser = argparse.ArgumentParser(description="Generate an Auralake API key")
    parser.add_argument(
        "--name",
        default="default",
        help="Human-readable name for the key (default: 'default')",
    )
    args = parser.parse_args()

    url = os.environ.get("AURALAKE_DATABASE_URL")
    if not url:
        print("ERROR: AURALAKE_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    init_engine(url)

    from sqlmodel import Session

    with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
        record, raw_key = create_api_key(session, args.name)

    print("API key created successfully.\n")
    print(f"  Name: {record.name}")
    print(f"  ID:   {record.id}")
    print(f"  Key:  {raw_key}")
    print("\nStore this key securely — it cannot be retrieved again.")
