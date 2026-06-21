"""
Unit tests for story 9.7 — delete_dynamodb_personal_data Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. Lambda queries Notifications (PK=user_id) and BatchWriteItem-deletes all
     rows in batches of at most 25.
  B. Lambda queries PushNotificationTokens (PK=user_id) and BatchWriteItem-deletes
     all rows in batches of at most 25.
  C. Pagination: when Query returns a LastEvaluatedKey, the Lambda continues
     fetching pages until exhausted (simple in-Lambda loop for both tables).
  D. Idempotent: re-running when the result set is empty returns success with
     zero rows deleted for both tables.
  E. The response payload is {notifications_deleted: <n>, push_tokens_deleted: <n>}.
  F. 30 notifications (>25) — BatchWriteItem is called with at least 2 batches for
     notifications (25-item cap honoured).
  G. Integration scenario (acceptance criterion): seed 30 notifications and 2 push
     tokens, invoke, assert zero remain (simulated by checking delete counts).
  H. Query uses correct PK=user_id KeyConditionExpression for both tables.
  I. BatchWriteItem delete keys for Notifications include the correct SK
     (created_at_notification_id) per row.
  J. BatchWriteItem delete keys for PushNotificationTokens include the correct SK
     (device_id) per row.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Environment variables required before module import
# ---------------------------------------------------------------------------

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")
os.environ.setdefault("TABLE_NOTIFICATIONS", "Notifications")
os.environ.setdefault("TABLE_PUSH_NOTIFICATION_TOKENS", "PushNotificationTokens")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-del-001"

_NOTIF_1 = "2024-01-01T00:00:00.000Z#notif-001"
_NOTIF_2 = "2024-01-02T00:00:00.000Z#notif-002"

_DEVICE_1 = "device-aaa"
_DEVICE_2 = "device-bbb"


def _make_event(user_id: str = _USER_ID) -> dict:
    """Build a minimal Lambda input event."""
    return {"user_id": user_id}


def _make_notification_item(user_id: str, sort_key: str) -> dict:
    """Build a DynamoDB-style Notifications item (PK=user_id, SK=created_at_notification_id)."""
    return {
        "user_id": {"S": user_id},
        "created_at_notification_id": {"S": sort_key},
    }


def _make_push_token_item(user_id: str, device_id: str) -> dict:
    """Build a DynamoDB-style PushNotificationTokens item (PK=user_id, SK=device_id)."""
    return {
        "user_id": {"S": user_id},
        "device_id": {"S": device_id},
    }


def _make_query_response(items: list[dict], last_evaluated_key: dict | None = None) -> dict:
    """Build a DynamoDB Query response."""
    resp: dict = {"Items": items, "Count": len(items)}
    if last_evaluated_key is not None:
        resp["LastEvaluatedKey"] = last_evaluated_key
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_dynamo():
    """
    Patch handler._get_dynamo so no real AWS call is made.
    Returns the mock DynamoDB client so tests can inspect calls.
    Default: all queries return empty lists (no rows).
    """
    with patch("delete_dynamodb_personal_data.handler._get_dynamo") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # Default: both table queries return empty
        mock_client.query.return_value = _make_query_response([])
        yield mock_client


# ---------------------------------------------------------------------------
# Test D: idempotent — empty result set returns success
# ---------------------------------------------------------------------------


def test_given_user_with_no_notifications_and_no_tokens_when_handler_called_then_returns_zero_counts(
    mock_dynamo,
):
    """
    AC-D: user has no Notifications and no PushNotificationTokens rows.
    Lambda returns {notifications_deleted: 0, push_tokens_deleted: 0}
    and makes zero BatchWriteItem calls.
    """
    from delete_dynamodb_personal_data import handler

    result = handler.handler(_make_event(), None)

    assert result["notifications_deleted"] == 0
    assert result["push_tokens_deleted"] == 0
    mock_dynamo.batch_write_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test E: response payload shape
# ---------------------------------------------------------------------------


def test_given_user_with_two_notifications_and_two_tokens_when_handler_called_then_response_has_correct_counts(
    mock_dynamo,
):
    """
    AC-E: response must be {notifications_deleted: <n>, push_tokens_deleted: <n>}.
    """
    from delete_dynamodb_personal_data import handler

    # Query returns different pages for each table call — side_effect cycles
    mock_dynamo.query.side_effect = [
        # Notifications query
        _make_query_response([
            _make_notification_item(_USER_ID, _NOTIF_1),
            _make_notification_item(_USER_ID, _NOTIF_2),
        ]),
        # PushNotificationTokens query
        _make_query_response([
            _make_push_token_item(_USER_ID, _DEVICE_1),
            _make_push_token_item(_USER_ID, _DEVICE_2),
        ]),
    ]

    result = handler.handler(_make_event(), None)

    assert result["notifications_deleted"] == 2
    assert result["push_tokens_deleted"] == 2


# ---------------------------------------------------------------------------
# Test A: Notifications BatchWriteItem-deletes in ≤25 batches
# ---------------------------------------------------------------------------


def test_given_user_with_two_notifications_when_handler_called_then_batch_write_deletes_notifications(
    mock_dynamo,
):
    """
    AC-A: BatchWriteItem delete requests are issued for Notifications rows.
    The delete keys include the correct PK (user_id) and SK (created_at_notification_id).
    """
    from delete_dynamodb_personal_data import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([
            _make_notification_item(_USER_ID, _NOTIF_1),
            _make_notification_item(_USER_ID, _NOTIF_2),
        ]),
        _make_query_response([]),  # PushNotificationTokens — empty
    ]

    handler.handler(_make_event(), None)

    # Collect all delete requests across all batch_write_item calls
    all_delete_keys: list[dict] = []
    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or (c.args[0] if c.args else {}).get("RequestItems", {})
        for table, writes in req_items.items():
            if table == "Notifications":
                for w in writes:
                    all_delete_keys.append(w["DeleteRequest"]["Key"])

    # Both notifications should appear as delete keys
    sort_keys_deleted = {k["created_at_notification_id"]["S"] for k in all_delete_keys}
    assert _NOTIF_1 in sort_keys_deleted
    assert _NOTIF_2 in sort_keys_deleted


# ---------------------------------------------------------------------------
# Test B: PushNotificationTokens BatchWriteItem-deletes
# ---------------------------------------------------------------------------


def test_given_user_with_two_push_tokens_when_handler_called_then_batch_write_deletes_tokens(
    mock_dynamo,
):
    """
    AC-B: BatchWriteItem delete requests are issued for PushNotificationTokens rows.
    The delete keys include the correct PK (user_id) and SK (device_id).
    """
    from delete_dynamodb_personal_data import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([]),  # Notifications — empty
        _make_query_response([
            _make_push_token_item(_USER_ID, _DEVICE_1),
            _make_push_token_item(_USER_ID, _DEVICE_2),
        ]),
    ]

    handler.handler(_make_event(), None)

    all_delete_keys: list[dict] = []
    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or (c.args[0] if c.args else {}).get("RequestItems", {})
        for table, writes in req_items.items():
            if table == "PushNotificationTokens":
                for w in writes:
                    all_delete_keys.append(w["DeleteRequest"]["Key"])

    device_ids_deleted = {k["device_id"]["S"] for k in all_delete_keys}
    assert _DEVICE_1 in device_ids_deleted
    assert _DEVICE_2 in device_ids_deleted


# ---------------------------------------------------------------------------
# Test F: 30 notifications → multiple batches (25-item cap)
# ---------------------------------------------------------------------------


def test_given_thirty_notifications_when_handler_called_then_batch_write_uses_multiple_batches(
    mock_dynamo,
):
    """
    AC-F: DynamoDB BatchWriteItem accepts at most 25 requests per call.
    30 notifications must be split into at least 2 batch calls.
    """
    from delete_dynamodb_personal_data import handler

    notifs = [
        _make_notification_item(_USER_ID, f"2024-01-{i+1:02d}T00:00:00Z#notif-{i+1:03d}")
        for i in range(30)
    ]

    mock_dynamo.query.side_effect = [
        _make_query_response(notifs),  # Notifications — 30 items
        _make_query_response([]),      # PushNotificationTokens — empty
    ]

    result = handler.handler(_make_event(), None)

    assert result["notifications_deleted"] == 30

    # Count BatchWriteItem calls targeting Notifications only
    notif_batch_calls = 0
    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or (c.args[0] if c.args else {}).get("RequestItems", {})
        if "Notifications" in req_items:
            notif_batch_calls += 1
            # Each individual call must not exceed 25 items
            assert len(req_items["Notifications"]) <= 25

    assert notif_batch_calls >= 2, "30 notifications must require at least 2 batch calls"


# ---------------------------------------------------------------------------
# Test C: pagination — multi-page Query for Notifications
# ---------------------------------------------------------------------------


def test_given_notifications_query_returns_two_pages_when_handler_called_then_both_pages_deleted(
    mock_dynamo,
):
    """
    AC-C: when Query returns a LastEvaluatedKey, the Lambda fetches the next page
    using ExclusiveStartKey and deletes all rows across pages.
    """
    from delete_dynamodb_personal_data import handler

    page1_key = {"user_id": {"S": _USER_ID}, "created_at_notification_id": {"S": _NOTIF_1}}

    mock_dynamo.query.side_effect = [
        # Notifications — page 1
        _make_query_response(
            [_make_notification_item(_USER_ID, _NOTIF_1)],
            last_evaluated_key=page1_key,
        ),
        # Notifications — page 2
        _make_query_response(
            [_make_notification_item(_USER_ID, _NOTIF_2)],
        ),
        # PushNotificationTokens — empty
        _make_query_response([]),
    ]

    result = handler.handler(_make_event(), None)

    assert result["notifications_deleted"] == 2


def test_given_notifications_query_returns_two_pages_when_handler_called_then_second_query_uses_exclusive_start_key(
    mock_dynamo,
):
    """
    AC-C: the second Notifications Query call must include ExclusiveStartKey from
    the first page's LastEvaluatedKey.
    """
    from delete_dynamodb_personal_data import handler

    page1_key = {"user_id": {"S": _USER_ID}, "created_at_notification_id": {"S": _NOTIF_1}}

    mock_dynamo.query.side_effect = [
        _make_query_response(
            [_make_notification_item(_USER_ID, _NOTIF_1)],
            last_evaluated_key=page1_key,
        ),
        _make_query_response([]),  # end of Notifications
        _make_query_response([]),  # PushNotificationTokens
    ]

    handler.handler(_make_event(), None)

    # The second query call (index 1) should carry ExclusiveStartKey
    second_call_kwargs = mock_dynamo.query.call_args_list[1].kwargs
    assert "ExclusiveStartKey" in second_call_kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == page1_key


# ---------------------------------------------------------------------------
# Test H: Query uses correct KeyConditionExpression
# ---------------------------------------------------------------------------


def test_given_handler_called_when_querying_notifications_then_uses_user_id_as_pk(
    mock_dynamo,
):
    """
    AC-H: Query on Notifications uses PK=user_id (KeyConditionExpression).
    """
    from delete_dynamodb_personal_data import handler

    mock_dynamo.query.return_value = _make_query_response([])

    handler.handler(_make_event(user_id="user-xyz-999"), None)

    # First query should be for Notifications
    first_call = mock_dynamo.query.call_args_list[0]
    assert first_call.kwargs["TableName"] == "Notifications"
    expr_attr_vals = first_call.kwargs["ExpressionAttributeValues"]
    user_id_val = next(
        (v["S"] for v in expr_attr_vals.values() if v.get("S") == "user-xyz-999"),
        None,
    )
    assert user_id_val == "user-xyz-999"


def test_given_handler_called_when_querying_push_tokens_then_uses_user_id_as_pk(
    mock_dynamo,
):
    """
    AC-H: Query on PushNotificationTokens uses PK=user_id (KeyConditionExpression).
    """
    from delete_dynamodb_personal_data import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([]),  # Notifications
        _make_query_response([]),  # PushNotificationTokens
    ]

    handler.handler(_make_event(user_id="user-xyz-888"), None)

    # Second query should be for PushNotificationTokens
    second_call = mock_dynamo.query.call_args_list[1]
    assert second_call.kwargs["TableName"] == "PushNotificationTokens"
    expr_attr_vals = second_call.kwargs["ExpressionAttributeValues"]
    user_id_val = next(
        (v["S"] for v in expr_attr_vals.values() if v.get("S") == "user-xyz-888"),
        None,
    )
    assert user_id_val == "user-xyz-888"


# ---------------------------------------------------------------------------
# Test I: Notifications delete keys include correct SK
# ---------------------------------------------------------------------------


def test_given_notification_row_when_deleted_then_delete_key_includes_created_at_notification_id(
    mock_dynamo,
):
    """
    AC-I: the DeleteRequest Key for a Notifications row must include both
    user_id (PK) and created_at_notification_id (SK).
    """
    from delete_dynamodb_personal_data import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([_make_notification_item(_USER_ID, _NOTIF_1)]),
        _make_query_response([]),
    ]

    handler.handler(_make_event(), None)

    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or (c.args[0] if c.args else {}).get("RequestItems", {})
        for table, writes in req_items.items():
            if table == "Notifications":
                for w in writes:
                    key = w["DeleteRequest"]["Key"]
                    assert "user_id" in key
                    assert "created_at_notification_id" in key


# ---------------------------------------------------------------------------
# Test J: PushNotificationTokens delete keys include correct SK
# ---------------------------------------------------------------------------


def test_given_push_token_row_when_deleted_then_delete_key_includes_device_id(
    mock_dynamo,
):
    """
    AC-J: the DeleteRequest Key for a PushNotificationTokens row must include both
    user_id (PK) and device_id (SK).
    """
    from delete_dynamodb_personal_data import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([]),
        _make_query_response([_make_push_token_item(_USER_ID, _DEVICE_1)]),
    ]

    handler.handler(_make_event(), None)

    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or (c.args[0] if c.args else {}).get("RequestItems", {})
        for table, writes in req_items.items():
            if table == "PushNotificationTokens":
                for w in writes:
                    key = w["DeleteRequest"]["Key"]
                    assert "user_id" in key
                    assert "device_id" in key


# ---------------------------------------------------------------------------
# Test G: integration scenario — 30 notifications + 2 tokens → all deleted
# ---------------------------------------------------------------------------


def test_given_thirty_notifications_and_two_push_tokens_when_handler_called_then_all_rows_deleted(
    mock_dynamo,
):
    """
    AC (integration): seed 30 notifications and 2 push tokens — invoke, assert
    the response reports zero remaining (notifications_deleted=30, push_tokens_deleted=2).
    All delete requests are issued for both tables.
    """
    from delete_dynamodb_personal_data import handler

    notifs = [
        _make_notification_item(_USER_ID, f"2024-01-{i+1:02d}T00:00:00Z#notif-{i+1:03d}")
        for i in range(30)
    ]
    tokens = [
        _make_push_token_item(_USER_ID, f"device-{i+1:03d}")
        for i in range(2)
    ]

    mock_dynamo.query.side_effect = [
        _make_query_response(notifs),
        _make_query_response(tokens),
    ]

    result = handler.handler(_make_event(), None)

    assert result["notifications_deleted"] == 30
    assert result["push_tokens_deleted"] == 2

    # Verify all notification sort keys appear in delete calls
    deleted_notif_sks: set[str] = set()
    deleted_token_device_ids: set[str] = set()

    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or (c.args[0] if c.args else {}).get("RequestItems", {})
        for table, writes in req_items.items():
            for w in writes:
                key = w["DeleteRequest"]["Key"]
                if table == "Notifications":
                    deleted_notif_sks.add(key["created_at_notification_id"]["S"])
                elif table == "PushNotificationTokens":
                    deleted_token_device_ids.add(key["device_id"]["S"])

    assert len(deleted_notif_sks) == 30
    assert len(deleted_token_device_ids) == 2
