"""
knotify-delete-dynamodb-personal-data Lambda handler — story 9.7

Deletes all personal DynamoDB data for a user whose account is being deleted.
Called from the account-deletion Step Functions state machine inside the
ParallelCleanup block.

Two tables are cleaned:
  1. Notifications    — PK: user_id (S), SK: created_at_notification_id (S)
  2. PushNotificationTokens — PK: user_id (S), SK: device_id (S)

For each table the Lambda:
  1. Queries with KeyConditionExpression user_id = :uid, paginating through
     all result pages via LastEvaluatedKey until exhausted.
  2. Collects all matching items across pages.
  3. BatchWriteItem-deletes them in chunks of at most 25 (DynamoDB hard limit).

Idempotent: if both Query pages are empty (user never had rows, or rows already
deleted), the Lambda succeeds with zero rows deleted.

Placement: OUTSIDE the VPC.
WHY: DynamoDB is reachable via the regional public endpoint — no VPC endpoint or
NAT required.  Running outside the VPC avoids the ENI cold-start penalty and
eliminates the hotfix #106 blackhole trap.  Consistent with deactivate_chat_rooms,
anonymize_chat_messages, write_audit_log, and push_fanout.
AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.

Return value:
  {notifications_deleted: <int>, push_tokens_deleted: <int>}

Environment variables:
  TABLE_NOTIFICATIONS             — DynamoDB table name (default: Notifications)
  TABLE_PUSH_NOTIFICATION_TOKENS  — DynamoDB table name (default: PushNotificationTokens)
  LOG_LEVEL                       — logging level (default: INFO)
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NOTIFICATIONS: str = os.environ.get("TABLE_NOTIFICATIONS", "Notifications")
TABLE_PUSH_NOTIFICATION_TOKENS: str = os.environ.get(
    "TABLE_PUSH_NOTIFICATION_TOKENS", "PushNotificationTokens"
)

# DynamoDB BatchWriteItem hard limit
_BATCH_WRITE_MAX: int = 25

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
      user_id — Cognito sub of the user whose account is being deleted

    Returns:
      {"notifications_deleted": <int>, "push_tokens_deleted": <int>}
    """
    user_id: str = event["user_id"]

    logger.info("delete_dynamodb_personal_data_start", extra={"user_id": user_id})

    notifications_deleted = _delete_all_rows(
        table_name=TABLE_NOTIFICATIONS,
        user_id=user_id,
        sort_key_attr="created_at_notification_id",
    )

    push_tokens_deleted = _delete_all_rows(
        table_name=TABLE_PUSH_NOTIFICATION_TOKENS,
        user_id=user_id,
        sort_key_attr="device_id",
    )

    logger.info(
        "delete_dynamodb_personal_data_complete",
        extra={
            "user_id": user_id,
            "notifications_deleted": notifications_deleted,
            "push_tokens_deleted": push_tokens_deleted,
        },
    )

    return {
        "notifications_deleted": notifications_deleted,
        "push_tokens_deleted": push_tokens_deleted,
    }


# ---------------------------------------------------------------------------
# Core deletion logic
# ---------------------------------------------------------------------------


def _delete_all_rows(table_name: str, user_id: str, sort_key_attr: str) -> int:
    """
    Query all rows for user_id in the given table, then BatchWriteItem-delete
    them in chunks of at most 25.

    Paginates through all Query pages via LastEvaluatedKey before issuing any
    deletes.  This keeps the implementation simple and avoids interleaving
    Query and BatchWriteItem calls.

    Returns the total number of rows deleted.

    The sort_key_attr parameter names the SK attribute so the correct key
    structure is built for the DeleteRequest.  For Notifications this is
    'created_at_notification_id'; for PushNotificationTokens this is 'device_id'.
    """
    items = _query_all_items(table_name, user_id)

    if not items:
        logger.info(
            "delete_dynamodb_personal_data_table_empty",
            extra={"table": table_name, "user_id": user_id},
        )
        return 0

    _batch_delete(table_name, user_id, items, sort_key_attr)

    logger.info(
        "delete_dynamodb_personal_data_table_done",
        extra={"table": table_name, "user_id": user_id, "deleted": len(items)},
    )
    return len(items)


def _query_all_items(table_name: str, user_id: str) -> list[dict]:
    """
    Paginate through all rows in table_name where user_id = :uid.
    Returns the raw DynamoDB-typed item dicts (e.g. {"user_id": {"S": ...}, ...}).
    """
    client = _get_dynamo()
    items: list[dict] = []
    exclusive_start_key: dict | None = None

    while True:
        kwargs: dict = {
            "TableName": table_name,
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


def _batch_delete(
    table_name: str, user_id: str, items: list[dict], sort_key_attr: str
) -> None:
    """
    Issue BatchWriteItem Delete requests for all items, split into chunks of ≤25.
    Already-deleted rows are tolerated (DynamoDB ignores deletes for missing keys).
    """
    client = _get_dynamo()

    for i in range(0, len(items), _BATCH_WRITE_MAX):
        chunk = items[i : i + _BATCH_WRITE_MAX]
        delete_requests = [
            {
                "DeleteRequest": {
                    "Key": {
                        "user_id": {"S": user_id},
                        sort_key_attr: item[sort_key_attr],
                    }
                }
            }
            for item in chunk
        ]
        client.batch_write_item(RequestItems={table_name: delete_requests})
