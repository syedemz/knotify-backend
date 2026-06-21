"""
knotify-hard-delete-user-chat-messages Lambda handler — story 9.12

Hard-deletes all ChatMessages rows sent by the deleted user across every room
they were a member of.  Called from the purge_immediately branch of the
account-deletion Step Functions state machine inside PurgeImmediately_ParallelCleanup,
after DeactivateChatRooms has already deleted the user's ChatRoomMembership rows.

This Lambda replaces AnonymizeChatMessages in the purge_immediately branch.
The soft-delete branch continues to use AnonymizeChatMessages (story 9.6) which
rewrites sender_id to '[deleted-user]'.  This Lambda deletes the rows outright.

The Lambda does NOT query ChatRoomMembership.  The room_ids list is injected
by the state machine Parameters block from the DeactivateChatRooms output.

For each room_id, the Lambda:
  1. Queries ChatMessages (PK=room_id) with a FilterExpression on sender_id
     matching the deleted user's user_id.
  2. Calls DeleteItem on each matching row — the item is removed from the table.
  3. Paginates within each room using ExclusiveStartKey when LastEvaluatedKey
     is returned by Query.

No new GSI is required — Query uses the table's native PK (room_id).  Cost is
linear in the number of messages the user sent in the rooms they were in.

The Lambda is idempotent: if all messages have already been deleted (e.g., by a
concurrent invocation), Query returns empty and DeleteItem is never called.

Continuation token contract:
  Input fields:
    user_id             — Cognito sub of the user whose messages are being deleted
    room_ids            — list of room_ids the user was a member of (from 9.4 output)
    current_room_index  — (int, default 0) index into room_ids of the current room
    last_evaluated_key  — (optional dict) DynamoDB pagination key for the current room

  Return fields:
    has_more            — True if there is more work; Step Functions re-invokes
    room_ids            — pass-through (for the state machine loop)
    current_room_index  — next room index to process on re-invocation
    last_evaluated_key  — pagination key for the resumed room (None if advancing)

Step Functions wires a Choice → Task → Choice loop:
  - Choice inspects has_more; True → loop back to Task with returned payload
  - False → exits the loop

Placement: OUTSIDE the VPC.
WHY: only touches DynamoDB (ChatMessages table) — no Aurora, no AppSync.
Running outside the VPC avoids the ENI cold-start penalty and the hotfix #106
blackhole trap (private subnets without NAT cannot reach DynamoDB service
endpoints).  Consistent with anonymize_chat_messages (story 9.6), write_audit_log,
deactivate_chat_rooms, and push_fanout.  AWSLambdaBasicExecutionRole is sufficient.

Environment variables:
  TABLE_CHAT_MESSAGES — DynamoDB table name (default: ChatMessages)
  LOG_LEVEL           — logging level (default: INFO)
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_CHAT_MESSAGES: str = os.environ.get("TABLE_CHAT_MESSAGES", "ChatMessages")

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

    Input fields (see module docstring for full contract):
      user_id             — Cognito sub of the deleted user
      room_ids            — list of room_ids to process
      current_room_index  — (int, default 0) resume point
      last_evaluated_key  — (optional) DynamoDB pagination key

    Returns:
      {
        "has_more": bool,
        "room_ids": [...],
        "current_room_index": int,
        "last_evaluated_key": dict | None,
      }
    """
    user_id: str = event["user_id"]
    room_ids: list[str] = event.get("room_ids", [])
    current_room_index: int = int(event.get("current_room_index", 0))
    last_evaluated_key: dict | None = event.get("last_evaluated_key")

    if not room_ids:
        logger.info("hard_delete_chat_messages_noop", extra={"user_id": user_id, "reason": "empty_room_ids"})
        return {
            "has_more": False,
            "room_ids": room_ids,
            "current_room_index": 0,
            "last_evaluated_key": None,
        }

    logger.info(
        "hard_delete_chat_messages_start",
        extra={
            "user_id": user_id,
            "room_count": len(room_ids),
            "current_room_index": current_room_index,
            "resuming": last_evaluated_key is not None,
        },
    )

    idx = current_room_index
    lek = last_evaluated_key

    while idx < len(room_ids):
        room_id = room_ids[idx]
        lek = _delete_room_messages(user_id, room_id, lek)
        # lek is None once the room is exhausted — advance to next room
        if lek is None:
            idx += 1

    logger.info(
        "hard_delete_chat_messages_complete",
        extra={"user_id": user_id, "current_room_index": idx},
    )

    return {
        "has_more": False,
        "room_ids": room_ids,
        "current_room_index": idx,
        "last_evaluated_key": None,
    }


# ---------------------------------------------------------------------------
# Per-room hard deletion
# ---------------------------------------------------------------------------


def _delete_room_messages(
    user_id: str,
    room_id: str,
    exclusive_start_key: dict | None,
) -> dict | None:
    """
    Query ChatMessages for all rows where room_id=<room_id> AND sender_id=<user_id>,
    then DeleteItem each matching row.

    Handles a single page of Query results.  Returns the LastEvaluatedKey from
    the page (the DynamoDB pagination cursor), or None when the room is exhausted.

    The outer loop in handler() keeps calling this function with the returned key
    until None is returned, at which point it advances to the next room.
    """
    client = _get_dynamo()

    kwargs: dict = {
        "TableName": TABLE_CHAT_MESSAGES,
        "KeyConditionExpression": "room_id = :rid",
        "FilterExpression": "sender_id = :uid",
        "ExpressionAttributeValues": {
            ":rid": {"S": room_id},
            ":uid": {"S": user_id},
        },
    }
    if exclusive_start_key is not None:
        kwargs["ExclusiveStartKey"] = exclusive_start_key

    response = client.query(**kwargs)

    for item in response.get("Items", []):
        _delete_message(item)

    return response.get("LastEvaluatedKey")


def _delete_message(item: dict) -> None:
    """
    DeleteItem on a single ChatMessages row — removes the row entirely.
    Uses room_id (PK) and message_id (SK) from the Query result item.

    Idempotent: a DeleteItem on a key that no longer exists is a no-op
    (DynamoDB returns success with no error for missing keys).
    """
    client = _get_dynamo()

    room_id: str = item["room_id"]["S"]
    message_id: str = item["message_id"]["S"]

    client.delete_item(
        TableName=TABLE_CHAT_MESSAGES,
        Key={
            "room_id": {"S": room_id},
            "message_id": {"S": message_id},
        },
    )

    logger.debug(
        "hard_delete_chat_messages_message_deleted",
        extra={"room_id": room_id, "message_id": message_id},
    )
