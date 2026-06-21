"""
Unit tests for story 9.4 — deactivate_chat_rooms Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. Lambda queries ChatRoomMembership PK=user_id and collects all room_ids.
  B. For each room_id collected, UpdateItem is called on ChatRooms with
     status='deactivated', deactivated_reason='user_deleted_account',
     and deactivated_at set to an ISO-8601 UTC timestamp.
  C. The UpdateItem condition skips rooms already deactivated (idempotent):
     ConditionalCheckFailedException is treated as success/no-op.
  D. After ChatRooms UpdateItem calls, BatchWriteItem-deletes the user's own
     ChatRoomMembership rows (PK=user_id, SK=room_id) for each room collected.
  E. Surviving participants' ChatRoomMembership rows are NOT touched (only the
     deleted user's rows are removed — BatchWriteItem is keyed by user_id).
  F. The Lambda returns {room_ids: [list of room_ids]} so the Step Functions
     state machine can inject them into downstream tasks.
  G. Empty-room case: user with zero ChatRoomMembership rows returns
     {room_ids: []} and makes zero DynamoDB calls beyond the initial Query.
  H. Pagination: Query uses ExclusiveStartKey when LastEvaluatedKey is present;
     all pages are collected before proceeding.
  I. Integration-scenario: 3 rooms, one already deactivated (blocked).
     All 3 ChatRooms rows receive UpdateItem calls; the one with a
     ConditionalCheckFailedException (already deactivated) is skipped silently;
     all 3 user membership rows are deleted; response contains all 3 room_ids.
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
os.environ.setdefault("TABLE_CHAT_ROOMS", "ChatRooms")
os.environ.setdefault("TABLE_CHAT_ROOM_MEMBERSHIP", "ChatRoomMembership")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-abc-001"
_ROOM_1 = "room-aaa"
_ROOM_2 = "room-bbb"
_ROOM_3 = "room-ccc"


def _make_event(user_id: str = _USER_ID) -> dict:
    """Build a minimal Lambda input event."""
    return {"user_id": user_id}


def _make_membership_item(user_id: str, room_id: str) -> dict:
    """Build a DynamoDB-style ChatRoomMembership item."""
    return {
        "user_id": {"S": user_id},
        "room_id": {"S": room_id},
    }


def _make_query_response(items: list[dict], last_evaluated_key: dict | None = None) -> dict:
    """Build a DynamoDB Query response."""
    resp: dict = {"Items": items, "Count": len(items)}
    if last_evaluated_key is not None:
        resp["LastEvaluatedKey"] = last_evaluated_key
    return resp


def _make_client_error(code: str) -> Any:
    """Return a botocore ClientError with the given error code."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "Test error"}},
        "UpdateItem",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_dynamo():
    """
    Patch handler._get_dynamo so no real AWS call is made.
    Returns the mock DynamoDB client so tests can inspect calls.
    """
    with patch("deactivate_chat_rooms.handler._get_dynamo") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # Default: query returns empty list (no membership rows)
        mock_client.query.return_value = _make_query_response([])
        yield mock_client


# ---------------------------------------------------------------------------
# Test A + B: happy path — user has rooms, UpdateItem called for each
# ---------------------------------------------------------------------------


def test_given_user_with_two_rooms_when_handler_called_then_update_item_called_for_each(
    mock_dynamo,
):
    """
    AC-A + AC-B: for each room_id from the Query, UpdateItem is called on ChatRooms
    with status=deactivated, deactivated_reason=user_deleted_account, deactivated_at set.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
        _make_membership_item(_USER_ID, _ROOM_2),
    ])

    handler.handler(_make_event(), None)

    assert mock_dynamo.update_item.call_count == 2

    # Verify each call targets the ChatRooms table with the correct room_id key
    update_calls = mock_dynamo.update_item.call_args_list
    room_ids_updated = {
        c.kwargs["Key"]["room_id"]["S"]
        for c in update_calls
    }
    assert room_ids_updated == {_ROOM_1, _ROOM_2}

    # Verify the UpdateExpression sets status, deactivated_reason, deactivated_at
    for c in update_calls:
        update_expr: str = c.kwargs["UpdateExpression"]
        assert "deactivated_reason" in update_expr
        assert "deactivated_at" in update_expr
        expr_attr_vals: dict = c.kwargs["ExpressionAttributeValues"]
        assert ":deactivated" in expr_attr_vals
        assert expr_attr_vals[":reason"]["S"] == "user_deleted_account"


def test_given_user_with_two_rooms_when_handler_called_then_update_item_uses_condition(
    mock_dynamo,
):
    """
    AC-C: UpdateItem uses a ConditionExpression that prevents overwriting already-deactivated rooms.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
    ])

    handler.handler(_make_event(), None)

    update_call = mock_dynamo.update_item.call_args_list[0]
    assert "ConditionExpression" in update_call.kwargs


# ---------------------------------------------------------------------------
# Test C: idempotency — ConditionalCheckFailedException is a no-op
# ---------------------------------------------------------------------------


def test_given_room_already_deactivated_when_update_raises_conditional_check_then_success(
    mock_dynamo,
):
    """
    AC-C: ConditionalCheckFailedException from UpdateItem (room already deactivated)
    is treated as success — the Lambda does not raise.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
    ])
    mock_dynamo.update_item.side_effect = _make_client_error("ConditionalCheckFailedException")

    result = handler.handler(_make_event(), None)

    assert result["room_ids"] == [_ROOM_1]


def test_given_unexpected_dynamo_error_on_update_when_handler_called_then_raises(
    mock_dynamo,
):
    """
    Non-ConditionalCheckFailedException errors are re-raised (not swallowed).
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
    ])
    mock_dynamo.update_item.side_effect = _make_client_error("ProvisionedThroughputExceededException")

    with pytest.raises(Exception):
        handler.handler(_make_event(), None)


# ---------------------------------------------------------------------------
# Test D: BatchWriteItem deletes user's ChatRoomMembership rows
# ---------------------------------------------------------------------------


def test_given_user_with_two_rooms_when_handler_called_then_batch_write_deletes_membership_rows(
    mock_dynamo,
):
    """
    AC-D: after ChatRooms UpdateItem calls, BatchWriteItem is called to delete the user's
    ChatRoomMembership rows (PK=user_id, SK=room_id for each room).
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
        _make_membership_item(_USER_ID, _ROOM_2),
    ])

    handler.handler(_make_event(), None)

    assert mock_dynamo.batch_write_item.call_count >= 1

    # Gather all delete requests from all BatchWriteItem calls
    delete_keys = set()
    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or c.args[0].get("RequestItems", {})
        for table, writes in req_items.items():
            for w in writes:
                dr = w.get("DeleteRequest", {})
                key = dr.get("Key", {})
                uid = key.get("user_id", {}).get("S")
                rid = key.get("room_id", {}).get("S")
                if uid and rid:
                    delete_keys.add((uid, rid))

    assert (_USER_ID, _ROOM_1) in delete_keys
    assert (_USER_ID, _ROOM_2) in delete_keys


def test_given_user_with_two_rooms_when_handler_called_then_batch_write_only_deletes_user_rows(
    mock_dynamo,
):
    """
    AC-E: BatchWriteItem delete keys use only the deleted user's user_id — no other
    user_ids appear in the delete requests.
    """
    from deactivate_chat_rooms import handler

    other_user = "other-user-999"
    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
        _make_membership_item(_USER_ID, _ROOM_2),
    ])

    handler.handler(_make_event(), None)

    # No delete key should reference other_user
    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or c.args[0].get("RequestItems", {})
        for table, writes in req_items.items():
            for w in writes:
                dr = w.get("DeleteRequest", {})
                key = dr.get("Key", {})
                uid = key.get("user_id", {}).get("S")
                assert uid != other_user, f"BatchWriteItem must not delete {other_user}'s rows"


# ---------------------------------------------------------------------------
# Test F: response payload contains room_ids
# ---------------------------------------------------------------------------


def test_given_user_with_two_rooms_when_handler_called_then_response_contains_room_ids(
    mock_dynamo,
):
    """
    AC-F: the Lambda response must be {room_ids: [list of room_ids]} so the
    state machine Parameters block can inject them into downstream tasks.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
        _make_membership_item(_USER_ID, _ROOM_2),
    ])

    result = handler.handler(_make_event(), None)

    assert "room_ids" in result
    assert set(result["room_ids"]) == {_ROOM_1, _ROOM_2}


def test_given_user_with_two_rooms_when_handler_called_then_response_room_ids_are_strings(
    mock_dynamo,
):
    """
    room_ids in the response must be plain strings (not DynamoDB-typed dicts).
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
    ])

    result = handler.handler(_make_event(), None)

    for rid in result["room_ids"]:
        assert isinstance(rid, str), f"Expected str, got {type(rid)}"


# ---------------------------------------------------------------------------
# Test G: empty room case
# ---------------------------------------------------------------------------


def test_given_user_with_zero_rooms_when_handler_called_then_returns_empty_room_ids(
    mock_dynamo,
):
    """
    AC-G: user with no ChatRoomMembership rows returns {room_ids: []} without
    making any UpdateItem or BatchWriteItem calls.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(), None)

    assert result == {"room_ids": []}
    mock_dynamo.update_item.assert_not_called()
    mock_dynamo.batch_write_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test H: pagination — multiple Query pages are consumed
# ---------------------------------------------------------------------------


def test_given_query_returns_two_pages_when_handler_called_then_both_pages_collected(
    mock_dynamo,
):
    """
    AC-H: when Query returns a LastEvaluatedKey, the Lambda fetches the next
    page using ExclusiveStartKey and collects all room_ids before proceeding.
    """
    from deactivate_chat_rooms import handler

    page1_key = {"user_id": {"S": _USER_ID}, "room_id": {"S": _ROOM_1}}
    mock_dynamo.query.side_effect = [
        _make_query_response(
            [_make_membership_item(_USER_ID, _ROOM_1)],
            last_evaluated_key=page1_key,
        ),
        _make_query_response(
            [_make_membership_item(_USER_ID, _ROOM_2)],
            last_evaluated_key=None,
        ),
    ]

    result = handler.handler(_make_event(), None)

    assert mock_dynamo.query.call_count == 2
    assert set(result["room_ids"]) == {_ROOM_1, _ROOM_2}


def test_given_query_returns_two_pages_when_handler_called_then_second_query_uses_exclusive_start_key(
    mock_dynamo,
):
    """
    AC-H: the second Query call includes ExclusiveStartKey from the first page's
    LastEvaluatedKey.
    """
    from deactivate_chat_rooms import handler

    page1_key = {"user_id": {"S": _USER_ID}, "room_id": {"S": _ROOM_1}}
    mock_dynamo.query.side_effect = [
        _make_query_response(
            [_make_membership_item(_USER_ID, _ROOM_1)],
            last_evaluated_key=page1_key,
        ),
        _make_query_response([], last_evaluated_key=None),
    ]

    handler.handler(_make_event(), None)

    second_call_kwargs = mock_dynamo.query.call_args_list[1].kwargs
    assert "ExclusiveStartKey" in second_call_kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == page1_key


# ---------------------------------------------------------------------------
# Test I: integration scenario — 3 rooms, one already deactivated
# ---------------------------------------------------------------------------


def test_given_three_rooms_one_already_deactivated_when_handler_called_then_all_three_room_ids_returned(
    mock_dynamo,
):
    """
    AC-I: user with 3 rooms where one is already deactivated (ConditionalCheckFailedException).
    All 3 UpdateItem calls are made; the failing one is silently skipped.
    All 3 membership rows are deleted. Response contains all 3 room_ids.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
        _make_membership_item(_USER_ID, _ROOM_2),
        _make_membership_item(_USER_ID, _ROOM_3),
    ])

    # ROOM_2 is already deactivated (blocked); UpdateItem raises ConditionalCheckFailedException
    def update_item_side_effect(**kwargs):
        if kwargs["Key"]["room_id"]["S"] == _ROOM_2:
            raise _make_client_error("ConditionalCheckFailedException")

    mock_dynamo.update_item.side_effect = update_item_side_effect

    result = handler.handler(_make_event(), None)

    # All 3 UpdateItem calls were attempted
    assert mock_dynamo.update_item.call_count == 3

    # All 3 room_ids appear in the response
    assert set(result["room_ids"]) == {_ROOM_1, _ROOM_2, _ROOM_3}

    # All 3 membership rows are deleted
    delete_keys = set()
    for c in mock_dynamo.batch_write_item.call_args_list:
        req_items = c.kwargs.get("RequestItems") or c.args[0].get("RequestItems", {})
        for table, writes in req_items.items():
            for w in writes:
                dr = w.get("DeleteRequest", {})
                key = dr.get("Key", {})
                uid = key.get("user_id", {}).get("S")
                rid = key.get("room_id", {}).get("S")
                if uid and rid:
                    delete_keys.add((uid, rid))

    assert (_USER_ID, _ROOM_1) in delete_keys
    assert (_USER_ID, _ROOM_2) in delete_keys
    assert (_USER_ID, _ROOM_3) in delete_keys


def test_given_three_rooms_one_already_deactivated_when_handler_called_then_lambda_does_not_raise(
    mock_dynamo,
):
    """
    AC-I / AC-C: ConditionalCheckFailedException must not propagate — Lambda returns success.
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, _ROOM_1),
        _make_membership_item(_USER_ID, _ROOM_2),
    ])
    mock_dynamo.update_item.side_effect = _make_client_error("ConditionalCheckFailedException")

    result = handler.handler(_make_event(), None)

    assert "room_ids" in result


# ---------------------------------------------------------------------------
# Test: BatchWriteItem respects DynamoDB 25-item limit per batch
# ---------------------------------------------------------------------------


def test_given_thirty_rooms_when_handler_called_then_batch_write_uses_multiple_batches(
    mock_dynamo,
):
    """
    DynamoDB BatchWriteItem accepts at most 25 requests per call.
    When a user has 30 rooms, the Lambda must split into at least 2 batches.
    """
    from deactivate_chat_rooms import handler

    rooms = [f"room-{i:03d}" for i in range(30)]
    mock_dynamo.query.return_value = _make_query_response([
        _make_membership_item(_USER_ID, rid) for rid in rooms
    ])

    result = handler.handler(_make_event(), None)

    # With 30 items and a 25-item cap, we need at least 2 batch calls
    assert mock_dynamo.batch_write_item.call_count >= 2
    assert len(result["room_ids"]) == 30


# ---------------------------------------------------------------------------
# Test: Query uses correct KeyConditionExpression on ChatRoomMembership
# ---------------------------------------------------------------------------


def test_given_handler_called_when_querying_membership_then_uses_user_id_as_pk(
    mock_dynamo,
):
    """
    AC-A: Query is issued on ChatRoomMembership with PK=user_id (KeyConditionExpression).
    """
    from deactivate_chat_rooms import handler

    mock_dynamo.query.return_value = _make_query_response([])

    handler.handler(_make_event(user_id="user-xyz-999"), None)

    query_call = mock_dynamo.query.call_args_list[0]
    assert query_call.kwargs["TableName"] == "ChatRoomMembership"
    expr_attr_vals = query_call.kwargs["ExpressionAttributeValues"]
    # The user_id value must appear in the expression attribute values
    user_id_val = next(
        (v["S"] for v in expr_attr_vals.values() if v.get("S") == "user-xyz-999"),
        None,
    )
    assert user_id_val == "user-xyz-999", "Query ExpressionAttributeValues must include the user_id"
