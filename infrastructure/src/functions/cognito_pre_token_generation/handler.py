"""
cognito_pre_token_generation — Lambda handler for Cognito PreTokenGeneration V2 trigger.

Triggered on every token issuance (sign-in and refresh). Reads the
profile_complete_verified flag from the users table and embeds it as a custom
claim on both the ID token and the access token.

Design decisions (from brainstorm resolutions):
  - B1: claim embedded on BOTH idTokenGeneration and accessTokenGeneration so
    all API surfaces (REST, AppSync) can gate on profile completion without
    decoding the ID token.
  - B2: V2 trigger shape (pre_token_generation_config lambda_version = "V2_0")
    used throughout. The V1 pre_token_generation field is explicitly prohibited.
    V2 requires AUDIT or ENFORCED Advanced Security Mode on the User Pool.
  - M1: DB_SECRET_NAME env var holds the friendly secret name
    `knotify-${var.environment}-app-user-credential`. boto3 resolves by name.
  - Md1: one DB hit per token issue. Cold-start 200ms–1s ENI penalty is
    acceptable for pre-launch volumes; provisioned concurrency deferred to
    phase 11 if observed.
  - Md2: if the users row is missing, return custom:profile_complete="false" on
    both tokens and log a structured warning. Do NOT raise — a raised
    PreTokenGeneration handler blocks login entirely.

Supported trigger sources (V2):
  - TokenGeneration_Authentication   (sign-in)
  - TokenGeneration_RefreshTokens    (refresh)

Both share the same V2 event shape and require the same response structure.

Dependencies:
  - knotify_obs layer: init_logger
  - knotify_db layer: get_connection (resolves by friendly name via boto3)
"""

from __future__ import annotations

import os

import knotify_db
from knotify_obs import init_logger

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("cognito_pre_token_generation")

# The friendly Secrets Manager secret name for the app_user credential.
# boto3 GetSecretValue resolves by name; ARN wildcard strings are not accepted.
# The IAM policy in story 3.4 scopes GetSecretValue to the
# knotify-<env>-app-user-credential-* pattern, which covers this name.
_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# SQL query — single indexed lookup by primary key (user_id).
_SELECT_SQL = (
    "SELECT profile_complete_verified FROM users WHERE user_id = %s"
)

# Claim name written to both ID token and access token (B1 resolution).
_CLAIM_NAME = "custom:profile_complete"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_conn():
    """
    Return a psycopg2 connection using the module-level secret name.

    Extracted so integration tests can patch it at the handler module level
    without monkeypatching knotify_db directly.
    """
    return knotify_db.get_connection(_DB_SECRET_NAME)


def _query_profile_complete(conn, user_id: str) -> bool:
    """
    Query profile_complete_verified for the given user_id.

    Returns False when the row is absent (Md2 resolution) and logs a
    structured warning — callers must not raise on the missing-row path.

    Args:
        conn:    An open psycopg2 connection.
        user_id: Cognito sub UUID string.

    Returns:
        True if profile_complete_verified is True in the DB; False otherwise
        (including when the row is missing).
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, (user_id,))
        row = cur.fetchone()

    if row is None:
        logger.warning(
            "users_row_missing_for_pre_token_generation",
            extra={"user_id": user_id},
        )
        return False

    return bool(row[0])


def _build_claims_override(profile_complete: bool) -> dict:
    """
    Build the V2 claimsAndScopeOverrideDetails response dict.

    Sets custom:profile_complete on BOTH idTokenGeneration and
    accessTokenGeneration (B1 resolution). The claim value is a string
    "true" or "false" — Cognito custom claims are always strings.
    """
    claim_value = "true" if profile_complete else "false"
    return {
        "claimsAndScopeOverrideDetails": {
            "idTokenGeneration": {
                "claimsToAddOrOverride": {
                    _CLAIM_NAME: claim_value,
                },
            },
            "accessTokenGeneration": {
                "claimsToAddOrOverride": {
                    _CLAIM_NAME: claim_value,
                },
            },
        }
    }


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

def handler(event: dict, context: object) -> dict:
    """
    Cognito PreTokenGeneration V2 Lambda entrypoint.

    Reads profile_complete_verified from Aurora and embeds it as
    custom:profile_complete on both the ID token and the access token.

    Always returns the mutated event — Cognito requires the trigger to return
    the event object (possibly with a modified response block). Never raises:
    an unhandled exception in PreTokenGeneration blocks the user's login.

    Args:
        event:   Cognito V2 PreTokenGeneration event dict.
        context: Lambda context object (unused).

    Returns:
        The input event mutated with response.claimsAndScopeOverrideDetails set.
    """
    user_id: str = event.get("request", {}).get("userAttributes", {}).get("sub", "")

    profile_complete = False  # safe default — fail-closed on any error

    conn = None
    try:
        conn = _get_conn()
        try:
            profile_complete = _query_profile_complete(conn, user_id)
        finally:
            conn.close()
    except Exception as exc:
        logger.error(
            "pre_token_generation_db_error",
            extra={
                "user_id": user_id,
                "error": str(exc),
                "trigger_source": event.get("triggerSource"),
            },
        )

    event["response"].update(_build_claims_override(profile_complete))
    return event
