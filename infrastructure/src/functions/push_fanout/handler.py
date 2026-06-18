"""
push_fanout Lambda — story 8.10

Consumes DynamoDB Stream events from TWO sources:
  - ChatMessages table (NEW_IMAGE): fan-out push notifications to the recipient
  - Notifications table (NEW_IMAGE): fan-out push notifications to the user_id

VPC placement: OUTSIDE the VPC.
WHY: only touches DynamoDB (no VPC required) and Expo (open internet); inside-VPC
placement would repeat the blackhole failure documented in hotfix #106 (private
subnets without NAT cannot reach public service endpoints).

Expo authentication (controlled via EXPO_AUTH_MODE env var):
  EXPO_AUTH_MODE=none   — unauthenticated mode (dev); no Authorization header.
  EXPO_AUTH_MODE=bearer — prod mode; token read on cold start from Secrets Manager
                          (secret: knotify-prod-expo-push-credential) and sent as
                          Authorization: Bearer <token>.

Stream source routing is determined by matching the record's eventSourceARN against
the CHAT_MESSAGES_STREAM_ARN and NOTIFICATIONS_STREAM_ARN env vars.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
import requests

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Constants — resolved at cold start from environment variables
# ---------------------------------------------------------------------------

EXPO_PUSH_URL: str = os.environ.get("EXPO_PUSH_URL", "https://exp.host/--/api/v2/push/send")

# EXPO_AUTH_MODE is read at call time (not cold-start) so tests can override
# via monkeypatch without reloading the module.
def _get_expo_auth_mode() -> str:
    return os.environ.get("EXPO_AUTH_MODE", "none")


CHAT_MESSAGES_STREAM_ARN: str = os.environ.get("CHAT_MESSAGES_STREAM_ARN", "")
NOTIFICATIONS_STREAM_ARN: str = os.environ.get("NOTIFICATIONS_STREAM_ARN", "")

# ---------------------------------------------------------------------------
# Cold-start Expo access token (prod only)
#
# In bearer mode the token is fetched once from Secrets Manager at Lambda
# cold start.  Unit tests bypass this entirely by injecting EXPO_ACCESS_TOKEN
# via environment variable; the Secrets Manager path is only taken in prod
# when EXPO_ACCESS_TOKEN is absent.
# ---------------------------------------------------------------------------

_EXPO_ACCESS_TOKEN: str | None = None


def _load_expo_token() -> str | None:
    """
    Return the Expo access token for bearer mode.

    Resolution order:
      1. EXPO_ACCESS_TOKEN env var (injected by unit tests or CI).
      2. Secrets Manager secret 'knotify-prod-expo-push-credential' (prod Lambda).
    """
    env_token = os.environ.get("EXPO_ACCESS_TOKEN")
    if env_token:
        return env_token

    secret_name = "knotify-prod-expo-push-credential"
    try:
        sm = boto3.client("secretsmanager")
        response = sm.get_secret_value(SecretId=secret_name)
        return response["SecretString"]
    except Exception:
        logger.error(
            "Failed to fetch Expo push credential from Secrets Manager",
            extra={"secret_name": secret_name},
        )
        return None


def _get_expo_token() -> str | None:
    """
    Return the Expo access token, loading it on first use (lazy cold-start read).
    Returns the cached value on subsequent calls in the same Lambda execution context.
    In unit tests EXPO_ACCESS_TOKEN env var is read directly; no SM call is made.
    """
    global _EXPO_ACCESS_TOKEN
    if _EXPO_ACCESS_TOKEN is None and _get_expo_auth_mode() == "bearer":
        _EXPO_ACCESS_TOKEN = _load_expo_token()
    return _EXPO_ACCESS_TOKEN

# ---------------------------------------------------------------------------
# DynamoDB client factory (injectable for testing)
# ---------------------------------------------------------------------------


def _get_ddb_client():
    """Return a boto3 DynamoDB client. Separated for test patching."""
    return boto3.client("dynamodb")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> None:
    """Lambda handler: iterate stream records and fan-out push notifications."""
    records = event.get("Records", [])
    logger.info(
        "push_fanout invoked",
        extra={"record_count": len(records)},
    )
    ddb = _get_ddb_client()
    for record in records:
        _process_record(record, ddb)


# ---------------------------------------------------------------------------
# Record dispatch
# ---------------------------------------------------------------------------


def _process_record(record: dict, ddb: Any) -> None:
    """Dispatch a single DynamoDB stream record to the correct handler."""
    event_name: str = record.get("eventName", "")
    if event_name != "INSERT":
        logger.debug(
            "Skipping non-INSERT record",
            extra={"event_name": event_name},
        )
        return

    dynamodb = record.get("dynamodb", {})
    new_image = dynamodb.get("NewImage")
    if not new_image:
        logger.warning(
            "INSERT record missing NewImage — skipping",
            extra={"record": str(record)[:200]},
        )
        return

    source_arn: str = record.get("eventSourceARN", "")

    if CHAT_MESSAGES_STREAM_ARN and source_arn == CHAT_MESSAGES_STREAM_ARN:
        _handle_chat_message(new_image, ddb)
    elif NOTIFICATIONS_STREAM_ARN and source_arn == NOTIFICATIONS_STREAM_ARN:
        _handle_notification(new_image, ddb)
    else:
        # Fallback: detect by table name in the ARN substring
        if "/table/ChatMessages/" in source_arn:
            _handle_chat_message(new_image, ddb)
        elif "/table/Notifications/" in source_arn:
            _handle_notification(new_image, ddb)
        else:
            logger.warning(
                "Unrecognised stream source ARN — skipping",
                extra={"source_arn": source_arn},
            )


# ---------------------------------------------------------------------------
# ChatMessages INSERT handler
# ---------------------------------------------------------------------------


def _handle_chat_message(new_image: dict, ddb: Any) -> None:
    """
    Fan-out push notification for a new chat message.

    Flow:
      1. Extract room_id and sender_id from the stream image.
      2. GetItem ChatRooms(room_id) → find user_a and user_b; recipient is
         whichever is not sender_id.
      3. GetItem ChatRoomMembership(recipient, room_id) → check notifications_muted
         and read cached_other_name (which holds the sender's display name from
         the recipient's perspective).
      4. If notifications_muted is True → skip (no push call).
      5. Query PushNotificationTokens(PK=recipient) → get all device tokens.
      6. For each token: POST to Expo.
         - title = cached_other_name
         - body  = first 80 chars of message content
         - data.deepLink = knotify://chat/<room_id>

    NOTE: chat messages do NOT write to the Notifications table (§5.4.2).
    """
    room_id = _extract_str(new_image, "room_id")
    sender_id = _extract_str(new_image, "sender_id")
    content = _extract_str(new_image, "content") or ""

    if not room_id or not sender_id:
        logger.warning(
            "ChatMessages image missing room_id or sender_id — skipping",
            extra={"has_room_id": bool(room_id), "has_sender_id": bool(sender_id)},
        )
        return

    # Step 2: look up room to find recipient
    chat_rooms_response = ddb.get_item(
        TableName="ChatRooms",
        Key={"room_id": {"S": room_id}},
    )
    room_item = chat_rooms_response.get("Item")
    if not room_item:
        logger.warning(
            "ChatRooms row not found — skipping",
            extra={"room_id": room_id},
        )
        return

    user_a = _extract_str(room_item, "user_a")
    user_b = _extract_str(room_item, "user_b")
    recipient_id = user_b if sender_id == user_a else user_a

    logger.info(
        "chat_message push fan-out",
        extra={"room_id": room_id, "sender_id": sender_id, "recipient_id": recipient_id},
    )

    # Step 3: check membership for mute flag and cached sender name
    membership_response = ddb.get_item(
        TableName="ChatRoomMembership",
        Key={
            "user_id": {"S": recipient_id},
            "room_id": {"S": room_id},
        },
    )
    membership_item = membership_response.get("Item")
    if not membership_item:
        logger.warning(
            "ChatRoomMembership row for recipient not found — skipping",
            extra={"recipient_id": recipient_id, "room_id": room_id},
        )
        return

    # Step 4: check mute
    notifications_muted = _extract_bool(membership_item, "notifications_muted")
    if notifications_muted:
        logger.info(
            "Recipient has notifications muted — skipping push",
            extra={"recipient_id": recipient_id, "room_id": room_id},
        )
        return

    # cached_other_name on the recipient's row holds the sender's display name
    sender_display_name = _extract_str(membership_item, "cached_other_name") or ""

    # Step 5: query push tokens
    tokens = _query_push_tokens(recipient_id, ddb)
    if not tokens:
        logger.info(
            "No push tokens for recipient — skipping",
            extra={"recipient_id": recipient_id},
        )
        return

    # Step 6: push to each token
    notification_payload = {
        "title": sender_display_name,
        "body": content[:80],
        "data": {"deepLink": f"knotify://chat/{room_id}"},
    }
    for device_id, push_token in tokens:
        _send_expo_push(
            user_id=recipient_id,
            device_id=device_id,
            push_token=push_token,
            notification=notification_payload,
            ddb=ddb,
            mark_delivered=False,  # chat messages never touch Notifications table
            notification_id=None,
        )


# ---------------------------------------------------------------------------
# Notifications INSERT handler
# ---------------------------------------------------------------------------


def _handle_notification(new_image: dict, ddb: Any) -> None:
    """
    Fan-out push notification for a new Notifications row.

    Flow:
      1. Extract recipient user_id and notification_id from the image.
      2. Query PushNotificationTokens(PK=user_id).
      3. For each token: POST to Expo.
      4. On HTTP 200: UpdateItem Notifications SET delivered=true.
    """
    recipient_id = _extract_str(new_image, "user_id")
    notification_id = _extract_str(new_image, "notification_id")
    notif_type = _extract_str(new_image, "type") or ""
    sender_name = _extract_str(new_image, "sender_name") or ""

    if not recipient_id:
        logger.warning(
            "Notification image missing user_id — skipping",
        )
        return

    logger.info(
        "notification push fan-out",
        extra={
            "recipient_id": recipient_id,
            "notification_id": notification_id,
            "type": notif_type,
        },
    )

    tokens = _query_push_tokens(recipient_id, ddb)
    if not tokens:
        logger.info(
            "No push tokens for recipient — skipping",
            extra={"recipient_id": recipient_id},
        )
        return

    notification_payload = {
        "title": sender_name or "Knotify",
        "body": notif_type,
        "data": {"notificationId": notification_id, "type": notif_type},
    }
    for device_id, push_token in tokens:
        _send_expo_push(
            user_id=recipient_id,
            device_id=device_id,
            push_token=push_token,
            notification=notification_payload,
            ddb=ddb,
            mark_delivered=True,
            notification_id=notification_id,
        )


# ---------------------------------------------------------------------------
# Expo push sender
# ---------------------------------------------------------------------------


def _send_expo_push(
    *,
    user_id: str,
    device_id: str,
    push_token: str,
    notification: dict,
    ddb: Any,
    mark_delivered: bool,
    notification_id: str | None,
) -> None:
    """
    POST a single push notification to the Expo Push API.

    On HTTP 200:
      - If the response contains DeviceNotRegistered → delete the stale token.
      - Else if mark_delivered=True → UpdateItem Notifications SET delivered=true.
    """
    body = json.dumps({"to": push_token, **notification})
    headers = {"Content-Type": "application/json"}
    if _get_expo_auth_mode() == "bearer":
        token = _get_expo_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

    logger.info(
        "Sending Expo push",
        extra={"user_id": user_id, "device_id": device_id},
    )

    try:
        response = requests.post(
            EXPO_PUSH_URL,
            data=body,
            headers=headers,
            timeout=10,
        )
    except Exception as exc:
        logger.error(
            "Expo POST raised an exception",
            extra={"user_id": user_id, "device_id": device_id, "error": str(exc)},
        )
        raise

    if response.status_code != 200:
        logger.error(
            "Expo returned non-200 status",
            extra={
                "user_id": user_id,
                "status_code": response.status_code,
                "body": response.text[:500],
            },
        )
        response.raise_for_status()

    data = response.json()
    ticket = (data.get("data") or [{}])[0] if isinstance(data.get("data"), list) else {}
    error_detail = ticket.get("details", {}).get("error") if isinstance(ticket, dict) else None

    if error_detail == "DeviceNotRegistered":
        logger.info(
            "DeviceNotRegistered — deleting stale token",
            extra={"user_id": user_id, "device_id": device_id},
        )
        ddb.delete_item(
            TableName="PushNotificationTokens",
            Key={
                "user_id": {"S": user_id},
                "device_id": {"S": device_id},
            },
        )
        return

    logger.info(
        "Expo push sent successfully",
        extra={"user_id": user_id, "device_id": device_id},
    )

    if mark_delivered and notification_id:
        ddb.update_item(
            TableName="Notifications",
            Key={
                "user_id": {"S": user_id},
                "notification_id": {"S": notification_id},
            },
            UpdateExpression="SET delivered = :delivered",
            ExpressionAttributeValues={":delivered": {"BOOL": True}},
        )


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _query_push_tokens(user_id: str, ddb: Any) -> list[tuple[str, str]]:
    """
    Query PushNotificationTokens for all tokens belonging to user_id.
    Returns a list of (device_id, push_token) tuples.
    """
    response = ddb.query(
        TableName="PushNotificationTokens",
        KeyConditionExpression="#uid = :uid",
        ExpressionAttributeNames={"#uid": "user_id"},
        ExpressionAttributeValues={":uid": {"S": user_id}},
    )
    items = response.get("Items", [])
    return [
        (
            item.get("device_id", {}).get("S", ""),
            item.get("push_token", {}).get("S", ""),
        )
        for item in items
        if item.get("device_id", {}).get("S") and item.get("push_token", {}).get("S")
    ]


def _extract_str(image: dict, key: str) -> str | None:
    """Extract a string value from a DynamoDB image dict. Returns None if absent."""
    attr = image.get(key)
    if attr is None:
        return None
    return attr.get("S")


def _extract_bool(image: dict, key: str) -> bool | None:
    """Extract a boolean value from a DynamoDB image dict. Returns None if absent."""
    attr = image.get(key)
    if attr is None:
        return None
    return attr.get("BOOL")
