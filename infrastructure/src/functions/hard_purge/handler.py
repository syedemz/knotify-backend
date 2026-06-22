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

Post-DELETE deck_view refresh (hotfix #6):
  After a successful purge (rows_affected > 0 in either mode), this Lambda
  async-invokes the refresh_deck_view Lambda. deck_view is a materialised
  view of `users WHERE deleted_at IS NULL`; a hard-deleted user is gone from
  the source table but the MV snapshot still holds the row until a refresh
  runs. Mirrors the profile handler's _invoke_refresh_lambda pattern (story
  7.4): fire-and-forget, best-effort, skipped when REFRESH_LAMBDA_ARN is
  unset. IAM is already in place — hard_purge runs as aurora_writer which
  holds lambda:InvokeFunction scoped to the refresh Lambda ARN (story 7.4).

Environment variables:
    DB_SECRET_NAME     — Secrets Manager secret name for the Aurora app-user
                         credential (knotify-<env>-app-user-credential).
    AURORA_HOST        — Aurora cluster writer endpoint.
    AURORA_PORT        — Aurora port (default 5432).
    AURORA_DBNAME      — Aurora database name.
    REFRESH_LAMBDA_ARN — ARN of the refresh_deck_view Lambda (hotfix #6).
                         Empty string disables the post-DELETE refresh.
    LOG_LEVEL          — logging level (default: INFO).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
import knotify_db

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")
_REFRESH_LAMBDA_ARN: str = os.environ.get("REFRESH_LAMBDA_ARN", "")

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

    # Post-DELETE deck_view refresh (hotfix #6). One refresh covers any
    # number of rows deleted in this invocation. Skipped when no rows
    # were deleted — no MV staleness to clear.
    if rows_affected > 0:
        _invoke_refresh_lambda()

    return {"rows_affected": rows_affected, "mode": mode}


def _invoke_refresh_lambda() -> None:
    """
    Best-effort async invocation of the refresh_deck_view Lambda.

    See module docstring for rationale. Mirrors profile handler's pattern:
    fire-and-forget (InvocationType="Event"); any failure (missing ARN,
    transient AWS error) is logged as a warning and swallowed — the
    EventBridge 15-minute refresh covers any missed invoke.
    """
    if not _REFRESH_LAMBDA_ARN:
        logger.warning(
            "refresh_lambda_invoke_skipped",
            extra={"reason": "REFRESH_LAMBDA_ARN not configured"},
        )
        return

    try:
        client = boto3.client("lambda")
        client.invoke(
            FunctionName=_REFRESH_LAMBDA_ARN,
            InvocationType="Event",
            Payload=b"{}",
        )
        logger.info(
            "refresh_lambda_invoked",
            extra={"refresh_lambda_arn": _REFRESH_LAMBDA_ARN},
        )
    except Exception as exc:
        logger.warning(
            "refresh_lambda_invoke_failed",
            extra={
                "refresh_lambda_arn": _REFRESH_LAMBDA_ARN,
                "error": str(exc),
                "note": "Aurora DELETE succeeded; deck_view will refresh on next scheduled run",
            },
        )


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
