#!/usr/bin/env python3
"""
wait_for_db.py — poll docker compose healthcheck until Postgres is healthy.

Usage:
    python wait_for_db.py <compose_file> [max_tries]

Polls `docker compose ps --format '{{.Health}}'` every 2 seconds until the
output contains "healthy" or max_tries is reached.

Exit codes:
  0 — healthy
  1 — timed out or argument error
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"Usage: {argv[0]} <compose_file> [max_tries]", file=sys.stderr)
        return 1

    compose_file = argv[1]
    max_tries = int(argv[2]) if len(argv) >= 3 else 30

    for attempt in range(1, max_tries + 1):
        result = subprocess.run(
            [
                "docker", "compose", "-f", compose_file,
                "ps", "--format", "{{.Health}}",
            ],
            capture_output=True,
            text=True,
        )
        if "healthy" in result.stdout:
            print("[db-up] Postgres is healthy")
            return 0
        print(f"[db-up]   still waiting (attempt {attempt}/{max_tries}) ...")
        time.sleep(2)

    print(
        f"ERROR: Postgres container did not become healthy after {max_tries} attempts",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
