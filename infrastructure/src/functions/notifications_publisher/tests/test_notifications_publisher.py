"""
Unit tests for story 8.9c — notifications_publisher Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. INSERT event with type=friend_request_received calls publishNotification
     exactly once with the row payload including user_id.
  B. INSERT event with type=bookmark calls publishNotification exactly once.
  C. INSERT event with type=match calls publishNotification exactly once.
  D. INSERT event with type=friend_request_accepted calls
     _publishFriendRequestUpdated exactly once with the row payload.
  E. INSERT event with an unknown type logs a warning and does NOT call any
     publish mutation (no crash).
  F. MODIFY events are ignored entirely — no publish call regardless of type.
  G. REMOVE events are ignored entirely — no publish call.
  H. Payload forwarded to AppSync MUST include user_id sourced from the
     Notifications row PK (recipient's Cognito sub).
  I. A batch containing multiple INSERT records dispatches correctly per type.
  J. Missing or None NewImage is handled safely (no publish, no crash).
  K. _build_notification_payload converts DynamoDB NewImage to plain dict
     with camelCase keys matching the GraphQL Notification type.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — build synthetic DynamoDB stream records
# ---------------------------------------------------------------------------


def _ddb_str(value: str) -> dict:
    return {"S": value}


def _ddb_bool(value: bool) -> dict:
    return {"BOOL": value}


def _make_record(
    event_name: str,
    new_image: dict | None = None,
    old_image: dict | None = None,
) -> dict:
    """Build a DynamoDB stream record compatible with the Lambda event format."""
    record: dict[str, Any] = {"eventName": event_name, "dynamodb": {}}
    if new_image is not None:
        record["dynamodb"]["NewImage"] = new_image
    if old_image is not None:
        record["dynamodb"]["OldImage"] = old_image
    return record


def _notification_image(
    user_id: str = "user-recipient-abc",
    notification_id: str = "notif-001",
    notification_type: str = "friend_request_received",
    sender_id: str = "user-sender-xyz",
    created_at: str = "2026-06-18T10:00:00.000000#notif-001",
) -> dict:
    """
    Build a Notifications row DynamoDB image.

    PK: user_id (recipient's Cognito sub)
    SK: <ISO timestamp>#<notification_id>
    """
    return {
        "user_id": _ddb_str(user_id),
        "created_at_notification_id": _ddb_str(created_at),
        "notification_id": _ddb_str(notification_id),
        "type": _ddb_str(notification_type),
        "sender_user_id": _ddb_str(sender_id),
        "sender_name": _ddb_str("Alice"),
        "sender_avatar": _ddb_str("https://example.com/alice.jpg"),
        "read": _ddb_bool(False),
        "delivered": _ddb_bool(False),
    }


def _notification_image_with_payload(
    user_id: str = "user-recipient-abc",
    notification_type: str = "friend_request_accepted",
    extra_payload: str = '{"friendId":"user-sender-xyz"}',
) -> dict:
    base = _notification_image(
        user_id=user_id,
        notification_type=notification_type,
    )
    base["payload"] = _ddb_str(extra_payload)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_publish():
    """
    Patch handler._publish_to_appsync so no real HTTP call is made.
    Returns the mock so tests can assert call count and arguments.
    """
    with patch(
        "notifications_publisher.handler._publish_to_appsync"
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Test A: INSERT friend_request_received → publishNotification
# ---------------------------------------------------------------------------


def test_given_insert_friend_request_received_when_processed_then_publish_notification_called_once(
    mock_publish,
):
    """
    AC-A: INSERT record type=friend_request_received → publishNotification called once.
    """
    from notifications_publisher import handler

    user_id = "user-recipient-fr-received"
    record = _make_record(
        "INSERT",
        new_image=_notification_image(
            user_id=user_id,
            notification_type="friend_request_received",
        ),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_called_once()
    mutation_name, payload = mock_publish.call_args.args
    assert mutation_name == "publishNotification", (
        f"expected 'publishNotification', got {mutation_name!r}"
    )


def test_given_insert_friend_request_received_when_processed_then_payload_contains_user_id(
    mock_publish,
):
    """
    AC-H: publishNotification payload must include user_id from the row PK.
    """
    from notifications_publisher import handler

    user_id = "user-recipient-userid-check"
    record = _make_record(
        "INSERT",
        new_image=_notification_image(
            user_id=user_id,
            notification_type="friend_request_received",
        ),
    )
    handler.handler({"Records": [record]}, None)

    _, payload = mock_publish.call_args.args
    assert payload.get("userId") == user_id, (
        f"payload['userId'] must equal PK user_id={user_id!r}, got {payload.get('userId')!r}"
    )


# ---------------------------------------------------------------------------
# Test B: INSERT bookmark → publishNotification
# ---------------------------------------------------------------------------


def test_given_insert_bookmark_when_processed_then_publish_notification_called_once(
    mock_publish,
):
    """
    AC-B: INSERT record type=bookmark → publishNotification called once.
    """
    from notifications_publisher import handler

    record = _make_record(
        "INSERT",
        new_image=_notification_image(
            user_id="user-recipient-bm",
            notification_type="bookmark",
        ),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_called_once()
    mutation_name, _ = mock_publish.call_args.args
    assert mutation_name == "publishNotification"


# ---------------------------------------------------------------------------
# Test C: INSERT match → publishNotification
# ---------------------------------------------------------------------------


def test_given_insert_match_when_processed_then_publish_notification_called_once(
    mock_publish,
):
    """
    AC-C: INSERT record type=match → publishNotification called once.
    """
    from notifications_publisher import handler

    record = _make_record(
        "INSERT",
        new_image=_notification_image(
            user_id="user-recipient-match",
            notification_type="match",
        ),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_called_once()
    mutation_name, _ = mock_publish.call_args.args
    assert mutation_name == "publishNotification"


# ---------------------------------------------------------------------------
# Test D: INSERT friend_request_accepted → _publishFriendRequestUpdated
# ---------------------------------------------------------------------------


def test_given_insert_friend_request_accepted_when_processed_then_publish_friend_request_updated_called_once(
    mock_publish,
):
    """
    AC-D: INSERT record type=friend_request_accepted → _publishFriendRequestUpdated called once.
    """
    from notifications_publisher import handler

    user_id = "user-recipient-fr-accepted"
    record = _make_record(
        "INSERT",
        new_image=_notification_image_with_payload(
            user_id=user_id,
            notification_type="friend_request_accepted",
        ),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_called_once()
    mutation_name, payload = mock_publish.call_args.args
    assert mutation_name == "_publishFriendRequestUpdated", (
        f"expected '_publishFriendRequestUpdated', got {mutation_name!r}"
    )


def test_given_insert_friend_request_accepted_when_processed_then_payload_contains_user_id(
    mock_publish,
):
    """
    AC-H: _publishFriendRequestUpdated payload must include user_id from row PK.
    """
    from notifications_publisher import handler

    user_id = "user-recipient-fr-accepted-userid"
    record = _make_record(
        "INSERT",
        new_image=_notification_image_with_payload(
            user_id=user_id,
            notification_type="friend_request_accepted",
        ),
    )
    handler.handler({"Records": [record]}, None)

    _, payload = mock_publish.call_args.args
    assert payload.get("userId") == user_id, (
        f"payload['userId'] must equal PK user_id={user_id!r}, got {payload.get('userId')!r}"
    )


# ---------------------------------------------------------------------------
# Test E: INSERT unknown type → log warning, no publish call
# ---------------------------------------------------------------------------


def test_given_insert_unknown_type_when_processed_then_no_publish_called(mock_publish):
    """
    AC-E: INSERT record with unknown type must NOT call any publish mutation.
    Handler logs a warning but does not fail the batch.
    """
    from notifications_publisher import handler

    record = _make_record(
        "INSERT",
        new_image=_notification_image(
            user_id="user-recipient-unknown",
            notification_type="some_future_type_not_yet_defined",
        ),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test F: MODIFY events are ignored
# ---------------------------------------------------------------------------


def test_given_modify_event_when_processed_then_no_publish_called(mock_publish):
    """
    AC-F: MODIFY events (e.g. delivered=true update by PushFanout 8.10) must
    NOT trigger any publish call — publisher only acts on INSERT.
    """
    from notifications_publisher import handler

    img = _notification_image(user_id="user-recipient-modify")
    record = _make_record(
        "MODIFY",
        old_image=img,
        new_image={**img, "delivered": _ddb_bool(True)},
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test G: REMOVE events are ignored
# ---------------------------------------------------------------------------


def test_given_remove_event_when_processed_then_no_publish_called(mock_publish):
    """
    AC-G: REMOVE events must be ignored — no publish, no crash.
    """
    from notifications_publisher import handler

    record = _make_record(
        "REMOVE",
        old_image=_notification_image(user_id="user-recipient-remove"),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test I: batch with mixed records dispatches correctly
# ---------------------------------------------------------------------------


def test_given_batch_with_mixed_records_when_processed_then_dispatched_by_type(
    mock_publish,
):
    """
    AC-I: batch of 4 records — 1 friend_request_received (INSERT), 1 bookmark
    (INSERT), 1 friend_request_accepted (INSERT), 1 MODIFY (delivered update) —
    should result in exactly 2 publishNotification calls and 1
    _publishFriendRequestUpdated call (MODIFY ignored).
    """
    from notifications_publisher import handler

    records = [
        _make_record(
            "INSERT",
            new_image=_notification_image(
                user_id="user-a",
                notification_type="friend_request_received",
            ),
        ),
        _make_record(
            "INSERT",
            new_image=_notification_image(
                user_id="user-b",
                notification_type="bookmark",
            ),
        ),
        _make_record(
            "INSERT",
            new_image=_notification_image_with_payload(
                user_id="user-c",
                notification_type="friend_request_accepted",
            ),
        ),
        _make_record(
            "MODIFY",
            old_image=_notification_image(user_id="user-d"),
            new_image={
                **_notification_image(user_id="user-d"),
                "delivered": _ddb_bool(True),
            },
        ),
    ]

    handler.handler({"Records": records}, None)

    assert mock_publish.call_count == 3
    call_mutations = [c.args[0] for c in mock_publish.call_args_list]
    assert call_mutations.count("publishNotification") == 2
    assert call_mutations.count("_publishFriendRequestUpdated") == 1


# ---------------------------------------------------------------------------
# Test J: missing NewImage handled safely
# ---------------------------------------------------------------------------


def test_given_insert_with_no_new_image_when_processed_then_no_publish_and_no_crash(
    mock_publish,
):
    """
    AC-J: an INSERT record missing NewImage must not crash and must not publish.
    """
    from notifications_publisher import handler

    record = _make_record("INSERT")  # no new_image
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test K: _build_notification_payload — pure function, camelCase conversion
# ---------------------------------------------------------------------------


def test_build_notification_payload_returns_camelcase_fields():
    """
    AC-K: _build_notification_payload converts a DynamoDB NewImage dict into
    a plain Python dict with camelCase keys matching the GraphQL Notification type.
    user_id must be present as userId.
    """
    from notifications_publisher.handler import _build_notification_payload

    new_image = {
        "user_id": _ddb_str("user-recipient-xyz"),
        "created_at_notification_id": _ddb_str("2026-06-18T10:00:00.000000#notif-001"),
        "notification_id": _ddb_str("notif-001"),
        "type": _ddb_str("friend_request_received"),
        "sender_user_id": _ddb_str("user-sender-abc"),
        "sender_name": _ddb_str("Bob"),
        "sender_avatar": _ddb_str("https://example.com/bob.jpg"),
        "read": _ddb_bool(False),
        "delivered": _ddb_bool(False),
    }

    payload = _build_notification_payload(new_image)

    assert payload["userId"] == "user-recipient-xyz"
    assert payload["notificationId"] == "notif-001"
    assert payload["type"] == "friend_request_received"
    assert payload["senderUserId"] == "user-sender-abc"
    assert payload["senderName"] == "Bob"
    assert payload["senderAvatar"] == "https://example.com/bob.jpg"
    assert payload["read"] is False
    assert payload["delivered"] is False


def test_build_notification_payload_optional_payload_field_preserved():
    """
    _build_notification_payload must include the optional 'payload' JSON string
    when present in the image (used by friend_request_accepted rows).
    """
    from notifications_publisher.handler import _build_notification_payload

    new_image = _notification_image_with_payload(
        user_id="user-recipient",
        notification_type="friend_request_accepted",
        extra_payload='{"friendId":"user-xyz"}',
    )

    result = _build_notification_payload(new_image)

    assert result.get("payload") == '{"friendId":"user-xyz"}'
