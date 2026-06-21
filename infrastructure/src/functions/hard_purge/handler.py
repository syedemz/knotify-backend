"""
knotify-hard-purge Lambda handler — story 9.11

Removes users whose soft-delete retention window has expired from Aurora.

Two invocation modes:

  Scheduled (daily EventBridge rule) — no user_id in event:
    DELETE FROM users
    WHERE deleted_at IS NOT NULL
      AND deleted_at < NOW() - INTERVAL '30 days'

    All rows in sibling tables (siblings, friendships, friend_requests,
    bookmarks, blocks) are removed automatically via ON DELETE CASCADE
    declared in the schema migrations.

  Per-user (invoked by purge_immediately branch in Step Functions) — user_id present:
    DELETE FROM users
    WHERE user_id = %s::uuid
      AND deleted_at IS NOT NULL

    The 30-day window is dropped (GDPR right-to-be-forgotten bypasses it),
    but the soft-delete guard (deleted_at IS NOT NULL) is RETAINED — this
    Lambda cannot hard-delete a user who has not been soft-deleted first.
    If the row is not soft-deleted, rows_affected=0 is returned and the
    Lambda exits cleanly (no exception).

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

# Scheduled mode: delete all rows whose soft-delete is older than 30 days.
# ON DELETE CASCADE in the schema removes rows in siblings, friendships,
# friend_requests, bookmarks, and blocks automatically.
_SCHEDULED_DELETE_SQL = """
DELETE FROM users
WHERE deleted_at IS NOT NULL
  AND deleted_at < NOW() - INTERVAL '30 days'
"""

# Per-user mode: delete a specific user regardless of the 30-day window.
# The soft-delete guard (deleted_at IS NOT NULL) is intentionally retained:
# this Lambda must not hard-delete a user who has not been soft-deleted first.
# Parameterised with a single positional bind: (user_id,).
_PER_USER_DELETE_SQL = """
DELETE FROM users
WHERE user_id = %s::uuid
  AND deleted_at IS NOT NULL
"""

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

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            if mode == "per_user":
                cur.execute(_PER_USER_DELETE_SQL, (user_id,))
            else:
                cur.execute(_SCHEDULED_DELETE_SQL)
            rows_affected: int = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    logger.info(
        "hard_purge_complete",
        extra={"mode": mode, "rows_affected": rows_affected, "user_id": user_id},
    )

    return {"rows_affected": rows_affected, "mode": mode}
