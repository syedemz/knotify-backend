"""
Unit tests for the knotify-chat-resolver Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. External collaborators (DB connection,
DynamoDB client) are stubbed at the module level.

Run:
    pytest infrastructure/src/functions/chat_resolver/tests/test_chat_resolver.py -v

Conventions:
  - Test names follow the pattern: given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.
  - The handler module is imported via importlib to allow layer-import patching
    without physically installing the layers.

Test coverage for story 8.0 (empty dispatcher):
  A. Dispatcher — any (typeName, fieldName) pair not yet implemented returns
     a structured Unimplemented error response (never raises, never returns None).
  B. @require_profile_complete_appsync — the decorator blocks access when the
     custom:profile_complete claim is missing or not "true" and lets requests
     through when it is "true".

Test coverage for story 8.4 (sendMessage — token derivation):
  C. derive_client_request_token — pure function: same inputs produce same hash;
     differing inputs (content diff by one byte, second ±1) produce different hashes.

Test coverage for story 8.5 (listMyRooms + messagesByChatRoom):
  D. Pagination helpers — _encode_next_token / _decode_next_token are pure
     inverse functions; None in → None out; non-None dict round-trips losslessly.
  E. Dispatcher routing — (Query, listMyRooms) and (Query, messagesByChatRoom)
     are no longer Unimplemented once story 8.5 is wired.
  F. listMyRooms sort order — rooms returned in last_message_at descending order.
  G. messagesByChatRoom membership gate — non-member caller returns Unauthorized.
  H. @require_profile_complete_appsync gate applied to both resolvers (via handler
     entry point, same pattern as story 8.3 IT-8.3-4).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helpers
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

_OBS_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "../../../layers/observability",
    )
)


def _import_handler() -> ModuleType:
    """Import the chat_resolver handler module fresh for each test.

    Patches layer imports so the test suite runs without the layers
    physically installed.  knotify_obs.require_profile_complete_appsync
    is passed through as a no-op decorator so dispatcher tests are not
    gated by the profile-complete check.
    """
    spec = importlib.util.spec_from_file_location(
        "chat_resolver_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules",
        {
            "knotify_db": MagicMock(),
            "knotify_obs": MagicMock(
                init_logger=MagicMock(return_value=MagicMock()),
                # Pass-through decorator so the dispatcher tests are not gated
                require_profile_complete_appsync=lambda f: f,
            ),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


def _import_profile_complete_appsync() -> ModuleType:
    """Import _profile_complete_appsync directly from the layer source.

    Uses sys.path insertion so the import resolves without pip-installing
    the layer.  This tests the decorator in isolation from the handler.
    """
    layer_pkg_dir = os.path.join(_OBS_DIR, "knotify_obs")
    spec = importlib.util.spec_from_file_location(
        "_profile_complete_appsync_" + str(id(object())),
        os.path.join(layer_pkg_dir, "_profile_complete_appsync.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers — build minimal AppSync Lambda event dicts
# ---------------------------------------------------------------------------


def _make_appsync_event(
    type_name: str = "Query",
    field_name: str = "listMyRooms",
    *,
    profile_complete: str | None = "true",
    user_sub: str = "user-sub-appsync-1234",
) -> dict:
    """Build a minimal AppSync Lambda resolver event.

    AppSync Lambda resolvers receive identity under event["identity"],
    with Cognito claims at event["identity"]["claims"].
    The custom:profile_complete claim is set by the pre-token-gen Lambda.
    """
    claims: dict = {"sub": user_sub}
    if profile_complete is not None:
        claims["custom:profile_complete"] = profile_complete

    return {
        "typeName": type_name,
        "fieldName": field_name,
        "identity": {
            "sub": user_sub,
            "issuer": "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_test",
            "username": user_sub,
            "claims": claims,
        },
        "arguments": {},
        "source": None,
        "request": {"headers": {}},
    }


_CONTEXT = MagicMock()

# ---------------------------------------------------------------------------
# Section A — Dispatcher returns Unimplemented for all unknown fields
#
# Story 8.0 ships an empty dispatcher.  Every (typeName, fieldName) pair that
# has not been slotted in by a later story must receive a structured error
# response (not a raised exception, not None).
#
# The required shape:
#   {"errorType": "Unimplemented", "message": "<typeName>.<fieldName> not yet implemented"}
# ---------------------------------------------------------------------------


def test_given_unknown_query_field_when_dispatched_then_returns_unimplemented() -> None:
    """given Query.unknownField, when _dispatch called, then Unimplemented error."""
    mod = _import_handler()
    event = _make_appsync_event("Query", "unknownField")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"
    assert "Query.unknownField" in result["message"]


def test_given_mutation_send_message_when_dispatched_then_not_unimplemented() -> None:
    """given Mutation.sendMessage (story 8.4 wired), when _dispatch called, then NOT Unimplemented.

    sendMessage is now routed to _handle_send_message; it will reject the
    caller (Unauthorized — not a room member) rather than returning Unimplemented.
    """
    from unittest.mock import MagicMock

    mod = _import_handler()
    # Inject a mock DDB client so no real AWS call is made.
    # GetItem returns no Item → sender is not a member → Unauthorized.
    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "sendMessage")
    event["arguments"] = {"roomId": "room-x", "content": "hello"}
    result = mod._dispatch(event)
    # Must NOT be Unimplemented — the route is now wired.
    assert result.get("errorType") != "Unimplemented", (
        f"sendMessage must be routed but got Unimplemented: {result}"
    )


def test_given_subscription_field_when_dispatched_then_returns_unimplemented() -> None:
    """given Subscription.onMessageInRoom, when _dispatch called, then Unimplemented error."""
    mod = _import_handler()
    event = _make_appsync_event("Subscription", "onMessageInRoom")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"
    assert "Subscription.onMessageInRoom" in result["message"]


def test_given_arbitrary_type_when_dispatched_then_returns_unimplemented() -> None:
    """given an arbitrary typeName/fieldName, when _dispatch called, then Unimplemented."""
    mod = _import_handler()
    event = _make_appsync_event("SomeType", "someField")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"


def test_given_empty_type_name_when_dispatched_then_returns_unimplemented() -> None:
    """given empty typeName, when _dispatch called, then Unimplemented (never raises)."""
    mod = _import_handler()
    event = _make_appsync_event("", "")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"


def test_given_dispatch_result_when_checked_then_has_message_key() -> None:
    """given any dispatch result, when examined, then 'message' key is present."""
    mod = _import_handler()
    event = _make_appsync_event("Query", "createOrGetRoom")
    result = mod._dispatch(event)
    assert "message" in result


# ---------------------------------------------------------------------------
# Section B — @require_profile_complete_appsync decorator
#
# AppSync events carry identity.claims, unlike REST handlers that use
# requestContext.authorizer.jwt.claims.  The new decorator must:
#   - Allow requests when custom:profile_complete == "true"
#   - Return Unauthorized when the claim is absent (fail-closed)
#   - Return Unauthorized when the claim is "false"
#   - Return Unauthorized when the claim is any other value
# ---------------------------------------------------------------------------


def test_given_profile_complete_true_when_decorator_applied_then_handler_called() -> None:
    """given profile_complete='true', when decorator applied, then handler is called."""
    mod = _import_profile_complete_appsync()

    called_with = {}

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        called_with["event"] = event
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete="true")
    result = fake_handler(event, _CONTEXT)
    assert result == {"data": "ok"}
    assert "event" in called_with


def test_given_profile_complete_false_when_decorator_applied_then_unauthorized() -> None:
    """given profile_complete='false', when decorator applied, then Unauthorized."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete="false")
    result = fake_handler(event, _CONTEXT)
    assert result["errorType"] == "Unauthorized"
    assert result.get("reason") == "PROFILE_INCOMPLETE"


def test_given_profile_complete_missing_when_decorator_applied_then_unauthorized() -> None:
    """given claim absent, when decorator applied, then Unauthorized (fail-closed)."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete=None)
    result = fake_handler(event, _CONTEXT)
    assert result["errorType"] == "Unauthorized"
    assert result.get("reason") == "PROFILE_INCOMPLETE"


def test_given_profile_complete_arbitrary_value_when_decorator_applied_then_unauthorized() -> None:
    """given claim == 'yes', when decorator applied, then Unauthorized (only 'true' allowed)."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete="yes")
    result = fake_handler(event, _CONTEXT)
    assert result["errorType"] == "Unauthorized"


def test_given_no_identity_key_when_decorator_applied_then_unauthorized() -> None:
    """given event with no 'identity' key, when decorator applied, then Unauthorized."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    result = fake_handler({}, _CONTEXT)
    assert result["errorType"] == "Unauthorized"


# ---------------------------------------------------------------------------
# Section C — derive_client_request_token (story 8.4)
#
# Determinism proof: same (sender, room, content, second) inputs always yield
# the same SHA-256 hex digest; any single-field difference yields a different
# digest.  This is the sole automated proof of the idempotency property —
# the deployed Lambda's wall clock cannot be pinned, so an integration-test
# replay would be flaky and is intentionally omitted.
# ---------------------------------------------------------------------------


def test_given_same_inputs_when_derive_token_called_twice_then_same_hash() -> None:
    """given identical (sender, room, content, second), when called twice, then identical token."""
    mod = _import_handler()
    token_a = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_b = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    assert token_a == token_b


def test_given_content_differs_by_one_byte_when_derive_token_called_then_different_hashes() -> None:
    """given content differing by one byte, when derive_token called, then different tokens."""
    mod = _import_handler()
    token_original = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_mutated = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello worle",  # last char differs
        epoch_second=1_718_000_000,
    )
    assert token_original != token_mutated


def test_given_epoch_second_minus_one_when_derive_token_called_then_different_hash() -> None:
    """given epoch_second - 1, when derive_token called, then token differs (clock boundary)."""
    mod = _import_handler()
    base_second = 1_718_000_000
    token_base = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second,
    )
    token_prev = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second - 1,
    )
    assert token_base != token_prev


def test_given_epoch_second_plus_one_when_derive_token_called_then_different_hash() -> None:
    """given epoch_second + 1, when derive_token called, then token differs (clock boundary)."""
    mod = _import_handler()
    base_second = 1_718_000_000
    token_base = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second,
    )
    token_next = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second + 1,
    )
    assert token_base != token_next


def test_given_different_sender_ids_when_derive_token_called_then_different_hashes() -> None:
    """given different sender_ids, when derive_token called, then tokens differ."""
    mod = _import_handler()
    token_a = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_b = mod.derive_client_request_token(
        sender_id="user-bbb",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    assert token_a != token_b


def test_given_different_room_ids_when_derive_token_called_then_different_hashes() -> None:
    """given different room_ids, when derive_token called, then tokens differ."""
    mod = _import_handler()
    token_a = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_b = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-222",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    assert token_a != token_b


def test_given_any_inputs_when_derive_token_called_then_result_is_64_char_hex_string() -> None:
    """given valid inputs, when derive_token called, then result is a 64-character hex SHA-256."""
    mod = _import_handler()
    token = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="test message",
        epoch_second=1_718_000_000,
    )
    assert isinstance(token, str)
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)


# ---------------------------------------------------------------------------
# Section D — Pagination helpers (story 8.5)
#
# _encode_next_token / _decode_next_token are pure, stateless, and inverse of
# each other.  Their correctness underpins the nextToken contract exposed in the
# GraphQL schema's MessageConnection.nextToken field.
# ---------------------------------------------------------------------------


def test_given_none_input_when_encode_next_token_called_then_returns_none() -> None:
    """given None, when _encode_next_token called, then returns None (no cursor)."""
    mod = _import_handler()
    assert mod._encode_next_token(None) is None


def test_given_none_input_when_decode_next_token_called_then_returns_none() -> None:
    """given None, when _decode_next_token called, then returns None (no cursor)."""
    mod = _import_handler()
    assert mod._decode_next_token(None) is None


def test_given_dict_when_encode_then_decode_returns_original_dict() -> None:
    """given a DynamoDB LastEvaluatedKey dict, when encode then decode, then
    round-trip is lossless (same dict recovered)."""
    mod = _import_handler()
    original = {
        "room_id": {"S": "some-room-id"},
        "created_at_message_id": {"S": "2024-01-01T00:00:00+00:00#01ARZ3NDEKTSV4RRFFQ69G5FAV"},
    }
    token = mod._encode_next_token(original)
    assert token is not None
    assert isinstance(token, str)
    recovered = mod._decode_next_token(token)
    assert recovered == original


def test_given_encoded_token_when_decoded_then_result_is_dict() -> None:
    """given a token produced by _encode_next_token, when decoded, then result is a dict."""
    mod = _import_handler()
    source = {"room_id": {"S": "r1"}, "created_at_message_id": {"S": "ts#ulid"}}
    token = mod._encode_next_token(source)
    result = mod._decode_next_token(token)
    assert isinstance(result, dict)


def test_given_two_distinct_dicts_when_encoded_then_tokens_differ() -> None:
    """given two different LastEvaluatedKey dicts, when encoded, then tokens are different."""
    mod = _import_handler()
    token_a = mod._encode_next_token({"created_at_message_id": {"S": "cursor-a"}})
    token_b = mod._encode_next_token({"created_at_message_id": {"S": "cursor-b"}})
    assert token_a != token_b


def test_given_empty_dict_when_encode_then_decode_returns_empty_dict() -> None:
    """given an empty dict (degenerate LastEvaluatedKey), when encode then decode,
    then empty dict is recovered."""
    mod = _import_handler()
    token = mod._encode_next_token({})
    assert token is not None
    recovered = mod._decode_next_token(token)
    assert recovered == {}


# ---------------------------------------------------------------------------
# Section E — Dispatcher routing for listMyRooms + messagesByChatRoom (story 8.5)
#
# After story 8.5 is wired, (Query, listMyRooms) and (Query, messagesByChatRoom)
# must NOT return an Unimplemented error.
# ---------------------------------------------------------------------------


def test_given_list_my_rooms_query_when_dispatched_then_not_unimplemented() -> None:
    """given (Query, listMyRooms) after story 8.5, when _dispatch called,
    then result is NOT Unimplemented (it is a list, not an error dict)."""
    mod = _import_handler()

    # Mock DDB: Query returns empty Items (no memberships) so listMyRooms returns []
    mock_ddb = MagicMock()
    mock_ddb.query.return_value = {"Items": []}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "listMyRooms")
    result = mod._dispatch(event)
    # listMyRooms returns a list on success (not an error dict)
    is_unimplemented = isinstance(result, dict) and result.get("errorType") == "Unimplemented"
    assert not is_unimplemented, (
        f"listMyRooms must be routed but returned Unimplemented: {result}"
    )


def test_given_messages_by_chat_room_query_when_dispatched_then_not_unimplemented() -> None:
    """given (Query, messagesByChatRoom) after story 8.5, when _dispatch called,
    then result is NOT Unimplemented."""
    mod = _import_handler()

    # Mock DDB: GetItem returns no Item → non-member → Unauthorized
    # (Unauthorized is the expected path when the caller is not in the room)
    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {}  # no Item key → membership absent
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom")
    event["arguments"] = {"roomId": "test-room-id"}
    result = mod._dispatch(event)
    assert result.get("errorType") != "Unimplemented", (
        f"messagesByChatRoom must be routed but returned Unimplemented: {result}"
    )


# ---------------------------------------------------------------------------
# Section F — listMyRooms sort order (story 8.5)
#
# The handler must sort ChatRooms by last_message_at descending before
# returning the list.  BatchGetItem responses are unordered by spec so the
# sort must happen in Python, not be assumed from DynamoDB.
# ---------------------------------------------------------------------------


def test_given_two_rooms_with_different_last_message_at_when_list_my_rooms_then_sorted_descending() -> None:
    """given two rooms with different last_message_at, when listMyRooms is called,
    then they are returned with the most-recent room first (descending order)."""
    mod = _import_handler()

    user_id = "test-user-sub-sort"
    room_old_id = "room-older-abc"
    room_new_id = "room-newer-xyz"

    mock_ddb = MagicMock()

    # Query(ChatRoomMembership, PK=user_id) returns two membership rows
    mock_ddb.query.return_value = {
        "Items": [
            {"user_id": {"S": user_id}, "room_id": {"S": room_old_id}},
            {"user_id": {"S": user_id}, "room_id": {"S": room_new_id}},
        ]
    }

    # BatchGetItem returns the two rooms in arbitrary order (DynamoDB does not
    # guarantee order on BatchGetItem responses)
    mock_ddb.batch_get_item.return_value = {
        "Responses": {
            "ChatRooms": [
                # Intentionally return the newer room second to prove Python sort
                {
                    "room_id": {"S": room_old_id},
                    "status": {"S": "active"},
                    "friendship_active": {"BOOL": True},
                    "last_message_at": {"S": "2024-01-01T10:00:00+00:00"},
                },
                {
                    "room_id": {"S": room_new_id},
                    "status": {"S": "active"},
                    "friendship_active": {"BOOL": True},
                    "last_message_at": {"S": "2024-01-02T10:00:00+00:00"},
                },
            ]
        },
        "UnprocessedKeys": {},
    }

    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "listMyRooms", user_sub=user_id)
    result = mod._dispatch(event)

    assert isinstance(result, list), f"Expected list, got: {type(result)} — {result}"
    assert len(result) == 2, f"Expected 2 rooms, got: {len(result)}"
    # Newer room must be first (descending by last_message_at)
    assert result[0]["roomId"] == room_new_id, (
        f"Expected newer room first, got: {result[0]['roomId']!r}"
    )
    assert result[1]["roomId"] == room_old_id, (
        f"Expected older room second, got: {result[1]['roomId']!r}"
    )


def test_given_no_rooms_when_list_my_rooms_then_returns_empty_list() -> None:
    """given a user with no rooms, when listMyRooms is called, then returns []."""
    mod = _import_handler()

    mock_ddb = MagicMock()
    mock_ddb.query.return_value = {"Items": []}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "listMyRooms")
    result = mod._dispatch(event)

    assert result == [], f"Expected empty list, got: {result}"


def test_given_rooms_without_last_message_at_when_list_my_rooms_then_no_error() -> None:
    """given rooms with no last_message_at (never sent a message), when listMyRooms,
    then handler does not raise (rooms sort to the end, no KeyError)."""
    mod = _import_handler()

    user_id = "test-user-no-msg"
    room_id = "room-no-messages"

    mock_ddb = MagicMock()
    mock_ddb.query.return_value = {
        "Items": [{"user_id": {"S": user_id}, "room_id": {"S": room_id}}]
    }
    mock_ddb.batch_get_item.return_value = {
        "Responses": {
            "ChatRooms": [
                {
                    "room_id": {"S": room_id},
                    "status": {"S": "active"},
                    "friendship_active": {"BOOL": True},
                    # last_message_at deliberately absent
                }
            ]
        },
        "UnprocessedKeys": {},
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "listMyRooms", user_sub=user_id)
    result = mod._dispatch(event)
    assert isinstance(result, list)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Section G — messagesByChatRoom membership gate (story 8.5)
# ---------------------------------------------------------------------------


def test_given_non_member_caller_when_messages_by_chat_room_then_unauthorized() -> None:
    """given a caller not in the room (ChatRoomMembership GetItem returns no Item),
    when messagesByChatRoom is called, then returns Unauthorized."""
    mod = _import_handler()

    mock_ddb = MagicMock()
    # GetItem returns no Item → membership absent
    mock_ddb.get_item.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom")
    event["arguments"] = {"roomId": "any-room-id"}
    result = mod._dispatch(event)

    assert result.get("errorType") == "Unauthorized", (
        f"Expected Unauthorized for non-member, got: {result}"
    )


def test_given_member_caller_no_messages_when_messages_by_chat_room_then_empty_connection() -> None:
    """given a member caller and no messages in room, when messagesByChatRoom,
    then returns MessageConnection with empty items and no nextToken."""
    mod = _import_handler()

    user_id = "member-user-id"
    room_id = "room-with-no-messages"

    mock_ddb = MagicMock()

    # GetItem for membership check succeeds (Item present)
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    # Query for messages returns empty Items
    mock_ddb.query.return_value = {"Items": []}

    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom", user_sub=user_id)
    event["arguments"] = {"roomId": room_id}
    result = mod._dispatch(event)

    assert "errorType" not in result, f"Expected success but got error: {result}"
    assert "items" in result, f"Expected 'items' key in result, got: {result.keys()}"
    assert result["items"] == [], f"Expected empty items, got: {result['items']}"
    assert result.get("nextToken") is None


def test_given_member_caller_with_messages_when_messages_by_chat_room_then_returns_items() -> None:
    """given a member caller and two messages, when messagesByChatRoom, then
    returns MessageConnection with two Message items."""
    mod = _import_handler()

    user_id = "member-user-id-2"
    room_id = "room-with-messages"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.query.return_value = {
        "Items": [
            {
                "room_id": {"S": room_id},
                "created_at_message_id": {"S": "2024-01-02T00:00:00+00:00#ULID2"},
                "sender_id": {"S": "other-user"},
                "content": {"S": "hello"},
                "content_type": {"S": "text"},
                "delivered_at": {"S": "2024-01-02T00:00:00+00:00"},
            },
            {
                "room_id": {"S": room_id},
                "created_at_message_id": {"S": "2024-01-01T00:00:00+00:00#ULID1"},
                "sender_id": {"S": user_id},
                "content": {"S": "first message"},
                "content_type": {"S": "text"},
                "delivered_at": {"S": "2024-01-01T00:00:00+00:00"},
            },
        ]
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom", user_sub=user_id)
    event["arguments"] = {"roomId": room_id}
    result = mod._dispatch(event)

    assert "errorType" not in result, f"Unexpected error: {result}"
    assert len(result["items"]) == 2
    # nextToken absent when no LastEvaluatedKey in DynamoDB response
    assert result.get("nextToken") is None


def test_given_member_caller_with_last_evaluated_key_when_messages_by_chat_room_then_next_token_present() -> None:
    """given DynamoDB returns LastEvaluatedKey, when messagesByChatRoom,
    then nextToken is a non-empty string in the response."""
    mod = _import_handler()

    user_id = "paged-user"
    room_id = "paged-room"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.query.return_value = {
        "Items": [
            {
                "room_id": {"S": room_id},
                "created_at_message_id": {"S": "2024-01-01T00:00:00+00:00#ULID1"},
                "sender_id": {"S": user_id},
                "content": {"S": "msg"},
                "content_type": {"S": "text"},
                "delivered_at": {"S": "2024-01-01T00:00:00+00:00"},
            }
        ],
        # DynamoDB sets LastEvaluatedKey when there are more pages
        "LastEvaluatedKey": {
            "room_id": {"S": room_id},
            "created_at_message_id": {"S": "2024-01-01T00:00:00+00:00#ULID1"},
        },
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom", user_sub=user_id)
    event["arguments"] = {"roomId": room_id}
    result = mod._dispatch(event)

    assert "errorType" not in result, f"Unexpected error: {result}"
    assert result.get("nextToken") is not None, "Expected nextToken when LastEvaluatedKey present"
    assert isinstance(result["nextToken"], str)
    assert len(result["nextToken"]) > 0


def test_given_member_caller_with_limit_arg_when_messages_by_chat_room_then_limit_capped_at_100() -> None:
    """given limit=500 (excessive), when messagesByChatRoom, then DynamoDB is
    called with Limit <= 100 (defensive cap) not with the client-supplied value."""
    mod = _import_handler()

    user_id = "limit-test-user"
    room_id = "limit-test-room"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.query.return_value = {"Items": []}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "limit": 500}
    mod._dispatch(event)

    # Assert DynamoDB query was called with a Limit of at most 100
    assert mock_ddb.query.called, "DynamoDB query was not called"
    call_kwargs = mock_ddb.query.call_args[1]
    effective_limit = call_kwargs.get("Limit", call_kwargs.get("limit"))
    assert effective_limit is not None, "Limit was not passed to DynamoDB query"
    assert effective_limit <= 100, (
        f"Expected Limit <= 100 (defensive cap), got {effective_limit}"
    )


# ---------------------------------------------------------------------------
# Section H — @require_profile_complete_appsync gate on new resolvers
#
# Both listMyRooms and messagesByChatRoom must be gated by the decorator.
# We drive the full handler() entrypoint (not _dispatch()) to verify that
# the decorator fires before any DDB operation.
# ---------------------------------------------------------------------------


def _make_handler_module_with_real_decorator() -> ModuleType:
    """Import handler with the real @require_profile_complete_appsync decorator
    from the layer source, not the no-op pass-through used in other tests."""
    # _OBS_DIR is already defined at module level; use it directly.
    obs_pkg_dir = os.path.join(_OBS_DIR, "knotify_obs")

    spec = importlib.util.spec_from_file_location(
        "chat_resolver_handler_real_dec_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)

    # Patch knotify_obs with a real-ish stub that exposes the actual decorator
    # sourced from the layer file, so the gate is real, not a no-op.
    dec_spec = importlib.util.spec_from_file_location(
        "_pca_dec_" + str(id(object())),
        os.path.join(obs_pkg_dir, "_profile_complete_appsync.py"),
    )
    dec_mod = importlib.util.module_from_spec(dec_spec)
    dec_spec.loader.exec_module(dec_mod)
    real_decorator = dec_mod.require_profile_complete_appsync

    with patch.dict(
        "sys.modules",
        {
            "knotify_db": MagicMock(),
            "knotify_obs": MagicMock(
                init_logger=MagicMock(return_value=MagicMock()),
                require_profile_complete_appsync=real_decorator,
                is_blocked=MagicMock(return_value=False),
                chat_room_id=MagicMock(return_value="computed-room-id"),
            ),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


def test_given_profile_incomplete_when_list_my_rooms_via_handler_then_unauthorized() -> None:
    """given custom:profile_complete != 'true', when handler() called for listMyRooms,
    then decorator returns Unauthorized before any DDB call."""
    mod = _make_handler_module_with_real_decorator()
    mock_ddb = MagicMock()
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "listMyRooms", profile_complete=None)
    result = mod.handler(event, {})

    assert result.get("errorType") == "Unauthorized", (
        f"Expected Unauthorized from decorator, got: {result}"
    )
    assert result.get("reason") == "PROFILE_INCOMPLETE"
    # No DDB call should have been made
    mock_ddb.query.assert_not_called()


def test_given_profile_incomplete_when_messages_by_chat_room_via_handler_then_unauthorized() -> None:
    """given custom:profile_complete != 'true', when handler() called for messagesByChatRoom,
    then decorator returns Unauthorized before any DDB call."""
    mod = _make_handler_module_with_real_decorator()
    mock_ddb = MagicMock()
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Query", "messagesByChatRoom", profile_complete="false")
    event["arguments"] = {"roomId": "some-room"}
    result = mod.handler(event, {})

    assert result.get("errorType") == "Unauthorized", (
        f"Expected Unauthorized from decorator, got: {result}"
    )
    assert result.get("reason") == "PROFILE_INCOMPLETE"
    mock_ddb.get_item.assert_not_called()


# ---------------------------------------------------------------------------
# Section I — markAsRead resolver (story 8.7)
#
# markAsRead(roomId, lastMessageId) performs:
#   1. GetItem ChatRoomMembership(identity.sub, roomId) — Unauthorized on miss.
#   2. TransactWriteItems with two operations:
#        a. PutItem MessageReads   (PK=room_id, SK=user_id)
#           attributes: last_read_message_id, last_read_at
#        b. UpdateItem ChatRoomMembership (PK=user_id, SK=room_id)
#           same cached values: last_read_message_id, last_read_at
#   3. Return MessageRead dict: roomId, userId, lastReadMessageId, lastReadAt
#      (type subscribed by onReadReceipt(roomId)).
#
# Tests in this section are pure unit tests — no AWS calls.
# ---------------------------------------------------------------------------


def test_given_mark_as_read_when_dispatched_then_not_unimplemented() -> None:
    """given (Mutation, markAsRead) after story 8.7 is wired, when _dispatch called,
    then result is NOT Unimplemented (route is live)."""
    mod = _import_handler()

    # GetItem returns membership present → proceed; transact_write_items succeeds.
    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {
            "user_id": {"S": "caller-sub"},
            "room_id": {"S": "room-abc"},
        }
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub="caller-sub")
    event["arguments"] = {"roomId": "room-abc", "lastMessageId": "2024-01-01T00:00:00+00:00#ULID"}
    result = mod._dispatch(event)

    assert result.get("errorType") != "Unimplemented", (
        f"markAsRead must be routed but returned Unimplemented: {result}"
    )


def test_given_non_member_caller_when_mark_as_read_then_unauthorized() -> None:
    """given caller not in room (ChatRoomMembership GetItem returns no Item),
    when markAsRead is called, then returns Unauthorized."""
    mod = _import_handler()

    mock_ddb = MagicMock()
    # No Item → membership absent
    mock_ddb.get_item.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub="outsider-sub")
    event["arguments"] = {"roomId": "room-xyz", "lastMessageId": "some-msg-id"}
    result = mod._dispatch(event)

    assert result.get("errorType") == "Unauthorized", (
        f"Expected Unauthorized for non-member, got: {result}"
    )
    # transact_write_items must NOT have been called — no write before auth check
    mock_ddb.transact_write_items.assert_not_called()


def test_given_member_caller_when_mark_as_read_then_transact_write_called() -> None:
    """given a member caller, when markAsRead is called, then TransactWriteItems
    is called exactly once with two items."""
    mod = _import_handler()

    user_id = "user-sub-mark"
    room_id = "room-mark-test"
    last_message_id = "2024-06-17T10:00:00+00:00#ULID99"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    result = mod._dispatch(event)

    assert mock_ddb.transact_write_items.called, "Expected TransactWriteItems to be called"
    call_kwargs = mock_ddb.transact_write_items.call_args[1]
    items = call_kwargs.get("TransactItems", [])
    assert len(items) == 2, f"Expected 2 transact items, got {len(items)}"


def test_given_member_caller_when_mark_as_read_then_message_reads_put_item_uses_correct_keys() -> None:
    """given a member caller, when markAsRead succeeds, then the first TransactItem
    is a PutItem on MessageReads with PK=room_id and SK=user_id."""
    mod = _import_handler()

    user_id = "user-sub-reads-key"
    room_id = "room-reads-key"
    last_message_id = "2024-06-17T11:00:00+00:00#ULID88"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    mod._dispatch(event)

    call_kwargs = mock_ddb.transact_write_items.call_args[1]
    items = call_kwargs["TransactItems"]

    # First item must be the MessageReads PutItem
    put_op = items[0]["Put"]
    assert put_op["TableName"] == "MessageReads"
    item_keys = put_op["Item"]
    # PK=room_id, SK=user_id — exact attribute names from dynamodb/main.tf
    assert item_keys.get("room_id", {}).get("S") == room_id, (
        f"Expected room_id={room_id!r} in MessageReads PutItem, got: {item_keys}"
    )
    assert item_keys.get("user_id", {}).get("S") == user_id, (
        f"Expected user_id={user_id!r} in MessageReads PutItem, got: {item_keys}"
    )


def test_given_member_caller_when_mark_as_read_then_message_reads_contains_last_read_fields() -> None:
    """given a member caller, when markAsRead succeeds, then MessageReads PutItem
    contains last_read_message_id and last_read_at attributes."""
    mod = _import_handler()

    user_id = "user-sub-reads-attrs"
    room_id = "room-reads-attrs"
    last_message_id = "2024-06-17T12:00:00+00:00#ULIDAA"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    mod._dispatch(event)

    call_kwargs = mock_ddb.transact_write_items.call_args[1]
    items = call_kwargs["TransactItems"]
    put_item = items[0]["Put"]["Item"]

    # last_read_message_id must equal the lastMessageId argument
    assert put_item.get("last_read_message_id", {}).get("S") == last_message_id, (
        f"Expected last_read_message_id={last_message_id!r}, got: {put_item}"
    )
    # last_read_at must be a non-empty string (server-set ISO timestamp)
    last_read_at = put_item.get("last_read_at", {}).get("S", "")
    assert last_read_at, "Expected last_read_at to be set on MessageReads PutItem"


def test_given_member_caller_when_mark_as_read_then_membership_update_uses_correct_keys() -> None:
    """given a member caller, when markAsRead succeeds, then the second TransactItem
    is an UpdateItem on ChatRoomMembership with PK=user_id, SK=room_id."""
    mod = _import_handler()

    user_id = "user-sub-membership-key"
    room_id = "room-membership-key"
    last_message_id = "2024-06-17T13:00:00+00:00#ULIDBB"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    mod._dispatch(event)

    call_kwargs = mock_ddb.transact_write_items.call_args[1]
    items = call_kwargs["TransactItems"]

    # Second item must be the ChatRoomMembership UpdateItem
    update_op = items[1]["Update"]
    assert update_op["TableName"] == "ChatRoomMembership"
    key = update_op["Key"]
    # PK=user_id, SK=room_id — exact attribute names from dynamodb/main.tf
    assert key.get("user_id", {}).get("S") == user_id, (
        f"Expected user_id={user_id!r} in ChatRoomMembership UpdateItem key, got: {key}"
    )
    assert key.get("room_id", {}).get("S") == room_id, (
        f"Expected room_id={room_id!r} in ChatRoomMembership UpdateItem key, got: {key}"
    )


def test_given_member_caller_when_mark_as_read_then_membership_update_contains_last_read_fields() -> None:
    """given a member caller, when markAsRead succeeds, then ChatRoomMembership UpdateItem
    sets last_read_message_id and last_read_at (cached copy) in the ExpressionAttributeValues."""
    mod = _import_handler()

    user_id = "user-sub-membership-attrs"
    room_id = "room-membership-attrs"
    last_message_id = "2024-06-17T14:00:00+00:00#ULIDCC"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    mod._dispatch(event)

    call_kwargs = mock_ddb.transact_write_items.call_args[1]
    items = call_kwargs["TransactItems"]
    update_op = items[1]["Update"]

    # ExpressionAttributeValues must include :lrmi (last_read_message_id) and :lrat (last_read_at)
    expr_vals = update_op.get("ExpressionAttributeValues", {})
    # Find the value bound to last_read_message_id
    lrmi_val = expr_vals.get(":lrmi", {}).get("S")
    assert lrmi_val == last_message_id, (
        f"Expected :lrmi={last_message_id!r} in ChatRoomMembership UpdateItem, got: {expr_vals}"
    )
    lrat_val = expr_vals.get(":lrat", {}).get("S", "")
    assert lrat_val, (
        "Expected :lrat (last_read_at) to be set in ChatRoomMembership UpdateItem"
    )


def test_given_member_caller_when_mark_as_read_succeeds_then_returns_message_read_type() -> None:
    """given a member caller and successful write, when markAsRead is called,
    then the result is a MessageRead dict matching the GraphQL type:
    {roomId, userId, lastReadMessageId, lastReadAt}."""
    mod = _import_handler()

    user_id = "user-sub-return-type"
    room_id = "room-return-type"
    last_message_id = "2024-06-17T15:00:00+00:00#ULIDDD"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    result = mod._dispatch(event)

    assert "errorType" not in result, f"Expected success but got error: {result}"
    # MessageRead type fields (camelCase per schema.graphql)
    assert result.get("roomId") == room_id, f"Expected roomId={room_id!r}, got: {result}"
    assert result.get("userId") == user_id, f"Expected userId={user_id!r}, got: {result}"
    assert result.get("lastReadMessageId") == last_message_id, (
        f"Expected lastReadMessageId={last_message_id!r}, got: {result}"
    )
    assert result.get("lastReadAt"), "Expected lastReadAt to be set in response"


def test_given_member_caller_when_mark_as_read_then_response_roomId_matches_argument() -> None:
    """given markAsRead result, then roomId in response matches the roomId argument
    (required so onReadReceipt subscription field filter fires correctly)."""
    mod = _import_handler()

    user_id = "user-sub-roomid-check"
    room_id = "room-specific-id-for-subscription"
    last_message_id = "2024-06-17T16:00:00+00:00#ULIDEE"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mock_ddb.transact_write_items.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "lastMessageId": last_message_id}
    result = mod._dispatch(event)

    # The subscription onReadReceipt(roomId) filters by result.roomId.
    # If this doesn't match the argument, the subscription never fires.
    assert result.get("roomId") == room_id, (
        f"roomId in response ({result.get('roomId')!r}) must match argument ({room_id!r}) "
        "for onReadReceipt subscription field filter to work"
    )


def test_given_profile_incomplete_when_mark_as_read_via_handler_then_unauthorized() -> None:
    """given custom:profile_complete != 'true', when handler() called for markAsRead,
    then decorator returns Unauthorized before any DDB call."""
    mod = _make_handler_module_with_real_decorator()
    mock_ddb = MagicMock()
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "markAsRead", profile_complete=None)
    event["arguments"] = {"roomId": "some-room", "lastMessageId": "some-msg"}
    result = mod.handler(event, {})

    assert result.get("errorType") == "Unauthorized", (
        f"Expected Unauthorized from decorator, got: {result}"
    )
    assert result.get("reason") == "PROFILE_INCOMPLETE"
    mock_ddb.get_item.assert_not_called()


# ---------------------------------------------------------------------------
# Section J — setTyping resolver (story 8.8)
#
# setTyping(roomId, isTyping) is an APPSYNC_JS PIPELINE resolver wired to the
# NONE datasource. It performs no DynamoDB write. The membership check reuses
# the check_room_membership pipeline function from story 8.6.
#
# Because setTyping is handled entirely in the APPSYNC_JS pipeline (not routed
# through the chat_resolver Lambda), the Python dispatcher still returns
# Unimplemented for (Mutation, setTyping) — the AC does not require a Python
# handler; the authoritative proof of "no DynamoDB write" is the Terraform
# assertion that the resolver's pipeline contains no write operation and only
# references NoneDS or read-only DDB functions.
#
# Python-level assertions in this section prove:
#   J.1  (Mutation, setTyping) routed in dispatcher returns Unimplemented
#        (because setTyping is APPSYNC_JS, not Lambda-backed — no Python handler).
#        This is intentional: if AppSync ever mistakenly routes setTyping to the
#        Lambda datasource, it will receive a clear Unimplemented error rather
#        than silently doing nothing.
#   J.2  _handle_set_typing does NOT call any DynamoDB write method
#        (put_item, update_item, delete_item, transact_write_items) — asserts
#        the "no storage" guarantee at the unit-test level for the Python path.
#   J.3  _handle_set_typing returns a TypingEvent-shaped dict
#        {userId, isTyping, roomId} on a successful membership check (the
#        fallback path if AppSync ever routes to Lambda for any reason).
#   J.4  _handle_set_typing returns Unauthorized when the caller is not a member.
# ---------------------------------------------------------------------------


def test_given_set_typing_when_dispatched_then_dispatcher_routes_to_handler() -> None:
    """given (Mutation, setTyping) after story 8.8, when _dispatch called,
    then result is NOT Unimplemented — the route is wired in the dispatcher."""
    mod = _import_handler()

    mock_ddb = MagicMock()
    # GetItem returns membership present — caller is in the room
    mock_ddb.get_item.return_value = {
        "Item": {
            "user_id": {"S": "caller-sub-typing"},
            "room_id": {"S": "room-typing-abc"},
        }
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "setTyping", user_sub="caller-sub-typing")
    event["arguments"] = {"roomId": "room-typing-abc", "isTyping": True}
    result = mod._dispatch(event)

    assert result.get("errorType") != "Unimplemented", (
        f"setTyping must be routed but returned Unimplemented: {result}"
    )


def test_given_set_typing_handler_when_called_with_member_then_no_ddb_write_occurs() -> None:
    """given _handle_set_typing called with a member caller, then NO DynamoDB
    write method (put_item, update_item, delete_item, transact_write_items)
    is invoked — the 'no storage' guarantee in the Python path."""
    mod = _import_handler()

    mock_ddb = MagicMock()
    # GetItem returns membership present
    mock_ddb.get_item.return_value = {
        "Item": {
            "user_id": {"S": "caller-sub-nostoring"},
            "room_id": {"S": "room-nostoring"},
        }
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "setTyping", user_sub="caller-sub-nostoring")
    event["arguments"] = {"roomId": "room-nostoring", "isTyping": True}
    mod._handle_set_typing(event)

    # Assert no write methods were called
    mock_ddb.put_item.assert_not_called()
    mock_ddb.update_item.assert_not_called()
    mock_ddb.delete_item.assert_not_called()
    mock_ddb.transact_write_items.assert_not_called()


def test_given_set_typing_handler_when_called_with_non_member_then_unauthorized() -> None:
    """given caller not in room (ChatRoomMembership GetItem returns no Item),
    when _handle_set_typing is called, then returns Unauthorized and makes
    no DynamoDB write."""
    mod = _import_handler()

    mock_ddb = MagicMock()
    # No Item key → membership absent
    mock_ddb.get_item.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "setTyping", user_sub="outsider-typing")
    event["arguments"] = {"roomId": "room-typing-xyz", "isTyping": False}
    result = mod._handle_set_typing(event)

    assert result.get("errorType") == "Unauthorized", (
        f"Expected Unauthorized for non-member, got: {result}"
    )
    # No write must occur regardless
    mock_ddb.put_item.assert_not_called()
    mock_ddb.update_item.assert_not_called()
    mock_ddb.delete_item.assert_not_called()
    mock_ddb.transact_write_items.assert_not_called()


def test_given_set_typing_handler_when_member_calls_with_is_typing_true_then_returns_typing_event() -> None:
    """given a member caller sends isTyping=True, when _handle_set_typing returns,
    then the result is a TypingEvent dict: {userId, isTyping, roomId}."""
    mod = _import_handler()

    user_id = "user-sub-typing-event"
    room_id = "room-typing-event"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "setTyping", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "isTyping": True}
    result = mod._handle_set_typing(event)

    assert "errorType" not in result, f"Expected TypingEvent payload, got error: {result}"
    assert result.get("userId") == user_id, (
        f"Expected userId={user_id!r} in TypingEvent, got: {result}"
    )
    assert result.get("isTyping") is True, (
        f"Expected isTyping=True in TypingEvent, got: {result}"
    )
    assert result.get("roomId") == room_id, (
        f"Expected roomId={room_id!r} in TypingEvent (drives onTypingInRoom field filter), "
        f"got: {result}"
    )


def test_given_set_typing_handler_when_member_calls_with_is_typing_false_then_typing_event_reflects_false() -> None:
    """given a member caller sends isTyping=False, when _handle_set_typing returns,
    then the TypingEvent has isTyping=False."""
    mod = _import_handler()

    user_id = "user-sub-stop-typing"
    room_id = "room-stop-typing"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "setTyping", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "isTyping": False}
    result = mod._handle_set_typing(event)

    assert result.get("isTyping") is False, (
        f"Expected isTyping=False in TypingEvent, got: {result}"
    )


def test_given_set_typing_handler_when_called_then_only_one_ddb_get_item_is_called() -> None:
    """given _handle_set_typing called for a member, then exactly one GetItem
    (membership check) and no other DDB operations are performed."""
    mod = _import_handler()

    user_id = "user-sub-one-get"
    room_id = "room-one-get"

    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {
        "Item": {"user_id": {"S": user_id}, "room_id": {"S": room_id}}
    }
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "setTyping", user_sub=user_id)
    event["arguments"] = {"roomId": room_id, "isTyping": True}
    mod._handle_set_typing(event)

    # Exactly one GetItem (membership check)
    assert mock_ddb.get_item.call_count == 1, (
        f"Expected exactly 1 GetItem call, got {mock_ddb.get_item.call_count}"
    )
    # No query, scan, or write operations
    mock_ddb.query.assert_not_called()
    mock_ddb.scan.assert_not_called()
    mock_ddb.put_item.assert_not_called()
    mock_ddb.update_item.assert_not_called()
    mock_ddb.delete_item.assert_not_called()
    mock_ddb.transact_write_items.assert_not_called()
    mock_ddb.batch_write_item.assert_not_called()
