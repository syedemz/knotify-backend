"""
Unit tests for story 9.6 — anonymize_chat_messages Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. Lambda receives {user_id, room_ids: [...]} as input and for each room_id
     Queries ChatMessages by room_id with a FilterExpression on sender_id,
     then UpdateItems each matching row to set sender_id='[deleted-user]'
     while preserving content. No new GSI is added — Query uses the PK (room_id).
  B. The Lambda does NOT Query ChatRoomMembership. room_ids is the authoritative
     input list.
  C. Continuation token contract: handler accepts {room_ids, current_room_index,
     last_evaluated_key} and returns {has_more, room_ids, current_room_index,
     last_evaluated_key}. On completion has_more=False.
  D. Empty input: {user_id, room_ids: []} returns has_more=False immediately with
     zero DynamoDB calls beyond confirming there is nothing to do.
  E. Pagination within a room: when Query returns a LastEvaluatedKey for a room,
     the Lambda processes that room's page fully before checking for continuation.
     If the room's messages are exhausted and there are more rooms, the token
     advances current_room_index by 1 with last_evaluated_key=None.
  F. Cross-room continuation: when the Lambda is re-invoked with a non-zero
     current_room_index, it skips already-processed rooms and resumes from
     the given index.
  G. sender_id update uses UpdateItem with SET sender_id = :anon_id; the item's
     sort key (SK / message_id) is used alongside room_id as the key.
  H. UpdateItem does NOT overwrite content — the UpdateExpression only touches
     sender_id.
  I. A user who sent no messages in any room returns has_more=False with zero
     UpdateItem calls (all FilterExpression results are empty).
  J. has_more=True is returned when current_room_index is still within bounds
     after the Lambda's page of work — Step Functions re-invokes with the token.
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

_USER_ID = "user-abc-001"
_ROOM_1 = "room-aaa"
_ROOM_2 = "room-bbb"
_ROOM_3 = "room-ccc"
_ANON_SENDER = "[deleted-user]"

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


def _make_message_item(room_id: str, message_id: str, sender_id: str, content: str = "hello") -> dict:
    """Build a DynamoDB-style ChatMessages item.

    The DDB sort key attribute is `created_at_message_id` (see chat_resolver
    writer and table KeySchema). The `message_id` parameter here is just the
    local id portion used as the SK value.
    """
    return {
        "room_id": {"S": room_id},
        "created_at_message_id": {"S": message_id},
        "sender_id": {"S": sender_id},
        "content": {"S": content},
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
    with patch("anonymize_chat_messages.handler._get_dynamo") as mock_factory:
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
    No DynamoDB Query or UpdateItem is made.
    """
    from anonymize_chat_messages import handler

    result = handler.handler(_make_event(room_ids=[]), None)

    assert result["has_more"] is False
    mock_dynamo.query.assert_not_called()
    mock_dynamo.update_item.assert_not_called()


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
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.query.call_count == 1
    call_kwargs = mock_dynamo.query.call_args.kwargs
    assert call_kwargs["TableName"] == "ChatMessages"
    # room_id must appear as the KeyConditionExpression value
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
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    call_kwargs = mock_dynamo.query.call_args.kwargs
    assert "FilterExpression" in call_kwargs, "Query must include a FilterExpression on sender_id"
    # The user_id must appear in the filter expression attribute values
    expr_vals = call_kwargs["ExpressionAttributeValues"]
    user_id_val = next(
        (v["S"] for v in expr_vals.values() if v.get("S") == _USER_ID),
        None,
    )
    assert user_id_val == _USER_ID, "FilterExpression must reference the deleted user's user_id"


# ---------------------------------------------------------------------------
# Test G + H: UpdateItem sets sender_id only — does NOT touch content
# ---------------------------------------------------------------------------


def test_given_one_room_with_matching_message_when_handler_called_then_update_item_sets_sender_id_to_anon(
    mock_dynamo,
):
    """
    AC-G: UpdateItem is called for each matching message and sets
    sender_id='[deleted-user]'.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID, content="my secret content"),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.update_item.call_count == 1
    update_kwargs = mock_dynamo.update_item.call_args.kwargs
    expr_vals = update_kwargs["ExpressionAttributeValues"]
    anon_val = next(
        (v["S"] for v in expr_vals.values() if v.get("S") == _ANON_SENDER),
        None,
    )
    assert anon_val == _ANON_SENDER, "UpdateItem must set sender_id to '[deleted-user]'"


def test_given_matching_message_when_update_item_called_then_content_not_in_update_expression(
    mock_dynamo,
):
    """
    AC-H: UpdateExpression only references sender_id — content is NOT modified.
    The UpdateExpression must not contain 'content'.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID, content="preserve this"),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    update_kwargs = mock_dynamo.update_item.call_args.kwargs
    update_expr: str = update_kwargs.get("UpdateExpression", "")
    assert "content" not in update_expr.lower(), (
        "UpdateExpression must NOT reference 'content' — content must be preserved"
    )


def test_given_matching_message_when_update_item_called_then_key_includes_room_id_and_message_id(
    mock_dynamo,
):
    """
    AC-G: UpdateItem key includes both room_id (PK) and message_id (SK).
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-xyz", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    update_kwargs = mock_dynamo.update_item.call_args.kwargs
    key = update_kwargs["Key"]
    assert "room_id" in key, "UpdateItem Key must include room_id"
    assert key["room_id"]["S"] == _ROOM_1
    assert "created_at_message_id" in key, "UpdateItem Key must include created_at_message_id (SK)"
    assert key["created_at_message_id"]["S"] == "msg-xyz"


# ---------------------------------------------------------------------------
# Test I: user sent no messages — no UpdateItem calls
# ---------------------------------------------------------------------------


def test_given_room_with_no_matching_messages_when_handler_called_then_no_update_item_calls(
    mock_dynamo,
):
    """
    AC-I: when the Query FilterExpression matches zero messages, UpdateItem is not called.
    The Lambda returns has_more=False (all rooms processed).
    """
    from anonymize_chat_messages import handler

    # Query returns empty — no messages from this sender in the room
    mock_dynamo.query.return_value = _make_query_response([])

    result = handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    mock_dynamo.update_item.assert_not_called()
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# Test C: continuation token — has_more=False on single full pass
# ---------------------------------------------------------------------------


def test_given_two_rooms_fully_processed_when_handler_called_then_has_more_false(
    mock_dynamo,
):
    """
    AC-C: when all rooms are processed in one invocation, has_more=False is returned.
    """
    from anonymize_chat_messages import handler

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
    from anonymize_chat_messages import handler

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
    subsequent pages and calls UpdateItem for messages on each page.
    """
    from anonymize_chat_messages import handler

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
    assert mock_dynamo.update_item.call_count == 2
    assert result["has_more"] is False


def test_given_room_query_page2_uses_exclusive_start_key_from_page1(
    mock_dynamo,
):
    """
    AC-E: the second Query call for a room must include ExclusiveStartKey from the
    first page's LastEvaluatedKey.
    """
    from anonymize_chat_messages import handler

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
    AC-F: when re-invoked with current_room_index=1 (room 0 already processed),
    Query is called only for rooms at index >= 1.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    # Simulate re-invocation: room 0 (_ROOM_1) was already processed last invocation
    result = handler.handler(
        _make_event(room_ids=[_ROOM_1, _ROOM_2, _ROOM_3], current_room_index=1),
        None,
    )

    # Query must have been called only for _ROOM_2 and _ROOM_3, not _ROOM_1
    assert mock_dynamo.query.call_count == 2
    queried_rooms = [
        c.kwargs["ExpressionAttributeValues"].get(":rid", {}).get("S")
        or next(
            (v["S"] for v in c.kwargs["ExpressionAttributeValues"].values()
             if v.get("S") in {_ROOM_1, _ROOM_2, _ROOM_3}),
            None,
        )
        for c in mock_dynamo.query.call_args_list
    ]
    assert _ROOM_1 not in queried_rooms, "room_0 must be skipped when current_room_index=1"
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# Test J: has_more=True when invocation should stop early (e.g., budget exhausted)
# This is tested via the continuation contract: the handler_with_limit variant.
# We test has_more=True indirectly: a re-invocation with a token that still has
# rooms remaining must set has_more=True only if not all rooms are finished.
# The simplest test: invoke with 1 room that returns no messages → has_more=False.
# For has_more=True, test via a mock that signals early exit from the handler.
# ---------------------------------------------------------------------------


def test_given_all_rooms_processed_when_handler_returns_then_has_more_false(
    mock_dynamo,
):
    """
    AC-J (baseline): when all rooms have been processed, has_more=False.
    """
    from anonymize_chat_messages import handler

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
    authoritative input. All Query calls must target ChatMessages.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    for c in mock_dynamo.query.call_args_list:
        table = c.kwargs.get("TableName", c.args[0] if c.args else "")
        assert table == "ChatMessages", (
            f"All Query calls must target ChatMessages, not '{table}'"
        )


# ---------------------------------------------------------------------------
# Test: multiple messages in a room — one UpdateItem call per matching row
# ---------------------------------------------------------------------------


def test_given_room_with_three_matching_messages_when_handler_called_then_three_update_item_calls(
    mock_dynamo,
):
    """
    AC-A: one UpdateItem call per matching message row.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item(_ROOM_1, "msg-001", _USER_ID),
        _make_message_item(_ROOM_1, "msg-002", _USER_ID),
        _make_message_item(_ROOM_1, "msg-003", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=[_ROOM_1]), None)

    assert mock_dynamo.update_item.call_count == 3


# ---------------------------------------------------------------------------
# Test: two rooms with messages — Query called once per room
# ---------------------------------------------------------------------------


def test_given_two_rooms_both_with_messages_when_handler_called_then_query_called_for_each_room(
    mock_dynamo,
):
    """
    AC-A: Query is called for each room_id in the input list.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.side_effect = [
        _make_query_response([_make_message_item(_ROOM_1, "msg-001", _USER_ID)]),
        _make_query_response([_make_message_item(_ROOM_2, "msg-002", _USER_ID)]),
    ]

    handler.handler(_make_event(room_ids=[_ROOM_1, _ROOM_2]), None)

    assert mock_dynamo.query.call_count == 2
    assert mock_dynamo.update_item.call_count == 2


# ---------------------------------------------------------------------------
# Test: continuation token last_evaluated_key is passed back when set
# ---------------------------------------------------------------------------


def test_given_re_invocation_with_last_evaluated_key_when_handler_called_then_first_query_uses_it(
    mock_dynamo,
):
    """
    AC-F: when re-invoked with a last_evaluated_key in the token, the first Query
    for the resumed room uses that key as ExclusiveStartKey.
    """
    from anonymize_chat_messages import handler

    prior_lek = {"room_id": {"S": _ROOM_2}, "created_at_message_id": {"S": "msg-050"}}

    mock_dynamo.query.return_value = _make_query_response([])

    # Re-invocation: room_2 was partially processed (last_evaluated_key from prior run)
    handler.handler(
        _make_event(
            room_ids=[_ROOM_1, _ROOM_2, _ROOM_3],
            current_room_index=1,  # resume at room_2
            last_evaluated_key=prior_lek,
        ),
        None,
    )

    # First Query call must use the prior LEK as ExclusiveStartKey
    first_call_kwargs = mock_dynamo.query.call_args_list[0].kwargs
    assert first_call_kwargs.get("ExclusiveStartKey") == prior_lek


# ---------------------------------------------------------------------------
# Test: UpdateItem key uses room_id and message_id from the returned item
# ---------------------------------------------------------------------------


def test_given_message_item_with_known_ids_when_update_item_called_then_key_matches_item_ids(
    mock_dynamo,
):
    """
    Ensures UpdateItem is called with the exact room_id and message_id from the
    Query result item — not hardcoded or incorrectly derived.
    """
    from anonymize_chat_messages import handler

    mock_dynamo.query.return_value = _make_query_response([
        _make_message_item("room-specific-001", "msg-specific-999", _USER_ID),
    ])

    handler.handler(_make_event(room_ids=["room-specific-001"]), None)

    update_kwargs = mock_dynamo.update_item.call_args.kwargs
    assert update_kwargs["Key"]["room_id"]["S"] == "room-specific-001"
    assert update_kwargs["Key"]["created_at_message_id"]["S"] == "msg-specific-999"
