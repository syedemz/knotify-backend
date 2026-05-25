"""
cognito_post_confirmation — Lambda handler for Cognito PostConfirmation trigger.

Triggered after a user confirms sign-up (PostConfirmation_ConfirmSignUp).
Inserts a minimal users row using the attributes Cognito supplies.

Design decisions:
  - Always returns the event (Cognito rejects non-event return values and the
    Lambda result has no bearing on whether the user is confirmed).
  - INSERT ... ON CONFLICT (user_id) DO NOTHING ensures idempotency on retries.
  - Email is the only required field; missing email → log + return without insert.
  - Optional fields (first_name, last_name, sex, birthday) are NULL when absent.
    Profile completion (phase 6) fills them in.
  - DB errors on the INSERT path are logged and swallowed — Cognito does NOT
    roll back the signup on trigger failure (the user is already confirmed).
  - DB_SECRET_NAME is the friendly name of the Secrets Manager secret that holds
    the app_user credential. boto3 resolves by name (not ARN wildcard).

Dependencies:
  - knotify_obs layer: init_logger
  - knotify_db layer: get_connection (resolves by friendly name via boto3)
"""

from __future__ import annotations

import datetime
import os

import psycopg2
import knotify_db
from knotify_obs import init_logger

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("cognito_post_confirmation")

# The friendly Secrets Manager secret name for the app_user credential.
# Renamed from DB_SECRET_ARN to DB_SECRET_NAME because boto3 GetSecretValue
# does NOT accept wildcard ARN strings — the name resolves correctly.
# The IAM policy in story 3.4 scopes to `...:secret:knotify-<env>-app-user-credential-*`
# which covers both ARN and name-based lookups.
_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GENDER_MAP: dict[str, str] = {
    "m":      "Male",
    "M":      "Male",
    "male":   "Male",
    "f":      "Female",
    "F":      "Female",
    "female": "Female",
    "Female": "Female",
}

_INSERT_SQL = """
INSERT INTO users (user_id, email, first_name, last_name, sex, birthday)
VALUES (%s, %s, %s, %s, %s, %s)
"""

# Note: ON CONFLICT (user_id) DO NOTHING is NOT used here because Postgres
# applies the SELECT RLS policy to the conflicting row when evaluating
# ON CONFLICT clauses, and the GUCs (app.requesting_user_id,
# app.requesting_user_sex) are not set for PostConfirmation inserts.
# Without GUCs the SELECT policy evaluates to NULL → fail-closed → the
# ON CONFLICT check raises InsufficientPrivilege even though the INSERT
# policy (WITH CHECK true) allows the new-row path.
#
# Idempotency is achieved instead by catching UniqueViolation on the
# primary key and treating it as a successful no-op. This is equivalent
# behavior to ON CONFLICT DO NOTHING for callers (Cognito retries see
# no error) and avoids the RLS/ON CONFLICT interaction.


def _normalize_gender(raw: str | None) -> str | None:
    """
    Map a raw Cognito gender string to the canonical DB value.

    Returns 'Male', 'Female', or None (when the value is absent or unrecognized).
    Unrecognized values are not an error — they yield NULL in the DB and a
    structured warning in the logs.
    """
    if raw is None:
        return None
    normalized = _GENDER_MAP.get(raw)
    if normalized is None and raw:
        logger.warning(
            "unrecognized_gender_value",
            extra={"raw_gender": raw},
        )
    return normalized


def _parse_birthdate(raw: str | None) -> datetime.date | None:
    """
    Parse an ISO-8601 date string (YYYY-MM-DD) into a datetime.date.

    Returns None on parse failure (logged as a warning).
    Birthdate parse failures do not abort the insert — the field is optional.
    """
    if raw is None:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        logger.warning(
            "invalid_birthdate_value",
            extra={"raw_birthdate": raw},
        )
        return None


def _get_conn():
    """
    Return a psycopg2 connection using the module-level secret name.

    Extracted as a separate function so unit tests can patch it at the
    handler module level without monkeypatching knotify_db directly.
    """
    return knotify_db.get_connection(_DB_SECRET_NAME)


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

def handler(event: dict, context: object) -> dict:
    """
    Cognito PostConfirmation Lambda entrypoint.

    Always returns `event` — Cognito requires the trigger return the event
    object regardless of the handler's outcome.

    Args:
        event:   Cognito PostConfirmation event dict.
        context: Lambda context object (unused).

    Returns:
        The input event, unmodified.
    """
    attrs: dict = event.get("request", {}).get("userAttributes", {})

    # user_id is the Cognito sub — canonical across federated and native users.
    user_id: str = attrs.get("sub", "")
    email: str | None = attrs.get("email") or None

    if not email:
        logger.error(
            "missing_email_in_cognito_event",
            extra={
                "user_id": user_id,
                "trigger_source": event.get("triggerSource"),
            },
        )
        return event

    first_name: str | None = attrs.get("given_name") or None
    last_name: str | None = attrs.get("family_name") or None
    sex: str | None = _normalize_gender(attrs.get("gender"))
    birthday: datetime.date | None = _parse_birthdate(attrs.get("birthdate"))

    try:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    _INSERT_SQL,
                    (user_id, email, first_name, last_name, sex, birthday),
                )
            conn.commit()
            logger.info(
                "user_row_inserted",
                extra={"user_id": user_id},
            )
        except psycopg2.errors.UniqueViolation:
            # Row already exists — re-run is a no-op. Rollback the aborted
            # transaction so the connection is left in a clean state.
            conn.rollback()
            logger.info(
                "user_row_already_exists_no_op",
                extra={"user_id": user_id},
            )
        finally:
            conn.close()
    except Exception as exc:
        # Log and swallow — Cognito does NOT roll back the signup on trigger
        # failure. The user is already confirmed. A failed insert here means
        # the profile row is missing; a recovery path can create it later.
        logger.error(
            "db_insert_failed",
            extra={
                "user_id": user_id,
                "error": str(exc),
            },
        )

    return event
