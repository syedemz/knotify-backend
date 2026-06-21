"""
knotify-deactivate-chat-rooms Lambda handler — story 9.4

Deactivates all ChatRooms the deleted user was a member of and removes the
deleted user's ChatRoomMembership rows.  Called from the account-deletion
Step Functions state machine immediately after DisableCognitoUser.

Sequence:
  1. Query ChatRoomMembership (PK=user_id) to collect all room_ids.
  2. For each room_id, UpdateItem on ChatRooms:
       SET status='deactivated', deactivated_reason='user_deleted_account',
           deactivated_at=<ISO-8601 UTC now>
     Condition: room is NOT already deactivated (idempotent — skips rooms
     already deactivated for any reason, e.g. a prior block).
  3. BatchWriteItem-delete the user's ChatRoomMembership rows (PK=user_id,
     SK=room_id). Surviving participants' rows are untouched.
  4. Return {room_ids: [<all room_ids collected in step 1>]} so the state
     machine Parameters block can inject them into downstream tasks
     (AnonymizeChatMessages / HardDeleteUserChatMessages) which run AFTER
     these membership rows are gone.

AppSync onRoomDeactivated publishes are NOT issued here — the room_state_publisher
Lambda (story 8.9a) consumes the ChatRooms DDB stream and handles that.

Environment variables:
  TABLE_CHAT_ROOMS           — DynamoDB table name (default: ChatRooms)
  TABLE_CHAT_ROOM_MEMBERSHIP — DynamoDB table name (default: ChatRoomMembership)
  LOG_LEVEL                  — logging level (default: INFO)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_CHAT_ROOMS: str = os.environ.get("TABLE_CHAT_ROOMS", "ChatRooms")
TABLE_CHAT_ROOM_MEMBERSHIP: str = os.environ.get("TABLE_CHAT_ROOM_MEMBERSHIP", "ChatRoomMembership")

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
      {"room_ids": [list of room_ids the user was a member of]}

    The returned room_ids list is used by the state machine to pass room context
    to downstream tasks (AnonymizeChatMessages, HardDeleteUserChatMessages).
    """
    user_id: str = event["user_id"]

    room_ids: list[str] = _query_all_room_ids(user_id)

    logger.info(
        "deactivate_chat_rooms_start",
        extra={"user_id": user_id, "room_count": len(room_ids)},
    )

    if not room_ids:
        return {"room_ids": []}

    _deactivate_rooms(room_ids)
    _delete_user_memberships(user_id, room_ids)

    logger.info(
        "deactivate_chat_rooms_complete",
        extra={"user_id": user_id, "room_count": len(room_ids)},
    )

    return {"room_ids": room_ids}


# ---------------------------------------------------------------------------
# Step 1 — Query ChatRoomMembership for all room_ids
# ---------------------------------------------------------------------------


def _query_all_room_ids(user_id: str) -> list[str]:
    """
    Paginate through ChatRoomMembership (PK=user_id) and return all room_ids.
    Handles LastEvaluatedKey for users who are members of many rooms.
    """
    client = _get_dynamo()
    room_ids: list[str] = []
    exclusive_start_key: dict | None = None

    while True:
        kwargs: dict = {
            "TableName": TABLE_CHAT_ROOM_MEMBERSHIP,
            "KeyConditionExpression": "user_id = :uid",
            "ExpressionAttributeValues": {":uid": {"S": user_id}},
            "ProjectionExpression": "room_id",
        }
        if exclusive_start_key is not None:
            kwargs["ExclusiveStartKey"] = exclusive_start_key

        response = client.query(**kwargs)

        for item in response.get("Items", []):
            room_ids.append(item["room_id"]["S"])

        exclusive_start_key = response.get("LastEvaluatedKey")
        if exclusive_start_key is None:
            break

    return room_ids


# ---------------------------------------------------------------------------
# Step 2 — UpdateItem on each ChatRooms row (idempotent, conditional)
# ---------------------------------------------------------------------------


def _deactivate_rooms(room_ids: list[str]) -> None:
    """
    Call UpdateItem on each ChatRooms row to mark it deactivated.

    The ConditionExpression only fires when the room is NOT already deactivated
    (attribute_not_exists(#status) OR #status <> :deactivated).  A
    ConditionalCheckFailedException means the room is already deactivated for
    any reason; we skip it silently (first-writer-wins: the original reason,
    e.g. 'blocked', is preserved).
    """
    client = _get_dynamo()
    now: str = datetime.now(timezone.utc).isoformat()

    for room_id in room_ids:
        try:
            client.update_item(
                TableName=TABLE_CHAT_ROOMS,
                Key={"room_id": {"S": room_id}},
                UpdateExpression=(
                    "SET #status = :deactivated, "
                    "deactivated_reason = :reason, "
                    "deactivated_at = :now"
                ),
                ConditionExpression=(
                    "attribute_not_exists(#status) OR #status <> :deactivated"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":deactivated": {"S": "deactivated"},
                    ":reason": {"S": "user_deleted_account"},
                    ":now": {"S": now},
                },
            )
        except Exception as exc:
            error_response = getattr(exc, "response", None)
            if error_response is None:
                raise
            code: str = error_response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                logger.info(
                    "deactivate_chat_rooms_room_already_deactivated_skipped",
                    extra={"room_id": room_id},
                )
                continue
            raise


# ---------------------------------------------------------------------------
# Step 3 — BatchWriteItem-delete the user's ChatRoomMembership rows
# ---------------------------------------------------------------------------


def _delete_user_memberships(user_id: str, room_ids: list[str]) -> None:
    """
    Issue BatchWriteItem Delete requests for all (user_id, room_id) pairs.
    Splits into chunks of at most 25 (DynamoDB BatchWriteItem limit).
    Already-deleted rows are tolerated (DynamoDB ignores deletes for missing keys).
    """
    client = _get_dynamo()

    for i in range(0, len(room_ids), _BATCH_WRITE_MAX):
        chunk = room_ids[i : i + _BATCH_WRITE_MAX]
        delete_requests = [
            {
                "DeleteRequest": {
                    "Key": {
                        "user_id": {"S": user_id},
                        "room_id": {"S": room_id},
                    }
                }
            }
            for room_id in chunk
        ]
        client.batch_write_item(
            RequestItems={TABLE_CHAT_ROOM_MEMBERSHIP: delete_requests}
        )
