"""
Unit tests for the createOrGetRoom resolver (story 8.3).

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. External collaborators (DB connection,
DynamoDB client) are stubbed at the module level.

Run:
    pytest infrastructure/src/functions/chat_resolver/tests/test_create_or_get_room.py -v

Test coverage:
  A. _canonical_pair  — returns sorted tuple regardless of argument order
  B. Unauthorized error shape — errorType + reason present for each reason code
  C. Dispatcher routing — (Mutation, createOrGetRoom) routes to the handler;
     other (typeName, fieldName) pairs still fall through to Unimplemented
  D. Caller == other user check — returns Unauthorized reason SELF_CHAT
  E. NOT_FRIENDS path — mocked Aurora returns no friendship → Unauthorized NOT_FRIENDS
  F. BLOCKED path — mocked is_blocked returns True → Unauthorized BLOCKED
  G. Happy path (new room) — TransactWriteItems called, correct response shape returned
  H. Idempotent path (room exists) — ConditionalCheckFailedException → GetItem + return existing
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch, call

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


def _import_handler(
    *,
    is_blocked_return: bool = False,
    friendship_rows=None,
    dynamodb_client_override=None,
) -> ModuleType:
    """Import the chat_resolver handler module fresh for each test.

    Patches layer imports so the test suite runs without the layers physically
    installed. Configures knotify_db and knotify_obs stubs with provided
    return-value overrides.

    Args:
        is_blocked_return: Value returned by the mocked knotify_obs.is_blocked.
        friendship_rows:   Rows returned by the mocked cursor.fetchone() for the
                           friendship SQL.  None means no friendship found.
        dynamodb_client_override: If provided, used as the DynamoDB client
                                   returned by boto3.client('dynamodb').
    """
    if friendship_rows is None:
        friendship_rows = None  # no friendship

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = friendship_rows

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_knotify_db = MagicMock()
    mock_knotify_db.get_connection.return_value = mock_conn

    mock_knotify_obs = MagicMock()
    mock_knotify_obs.init_logger.return_value = MagicMock()
    mock_knotify_obs.require_profile_complete_appsync = lambda f: f
    mock_knotify_obs.is_blocked.return_value = is_blocked_return
    # chat_room_id — use real implementation for correctness assertions
    import hashlib
    def _real_chat_room_id(a, b):
        lo, hi = min(a, b), max(a, b)
        return hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()
    mock_knotify_obs.chat_room_id = _real_chat_room_id

    if dynamodb_client_override is not None:
        mock_boto3 = MagicMock()
        mock_boto3.client.return_value = dynamodb_client_override
    else:
        mock_boto3 = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "chat_resolver_handler_8_3_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules",
        {
            "knotify_db": mock_knotify_db,
            "knotify_obs": mock_knotify_obs,
            "boto3": mock_boto3,
        },
    ):
        spec.loader.exec_module(mod)

    # Inject the DynamoDB client override directly into the module-level singleton
    # so _get_dynamodb() returns our mock without touching real boto3.
    if dynamodb_client_override is not None:
        mod._dynamodb_client = dynamodb_client_override

    # Inject the Aurora connection mock
    mod._conn = mock_conn

    # Attach mocks to module for assertion convenience
    mod._mock_knotify_db = mock_knotify_db
    mod._mock_knotify_obs = mock_knotify_obs
    mod._mock_conn = mock_conn
    mod._mock_cursor = mock_cursor
    mod._mock_boto3 = mock_boto3

    return mod


def _make_event(
    *,
    caller_id: str = "aaaa0000-0000-0000-0000-000000000001",
    other_user_id: str = "bbbb0000-0000-0000-0000-000000000002",
    profile_complete: str = "true",
) -> dict:
    """Build a minimal AppSync Mutation.createOrGetRoom event."""
    return {
        "typeName": "Mutation",
        "fieldName": "createOrGetRoom",
        "identity": {
            "sub": caller_id,
            "claims": {
                "sub": caller_id,
                "custom:profile_complete": profile_complete,
            },
        },
        "arguments": {"otherUserId": other_user_id},
        "source": None,
        "request": {"headers": {}},
    }


_CTX = MagicMock()

# ---------------------------------------------------------------------------
# Section A — _canonical_pair
# ---------------------------------------------------------------------------


def test_given_two_uuids_when_canonical_pair_called_then_smaller_is_first() -> None:
    """given two UUIDs, when _canonical_pair called, then (min, max) order."""
    mod = _import_handler()
    a = "aaaa0000-0000-0000-0000-000000000001"
    b = "zzzz0000-0000-0000-0000-000000000002"
    lo, hi = mod._canonical_pair(a, b)
    assert lo == a
    assert hi == b


def test_given_two_uuids_reversed_when_canonical_pair_called_then_same_result() -> None:
    """given args swapped, when _canonical_pair called, then same pair returned."""
    mod = _import_handler()
    a = "aaaa0000-0000-0000-0000-000000000001"
    b = "zzzz0000-0000-0000-0000-000000000002"
    lo_fwd, hi_fwd = mod._canonical_pair(a, b)
    lo_rev, hi_rev = mod._canonical_pair(b, a)
    assert (lo_fwd, hi_fwd) == (lo_rev, hi_rev)


# ---------------------------------------------------------------------------
# Section B — Unauthorized error shape
# ---------------------------------------------------------------------------


def test_given_not_friends_reason_when_unauthorized_built_then_correct_shape() -> None:
    """given NOT_FRIENDS reason, when _unauthorized_response called, then correct shape."""
    mod = _import_handler()
    result = mod._unauthorized_response(mod.REASON_NOT_FRIENDS)
    assert result["errorType"] == "Unauthorized"
    assert result["reason"] == "NOT_FRIENDS"


def test_given_blocked_reason_when_unauthorized_built_then_correct_shape() -> None:
    """given BLOCKED reason, when _unauthorized_response called, then correct shape."""
    mod = _import_handler()
    result = mod._unauthorized_response(mod.REASON_BLOCKED)
    assert result["errorType"] == "Unauthorized"
    assert result["reason"] == "BLOCKED"


def test_given_self_chat_reason_when_unauthorized_built_then_correct_shape() -> None:
    """given SELF_CHAT reason, when _unauthorized_response called, then correct shape."""
    mod = _import_handler()
    result = mod._unauthorized_response(mod.REASON_SELF_CHAT)
    assert result["errorType"] == "Unauthorized"
    assert result["reason"] == "SELF_CHAT"


# ---------------------------------------------------------------------------
# Section C — Dispatcher routing
# ---------------------------------------------------------------------------


def test_given_mutation_create_or_get_room_when_dispatch_called_then_handler_invoked() -> None:
    """given (Mutation, createOrGetRoom) event, when _dispatch called, then not Unimplemented."""
    # friendship exists → successful path so we can check it's NOT Unimplemented.
    # A mock DDB client is required so the handler does not hit real AWS.
    mock_ddb = MagicMock()
    mock_ddb.transact_write_items.return_value = {}
    mod = _import_handler(friendship_rows=(1,), dynamodb_client_override=mock_ddb)
    event = _make_event()
    result = mod._dispatch(event)
    assert result.get("errorType") != "Unimplemented", (
        f"Expected createOrGetRoom to be routed but got Unimplemented: {result}"
    )


def test_given_mutation_send_message_when_dispatch_called_then_not_unimplemented() -> None:
    """given (Mutation, sendMessage) — wired in story 8.4 — when _dispatch called, then NOT Unimplemented.

    The route is now live; the handler will reject the caller as Unauthorized
    (not a room member) rather than returning Unimplemented.
    """
    mock_ddb = MagicMock()
    # GetItem for membership returns no item (sender is not a member).
    mock_ddb.get_item.return_value = {}

    mod = _import_handler(dynamodb_client_override=mock_ddb)
    event = {
        "typeName": "Mutation",
        "fieldName": "sendMessage",
        "identity": {"sub": "x", "claims": {}},
        "arguments": {"roomId": "room-y", "content": "hello"},
    }
    result = mod._dispatch(event)
    # sendMessage is wired — must NOT be Unimplemented.
    assert result.get("errorType") != "Unimplemented", (
        f"sendMessage should be wired (story 8.4) but got Unimplemented: {result}"
    )


# ---------------------------------------------------------------------------
# Section D — Caller == other user
# ---------------------------------------------------------------------------


def test_given_caller_equals_other_user_when_create_or_get_room_then_self_chat_unauthorized() -> None:
    """given caller_id == other_user_id, when createOrGetRoom called, then SELF_CHAT Unauthorized."""
    mod = _import_handler()
    same_id = "aaaa0000-0000-0000-0000-000000000001"
    event = _make_event(caller_id=same_id, other_user_id=same_id)
    result = mod._dispatch(event)
    assert result["errorType"] == "Unauthorized"
    assert result["reason"] == "SELF_CHAT"


# ---------------------------------------------------------------------------
# Section E — NOT_FRIENDS path
# ---------------------------------------------------------------------------


def test_given_no_friendship_when_create_or_get_room_then_not_friends_unauthorized() -> None:
    """given no friendship row in Aurora, when createOrGetRoom called, then NOT_FRIENDS."""
    mod = _import_handler(is_blocked_return=False, friendship_rows=None)
    event = _make_event()
    result = mod._dispatch(event)
    assert result["errorType"] == "Unauthorized"
    assert result["reason"] == "NOT_FRIENDS"


# ---------------------------------------------------------------------------
# Section F — BLOCKED path
# ---------------------------------------------------------------------------


def test_given_blocked_when_create_or_get_room_then_blocked_unauthorized() -> None:
    """given is_blocked returns True, when createOrGetRoom called, then BLOCKED Unauthorized."""
    mod = _import_handler(is_blocked_return=True, friendship_rows=(1,))
    event = _make_event()
    result = mod._dispatch(event)
    assert result["errorType"] == "Unauthorized"
    assert result["reason"] == "BLOCKED"


# ---------------------------------------------------------------------------
# Section G — Happy path: new room created
# ---------------------------------------------------------------------------


def test_given_friends_not_blocked_when_new_room_then_transact_write_called_and_room_returned() -> None:
    """given friends + not blocked + no existing room, when createOrGetRoom, then TransactWriteItems called."""
    mock_ddb = MagicMock()
    # TransactWriteItems succeeds (no exception)
    mock_ddb.transact_write_items.return_value = {}
    # GetItem for fallback not needed on success path, but configure anyway
    mock_ddb.get_item.return_value = {}

    mod = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb,
    )
    event = _make_event()
    result = mod._dispatch(event)

    # TransactWriteItems must have been called exactly once
    mock_ddb.transact_write_items.assert_called_once()

    # Result must contain a roomId (not an error) — camelCase to match the
    # ChatRoom GraphQL type in schema.graphql.
    assert "errorType" not in result, f"Expected success but got: {result}"
    assert "roomId" in result, f"Expected roomId in result but got: {result}"


def test_given_new_room_created_when_result_returned_then_room_carries_status_active_and_friendship_active() -> None:
    """given new room created, when result returned, then status=active and friendship_active=true."""
    mock_ddb = MagicMock()
    mock_ddb.transact_write_items.return_value = {}

    mod = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb,
    )
    event = _make_event()
    result = mod._dispatch(event)

    assert result.get("status") == "active", f"Expected status=active in: {result}"
    assert result.get("friendshipActive") is True, f"Expected friendshipActive=True in: {result}"


def test_given_new_room_created_when_result_returned_then_room_id_is_symmetric_sha256() -> None:
    """given caller and other_user, when new room created, then room_id = sha256(canonical_pair)."""
    import hashlib
    mock_ddb = MagicMock()
    mock_ddb.transact_write_items.return_value = {}

    caller_id = "aaaa0000-0000-0000-0000-000000000001"
    other_id = "bbbb0000-0000-0000-0000-000000000002"
    lo, hi = min(caller_id, other_id), max(caller_id, other_id)
    expected_room_id = hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()

    mod = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb,
    )
    event = _make_event(caller_id=caller_id, other_user_id=other_id)
    result = mod._dispatch(event)

    assert result.get("roomId") == expected_room_id


def test_given_new_room_when_transact_write_called_then_three_put_items_in_batch() -> None:
    """given new room path, when TransactWriteItems called, then batch has 3 Put items (1 ChatRooms + 2 ChatRoomMembership)."""
    mock_ddb = MagicMock()
    mock_ddb.transact_write_items.return_value = {}

    mod = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb,
    )
    event = _make_event()
    mod._dispatch(event)

    call_kwargs = mock_ddb.transact_write_items.call_args
    transact_items = call_kwargs[1]["TransactItems"] if call_kwargs[1] else call_kwargs[0][0]["TransactItems"]
    # Expect exactly 3 Put operations: ChatRooms + 2 x ChatRoomMembership
    put_items = [item for item in transact_items if "Put" in item]
    assert len(put_items) == 3, f"Expected 3 Put items in TransactWriteItems, got {len(put_items)}: {transact_items}"


# ---------------------------------------------------------------------------
# Section H — Idempotent path: room already exists
# ---------------------------------------------------------------------------


def test_given_room_already_exists_when_conditional_check_failed_then_existing_room_returned() -> None:
    """given TransactWriteItems raises ConditionalCheckFailed, when dispatched, then GetItem returns existing room."""
    from botocore.exceptions import ClientError

    # Build a ClientError that simulates ConditionalCheckFailedException
    error_response = {
        "Error": {
            "Code": "TransactionCanceledException",
            "Message": "Transaction cancelled, please refer cancellation reasons for specific reasons [ConditionalCheckFailed]",
        },
        "CancellationReasons": [
            {"Code": "ConditionalCheckFailed"},
        ],
    }
    conditional_check_error = ClientError(error_response, "TransactWriteItems")

    mock_ddb = MagicMock()
    mock_ddb.transact_write_items.side_effect = conditional_check_error

    caller_id = "aaaa0000-0000-0000-0000-000000000001"
    other_id = "bbbb0000-0000-0000-0000-000000000002"

    import hashlib
    lo, hi = min(caller_id, other_id), max(caller_id, other_id)
    room_id = hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()

    # GetItem returns the existing room
    mock_ddb.get_item.return_value = {
        "Item": {
            "room_id": {"S": room_id},
            "user_a": {"S": lo},
            "user_b": {"S": hi},
            "status": {"S": "active"},
            "friendship_active": {"BOOL": True},
        }
    }

    mod = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb,
    )
    event = _make_event(caller_id=caller_id, other_user_id=other_id)
    result = mod._dispatch(event)

    # GetItem must have been called with the correct room_id key
    mock_ddb.get_item.assert_called_once()
    get_item_call = mock_ddb.get_item.call_args[1]
    assert get_item_call["TableName"] == "ChatRooms"
    assert get_item_call["Key"]["room_id"]["S"] == room_id

    # Result must be the existing room, not an error
    assert "errorType" not in result, f"Expected existing room but got error: {result}"
    assert result.get("roomId") == room_id


def test_given_room_exists_when_called_twice_then_room_id_is_same() -> None:
    """given two calls for the same pair, when dispatched, then both return identical room_id."""
    import hashlib
    from botocore.exceptions import ClientError

    caller_id = "aaaa0000-0000-0000-0000-000000000001"
    other_id = "bbbb0000-0000-0000-0000-000000000002"
    lo, hi = min(caller_id, other_id), max(caller_id, other_id)
    room_id = hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()

    # First call: TransactWriteItems succeeds
    mock_ddb_first = MagicMock()
    mock_ddb_first.transact_write_items.return_value = {}
    mod1 = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb_first,
    )
    result1 = mod1._dispatch(_make_event(caller_id=caller_id, other_user_id=other_id))

    # Second call: TransactWriteItems raises ConditionalCheckFailed
    error_response = {
        "Error": {
            "Code": "TransactionCanceledException",
            "Message": "Transaction cancelled [ConditionalCheckFailed]",
        },
        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
    }
    mock_ddb_second = MagicMock()
    mock_ddb_second.transact_write_items.side_effect = ClientError(error_response, "TransactWriteItems")
    mock_ddb_second.get_item.return_value = {
        "Item": {
            "room_id": {"S": room_id},
            "user_a": {"S": lo},
            "user_b": {"S": hi},
            "status": {"S": "active"},
            "friendship_active": {"BOOL": True},
        }
    }
    mod2 = _import_handler(
        is_blocked_return=False,
        friendship_rows=(1,),
        dynamodb_client_override=mock_ddb_second,
    )
    result2 = mod2._dispatch(_make_event(caller_id=caller_id, other_user_id=other_id))

    assert result1.get("roomId") == result2.get("roomId") == room_id
