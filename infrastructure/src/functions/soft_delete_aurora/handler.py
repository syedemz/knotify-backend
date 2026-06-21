"""
knotify-soft-delete-aurora Lambda handler — story 9.5

Runs the §11.1 step-2 soft-delete UPDATE against Aurora:

    UPDATE users
    SET deleted_at = NOW(),
        email = NULL,
        phone_number = NULL,
        photo_url = NULL,
        chosen_profile_avatar = NULL,
        preferences = '{}',
        preference_vector = NULL,
        username = '[deleted-user]',
        first_name = 'Deleted',
        last_name = 'User'
    WHERE user_id = %s::uuid
      AND deleted_at IS NULL

Called from the account-deletion Step Functions state machine as the
SoftDeleteAurora task (parallel cleanup branch).

Idempotency: the WHERE guard `deleted_at IS NULL` means a second invocation
on an already-soft-deleted user matches zero rows and returns rows_affected=0
without error.  Step Functions treats any non-exception return as success, so
the task is safe to retry.

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

# §11.1 step-2 soft-delete SQL.
# Sets deleted_at and nulls all PII columns in a single conditional UPDATE.
# The WHERE guard on deleted_at IS NULL makes the operation idempotent:
# a re-invocation on an already-deleted row returns rowcount=0 (no-op).
_SOFT_DELETE_SQL = """
UPDATE users
SET deleted_at             = NOW(),
    email                  = NULL,
    phone_number           = NULL,
    photo_url              = NULL,
    chosen_profile_avatar  = NULL,
    preferences            = '{}'::jsonb,
    preference_vector      = NULL,
    username               = '[deleted-user]',
    first_name             = 'Deleted',
    last_name              = 'User'
WHERE user_id = %s::uuid
  AND deleted_at IS NULL
"""

# Module-level connection cache — reused across warm invocations.
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
        user_id — UUID string of the user to soft-delete (required).

    Returns:
        {"user_id": <user_id>, "rows_affected": <0|1>}

        rows_affected=1 — row found and soft-deleted.
        rows_affected=0 — row was already soft-deleted (idempotent no-op).

    Raises:
        KeyError           — user_id missing from event.
        psycopg2 errors    — DB connection / execution failures propagate so
                             Step Functions can apply retry / Catch logic.
    """
    user_id: str = event["user_id"]

    conn = _get_conn()
    rows_affected: int

    # Migration 0007 forces RLS on the users table; the `users_opposite_sex_only`
    # policy requires `app.requesting_user_id` (or `app.requesting_user_sex`)
    # to be set, otherwise an UPDATE silently matches zero rows. Use the
    # `user_id = current_setting('app.requesting_user_id')::uuid` branch by
    # setting the GUC to the user being soft-deleted (i.e., the user is
    # "requesting their own deletion"). user_sex is unused for this row, but
    # must be a non-NULL string; pass "" since the row's sex column is the
    # SAME-sex case and the != comparison is irrelevant once user_id matches.
    conn.autocommit = False
    try:
        with knotify_db.rls_context(conn, user_id, ""):
            with conn.cursor() as cur:
                cur.execute(_SOFT_DELETE_SQL, (user_id,))
                rows_affected = cur.rowcount
    except Exception:
        # rls_context already rolled back on exception; nothing extra to do.
        raise

    logger.info(
        "soft_delete_aurora_complete",
        extra={"user_id": user_id, "rows_affected": rows_affected},
    )

    return {"user_id": user_id, "rows_affected": rows_affected}
