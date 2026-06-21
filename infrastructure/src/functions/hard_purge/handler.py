"""
knotify-hard-purge Lambda handler — story 9.11

Removes users whose soft-delete retention window has expired from Aurora.

Two invocation modes:

  Scheduled (daily EventBridge rule) — no user_id in event:
    1. SELECT eligible user_ids (deleted_at IS NOT NULL AND older than 30 days).
    2. For each user_id, in its own transaction with the RLS GUC set to that
       user, run:
           DELETE FROM users WHERE user_id = %s::uuid AND deleted_at IS NOT NULL
    Returns the total number of rows deleted.

  Per-user (invoked by purge_immediately branch in Step Functions) — user_id present:
    Single-row DELETE under the RLS context of that user:
        DELETE FROM users
        WHERE user_id = %s::uuid AND deleted_at IS NOT NULL
    The 30-day window is dropped (GDPR right-to-be-forgotten bypasses it),
    but the soft-delete guard (deleted_at IS NOT NULL) is RETAINED — this
    Lambda cannot hard-delete a user who has not been soft-deleted first.
    If the row is not soft-deleted, rows_affected=0 is returned and the
    Lambda exits cleanly (no exception).

Why the GUC dance is needed
---------------------------
Migration 0007 forces RLS on users and migration 0017 adds a DELETE policy:
    USING (user_id = current_setting('app.requesting_user_id', true)::uuid)
Each DELETE therefore must run with the GUC set to the row being removed,
which forces the scheduled path to iterate one user per transaction.

The eligibility SELECT runs under its own RLS context: the user_id GUC is set
to the zero UUID (no real user matches) and the user_sex GUC is set to the
empty string, which makes `sex != ''` true for all rows in the
users_opposite_sex_only SELECT policy, so the scan returns every eligible
soft-deleted user.

Environment variables:
    DB_SECRET_NAME  — Secrets Manager secret name for the Aurora app-user
                      credential (knotify-<env>-app-user-credential).
    AURORA_HOST     — Aurora cluster writer endpoint.
    AURORA_PORT     — Aurora port (default 5432).
    AURORA_DBNAME   — Aurora database name.
    LOG_LEVEL       — logging level (default: INFO).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import knotify_db

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

# Scheduled mode: enumerate stale user_ids first under an "all rows visible"
# RLS context (empty user_sex makes `sex != ''` true everywhere in the SELECT
# policy from migration 0007).
_SCHEDULED_SELECT_SQL = """
SELECT user_id
FROM users
WHERE deleted_at IS NOT NULL
  AND deleted_at < NOW() - INTERVAL '30 days'
"""

# Per-row DELETE used by BOTH scheduled and per-user modes.
# Runs under an RLS context with app.requesting_user_id = the target user.
# The soft-delete guard (deleted_at IS NOT NULL) is intentionally retained:
# this Lambda must not hard-delete a user who has not been soft-deleted first.
_PER_USER_DELETE_SQL = """
DELETE FROM users
WHERE user_id = %s::uuid
  AND deleted_at IS NOT NULL
"""

# Sentinel UUID for the eligibility SELECT — no real user matches this value,
# so the user_id branch of users_opposite_sex_only never fires; visibility
# comes entirely from `sex != ''`.
_NIL_UUID: str = "00000000-0000-0000-0000-000000000000"

# ---------------------------------------------------------------------------
# Module-level connection cache — reused across warm invocations.
# ---------------------------------------------------------------------------

_conn = None


def _get_conn():
    """Return a (possibly cached) psycopg2 connection."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = knotify_db.get_connection(_DB_SECRET_NAME)
    return _conn


def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point.

    Input fields:
        user_id (optional) — UUID string of the user to hard-purge.
                             Present → per-user mode.
                             Absent  → scheduled mode (30-day window applied).

    Returns:
        {"rows_affected": <int>, "mode": "scheduled"|"per_user"}

        scheduled mode:
            rows_affected = number of users deleted (may be 0 or many).
        per_user mode:
            rows_affected = 1 if the user was purged.
            rows_affected = 0 if the user was not soft-deleted (no-op, no error).

    Raises:
        psycopg2 errors — DB connection / execution failures propagate so
                          Step Functions can apply retry / Catch logic.
    """
    user_id: str | None = event.get("user_id")
    mode: str = "per_user" if user_id is not None else "scheduled"

    conn = _get_conn()
    conn.autocommit = False

    if mode == "per_user":
        rows_affected = _delete_one(conn, user_id)
    else:
        rows_affected = _scheduled_purge(conn)

    logger.info(
        "hard_purge_complete",
        extra={"mode": mode, "rows_affected": rows_affected, "user_id": user_id},
    )

    return {"rows_affected": rows_affected, "mode": mode}


def _delete_one(conn, user_id: str) -> int:
    """
    DELETE one users row under an RLS context bound to that user.

    user_sex="" is passed because the DELETE policy added in migration 0017
    only consults app.requesting_user_id; user_sex is unused for this operation
    but must be a non-NULL string for set_rls_context's bind to succeed.
    """
    with knotify_db.rls_context(conn, user_id, ""):
        with conn.cursor() as cur:
            cur.execute(_PER_USER_DELETE_SQL, (user_id,))
            return cur.rowcount


def _scheduled_purge(conn) -> int:
    """
    Enumerate eligible users (deleted_at older than 30 days) and DELETE each
    in its own RLS-scoped transaction.

    The eligibility SELECT runs in a read-only transaction with the user_sex
    GUC set to "" — this makes `sex != ''` true for all rows in the SELECT
    policy from migration 0007, so every stale soft-deleted user is visible.
    """
    eligible_ids: list[str] = []
    with knotify_db.rls_context(conn, _NIL_UUID, ""):
        with conn.cursor() as cur:
            cur.execute(_SCHEDULED_SELECT_SQL)
            eligible_ids = [str(row[0]) for row in cur.fetchall()]

    total = 0
    for uid in eligible_ids:
        total += _delete_one(conn, uid)
    return total
