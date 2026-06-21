"""
Unit tests for story 9.12 — hard_delete_user_chat_messages Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. Lambda receives {user_id, room_ids: [...]} as input and for each room_id
     Queries ChatMessages by room_id with a FilterExpression on sender_id,
     then DeleteItem each matching row. No new GSI — Query uses the PK (room_id).
  B. The Lambda does NOT Query ChatRoomMembership. room_ids is the authoritative
     input list (9.4 has already deleted membership rows by the time this runs).
  C. Continuation token contract: handler accepts {room_ids, current_room_index,
     last_evaluated_key} and returns {has_more, room_ids, current_room_index,
     last_evaluated_key}. On completion has_more=False.
  D. Empty input: {user_id, room_ids: []} returns has_more=False immediately with
     zero DynamoDB calls.
  E. Pagination within a room: when Query returns a LastEvaluatedKey for a room,
     the Lambda processes that page fully before checking for continuation.
     When a room's messages are exhausted, current_room_index advances by 1
     with last_evaluated_key=None.
  F. Cross-room continuation: when re-invoked with a non-zero current_room_index,
     it skips already-processed rooms and resumes from the given index.
  G. DeleteItem is called with room_id (PK) and message_id (SK) from the item —
     NOT UpdateItem.
  H. A user who sent no messages in any room returns has_more=False with zero
     DeleteItem calls (FilterExpression returns empty).
  I. Idempotent: re-invoking after all messages are already deleted (Query returns
     empty) succeeds with has_more=False and zero DeleteItem calls.
  J. has_more=False when all rooms are processed in one invocation.
  K. Skip-gated integration marker test: tagged with
     pytest.mark.integration so it is skipped unless
     KNOTIFY_RUN_INTEGRATION=1 is set (mirrors story 9.6 pattern).
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
os.environ.setdefault("TABLE_CHAT_MESSAGES", "ChatMessages")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_ID = "user-purge-001"
_ROOM_1 = "room-xxx"
_ROOM_2 = "room-yyy"
_ROOM_3 = "room-zzz"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    user_id: str = _USER_ID,
    room_ids: list[str] | None = None,
    current_room_index: int = 0,
    last_evaluated_key: dict | None = None,
) -> dict:
    """Build a Lambda input event (first call or continuation token call)."""
    ev: dict = {
        "user_id": user_id,
        "room_ids": room_ids if room_ids is not None else [],
        "current_room_index": current_room_index,
    }
    if last_evaluated_key is not None:
        ev["last_evaluated_key"] = last_evaluated_key
    return ev


def _make_message_item(room_id: str, message_id: str, sender_id: str) -> dict:
    """Build a DynamoDB-style ChatMessages item.

    The ChatMessages SK attribute name is `created_at_message_id` per the
    phase-2 DynamoDB module. The Python identifier `message_id` is kept
    for readability; only the DDB attribute key is the real schema.
    """
    return {
        "room_id": {"S": room_id},
        "created_at_message_id": {"S": message_id},
        "sender_id": {"S": sender_id},
        "content": {"S": "some content"},
    }


def _make_query_response(
    items: list[dict],
    last_evaluated_key: dict | None = None,
) -> dict:
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
    """
    with patch("hard_delete_user_chat_messages.handler._get_dynamo") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        # Default: Query returns empty list (no messages)
        mock_client.query.return_value = _make_query_response([])
        yield mock_client


# ---------------------------------------------------------------------------
# Test D: empty input — room_ids=[] exits immediately with has_more=False
# ---------------------------------------------------------------------------


def test_given_empty_room_ids_when_handler_called_then_has_more_false_and_no_dynamo_calls(
    mock_dynamo,
):
    """
    AC-D: {user_id, room_ids: []} returns has_more=False immediately.
    No DynamoDB Query or DeleteItem is made.
    """
    from hard_delete_user_chat_messages import handler

    result = handler.handler(_make_event(room_ids=[]), None)

    assert result["has_more"] is False
    mock_dynamo.query.assert_not_called()
    mock_dynamo.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test A: for each room_id, Query uses room_id as PK with sender_id filter
# ---------------------------------------------------------------------------


def test_given_one_room_with_one_matching_message_when_handler_called_then_query_uses_room_id_pk(
    mock_dynamo,
):
    """
    AC-A: Query is called on ChatMessages with room_id as the PK key condition.
    No GSI — the table's native PK is room_id.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.query.call_count == 1
    call_kwargs = mock_dynamo.query.call_args.kwargs
    assert call_kwargs["TableName"] == "ChatMessages"
    expr_vals = call_kwargs["ExpressionAttributeValues"]
    room_id_val = next(
        (v["S"] for v in expr_vals.values() if v.get("S") == _ROOM_1),
        None,
    )
    assert room_id_val == _ROOM_1, "Query must use room_id as the PK condition value"


def test_given_one_room_with_one_matching_message_when_handler_called_then_query_has_sender_id_filter(
    mock_dynamo,
):
    """
    AC-A: Query includes a FilterExpression on sender_id scoped to the deleted user.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    call_kwargs = mock_dynamo.query.call_args.kwargs
    assert "FilterExpression" in call_kwargs, "Query must include a FilterExpression on sender_id"
    expr_vals = call_kwargs["ExpressionAttributeValues"]
    user_id_val = next(
        (v["S"] for v in expr_vals.values() if v.get("S") == _USER_ID),
        None,
    )
    assert user_id_val == _USER_ID, "FilterExpression must reference the deleted user's user_id"


# ---------------------------------------------------------------------------
# Test G: DeleteItem is called (NOT UpdateItem) with room_id + message_id key
# ---------------------------------------------------------------------------


def test_given_one_room_with_matching_message_when_handler_called_then_delete_item_called_not_update_item(
    mock_dynamo,
):
    """
    AC-G: DeleteItem is called for each matching message — NOT UpdateItem.
    This is the key difference from anonymize_chat_messages (story 9.6).
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.delete_item.call_count == 1
    mock_dynamo.update_item.assert_not_called()


def test_given_matching_message_when_delete_item_called_then_key_includes_room_id_and_message_id(
    mock_dynamo,
):
    """
    AC-G: DeleteItem key includes both room_id (PK) and message_id (SK).
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-xyz", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    delete_kwargs = mock_dynamo.delete_item.call_args.kwargs
    key = delete_kwargs["Key"]
    assert "room_id" in key, "DeleteItem Key must include room_id"
    assert key["room_id"]["S"] == _ROOM_1
    assert "created_at_message_id" in key, "DeleteItem Key must include created_at_message_id (SK)"
    assert key["created_at_message_id"]["S"] == "msg-xyz"


def test_given_matching_message_when_delete_item_called_then_table_name_is_chat_messages(
    mock_dynamo,
):
    """
    AC-G: DeleteItem targets ChatMessages table.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    delete_kwargs = mock_dynamo.delete_item.call_args.kwargs
    assert delete_kwargs["TableName"] == "ChatMessages"


# ---------------------------------------------------------------------------
# Test H: user sent no messages — no DeleteItem calls
# ---------------------------------------------------------------------------


def test_given_room_with_no_matching_messages_when_handler_called_then_no_delete_item_calls(
    mock_dynamo,
):
    """
    AC-H: when the Query FilterExpression matches zero messages, DeleteItem is
    not called. The Lambda returns has_more=False.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    mock_dynamo.delete_item.assert_not_called()
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# Test I: idempotent — re-invocation after all messages already deleted
# ---------------------------------------------------------------------------


def test_given_messages_already_deleted_when_handler_reinvoked_then_succeeds_with_no_delete_calls(
    mock_dynamo,
):
    """
    AC-I: when Query returns empty (all messages already deleted), has_more=False
    and zero DeleteItem calls. Idempotent.
    """
    from hard_delete_user_chat_messages import handler

    # Simulate: query returns empty because messages already deleted
    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(room_ids=[_ROOM_1, _ROOM_2]), None)

    assert result["has_more"] is False
    mock_dynamo.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test C: continuation token — has_more=False on single full pass
# ---------------------------------------------------------------------------


def test_given_two_rooms_fully_processed_when_handler_called_then_has_more_false(
    mock_dynamo,
):
    """
    AC-C: when all rooms are processed in one invocation, has_more=False is returned.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(room_ids=[_ROOM_1, _ROOM_2]), None)

    assert result["has_more"] is False


def test_given_one_room_fully_processed_when_handler_called_then_response_has_room_ids_and_index(
    mock_dynamo,
):
    """
    AC-C: response always includes room_ids and current_room_index (for the token).
    On a complete pass, current_room_index equals len(room_ids) and has_more=False.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert "room_ids" in result
    assert "current_room_index" in result
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# Test E: pagination within a room (LastEvaluatedKey from Query)
# ---------------------------------------------------------------------------


def test_given_room_query_returns_multiple_pages_when_handler_called_then_all_pages_processed(
    mock_dynamo,
):
    """
    AC-E: when Query returns a LastEvaluatedKey for a room, the Lambda fetches
    subsequent pages and calls DeleteItem for messages on each page.
    """
    from hard_delete_user_chat_messages import handler

    page1_lek = {"room_id": {"S": _ROOM_1}, "created_at_message_id": {"S": "msg-001"}}
    mock_dynamo.query.side_effect = [
        _make_query_response(
            [_make_message_item(_ROOM_1, "msg-001", _USER_ID)],
            last_evaluated_key=page1_lek,
        ),
        _make_query_response(
            [_make_message_item(_ROOM_1, "msg-002", _USER_ID)],
        ),
    ]

    result = handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.query.call_count == 2
    assert mock_dynamo.delete_item.call_count == 2
    assert result["has_more"] is False


def test_given_room_query_page2_uses_exclusive_start_key_from_page1(
    mock_dynamo,
):
    """
    AC-E: the second Query call for a room must include ExclusiveStartKey from
    the first page's LastEvaluatedKey.
    """
    from hard_delete_user_chat_messages import handler

    page1_lek = {"room_id": {"S": _ROOM_1}, "created_at_message_id": {"S": "msg-001"}}
    mock_dynamo.query.side_effect = [
        _make_query_response(
            [_make_message_item(_ROOM_1, "msg-001", _USER_ID)],
            last_evaluated_key=page1_lek,
        ),
        _make_query_response([]),
    ]

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    second_call_kwargs = mock_dynamo.query.call_args_list[1].kwargs
    assert "ExclusiveStartKey" in second_call_kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == page1_lek


# ---------------------------------------------------------------------------
# Test F: cross-room continuation — resume from current_room_index
# ---------------------------------------------------------------------------


def test_given_continuation_token_with_nonzero_index_when_handler_called_then_skips_already_processed_rooms(
    mock_dynamo,
):
    """
    AC-F: when re-invoked with current_room_index=1, Query is called only for
    rooms at index >= 1. Room at index 0 is skipped.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(
        _make_event(room_ids=[_ROOM_1, _ROOM_2, _ROOM_3], current_room_index=1),
        None,
    )

    assert mock_dynamo.query.call_count == 2
    queried_rooms = [
        next(
            (v["S"] for v in c.kwargs["ExpressionAttributeValues"].values()
             if v.get("S") in {_ROOM_1, _ROOM_2, _ROOM_3}),
            None,
        )
        for c in mock_dynamo.query.call_args_list
    ]
    assert _ROOM_1 not in queried_rooms, "room_0 must be skipped when current_room_index=1"
    assert result["has_more"] is False


def test_given_re_invocation_with_last_evaluated_key_when_handler_called_then_first_query_uses_it(
    mock_dynamo,
):
    """
    AC-F: when re-invoked with a last_evaluated_key in the token, the first Query
    for the resumed room uses that key as ExclusiveStartKey.
    """
    from hard_delete_user_chat_messages import handler

    prior_lek = {"room_id": {"S": _ROOM_2}, "created_at_message_id": {"S": "msg-050"}}
    mock_dynamo.query.return_value = _make_query_response([])

    handler.handler(
        _make_event(
            room_ids=[_ROOM_1, _ROOM_2, _ROOM_3],
            current_room_index=1,
            last_evaluated_key=prior_lek,
        ),
        None,
    )

    first_call_kwargs = mock_dynamo.query.call_args_list[0].kwargs
    assert first_call_kwargs.get("ExclusiveStartKey") == prior_lek


# ---------------------------------------------------------------------------
# Test J: all rooms processed → has_more=False with correct room index
# ---------------------------------------------------------------------------


def test_given_all_rooms_processed_when_handler_returns_then_has_more_false_and_index_at_end(
    mock_dynamo,
):
    """
    AC-J: when all rooms have been processed, has_more=False and
    current_room_index == len(room_ids).
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(room_ids=[_ROOM_1, _ROOM_2, _ROOM_3]), None)

    assert result["has_more"] is False
    assert result["current_room_index"] == 3


# ---------------------------------------------------------------------------
# Test B: Lambda does NOT query ChatRoomMembership
# ---------------------------------------------------------------------------


def test_given_valid_event_when_handler_called_then_no_query_on_membership_table(
    mock_dynamo,
):
    """
    AC-B: the Lambda must never query ChatRoomMembership — room_ids is the
    authoritative input. All Query calls must target ChatMessages only.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    for c in mock_dynamo.query.call_args_list:
        table = c.kwargs.get("TableName", c.args[0] if c.args else "")
        assert table == "ChatMessages", (
            f"All Query calls must target ChatMessages, not '{table}'"
        )


# ---------------------------------------------------------------------------
# Test: multiple messages in a room — one DeleteItem call per matching row
# ---------------------------------------------------------------------------


def test_given_room_with_three_matching_messages_when_handler_called_then_three_delete_item_calls(
    mock_dynamo,
):
    """
    AC-A: one DeleteItem call per matching message row.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
        _make_message_item(_ROOM_1, "msg-002", _USER_ID),
        _make_message_item(_ROOM_1, "msg-003", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.delete_item.call_count == 3


# ---------------------------------------------------------------------------
# Test: two rooms with messages — Query called once per room
# ---------------------------------------------------------------------------


def test_given_two_rooms_both_with_messages_when_handler_called_then_query_called_for_each_room(
    mock_dynamo,
):
    """
    AC-A: Query is called for each room_id in the input list.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([_make_message_item(_ROOM_1, "msg-001", _USER_ID)]),
        _make_query_response([_make_message_item(_ROOM_2, "msg-002", _USER_ID)]),
    ]

    handler.handler(_make_event(room_ids=[_ROOM_1, _ROOM_2]), None)

    assert mock_dynamo.query.call_count == 2
    assert mock_dynamo.delete_item.call_count == 2


# ---------------------------------------------------------------------------
# Test: DeleteItem key uses room_id and message_id from the Query result item
# ---------------------------------------------------------------------------


def test_given_message_item_with_known_ids_when_delete_item_called_then_key_matches_item_ids(
    mock_dynamo,
):
    """
    Ensures DeleteItem is called with the exact room_id and message_id from the
    Query result item — not hardcoded or incorrectly derived.
    """
    from hard_delete_user_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item("room-specific-001", "msg-specific-999", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=["room-specific-001"]), None)

    delete_kwargs = mock_dynamo.delete_item.call_args.kwargs
    assert delete_kwargs["Key"]["room_id"]["S"] == "room-specific-001"
    assert delete_kwargs["Key"]["created_at_message_id"]["S"] == "msg-specific-999"


# ---------------------------------------------------------------------------
# Skip-gated integration test (AC-K)
#
# Verifies that given 3 seeded messages across 2 rooms, after invocation all 3
# rows have been DeleteItem'd. Skipped unless KNOTIFY_RUN_INTEGRATION=1.
# The full E2E orchestration assertion (users row gone, audit row hard_deleted,
# ChatRooms deactivated) lives in story 9.13.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("KNOTIFY_RUN_INTEGRATION") != "1",
    reason="Integration test — set KNOTIFY_RUN_INTEGRATION=1 to run against real AWS",
)
def test_integration_given_3_messages_across_2_rooms_when_handler_invoked_then_all_3_deleted():
    """
    Skip-gated integration test (story 9.12 AC).

    Pre-conditions (set up by caller or test fixture):
      - DynamoDB ChatMessages table exists.
      - 3 rows seeded: 2 in ROOM_1, 1 in ROOM_2, all with sender_id=USER_ID.
      - 1 row in ROOM_2 with a different sender_id (must NOT be deleted).

    Assertions after invocation:
      - Query on ROOM_1 for sender_id=USER_ID returns 0 items.
      - Query on ROOM_2 for sender_id=USER_ID returns 0 items.
      - The other-sender row in ROOM_2 is still present.
      - has_more=False.
    """
    import boto3

    from hard_delete_user_chat_messages import handler

    ddb = boto3.client("dynamodb", region_name="eu-central-1")
    table = os.environ.get("TABLE_CHAT_MESSAGES", "ChatMessages")
    user_id = "integ-test-purge-user"
    other_user = "integ-test-survivor-user"
    room_a = "integ-room-alpha"
    room_b = "integ-room-beta"

    # Seed: 2 messages in room_a, 1 in room_b from user_id; 1 in room_b from other_user
    seed_items = [
        {"room_id": {"S": room_a}, "created_at_message_id": {"S": "integ-msg-1"}, "sender_id": {"S": user_id}, "content": {"S": "hi"}},
        {"room_id": {"S": room_a}, "created_at_message_id": {"S": "integ-msg-2"}, "sender_id": {"S": user_id}, "content": {"S": "there"}},
        {"room_id": {"S": room_b}, "created_at_message_id": {"S": "integ-msg-3"}, "sender_id": {"S": user_id}, "content": {"S": "bye"}},
        {"room_id": {"S": room_b}, "created_at_message_id": {"S": "integ-msg-4"}, "sender_id": {"S": other_user}, "content": {"S": "still here"}},
    ]
    for item in seed_items:
        ddb.put_item(TableName=table, Item=item)

    # Invoke
    result = handler.handler(
        {"user_id": user_id, "room_ids": [room_a, room_b]},
        None,
    )

    assert result["has_more"] is False

    # Assert: user_id messages are GONE from room_a
    resp_a = ddb.query(
        TableName=table,
        KeyConditionExpression="room_id = :rid",
        FilterExpression="sender_id = :uid",
        ExpressionAttributeValues={":rid": {"S": room_a}, ":uid": {"S": user_id}},
    )
    assert resp_a["Count"] == 0, f"Expected 0 messages from user in room_a, got {resp_a['Count']}"

    # Assert: user_id messages are GONE from room_b
    resp_b = ddb.query(
        TableName=table,
        KeyConditionExpression="room_id = :rid",
        FilterExpression="sender_id = :uid",
        ExpressionAttributeValues={":rid": {"S": room_b}, ":uid": {"S": user_id}},
    )
    assert resp_b["Count"] == 0, f"Expected 0 messages from user in room_b, got {resp_b['Count']}"

    # Assert: other_user's message in room_b is still present
    resp_survivor = ddb.query(
        TableName=table,
        KeyConditionExpression="room_id = :rid",
        FilterExpression="sender_id = :uid",
        ExpressionAttributeValues={":rid": {"S": room_b}, ":uid": {"S": other_user}},
    )
    assert resp_survivor["Count"] == 1, (
        f"Survivor's message must still be present in room_b, got {resp_survivor['Count']}"
    )

    # Cleanup
    for item in seed_items:
        ddb.delete_item(
            TableName=table,
            Key={"room_id": item["room_id"], "created_at_message_id": item["created_at_message_id"]},
        )
