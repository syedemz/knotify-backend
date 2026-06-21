"""
knotify-cognito-user-state Lambda handler — story 9.3

Dispatches to Cognito IDP AdminDisableUser or AdminDeleteUser depending on
the input `mode` field.  Called twice by the account-deletion Step Functions
state machine:

  DisableCognitoUser  — mode="disable"  (early in workflow, prevents new sign-ins)
  DeleteCognitoUser   — mode="delete"   (late in workflow, removes Cognito record)

Idempotency contract:
  mode="disable": UserNotFoundException and the "already disabled"
    NotAuthorizedException are treated as success.  The user is either already
    gone or already disabled — both are the desired end state.
  mode="delete":  UserNotFoundException is treated as no-op success.  The user
    record is already absent, which is the desired end state.

Environment variables:
  USER_POOL_ID — Cognito User Pool ID (e.g. eu-central-1_XXXXXXXXX).
  LOG_LEVEL    — logging level (default: INFO).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

USER_POOL_ID: str = os.environ.get("USER_POOL_ID", "")

_VALID_MODES: frozenset[str] = frozenset({"disable", "delete"})

# ---------------------------------------------------------------------------
# Module-level singleton (cold-start optimisation)
# ---------------------------------------------------------------------------

_cognito_client = None


def _get_cognito_client():
    """Return a (possibly cached) boto3 Cognito IDP client."""
    global _cognito_client
    if _cognito_client is None:
        import boto3  # deferred — unavailable in unit-test environments

        _cognito_client = boto3.client("cognito-idp")
    return _cognito_client


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point.

    Input fields (both required):
      mode     — "disable" | "delete"
      user_id  — Cognito sub of the user to act on

    Returns:
      {"user_id": <user_id>, "mode": <mode>}

    Raises:
      ValueError — if mode is not "disable" or "delete".  No Cognito API call
                   is made in this case.
    """
    mode: str = event.get("mode", "")
    if mode not in _VALID_MODES:
        raise ValueError(
            f"invalid_mode={mode!r}. Must be one of {sorted(_VALID_MODES)}"
        )

    user_id: str = event["user_id"]

    if mode == "disable":
        _disable_user(user_id)
    else:
        _delete_user(user_id)

    logger.info(
        "cognito_user_state_applied",
        extra={"mode": mode, "user_id": user_id},
    )

    return {"user_id": user_id, "mode": mode}


# ---------------------------------------------------------------------------
# Internal dispatch helpers
# ---------------------------------------------------------------------------


def _disable_user(user_id: str) -> None:
    """
    Call AdminDisableUser.  Idempotent:
      - UserNotFoundException  → already gone; treat as success.
      - NotAuthorizedException with "already disabled" → treat as success.
      - Any other exception    → re-raise.
    """
    from botocore.exceptions import ClientError

    client = _get_cognito_client()
    try:
        client.admin_disable_user(UserPoolId=USER_POOL_ID, Username=user_id)
    except ClientError as exc:
        code: str = exc.response["Error"]["Code"]
        message: str = exc.response["Error"].get("Message", "")

        if code == "UserNotFoundException":
            logger.info(
                "cognito_disable_user_not_found_treated_as_success",
                extra={"user_id": user_id},
            )
            return

        if code == "NotAuthorizedException" and "already disabled" in message.lower():
            logger.info(
                "cognito_disable_user_already_disabled_treated_as_success",
                extra={"user_id": user_id},
            )
            return

        raise


def _delete_user(user_id: str) -> None:
    """
    Call AdminDeleteUser.  Idempotent:
      - UserNotFoundException → already gone; treat as no-op success.
      - Any other exception   → re-raise.
    """
    from botocore.exceptions import ClientError

    client = _get_cognito_client()
    try:
        client.admin_delete_user(UserPoolId=USER_POOL_ID, Username=user_id)
    except ClientError as exc:
        code: str = exc.response["Error"]["Code"]

        if code == "UserNotFoundException":
            logger.info(
                "cognito_delete_user_not_found_treated_as_success",
                extra={"user_id": user_id},
            )
            return

        raise
