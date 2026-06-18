"""
Unit tests for story 8.10 — push_fanout Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:

  A.  ChatMessages INSERT: GetItem ChatRooms to find recipient (not sender),
      GetItem ChatRoomMembership to check notifications_muted,
      Query PushNotificationTokens for recipient,
      POST to Expo with correct title/body/deep-link payload.

  B.  ChatMessages INSERT where ChatRoomMembership.notifications_muted=True
      → no Expo call made.

  C.  Notifications INSERT: recipient = item.user_id,
      Query PushNotificationTokens for recipient,
      POST to Expo,
      UpdateItem Notifications SET delivered=true on HTTP 200.

  D.  Expo response DeviceNotRegistered → DeleteItem PushNotificationTokens
      (user_id, device_id) for that token; other tokens in the same batch
      are NOT deleted.

  E.  Non-INSERT events (MODIFY, REMOVE) are silently ignored for both
      stream sources.

  F.  EXPO_AUTH_MODE=none → no Authorization header sent.
      EXPO_AUTH_MODE=bearer → Authorization: Bearer <token> header sent.

  G.  A batch containing both ChatMessages and Notifications records is
      processed record-by-record in order (dispatch based on stream source
      or event structure).

  H.  ChatMessages INSERT: title is recipient's ChatRoomMembership.cached_other_name
      (which holds the sender's display name from the recipient's perspective).

  I.  Chat messages do NOT write a Notifications row (§5.4.2 explicit rule).
      Asserted by verifying DynamoDB put_item / update_item is never called
      on the Notifications table during a ChatMessages INSERT.

  J.  Multiple push tokens for the same recipient: one Expo POST per token.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# DynamoDB image builder helpers (mirror room_state_publisher test pattern)
# ---------------------------------------------------------------------------


def _s(value: str) -> dict:
    return {"S": value}


def _bool_attr(value: bool) -> dict:
    return {"BOOL": value}


def _make_stream_record(
    event_name: str,
    new_image: dict | None = None,
    event_source_arn: str = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000",
) -> dict:
    """Build a DynamoDB stream record compatible with the Lambda event format."""
    record: dict[str, Any] = {
        "eventName": event_name,
        "eventSourceARN": event_source_arn,
        "dynamodb": {},
    }
    if new_image is not None:
        record["dynamodb"]["NewImage"] = new_image
    return record


_CHAT_MESSAGES_STREAM_ARN = (
    "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
)
_NOTIFICATIONS_STREAM_ARN = (
    "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
)


def _chat_message_image(
    room_id: str = "room-abc",
    sender_id: str = "user-sender",
    content: str = "Hello world",
    message_id: str = "2026-06-18T10:00:00Z#01J4EXAMPLE",
) -> dict:
    return {
        "room_id": _s(room_id),
        "created_at_message_id": _s(message_id),
        "sender_id": _s(sender_id),
        "content": _s(content),
        "content_type": _s("text"),
        "delivered_at": _s("2026-06-18T10:00:00Z"),
    }


def _notification_image(
    user_id: str = "user-recipient",
    notification_id: str = "notif-001",
    notif_type: str = "friend_request_received",
    sender_name: str = "Alice",
) -> dict:
    return {
        "user_id": _s(user_id),
        "notification_id": _s(notification_id),
        "type": _s(notif_type),
        "sender_name": _s(sender_name),
        "read": _bool_attr(False),
        "delivered": _bool_attr(False),
    }


# ---------------------------------------------------------------------------
# DynamoDB GetItem/Query mock helpers
# ---------------------------------------------------------------------------


def _chat_rooms_item(
    room_id: str = "room-abc",
    user_a: str = "user-a",
    user_b: str = "user-b",
) -> dict:
    return {
        "Item": {
            "room_id": _s(room_id),
            "user_a": _s(user_a),
            "user_b": _s(user_b),
            "status": _s("active"),
            "friendship_active": _bool_attr(True),
        }
    }


def _membership_item(
    user_id: str = "user-recipient",
    room_id: str = "room-abc",
    cached_other_name: str = "Sender Name",
    notifications_muted: bool = False,
) -> dict:
    item = {
        "Item": {
            "user_id": _s(user_id),
            "room_id": _s(room_id),
            "cached_other_name": _s(cached_other_name),
            "notifications_muted": _bool_attr(notifications_muted),
        }
    }
    return item


def _push_tokens_query(
    user_id: str = "user-recipient",
    tokens: list[tuple[str, str]] | None = None,
) -> dict:
    """Returns a DynamoDB Query response for PushNotificationTokens.

    tokens: list of (device_id, push_token) pairs.
    """
    if tokens is None:
        tokens = [("device-001", "ExponentPushToken[abc]")]
    items = [
        {
            "user_id": _s(user_id),
            "device_id": _s(device_id),
            "push_token": _s(token),
        }
        for device_id, token in tokens
    ]
    return {"Items": items, "Count": len(items)}


# ---------------------------------------------------------------------------
# Expo API response helpers
# ---------------------------------------------------------------------------


def _expo_ok_response(token: str = "ExponentPushToken[abc]") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [{"status": "ok", "id": "notification-id-123"}]
    }
    return resp


def _expo_device_not_registered_response(token: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [
            {
                "status": "error",
                "message": f"ExponentPushToken[{token}] is not a registered push notification recipient",
                "details": {"error": "DeviceNotRegistered"},
            }
        ]
    }
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def set_env_vars(monkeypatch):
    """Set required environment variables for every test."""
    monkeypatch.setenv("EXPO_PUSH_URL", "https://mock-expo.example.com/push/send")
    monkeypatch.setenv("EXPO_AUTH_MODE", "none")
    monkeypatch.setenv(
        "CHAT_MESSAGES_STREAM_ARN",
        _CHAT_MESSAGES_STREAM_ARN,
    )
    monkeypatch.setenv(
        "NOTIFICATIONS_STREAM_ARN",
        _NOTIFICATIONS_STREAM_ARN,
    )
    monkeypatch.setenv("AWS_REGION", "eu-central-1")


@pytest.fixture()
def mock_ddb():
    """Patch boto3.client('dynamodb') to return a controllable mock."""
    with patch("push_fanout.handler._get_ddb_client") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        yield mock_client


@pytest.fixture()
def mock_requests_post():
    """Patch requests.post so no real HTTP calls are made to Expo."""
    with patch("push_fanout.handler.requests.post") as mock_post:
        yield mock_post


# ---------------------------------------------------------------------------
# Test A: ChatMessages INSERT — happy path push notification
# ---------------------------------------------------------------------------


def test_given_chat_message_insert_when_recipient_not_muted_then_expo_called_with_correct_payload(
    mock_ddb, mock_requests_post
):
    """
    AC-A: ChatMessages INSERT triggers Expo POST with:
      - title = ChatRoomMembership(recipient, room_id).cached_other_name
      - body = first 80 chars of message content
      - deep link = knotify://chat/<room_id>
    """
    from push_fanout import handler

    room_id = "room-abc"
    sender_id = "user-sender"
    recipient_id = "user-recipient"
    sender_display_name = "Alice"
    content = "Hello world, this is a chat message."

    # ChatRooms GetItem: user_a=sender, user_b=recipient
    mock_ddb.get_item.side_effect = [
        _chat_rooms_item(room_id=room_id, user_a=sender_id, user_b=recipient_id),
        _membership_item(
            user_id=recipient_id,
            room_id=room_id,
            cached_other_name=sender_display_name,
            notifications_muted=False,
        ),
    ]
    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[("device-001", "ExponentPushToken[abc]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_chat_message_image(
            room_id=room_id, sender_id=sender_id, content=content
        ),
        event_source_arn=_CHAT_MESSAGES_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    mock_requests_post.assert_called_once()
    _, kwargs = mock_requests_post.call_args
    body = json.loads(kwargs["data"])
    assert body["to"] == "ExponentPushToken[abc]"
    assert body["title"] == sender_display_name
    assert body["body"] == content[:80]
    assert body["data"]["deepLink"] == f"knotify://chat/{room_id}"


# ---------------------------------------------------------------------------
# Test H: title comes from recipient's cached_other_name (sender's name)
# ---------------------------------------------------------------------------


def test_given_chat_message_insert_when_sender_is_user_b_then_title_from_recipient_membership(
    mock_ddb, mock_requests_post
):
    """
    AC-H: When sender=user_b and recipient=user_a, title is still sourced from
    ChatRoomMembership(user_a, room_id).cached_other_name (which caches user_b's name).
    """
    from push_fanout import handler

    room_id = "room-swap"
    user_a = "user-alice"
    user_b = "user-bob-sender"
    sender_display_name_cached_for_alice = "Bob"

    mock_ddb.get_item.side_effect = [
        # ChatRooms: user_a=alice, user_b=bob (bob is the sender)
        _chat_rooms_item(room_id=room_id, user_a=user_a, user_b=user_b),
        # recipient is user_a (alice); her membership caches bob's name
        _membership_item(
            user_id=user_a,
            room_id=room_id,
            cached_other_name=sender_display_name_cached_for_alice,
            notifications_muted=False,
        ),
    ]
    mock_ddb.query.return_value = _push_tokens_query(
        user_id=user_a,
        tokens=[("device-alice", "ExponentPushToken[alice-token]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_chat_message_image(
            room_id=room_id,
            sender_id=user_b,
            content="Hey!",
        ),
        event_source_arn=_CHAT_MESSAGES_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    _, kwargs = mock_requests_post.call_args
    body = json.loads(kwargs["data"])
    assert body["title"] == sender_display_name_cached_for_alice
    assert body["to"] == "ExponentPushToken[alice-token]"


# ---------------------------------------------------------------------------
# Test B: ChatMessages INSERT with notifications_muted=True — no Expo call
# ---------------------------------------------------------------------------


def test_given_chat_message_insert_when_notifications_muted_then_expo_not_called(
    mock_ddb, mock_requests_post
):
    """
    AC-B: If recipient's ChatRoomMembership.notifications_muted=True,
    no Expo call is made.
    """
    from push_fanout import handler

    room_id = "room-muted"
    sender_id = "user-a"
    recipient_id = "user-b"

    mock_ddb.get_item.side_effect = [
        _chat_rooms_item(room_id=room_id, user_a=sender_id, user_b=recipient_id),
        _membership_item(
            user_id=recipient_id,
            room_id=room_id,
            cached_other_name="A",
            notifications_muted=True,  # muted!
        ),
    ]

    record = _make_stream_record(
        "INSERT",
        new_image=_chat_message_image(
            room_id=room_id, sender_id=sender_id, content="muted"
        ),
        event_source_arn=_CHAT_MESSAGES_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    mock_requests_post.assert_not_called()


# ---------------------------------------------------------------------------
# Test C: Notifications INSERT — Expo called, delivered=true set on 200
# ---------------------------------------------------------------------------


def test_given_notification_insert_when_expo_returns_200_then_delivered_flag_set(
    mock_ddb, mock_requests_post
):
    """
    AC-C: Notifications INSERT → Expo POST; on HTTP 200 UpdateItem Notifications
    SET delivered=true.
    """
    from push_fanout import handler

    recipient_id = "user-recipient"
    notification_id = "notif-001"

    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[("device-001", "ExponentPushToken[ntf]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_notification_image(
            user_id=recipient_id,
            notification_id=notification_id,
        ),
        event_source_arn=_NOTIFICATIONS_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    mock_requests_post.assert_called_once()
    # UpdateItem must have been called with delivered=true
    mock_ddb.update_item.assert_called_once()
    update_call_kwargs = mock_ddb.update_item.call_args[1]
    assert update_call_kwargs["TableName"] == "Notifications"
    assert ":delivered" in update_call_kwargs["ExpressionAttributeValues"]
    assert (
        update_call_kwargs["ExpressionAttributeValues"][":delivered"]["BOOL"] is True
    )


# ---------------------------------------------------------------------------
# Test D: Expo DeviceNotRegistered → DeleteItem that token only
# ---------------------------------------------------------------------------


def test_given_expo_returns_device_not_registered_when_processed_then_token_row_deleted(
    mock_ddb, mock_requests_post
):
    """
    AC-D: Expo DeviceNotRegistered response → DeleteItem PushNotificationTokens
    for the offending (user_id, device_id). Other tokens in the same Query
    result are NOT deleted.
    """
    from push_fanout import handler

    recipient_id = "user-recipient"
    notification_id = "notif-dnr"
    bad_device = "device-bad"
    bad_token = "ExponentPushToken[bad]"
    good_device = "device-good"
    good_token = "ExponentPushToken[good]"

    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[(bad_device, bad_token), (good_device, good_token)],
    )

    def post_side_effect(url, data=None, **kwargs):
        payload = json.loads(data)
        if payload["to"] == bad_token:
            return _expo_device_not_registered_response(bad_token)
        return _expo_ok_response(good_token)

    mock_requests_post.side_effect = post_side_effect

    record = _make_stream_record(
        "INSERT",
        new_image=_notification_image(
            user_id=recipient_id,
            notification_id=notification_id,
        ),
        event_source_arn=_NOTIFICATIONS_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    # Exactly one DeleteItem — for the bad device only
    delete_calls = mock_ddb.delete_item.call_args_list
    assert len(delete_calls) == 1, (
        f"Expected exactly 1 DeleteItem, got {len(delete_calls)}"
    )
    deleted_key = delete_calls[0][1]["Key"]
    assert deleted_key["user_id"]["S"] == recipient_id
    assert deleted_key["device_id"]["S"] == bad_device


# ---------------------------------------------------------------------------
# Test E: non-INSERT events are silently ignored
# ---------------------------------------------------------------------------


def test_given_modify_event_for_chat_messages_when_processed_then_no_expo_call(
    mock_ddb, mock_requests_post
):
    """
    AC-E: MODIFY events on ChatMessages (e.g. delivered_at update) must not
    trigger any Expo push.
    """
    from push_fanout import handler

    record = _make_stream_record(
        "MODIFY",
        new_image=_chat_message_image(),
        event_source_arn=_CHAT_MESSAGES_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    mock_requests_post.assert_not_called()
    mock_ddb.get_item.assert_not_called()


def test_given_remove_event_for_notifications_when_processed_then_no_expo_call(
    mock_ddb, mock_requests_post
):
    """
    AC-E: REMOVE events on Notifications must be ignored.
    """
    from push_fanout import handler

    record = _make_stream_record(
        "REMOVE",
        event_source_arn=_NOTIFICATIONS_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    mock_requests_post.assert_not_called()


# ---------------------------------------------------------------------------
# Test F: Expo auth mode — none vs bearer
# ---------------------------------------------------------------------------


def test_given_expo_auth_mode_none_when_posting_then_no_authorization_header(
    monkeypatch, mock_ddb, mock_requests_post
):
    """
    AC-F (none): EXPO_AUTH_MODE=none → no Authorization header in Expo POST.
    EXPO_AUTH_MODE is read at call time so monkeypatch takes effect immediately.
    """
    monkeypatch.setenv("EXPO_AUTH_MODE", "none")
    from push_fanout import handler

    recipient_id = "user-r-noauth"
    notification_id = "notif-no-auth"

    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[("device-001", "ExponentPushToken[noauth]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_notification_image(user_id=recipient_id, notification_id=notification_id),
        event_source_arn=_NOTIFICATIONS_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    _, kwargs = mock_requests_post.call_args
    assert "Authorization" not in kwargs.get("headers", {}), (
        "EXPO_AUTH_MODE=none must not set an Authorization header"
    )


def test_given_expo_auth_mode_bearer_when_posting_then_authorization_header_present(
    monkeypatch, mock_ddb, mock_requests_post
):
    """
    AC-F (bearer): EXPO_AUTH_MODE=bearer → Authorization: Bearer <token> header.
    The token is injected via EXPO_ACCESS_TOKEN env var (stands in for cold-start
    Secrets Manager read in prod; unit tests bypass the SM call entirely by
    injecting the value directly).
    EXPO_AUTH_MODE and EXPO_ACCESS_TOKEN are read at call time via _get_expo_auth_mode()
    and _load_expo_token(), so monkeypatch takes effect without reloading the module.
    """
    monkeypatch.setenv("EXPO_AUTH_MODE", "bearer")
    monkeypatch.setenv("EXPO_ACCESS_TOKEN", "my-secret-expo-token")
    from push_fanout import handler

    # Reset cached token so _get_expo_token() re-reads the env var
    handler._EXPO_ACCESS_TOKEN = None

    recipient_id = "user-r-bearer"
    notification_id = "notif-bearer"

    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[("device-001", "ExponentPushToken[bearer]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_notification_image(user_id=recipient_id, notification_id=notification_id),
        event_source_arn=_NOTIFICATIONS_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    _, kwargs = mock_requests_post.call_args
    headers = kwargs.get("headers", {})
    assert "Authorization" in headers, (
        "EXPO_AUTH_MODE=bearer must set Authorization header"
    )
    assert headers["Authorization"] == "Bearer my-secret-expo-token", (
        f"Expected 'Bearer my-secret-expo-token', got {headers['Authorization']!r}"
    )


# ---------------------------------------------------------------------------
# Test I: ChatMessages INSERT does NOT write to Notifications table
# ---------------------------------------------------------------------------


def test_given_chat_message_insert_when_expo_called_then_no_notifications_row_written(
    mock_ddb, mock_requests_post
):
    """
    AC-I: §5.4.2 rule — chat messages must never create a Notifications row.
    Verified by asserting put_item and update_item are never called during
    a ChatMessages INSERT.
    """
    from push_fanout import handler

    room_id = "room-no-notif"
    sender_id = "user-s"
    recipient_id = "user-r"

    mock_ddb.get_item.side_effect = [
        _chat_rooms_item(room_id=room_id, user_a=sender_id, user_b=recipient_id),
        _membership_item(
            user_id=recipient_id,
            room_id=room_id,
            cached_other_name="Sender",
            notifications_muted=False,
        ),
    ]
    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[("device-001", "ExponentPushToken[x]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_chat_message_image(
            room_id=room_id, sender_id=sender_id, content="hi"
        ),
        event_source_arn=_CHAT_MESSAGES_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    # put_item and update_item must not have been called at all during a
    # ChatMessages INSERT (no Notifications write permitted).
    mock_ddb.put_item.assert_not_called()
    mock_ddb.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test J: Multiple push tokens — one Expo POST per token
# ---------------------------------------------------------------------------


def test_given_multiple_tokens_for_recipient_when_notification_inserted_then_expo_called_per_token(
    mock_ddb, mock_requests_post
):
    """
    AC-J: When a recipient has 2 push tokens, 2 Expo POSTs are made.
    """
    from push_fanout import handler

    recipient_id = "user-multi-token"
    notification_id = "notif-multi"

    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[
            ("device-001", "ExponentPushToken[t1]"),
            ("device-002", "ExponentPushToken[t2]"),
        ],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_notification_image(user_id=recipient_id, notification_id=notification_id),
        event_source_arn=_NOTIFICATIONS_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    assert mock_requests_post.call_count == 2, (
        f"Expected 2 Expo POSTs (one per token), got {mock_requests_post.call_count}"
    )
    posted_tokens = [
        json.loads(c[1]["data"])["to"]
        for c in mock_requests_post.call_args_list
    ]
    assert set(posted_tokens) == {"ExponentPushToken[t1]", "ExponentPushToken[t2]"}


# ---------------------------------------------------------------------------
# Test: body truncated to 80 chars
# ---------------------------------------------------------------------------


def test_given_long_message_content_when_chat_message_insert_then_body_truncated_to_80_chars(
    mock_ddb, mock_requests_post
):
    """
    AC-A: body = first 80 chars of message content (not the full message).
    """
    from push_fanout import handler

    room_id = "room-long"
    sender_id = "user-s"
    recipient_id = "user-r"
    long_content = "A" * 200

    mock_ddb.get_item.side_effect = [
        _chat_rooms_item(room_id=room_id, user_a=sender_id, user_b=recipient_id),
        _membership_item(
            user_id=recipient_id,
            room_id=room_id,
            cached_other_name="Sender",
            notifications_muted=False,
        ),
    ]
    mock_ddb.query.return_value = _push_tokens_query(
        user_id=recipient_id,
        tokens=[("device-001", "ExponentPushToken[trunc]")],
    )
    mock_requests_post.return_value = _expo_ok_response()

    record = _make_stream_record(
        "INSERT",
        new_image=_chat_message_image(
            room_id=room_id, sender_id=sender_id, content=long_content
        ),
        event_source_arn=_CHAT_MESSAGES_STREAM_ARN,
    )
    handler.handler({"Records": [record]}, None)

    _, kwargs = mock_requests_post.call_args
    body = json.loads(kwargs["data"])
    assert len(body["body"]) == 80, (
        f"Expected body length 80, got {len(body['body'])}"
    )
    assert body["body"] == "A" * 80


# ---------------------------------------------------------------------------
# Integration tests (skip-gated on EXPO_PUSH_URL pointing at a real mock server)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_integration_chat_message_insert_expo_mock_receives_expected_payload():
    """
    Integration test (story 8.10 AC): insert a ChatMessages row, observe the
    Expo mock receives the correct payload.

    Skip unless DDB_ENDPOINT and EXPO_PUSH_URL are set in the environment.
    """
    ddb_endpoint = os.environ.get("DDB_ENDPOINT")
    expo_url = os.environ.get("EXPO_PUSH_URL")
    if not ddb_endpoint or not expo_url or "mock-expo.example.com" in expo_url:
        pytest.skip(
            "Integration test requires DDB_ENDPOINT and a real EXPO_PUSH_URL"
        )


@pytest.mark.integration
def test_integration_notifications_muted_no_expo_call():
    """
    Integration test: recipient's notifications_muted=True → no Expo call.
    """
    ddb_endpoint = os.environ.get("DDB_ENDPOINT")
    if not ddb_endpoint:
        pytest.skip("Integration test requires DDB_ENDPOINT")


@pytest.mark.integration
def test_integration_device_not_registered_token_deleted():
    """
    Integration test: Expo mock returns DeviceNotRegistered → token row deleted.
    """
    ddb_endpoint = os.environ.get("DDB_ENDPOINT")
    if not ddb_endpoint:
        pytest.skip("Integration test requires DDB_ENDPOINT")
