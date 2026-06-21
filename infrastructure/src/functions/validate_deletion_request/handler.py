"""
knotify-validate-deletion-request Lambda handler — story 9.2

Validates an account-deletion request before the Step Functions state machine
proceeds with the irreversible cleanup steps.

Input contract (set by the deletion_initiator Lambda, story 9.9, which calls
StartExecution with this payload):

  {
    "user_id":          str,   # Cognito sub of the account to be deleted
    "jwt_sub":          str,   # JWT sub extracted by the initiator Lambda
    "purge_immediately": bool, # forwarded unchanged to downstream tasks
  }

The state machine wires this task with ResultPath: '$.validation', so the
original input fields (user_id, purge_immediately) survive unchanged and
remain addressable by the downstream Choice and all subsequent tasks.
This Lambda's return value is written only to $.validation — it does NOT
need to echo input fields back.

Output contract:

  {
    "validated":      True,
    "audit_event_id": str,   # uuid4 of the "deletion_initiated" row written
  }

Exception contract (Step Functions Catch matches by Error = class name):

  UserIdMismatch     — user_id != jwt_sub (defense-in-depth check; the
                       initiator Lambda should have enforced this already)
  DeletionInProgress — a prior deletion for this user_id is still in progress
                       (no corresponding deletion_completed / deletion_failed
                       row found)

Idempotency strategy:
  Query the account_deletion_audit table for all rows with PK=user_id.
  If there is at least one "deletion_initiated" row AND no "deletion_completed"
  or "deletion_failed" row, the deletion is still in progress.  The in-memory
  classification avoids a second Query and is correct because all three event
  types share the same PK (user_id).

Placement: OUTSIDE the VPC.
WHY: only touches DynamoDB (account_deletion_audit) via the regional public
endpoint.  No Aurora, no AppSync.  Consistent with write_audit_log,
deactivate_chat_rooms, anonymize_chat_messages, and delete_dynamodb_personal_data.
AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.

Direct DDB write (NOT invoking write_audit_log Lambda):
  This Lambda writes the "deletion_initiated" row itself via PutItem.
  The write_audit_log Lambda (story 9.8) is invoked by Step Functions as a
  separate task at the end of the workflow (deletion_completed / deletion_failed).
  Keeping the dependency one-way (Step Functions → both Lambdas independently)
  avoids a Lambda-to-Lambda coupling and simplifies IAM scoping.

Environment variables:
  TABLE_AUDIT — DynamoDB table name (default: account_deletion_audit)
  LOG_LEVEL   — logging level (default: INFO)
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_AUDIT: str = os.environ.get("TABLE_AUDIT", "account_deletion_audit")

_SEVEN_YEARS_SECS: int = 7 * 365 * 86400


# ---------------------------------------------------------------------------
# Typed exceptions — class name is the Step Functions Error string
# ---------------------------------------------------------------------------


class UserIdMismatch(Exception):
    """
    Raised when the input user_id does not match the JWT sub.
    Step Functions Catch uses Error="UserIdMismatch" to route this to the
    global failure handler (which writes a deletion_failed audit row).
    """


class DeletionInProgress(Exception):
    """
    Raised when the audit table shows an in-progress deletion for user_id
    (i.e., a "deletion_initiated" row exists with no corresponding
    "deletion_completed" or "deletion_failed" row).
    Step Functions Catch uses Error="DeletionInProgress" to surface this
    to the caller as a known, recoverable error state rather than a bug.
    """


# ---------------------------------------------------------------------------
# Module-level singleton (cold-start optimisation)
# ---------------------------------------------------------------------------

_dynamo = None


def _get_dynamo():
    """Return a (possibly cached) boto3 DynamoDB client."""
    global _dynamo
    if _dynamo is None:
        import boto3  # deferred — unavailable in unit-test environments

        _dynamo = boto3.client("dynamodb")
    return _dynamo


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """
    Lambda entry point.

    Input fields:
      user_id          — Cognito sub of the account to be deleted
      jwt_sub          — JWT sub extracted by the initiator Lambda (story 9.9)
      purge_immediately — bool forwarded unchanged from StartExecution input

    Returns:
      {"validated": True, "audit_event_id": <uuid4 str>}

    Raises:
      UserIdMismatch     — user_id != jwt_sub
      DeletionInProgress — a prior deletion is still in progress for user_id
    """
    user_id: str = event["user_id"]
    jwt_sub: str = event["jwt_sub"]

    _assert_user_id_matches_jwt_sub(user_id, jwt_sub)
    _assert_no_deletion_in_progress(user_id)

    audit_event_id = _write_initiated_row(user_id)

    logger.info(
        "validate_deletion_request_success",
        extra={"user_id": user_id, "audit_event_id": audit_event_id},
    )

    return {"validated": True, "audit_event_id": audit_event_id}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_user_id_matches_jwt_sub(user_id: str, jwt_sub: str) -> None:
    """
    Defense-in-depth check: the initiator Lambda (9.9) should have already
    enforced that the authenticated caller is deleting their own account.
    If for any reason the IDs diverge here, raise UserIdMismatch so the
    state machine can route the failure to the global Catch.
    """
    if user_id != jwt_sub:
        logger.warning(
            "validate_deletion_request_user_id_mismatch",
            extra={"user_id": user_id, "jwt_sub": jwt_sub},
        )
        raise UserIdMismatch(
            f"user_id={user_id!r} does not match jwt_sub={jwt_sub!r}"
        )


def _assert_no_deletion_in_progress(user_id: str) -> None:
    """
    Idempotency guard: query all audit rows for user_id, classify
    in-memory, and raise DeletionInProgress if any initiated row exists
    with no corresponding terminal event.

    All three event types (deletion_initiated / deletion_completed /
    deletion_failed) share PK=user_id, so a single Query fetching all
    rows for the PK is sufficient — no per-event-type GSI needed.
    """
    items = _query_all_audit_rows(user_id)

    event_types: set[str] = {
        item["event_type"]["S"] for item in items if "event_type" in item
    }

    has_initiated = "deletion_initiated" in event_types
    has_terminal = bool(
        event_types & {"deletion_completed", "deletion_failed"}
    )

    if has_initiated and not has_terminal:
        logger.warning(
            "validate_deletion_request_deletion_in_progress",
            extra={"user_id": user_id},
        )
        raise DeletionInProgress(
            f"A deletion is already in progress for user_id={user_id!r}"
        )


def _query_all_audit_rows(user_id: str) -> list[dict]:
    """
    Fetch all rows from the audit table for user_id, paginating through
    all result pages via LastEvaluatedKey until exhausted.
    Returns the raw DynamoDB-typed item dicts.
    """
    client = _get_dynamo()
    items: list[dict] = []
    exclusive_start_key: dict | None = None

    while True:
        kwargs: dict = {
            "TableName": TABLE_AUDIT,
            "KeyConditionExpression": "user_id = :uid",
            "ExpressionAttributeValues": {":uid": {"S": user_id}},
        }
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = client.query(**kwargs)
        items.extend(response.get("Items", []))

        exclusive_start_key = response.get("LastEvaluatedKey")
        if exclusive_start_key is None:
            break

    return items


def _write_initiated_row(user_id: str) -> str:
    """
    Write a 'deletion_initiated' audit record to account_deletion_audit.
    Returns the event_id (uuid4) of the newly written row.

    Does NOT invoke the write_audit_log Lambda — this Lambda writes the row
    directly to keep the dependency one-way (story cross-story note).
    """
    event_id: str = str(uuid.uuid4())
    expire_at: int = int(time.time()) + _SEVEN_YEARS_SECS

    item: dict = {
        "user_id": {"S": user_id},
        "event_id": {"S": event_id},
        "event_type": {"S": "deletion_initiated"},
        "expire_at": {"N": str(expire_at)},
    }

    _get_dynamo().put_item(TableName=TABLE_AUDIT, Item=item)

    logger.info(
        "validate_deletion_request_initiated_row_written",
        extra={"user_id": user_id, "event_id": event_id},
    )

    return event_id
