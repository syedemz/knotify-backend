"""
One-shot proof: SET LOCAL app.requesting_user_id survives inside a transaction
on the Supabase pooler (transaction mode, port 6543).

This is the gating assumption for phase 14 (Supabase alternative to Aurora RDS
and private VPC). If the pooler resets session-level settings mid-statement
under transaction mode, the RLS policy from migration 0007 would silently
return zero rows for every Lambda — and every downstream integration test
would fail without a clear signal.

See:
  - implementationplan/phase-14-supabase-alternative.md story 14.1
  - features/feature-1-supabase-alternative-plan.md story f1.0

Dependency: psycopg2-binary (or psycopg2). Install locally with:
    pip install psycopg2-binary

Run (bash / zsh):
    SUPABASE_HOST='aws-0-eu-central-1.pooler.supabase.com' \
    SUPABASE_PORT=6543 \
    SUPABASE_DBNAME=postgres \
    SUPABASE_USERNAME='postgres.<project-ref>' \
    SUPABASE_PASSWORD='<password>' \
    python3 scripts/supabase_pooler_proof.py

Run (Windows PowerShell):
    $env:SUPABASE_HOST='aws-0-eu-central-1.pooler.supabase.com'
    $env:SUPABASE_PORT='6543'
    $env:SUPABASE_DBNAME='postgres'
    $env:SUPABASE_USERNAME='postgres.<project-ref>'
    $env:SUPABASE_PASSWORD='<password>'
    python scripts/supabase_pooler_proof.py

Expected output:
    GUC observed in query: some-test-uuid
    PASS

Exit code 0 on PASS; exit code 1 on FAIL. A FAIL halts the entire phase —
surface the output and do not proceed. A different pooler mode (session) or
direct-connection endpoint would need evaluation in a separate design round.
"""

from __future__ import annotations

import os
import sys

import psycopg2


_EXPECTED_GUC_VALUE = "some-test-uuid"


def _require_env(name: str) -> str:
    """Fail fast with a clear message if a required env var is missing."""
    value = os.environ.get(name)
    if not value:
        print(
            f"FAIL — required environment variable {name} is not set. "
            "See the module docstring for the exact invocation.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def main() -> int:
    host = _require_env("SUPABASE_HOST")
    port = int(_require_env("SUPABASE_PORT"))
    dbname = _require_env("SUPABASE_DBNAME")
    username = _require_env("SUPABASE_USERNAME")
    password = _require_env("SUPABASE_PASSWORD")

    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
        sslmode="require",
    )
    try:
        # autocommit = False is the default for psycopg2 but is set explicitly
        # here to make the invariant visible: the SET LOCAL must live inside
        # a transaction, so we must not be in autocommit mode.
        conn.autocommit = False
        with conn.cursor() as cur:
            # psycopg2 implicitly opens a transaction on the first statement
            # after autocommit=False. An explicit BEGIN is redundant but
            # harmless and keeps the proof script readable.
            cur.execute("BEGIN")
            cur.execute(
                "SET LOCAL app.requesting_user_id = %s",
                (_EXPECTED_GUC_VALUE,),
            )
            cur.execute(
                "SELECT current_setting('app.requesting_user_id', true)"
            )
            row = cur.fetchone()
            value = row[0] if row else None
            cur.execute("COMMIT")

        print(f"GUC observed in query: {value!r}")
        if value == _EXPECTED_GUC_VALUE:
            print("PASS")
            return 0
        print(
            "FAIL — GUC did not survive to the SELECT inside the transaction. "
            "Phase 14 cannot proceed with transaction-mode pooling as-is."
        )
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
