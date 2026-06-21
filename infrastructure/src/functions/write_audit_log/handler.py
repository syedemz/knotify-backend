"""
knotify-write-audit-log Lambda handler — story 9.8

Writes a structured audit record to the account_deletion_audit DynamoDB table
for every account-deletion workflow event:

  event_type="deletion_initiated"
      — written by ValidateDeletionRequest (story 9.2) at workflow start
  event_type="deletion_completed"
      — written at the end of the soft-delete or purge_immediately branch
  event_type="deletion_failed"
      — written from the state machine's global Catch (story 9.1)

Table schema:
  PK  user_id    (S)
  SK  event_id   (S) — ULID generated at write time for uniqueness
  TTL expire_at  (N) — Unix epoch seconds, 7 years from now

DDB-only Lambda; runs OUTSIDE the VPC (no Aurora, no AppSync).
AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.

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

_VALID_EVENT_TYPES: frozenset[str] = frozenset(
    {"deletion_initiated", "deletion_completed", "deletion_failed"}
)

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

    Input fields (all required unless noted):
      event_type     — "deletion_initiated" | "deletion_completed" | "deletion_failed"
      user_id        — Cognito sub of the account being deleted
      execution_arn  — Step Functions execution ARN

    Additional fields for deletion_completed:
      purge_immediately  — bool; True → dynamodb_retention="hard_deleted"
      branches_succeeded — list[str] of succeeded parallel branch names
      completed_at       — ISO-8601 completion timestamp

    Additional fields for deletion_failed:
      failed_state_name — name of the state that caused the failure
      error             — Step Functions error code
      cause             — human-readable error cause string

    Raises:
      ValueError — if event_type is not one of the three recognised values.
                   PutItem is NOT called in this case.
    """
    event_type: str = event.get("event_type", "")
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(
            f"Unrecognised event_type={event_type!r}. "
            f"Must be one of {sorted(_VALID_EVENT_TYPES)}"
        )

    user_id: str = event["user_id"]
    execution_arn: str = event.get("execution_arn", "")
    event_id: str = str(uuid.uuid4())
    expire_at: int = int(time.time()) + _SEVEN_YEARS_SECS

    item: dict = {
        "user_id": {"S": user_id},
        "event_id": {"S": event_id},
        "event_type": {"S": event_type},
        "execution_arn": {"S": execution_arn},
        "expire_at": {"N": str(expire_at)},
    }

    if event_type == "deletion_initiated":
        _populate_initiated(item, event)
    elif event_type == "deletion_completed":
        _populate_completed(item, event)
    else:  # deletion_failed
        _populate_failed(item, event)

    _get_dynamo().put_item(TableName=TABLE_AUDIT, Item=item)

    logger.info(
        "audit_log_written",
        extra={
            "event_type": event_type,
            "user_id": user_id,
            "event_id": event_id,
        },
    )

    return {"event_id": event_id, "event_type": event_type}


# ---------------------------------------------------------------------------
# Per-event-type item populators
# ---------------------------------------------------------------------------


def _populate_initiated(item: dict, event: dict) -> None:
    """Add deletion_initiated-specific fields. Currently none beyond the base."""
    # created_at is implied by expire_at context; no extra fields for initiated.
    pass


def _populate_completed(item: dict, event: dict) -> None:
    """Add deletion_completed-specific fields to the item dict."""
    purge_immediately: bool = bool(event.get("purge_immediately", False))
    dynamodb_retention: str = "hard_deleted" if purge_immediately else "permanent_anonymized"

    branches_succeeded: list[str] = event.get("branches_succeeded", [])
    completed_at: str = event.get("completed_at", "")

    item["dynamodb_retention"] = {"S": dynamodb_retention}
    item["completed_at"] = {"S": completed_at}
    item["branches_succeeded"] = {
        "L": [{"S": branch} for branch in branches_succeeded]
    }


def _populate_failed(item: dict, event: dict) -> None:
    """Add deletion_failed-specific fields to the item dict."""
    item["failed_state_name"] = {"S": event.get("failed_state_name", "")}
    item["error"] = {"S": event.get("error", "")}
    item["cause"] = {"S": event.get("cause", "")}
