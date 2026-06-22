"""
knotify-soft-delete-aurora Lambda handler — story 9.5

Runs the §11.1 step-2 soft-delete UPDATE against Aurora:

    UPDATE users
    SET deleted_at = NOW(),
        email = 'deleted-' || user_id::text || '@deleted.knotify.local',
        phone_number = NULL,
        photo_url = NULL,
        chosen_profile_avatar = NULL,
        preferences = '{}',
        preference_vector = NULL,
        username = '[deleted-' || user_id::text || ']',
        first_name = 'Deleted',
        last_name = 'User'
    WHERE user_id = %s::uuid
      AND deleted_at IS NULL

Schema-driven sentinels (hotfix #4):
  - email column has NOT NULL + UNIQUE + a CHECK email_format regex constraint
    (migration 0002). Wiping to NULL fails the NOT NULL guard, so the redaction
    writes a per-user, format-valid, collision-free sentinel of the form
    'deleted-<user_id>@deleted.knotify.local'. The user_id is the row's own
    primary key so two soft-deletes never collide on the UNIQUE constraint.
  - username has a partial UNIQUE index on lower(username) WHERE username IS
    NOT NULL (migration 0010). Setting every deleted user to the same literal
    '[deleted-user]' would collide on the second deletion, so the redaction
    embeds the user_id: '[deleted-<user_id>]'. Same uniqueness guarantee.

The user_id is opaque (Cognito-issued UUID), so embedding it in the sentinels
does not leak PII; it is the same identifier already stored in the row's
primary key column.

Called from the account-deletion Step Functions state machine as the
SoftDeleteAurora task (parallel cleanup branch).

Idempotency: the WHERE guard `deleted_at IS NULL` means a second invocation
on an already-soft-deleted user matches zero rows and returns rows_affected=0
without error.  Step Functions treats any non-exception return as success, so
the task is safe to retry.

Post-commit deck_view refresh (hotfix #6):
  After a successful soft-delete (rows_affected > 0), this Lambda async-invokes
  the refresh_deck_view Lambda. deck_view is a materialised view filtered by
  `deleted_at IS NULL`; without a refresh the soft-deleted user remains visible
  in /v1/match/deck until the 15-minute scheduled refresh runs. The invoke
  mirrors the profile handler's _invoke_refresh_lambda pattern (story 7.4):
  fire-and-forget (InvocationType="Event"), best-effort (any error is logged
  and swallowed), and skipped when REFRESH_LAMBDA_ARN is unset. IAM is already
  in place — soft_delete_aurora runs as aurora_writer which holds
  lambda:InvokeFunction scoped to the refresh Lambda ARN (story 7.4).

Environment variables:
    DB_SECRET_NAME     — Secrets Manager secret name for the Aurora app-user
                         credential (knotify-<env>-app-user-credential).
    AURORA_HOST        — Aurora cluster writer endpoint.
    AURORA_PORT        — Aurora port (default 5432).
    AURORA_DBNAME      — Aurora database name.
    REFRESH_LAMBDA_ARN — ARN of the refresh_deck_view Lambda (hotfix #6).
                         Empty string disables the post-commit refresh.
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

# §11.1 step-2 soft-delete SQL.
# Sets deleted_at and nulls all PII columns in a single conditional UPDATE.
# The WHERE guard on deleted_at IS NULL makes the operation idempotent:
# a re-invocation on an already-deleted row returns rowcount=0 (no-op).
_SOFT_DELETE_SQL = """
UPDATE users
SET deleted_at             = NOW(),
    email                  = 'deleted-' || user_id::text || '@deleted.knotify.local',
    phone_number           = NULL,
    photo_url              = NULL,
    chosen_profile_avatar  = NULL,
    preferences            = '{}'::jsonb,
    preference_vector      = NULL,
    username               = '[deleted-' || user_id::text || ']',
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

    # Post-commit deck_view refresh (hotfix #6). Only invoked when the
    # soft-delete actually changed a row — a no-op re-invocation does not
    # need a refresh. Best-effort, never raises.
    if rows_affected > 0:
        _invoke_refresh_lambda()

    return {"user_id": user_id, "rows_affected": rows_affected}


def _invoke_refresh_lambda() -> None:
    """
    Best-effort async invocation of the refresh_deck_view Lambda.

    deck_view is a materialised view filtered by `deleted_at IS NULL`. After
    a soft-delete commits the row carries a non-null deleted_at, but the MV
    still has the pre-delete snapshot until a refresh runs. Without this
    invoke the deleted user remains visible in /v1/match/deck for up to 15
    minutes (the scheduled refresh cadence) — long enough to fail E2E and to
    surface a stale candidate in prod.

    Mirrors the profile handler's pattern: fire-and-forget (InvocationType=
    "Event"), best-effort. Any failure (missing ARN, transient AWS error) is
    logged as a warning and swallowed — the EventBridge 15-minute refresh
    covers any missed invoke. IAM is already in place: this Lambda's role
    (aurora_writer) carries lambda:InvokeFunction scoped to the refresh
    Lambda ARN (story 7.4).
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
                "note": "Aurora commit succeeded; deck_view will refresh on next scheduled run",
            },
        )
